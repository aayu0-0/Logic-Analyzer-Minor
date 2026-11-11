#!/usr/bin/env python3
"""
ESP32 Logic Analyzer - Desktop Application
Main entry point integrating all components:
- TCP/Serial connection to ESP32
- Frame parsing and RLE decoding
- Timeline model with gap/restart handling
- Waveform viewer with zoom/pan (PyQtGraph)
- Live statistics panel
- Measurements (cursors, frequency, duty cycle)
- Export (VCD, CSV)
"""

import sys
import os
from typing import Optional, List, Tuple
from dataclasses import dataclass

from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QLabel, QSlider, QSpinBox, QPushButton,
                             QCheckBox, QSplitter, QFrame, QMenuBar, QToolBar,
                             QFileDialog, QMessageBox, QDialog, QDialogButtonBox,
                             QFormLayout, QLineEdit, QComboBox, QStatusBar)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QSettings
from PyQt6.QtGui import QAction, QColor, QPen, QIcon, QFont, QKeySequence

import pyqtgraph as pg
from pyqtgraph import PlotWidget, ViewBox

from parser import FrameParser, DataFrame, StatsFrame, TCPClient, decode_file
from timeline import Timeline, TimelineBuilder, Gap
from measurements import measure_channel, measure_all_channels, measure_cursor_delta, ChannelMeasurements
from export import export_vcd, export_csv, export_csv_gaps, ExportOptions


# --- Constants ---
CHANNEL_COLORS = [
    '#FF6B6B', '#4ECDC4', '#FFE66D', '#95E1D3',
    '#F38181', '#AA96DA', '#FCBAD3', '#A8D8EA',
]
CHANNEL_NAMES = [f'CH{i}' for i in range(8)]
GAP_COLOR = '#888888'
BACKGROUND_COLOR = '#1e1e1e'
GRID_COLOR = '#333333'
TEXT_COLOR = '#ffffff'


def edges_to_step_data(edges: List, channel: int, channel_offset: int,
                       x_min_s: float, x_max_s: float,
                       initial_level: int = 0,
                       sample_period_s: float = 0.2e-6) -> Tuple[List[float], List[float]]:
    """
    Convert edges to step-like x,y data for PlotDataItem.
    Y values are offset by channel_offset so each channel stacks vertically.
    """
    xs = []
    ys = []

    # Start at left edge of view
    current_level = initial_level
    xs.append(x_min_s)
    ys.append(channel_offset + (0.35 if current_level else -0.35))

    # Process edges
    for edge in edges:
        x = edge.time_ns / 1e9
        if x < x_min_s:
            current_level = edge.level
            continue
        if x > x_max_s:
            break

        # Vertical transition at this x
        y_from = channel_offset + (0.35 if current_level else -0.35)
        y_to = channel_offset + (0.35 if edge.level else -0.35)
        xs.append(x)
        ys.append(y_from)
        xs.append(x)
        ys.append(y_to)
        current_level = edge.level

    # Extend to right edge
    xs.append(x_max_s)
    ys.append(channel_offset + (0.35 if current_level else -0.35))

    return xs, ys


class WaveformView(PlotWidget):
    """Main waveform plotting widget with zoom/pan."""

    range_changed = pyqtSignal(float, float)
    cursor_moved = pyqtSignal(float)

    def __init__(self, timeline: Timeline, parent=None):
        super().__init__(parent)
        self.timeline = timeline
        self.sample_period_ns = timeline.sample_period_ns

        # Configure plot
        self.setBackground(BACKGROUND_COLOR)
        self.showGrid(x=True, y=False, alpha=0.3)
        self.getAxis('bottom').setPen(GRID_COLOR)
        self.getAxis('bottom').setTextPen(TEXT_COLOR)
        self.getAxis('left').setPen(GRID_COLOR)
        self.getAxis('left').setTextPen(TEXT_COLOR)
        self.getAxis('left').setTicks([[(i, CHANNEL_NAMES[i]) for i in range(8)]])
        self.getAxis('left').setWidth(60)

        # Set Y range to fit all 8 channels (each takes 1 unit of height)
        self.setYRange(-1, 8, padding=0)

        # Disable mouse on Y axis
        self.setMouseEnabled(x=True, y=False)

        # Create PlotDataItem for each channel
        self.plot_items = []
        for ch in range(8):
            item = pg.PlotDataItem(
                pen=pg.mkPen(CHANNEL_COLORS[ch], width=1.5),
                antialias=False,
            )
            self.addItem(item)
            self.plot_items.append(item)

        # Gap regions (drawn as PlotDataItem with filled areas)
        self.gap_items = []
        for ch in range(8):
            item = pg.PlotDataItem(
                pen=None,
                brush=pg.mkBrush(GAP_COLOR, alpha=80),
                antialias=False,
            )
            self.addItem(item)
            self.gap_items.append(item)

        # Set initial range
        start_ns, end_ns = timeline.get_time_range()
        if end_ns > start_ns:
            self.setXRange(start_ns / 1e9, end_ns / 1e9, padding=0)
        else:
            self.setXRange(0, 1, padding=0)

        # View range changes
        self.vb = self.getViewBox()
        self.vb.sigXRangeChanged.connect(self._on_xrange_changed)

        # Cursor
        self.cursor_line = pg.InfiniteLine(
            pos=0, angle=90,
            pen=pg.mkPen('#FFD700', width=1, style=Qt.PenStyle.DashLine)
        )
        self.addItem(self.cursor_line)
        self.cursor_visible = False

        # Measurement cursors
        self.measure_cursors = []

        # Channel visibility
        self.channel_visible = [True] * 8

    def _on_xrange_changed(self, viewbox, ranges):
        try:
            # PyQtGraph passes ranges as (xmin, xmax) tuple of floats
            x_min = float(ranges[0])
            x_max = float(ranges[1])
        except Exception:
            # Fallback: read directly from viewbox
            x_range = viewbox.viewRange()[0]
            x_min = float(x_range[0])
            x_max = float(x_range[1])
        self.range_changed.emit(x_min, x_max)
        self._redraw_waveforms(x_min, x_max)

    def _redraw_waveforms(self, x_min_s: float, x_max_s: float):
        """Redraw all waveforms for the visible time range."""
        sample_period_s = self.sample_period_ns / 1e9

        for ch in range(8):
            edges = self.timeline.get_channel_edges(
                ch, int(x_min_s * 1e9), int(x_max_s * 1e9)
            )
            xs, ys = edges_to_step_data(
                edges, ch, ch, x_min_s, x_max_s,
                initial_level=self.timeline.channel_timelines[ch].initial_state,
                sample_period_s=sample_period_s,
            )
            self.plot_items[ch].setData(xs, ys)

        # Draw gaps
        gaps = self.timeline.get_gaps_in_range(
            int(x_min_s * 1e9), int(x_max_s * 1e9)
        )
        for ch in range(8):
            gap_xs = []
            gap_ys = []
            for gap in gaps:
                x1 = max(gap.start_time_ns / 1e9, x_min_s)
                x2 = min(gap.end_time_ns / 1e9, x_max_s)
                if x2 > x1:
                    gap_xs.extend([x1, x2, x2, x1])
                    gap_ys.extend([ch - 0.5, ch - 0.5, ch + 0.5, ch + 0.5])
            self.gap_items[ch].setData(gap_xs, gap_ys)

    def set_time_range(self, start_s: float, end_s: float):
        self.setXRange(start_s, end_s, padding=0)

    def set_cursor(self, time_s: float):
        self.cursor_line.setPos(time_s)
        self.cursor_visible = True

    def hide_cursor(self):
        self.cursor_visible = False
        self.cursor_line.setPos(-1e9)

    def add_measurement_cursor(self, label: str, time_s: float, color: str):
        line = pg.InfiniteLine(
            pos=time_s, angle=90,
            pen=pg.mkPen(color, width=1, style=Qt.PenStyle.DashLine),
            label=label, labelOpts={'color': color, 'fill': BACKGROUND_COLOR}
        )
        self.addItem(line)
        self.measure_cursors.append(line)
        return line

    def clear_measurement_cursors(self):
        for c in self.measure_cursors:
            self.removeItem(c)
        self.measure_cursors.clear()

    def get_visible_time_range(self) -> Tuple[float, float]:
        x_range = self.vb.viewRange()[0]
        return (x_range[0], x_range[1])

    def set_channel_visible(self, channel: int, visible: bool):
        self.channel_visible[channel] = visible
        self.plot_items[channel].setVisible(visible)
        self.gap_items[channel].setVisible(visible)

    def update_timeline(self, timeline: Timeline):
        """Update with new timeline data."""
        self.timeline = timeline
        self.sample_period_ns = timeline.sample_period_ns
        self.fit_all()

    def fit_all(self):
        start_ns, end_ns = self.timeline.get_time_range()
        if end_ns > start_ns:
            self.setXRange(start_ns / 1e9, end_ns / 1e9, padding=0)


class StatsPanel(QWidget):
    """Statistics panel showing live values from STATS frames."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setup_ui()

        self.stats = {
            'sample_hz': 5_000_000,
            'produced': 0,
            'consumed': 0,
            'lost_buffers': 0,
            'max_proc_us': 0,
            'tx_drop_pkts': 0,
            'tx_drop_samples': 0,
            'crc_bad': 0,
            'seq_gaps': 0,
            'lost_total': 0,
        }

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(4)
        layout.setContentsMargins(8, 8, 8, 8)

        # Title
        title = QLabel("STATISTICS")
        title.setStyleSheet("font-weight: bold; font-size: 12px; color: #888;")
        layout.addWidget(title)

        # Stats grid
        self.labels = {}
        stats_config = [
            ('Sample Rate', 'sample_hz', lambda v: f"{v/1e6:.1f} MHz"),
            ('Produced', 'produced', lambda v: f"{v:,}"),
            ('Consumed', 'consumed', lambda v: f"{v:,}"),
            ('Lost Buffers', 'lost_buffers', lambda v: f"{v:,}"),
            ('Max Proc (μs)', 'max_proc_us', lambda v: f"{v:,}"),
            ('TX Dropped Pkts', 'tx_drop_pkts', lambda v: f"{v:,}"),
            ('TX Dropped Samples', 'tx_drop_samples', lambda v: f"{v:,}"),
            ('CRC Errors', 'crc_bad', lambda v: f"{v:,}"),
            ('Seq Gaps', 'seq_gaps', lambda v: f"{v:,}"),
            ('Total Lost', 'lost_total', lambda v: f"{v:,}"),
        ]

        for label_text, key, formatter in stats_config:
            row = QHBoxLayout()
            row.setSpacing(8)
            label = QLabel(label_text)
            label.setStyleSheet("color: #aaa; font-size: 11px;")
            label.setMinimumWidth(120)
            value = QLabel("0")
            value.setStyleSheet("color: #fff; font-size: 11px; font-family: monospace;")
            value.setAlignment(Qt.AlignmentFlag.AlignRight)
            self.labels[key] = (value, formatter)
            row.addWidget(label)
            row.addStretch()
            row.addWidget(value)
            layout.addLayout(row)

        # Connection status
        self.conn_label = QLabel("Disconnected")
        self.conn_label.setStyleSheet("color: #f66; font-size: 11px; font-weight: bold;")
        self.conn_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.conn_label)

        layout.addStretch()

    def update_stats(self, stats_frame: StatsFrame):
        self.stats['sample_hz'] = stats_frame.sample_hz
        self.stats['produced'] = stats_frame.produced
        self.stats['consumed'] = stats_frame.consumed
        self.stats['lost_buffers'] = stats_frame.lost_buffers
        self.stats['max_proc_us'] = stats_frame.max_proc_us
        self.stats['tx_drop_pkts'] = stats_frame.tx_drop_pkts
        self.stats['tx_drop_samples'] = stats_frame.tx_drop_samples
        self._refresh_labels()

    def update_decoder_stats(self, crc_bad: int, seq_gaps: int, lost_total: int):
        self.stats['crc_bad'] = crc_bad
        self.stats['seq_gaps'] = seq_gaps
        self.stats['lost_total'] = lost_total
        self._refresh_labels()

    def set_connected(self, connected: bool):
        if connected:
            self.conn_label.setText("● Connected")
            self.conn_label.setStyleSheet("color: #6f6; font-size: 11px; font-weight: bold;")
        else:
            self.conn_label.setText("● Disconnected")
            self.conn_label.setStyleSheet("color: #f66; font-size: 11px; font-weight: bold;")

    def _refresh_labels(self):
        for key, (label, formatter) in self.labels.items():
            label.setText(formatter(self.stats[key]))


class MeasurementsDialog(QDialog):
    """Dialog showing channel measurements."""

    def __init__(self, measurements: List[ChannelMeasurements], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Channel Measurements")
        self.resize(500, 400)
        self.setup_ui(measurements)

    def setup_ui(self, measurements: List[ChannelMeasurements]):
        layout = QVBoxLayout(self)

        # Table
        from PyQt6.QtWidgets import QTableWidget, QTableWidgetItem, QHeaderView
        self.table = QTableWidget()
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels([
            'Channel', 'Edges', 'Frequency', 'Period',
            'Duty Cycle', 'High Time', 'Low Time'
        ])
        self.table.setRowCount(len(measurements))
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)

        for row, m in enumerate(measurements):
            self.table.setItem(row, 0, QTableWidgetItem(f"CH{m.channel}"))
            self.table.setItem(row, 1, QTableWidgetItem(str(m.edge_count)))
            self.table.setItem(row, 2, QTableWidgetItem(
                f"{m.frequency_hz:.2f} Hz" if m.frequency_hz else "N/A"))
            self.table.setItem(row, 3, QTableWidgetItem(
                f"{m.period_s*1e6:.2f} µs" if m.period_s else "N/A"))
            self.table.setItem(row, 4, QTableWidgetItem(
                f"{m.duty_cycle_pct:.1f}%" if m.duty_cycle_pct else "N/A"))
            self.table.setItem(row, 5, QTableWidgetItem(
                f"{m.high_time_s*1e6:.2f} µs" if m.high_time_s else "N/A"))
            self.table.setItem(row, 6, QTableWidgetItem(
                f"{m.low_time_s*1e6:.2f} µs" if m.low_time_s else "N/A"))

        layout.addWidget(self.table)

        # Close button
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class ConnectDialog(QDialog):
    """Dialog for connecting to ESP32."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Connect to ESP32")
        self.setup_ui()

    def setup_ui(self):
        layout = QFormLayout(self)

        self.host_edit = QLineEdit("192.168.4.1")
        self.port_edit = QSpinBox()
        self.port_edit.setRange(1, 65535)
        self.port_edit.setValue(5000)

        self.transport_combo = QComboBox()
        self.transport_combo.addItems(["TCP (WiFi)", "Serial"])
        self.serial_port_edit = QLineEdit("/dev/ttyUSB0")
        self.baud_edit = QSpinBox()
        self.baud_edit.setRange(9600, 2000000)
        self.baud_edit.setValue(921600)

        layout.addRow("Host/IP:", self.host_edit)
        layout.addRow("Port:", self.port_edit)
        layout.addRow("Transport:", self.transport_combo)
        layout.addRow("Serial Port:", self.serial_port_edit)
        layout.addRow("Baud Rate:", self.baud_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def get_settings(self):
        return {
            'host': self.host_edit.text(),
            'port': self.port_edit.value(),
            'transport': self.transport_combo.currentText(),
            'serial_port': self.serial_port_edit.text(),
            'baud': self.baud_edit.value(),
        }


class ExportDialog(QDialog):
    """Dialog for export options."""

    def __init__(self, timeline: Timeline, parent=None):
        super().__init__(parent)
        self.timeline = timeline
        self.setWindowTitle("Export Data")
        self.setup_ui()

    def setup_ui(self):
        layout = QFormLayout(self)

        self.format_combo = QComboBox()
        self.format_combo.addItems(["VCD (PulseView/GTKWave)", "CSV (Edges)", "CSV (Gaps)"])

        self.channels_edit = QLineEdit("0-7")
        self.channels_edit.setToolTip("Channel range, e.g., 0-3 or 0,2,4")

        self.start_time = QLineEdit("0")
        self.end_time = QLineEdit("")
        self.end_time.setPlaceholderText("End of capture")

        layout.addRow("Format:", self.format_combo)
        layout.addRow("Channels:", self.channels_edit)
        layout.addRow("Start Time (s):", self.start_time)
        layout.addRow("End Time (s):", self.end_time)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def get_options(self) -> ExportOptions:
        opts = ExportOptions()

        # Parse channels
        ch_text = self.channels_edit.text().strip()
        if ch_text:
            channels = []
            for part in ch_text.split(','):
                if '-' in part:
                    start, end = map(int, part.split('-'))
                    channels.extend(range(start, end + 1))
                else:
                    channels.append(int(part))
            opts.channels = channels

        # Parse times
        try:
            opts.start_time_s = float(self.start_time.text())
        except ValueError:
            opts.start_time_s = 0

        end_text = self.end_time.text().strip()
        if end_text:
            try:
                opts.end_time_s = float(end_text)
            except ValueError:
                pass

        return opts

    def get_format(self) -> str:
        return self.format_combo.currentText()


class MainWindow(QMainWindow):
    """Main application window."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("ESP32 Logic Analyzer")
        self.resize(1400, 900)

        # State
        self.timeline = Timeline()
        self.parser = FrameParser()
        self.builder = TimelineBuilder()
        self.tcp_client: Optional[TCPClient] = None
        self.capture_running = False

        # Connect parser to builder
        self.parser.on('data', self.builder.handle_data_frame)
        self.parser.on('stats', self.builder.handle_stats_frame)

        # Setup UI
        self.setup_ui()
        self.create_actions()
        self.create_menus()
        self.create_toolbar()

        # Update timer for live streaming
        self.update_timer = QTimer()
        self.update_timer.timeout.connect(self.update_view)
        self.update_timer.start(50)  # 20 FPS

        # Load settings
        self.load_settings()

        # Load test file if available
        self.load_test_file()

    def setup_ui(self):
        # Central widget
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(4, 4, 4, 4)
        main_layout.setSpacing(4)

        # Left: Waveform view
        self.waveform_view = WaveformView(self.timeline)
        self.waveform_view.range_changed.connect(self.on_range_changed)
        main_layout.addWidget(self.waveform_view, stretch=4)

        # Right: Control and stats panels
        right_panel = QWidget()
        right_panel.setMaximumWidth(300)
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(4)

        # Controls
        self.create_control_panel(right_layout)

        # Stats
        self.stats_panel = StatsPanel()
        right_layout.addWidget(self.stats_panel)

        main_layout.addWidget(right_panel, stretch=1)

        # Status bar
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Ready")

        # Time range label in status bar
        self.time_range_label = QLabel("Time: 0.000s - 0.000s")
        self.status_bar.addPermanentWidget(self.time_range_label)

    def create_control_panel(self, parent_layout):
        """Create control panel."""
        controls = QFrame()
        controls.setFrameStyle(QFrame.Shape.StyledPanel)
        controls.setStyleSheet("QFrame { background: #2d2d2d; border-radius: 4px; }")
        layout = QVBoxLayout(controls)
        layout.setSpacing(8)
        layout.setContentsMargins(8, 8, 8, 8)

        # Title
        title = QLabel("CONTROLS")
        title.setStyleSheet("font-weight: bold; font-size: 12px; color: #888;")
        layout.addWidget(title)

        # Time per division
        tdiv_layout = QHBoxLayout()
        tdiv_layout.addWidget(QLabel("Time/Div:"))
        self.tdiv_combo = QComboBox()
        self.tdiv_combo.addItems([
            "1 ns/div", "2 ns/div", "5 ns/div",
            "10 ns/div", "20 ns/div", "50 ns/div",
            "100 ns/div", "200 ns/div", "500 ns/div",
            "1 µs/div", "2 µs/div", "5 µs/div",
            "10 µs/div", "20 µs/div", "50 µs/div",
            "100 µs/div", "200 µs/div", "500 µs/div",
            "1 ms/div", "2 ms/div", "5 ms/div",
            "10 ms/div", "20 ms/div", "50 ms/div",
            "100 ms/div", "200 ms/div", "500 ms/div",
            "1 s/div", "2 s/div", "5 s/div",
        ])
        self.tdiv_combo.setCurrentText("1 ms/div")
        self.tdiv_combo.currentTextChanged.connect(self.on_time_div_changed)
        tdiv_layout.addWidget(self.tdiv_combo)
        layout.addLayout(tdiv_layout)

        # Position slider
        pos_layout = QVBoxLayout()
        pos_layout.addWidget(QLabel("Position:"))
        self.pos_slider = QSlider(Qt.Orientation.Horizontal)
        self.pos_slider.setRange(0, 1000)
        self.pos_slider.valueChanged.connect(self.on_pos_changed)
        pos_layout.addWidget(self.pos_slider)
        layout.addLayout(pos_layout)

        # Buttons
        btn_layout = QHBoxLayout()
        self.fit_btn = QPushButton("Fit All")
        self.fit_btn.clicked.connect(self.fit_all)
        btn_layout.addWidget(self.fit_btn)

        self.run_btn = QPushButton("Run")
        self.run_btn.setCheckable(True)
        self.run_btn.setChecked(True)
        self.run_btn.clicked.connect(self.toggle_capture)
        btn_layout.addWidget(self.run_btn)
        layout.addLayout(btn_layout)

        # Channel visibility
        layout.addWidget(QLabel("Channels:"))
        self.ch_checkboxes = []
        for i in range(8):
            cb = QCheckBox(f"CH{i}")
            cb.setChecked(True)
            cb.setStyleSheet(f"color: {CHANNEL_COLORS[i]}; font-size: 11px;")
            cb.toggled.connect(lambda checked, ch=i: self.on_channel_toggled(ch, checked))
            self.ch_checkboxes.append(cb)
            layout.addWidget(cb)

        layout.addStretch()
        parent_layout.addWidget(controls)

    def create_actions(self):
        # File actions
        self.open_action = QAction("&Open Stream...", self)
        self.open_action.setShortcut(QKeySequence.StandardKey.Open)
        self.open_action.triggered.connect(self.open_file)

        self.save_vcd_action = QAction("Export &VCD...", self)
        self.save_vcd_action.triggered.connect(lambda: self.export_data('vcd'))

        self.save_csv_action = QAction("Export &CSV (Edges)...", self)
        self.save_csv_action.triggered.connect(lambda: self.export_data('csv_edges'))

        self.save_csv_gaps_action = QAction("Export CSV (Gaps)...", self)
        self.save_csv_gaps_action.triggered.connect(lambda: self.export_data('csv_gaps'))

        self.exit_action = QAction("E&xit", self)
        self.exit_action.setShortcut(QKeySequence.StandardKey.Quit)
        self.exit_action.triggered.connect(self.close)

        # Connection actions
        self.connect_action = QAction("&Connect...", self)
        self.connect_action.setShortcut("Ctrl+Shift+C")
        self.connect_action.triggered.connect(self.show_connect_dialog)

        self.disconnect_action = QAction("&Disconnect", self)
        self.disconnect_action.triggered.connect(self.disconnect)
        self.disconnect_action.setEnabled(False)

        # View actions
        self.measure_action = QAction("&Measurements...", self)
        self.measure_action.setShortcut("Ctrl+M")
        self.measure_action.triggered.connect(self.show_measurements)

        self.fit_action = QAction("&Fit All", self)
        self.fit_action.setShortcut("F")
        self.fit_action.triggered.connect(self.fit_all)

        # Help actions
        self.about_action = QAction("&About", self)
        self.about_action.triggered.connect(self.show_about)

    def create_menus(self):
        menubar = self.menuBar()

        # File menu
        file_menu = menubar.addMenu("&File")
        file_menu.addAction(self.open_action)
        file_menu.addSeparator()
        file_menu.addAction(self.save_vcd_action)
        file_menu.addAction(self.save_csv_action)
        file_menu.addAction(self.save_csv_gaps_action)
        file_menu.addSeparator()
        file_menu.addAction(self.exit_action)

        # Connection menu
        conn_menu = menubar.addMenu("&Connection")
        conn_menu.addAction(self.connect_action)
        conn_menu.addAction(self.disconnect_action)

        # View menu
        view_menu = menubar.addMenu("&View")
        view_menu.addAction(self.measure_action)
        view_menu.addAction(self.fit_action)

        # Help menu
        help_menu = menubar.addMenu("&Help")
        help_menu.addAction(self.about_action)

    def create_toolbar(self):
        toolbar = QToolBar("Main Toolbar")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        toolbar.addAction(self.open_action)
        toolbar.addSeparator()
        toolbar.addAction(self.connect_action)
        toolbar.addAction(self.disconnect_action)
        toolbar.addSeparator()
        toolbar.addAction(self.fit_action)
        toolbar.addAction(self.measure_action)

    def load_test_file(self):
        stream_path = os.path.join(os.path.dirname(__file__), "stream.bin")
        if os.path.exists(stream_path):
            self.status_bar.showMessage(f"Loading {stream_path}...")
            decode_file(stream_path, self.parser)
            self.update_timeline()

            self.stats_panel.update_decoder_stats(
                self.parser.stats.crc_bad,
                self.parser.stats.seq_gaps,
                self.parser.stats.lost_total
            )

            self.status_bar.showMessage(
                f"Loaded: {self.parser.stats.frames} frames, "
                f"{self.timeline.get_sample_count():,} samples, "
                f"{self.parser.stats.lost_total:,} lost, "
                f"{self.parser.stats.crc_bad} CRC errors"
            )

    def update_timeline(self):
        self.timeline = self.builder.get_timeline()
        self.waveform_view.update_timeline(self.timeline)
        self.waveform_view.fit_all()  # Ensure view fits new data
        self.pos_slider.setValue(0)

    def update_view(self):
        if self.capture_running and self.tcp_client:
            pass  # TCP client runs in background

        # Update stats panel with decoder stats
        self.stats_panel.update_decoder_stats(
            self.parser.stats.crc_bad,
            self.parser.stats.seq_gaps,
            self.parser.stats.lost_total
        )

    def on_range_changed(self, x_min: float, x_max: float):
        self.time_range_label.setText(f"Time: {x_min:.3f}s - {x_max:.3f}s")

    def on_time_div_changed(self, text: str):
        # Parse time per division
        parts = text.split()
        if len(parts) >= 2:
            value = float(parts[0])
            unit = parts[1].replace('/div', '')

            multipliers = {'ns': 1e-9, 'µs': 1e-6, 'ms': 1e-3, 's': 1}
            time_per_div = value * multipliers.get(unit, 1)

            # 10 divisions visible
            visible_width = time_per_div * 10
            current_center = sum(self.waveform_view.get_visible_time_range()) / 2
            self.waveform_view.set_time_range(
                current_center - visible_width / 2,
                current_center + visible_width / 2
            )

    def on_pos_changed(self, value: int):
        start_ns, end_ns = self.timeline.get_time_range()
        if end_ns <= start_ns:
            return

        total_width = (end_ns - start_ns) / 1e9
        visible_width = self.waveform_view.get_visible_time_range()[1] - \
                        self.waveform_view.get_visible_time_range()[0]

        max_start = max(0, total_width - visible_width)
        new_start = start_ns / 1e9 + (value / 1000.0) * max_start
        self.waveform_view.set_time_range(new_start, new_start + visible_width)

    def on_channel_toggled(self, channel: int, visible: bool):
        self.waveform_view.set_channel_visible(channel, visible)

    def fit_all(self):
        self.waveform_view.fit_all()
        self.pos_slider.setValue(0)

    def toggle_capture(self, checked: bool):
        self.capture_running = checked
        self.run_btn.setText("Stop" if checked else "Run")
        if checked:
            self.status_bar.showMessage("Capture running...")
        else:
            self.status_bar.showMessage("Capture stopped")

    def open_file(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Open Stream File", "", "Binary Files (*.bin);;All Files (*)"
        )
        if file_path:
            self.status_bar.showMessage(f"Loading {file_path}...")
            self.parser = FrameParser()
            self.builder = TimelineBuilder()
            self.parser.on('data', self.builder.handle_data_frame)
            self.parser.on('stats', self.builder.handle_stats_frame)
            decode_file(file_path, self.parser)
            self.update_timeline()
            self.waveform_view.fit_all()  # Ensure view fits new data

            self.stats_panel.update_decoder_stats(
                self.parser.stats.crc_bad,
                self.parser.stats.seq_gaps,
                self.parser.stats.lost_total
            )

            self.status_bar.showMessage(
                f"Loaded: {self.parser.stats.frames} frames, "
                f"{self.timeline.get_sample_count():,} samples"
            )

    def show_connect_dialog(self):
        dialog = ConnectDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            settings = dialog.get_settings()
            self.connect_to_esp32(settings)

    def connect_to_esp32(self, settings: dict):
        if settings['transport'] == 'TCP (WiFi)':
            self.tcp_client = TCPClient(settings['host'], settings['port'], self.parser)
            self.tcp_client.run()
            self.stats_panel.set_connected(True)
            self.connect_action.setEnabled(False)
            self.disconnect_action.setEnabled(True)
            self.status_bar.showMessage(f"Connected to {settings['host']}:{settings['port']}")
        else:
            # Serial not implemented yet
            QMessageBox.information(self, "Not Implemented",
                "Serial connection not yet implemented. Use TCP (WiFi).")

    def disconnect(self):
        if self.tcp_client:
            self.tcp_client.stop()
            self.tcp_client = None
        self.stats_panel.set_connected(False)
        self.connect_action.setEnabled(True)
        self.disconnect_action.setEnabled(False)
        self.status_bar.showMessage("Disconnected")

    def show_measurements(self):
        # Measure visible range
        x_min, x_max = self.waveform_view.get_visible_time_range()
        measurements = measure_all_channels(self.timeline, x_min, x_max)

        dialog = MeasurementsDialog(measurements, self)
        dialog.exec()

    def export_data(self, format_type: str):
        dialog = ExportDialog(self.timeline, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        opts = dialog.get_options()
        fmt = dialog.get_format()

        if 'VCD' in fmt:
            ext = '.vcd'
            filter_str = "VCD Files (*.vcd)"
        elif 'Edges' in fmt:
            ext = '.csv'
            filter_str = "CSV Files (*.csv)"
        else:
            ext = '.csv'
            filter_str = "CSV Files (*.csv)"

        file_path, _ = QFileDialog.getSaveFileName(
            self, "Export Data", f"capture{ext}", filter_str
        )

        if not file_path:
            return

        try:
            with open(file_path, 'w') as f:
                if 'VCD' in fmt:
                    export_vcd(self.timeline, f, opts)
                elif 'Edges' in fmt:
                    export_csv(self.timeline, f, opts)
                else:
                    export_csv_gaps(self.timeline, f, opts)

            self.status_bar.showMessage(f"Exported to {file_path}")
            QMessageBox.information(self, "Export Complete",
                f"Data exported to {file_path}")
        except Exception as e:
            QMessageBox.critical(self, "Export Failed", str(e))

    def show_about(self):
        QMessageBox.about(self, "About ESP32 Logic Analyzer",
            "ESP32 Logic Analyzer Desktop Application\n\n"
            "8-channel, 5 MHz logic analyzer\n"
            "Built with Python + PyQtGraph\n\n"
            "Features:\n"
            "• Real-time waveform display\n"
            "• Zoom, pan, measurements\n"
            "• VCD/CSV export\n"
            "• Gap and restart detection\n"
            "• Live statistics")

    def load_settings(self):
        settings = QSettings("LogicAnalyzer", "ESP32LogicAnalyzer")
        geometry = settings.value("geometry")
        if geometry:
            self.restoreGeometry(geometry)

    def save_settings(self):
        settings = QSettings("LogicAnalyzer", "ESP32LogicAnalyzer")
        settings.setValue("geometry", self.saveGeometry())

    def closeEvent(self, event):
        self.save_settings()
        if self.tcp_client:
            self.tcp_client.stop()
        event.accept()


def main():
    # High DPI support
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName("ESP32 Logic Analyzer")
    app.setOrganizationName("LogicAnalyzer")

    # Dark theme
    app.setStyle('Fusion')
    palette = app.palette()
    palette.setColor(palette.ColorRole.Window, QColor(BACKGROUND_COLOR))
    palette.setColor(palette.ColorRole.WindowText, QColor(TEXT_COLOR))
    palette.setColor(palette.ColorRole.Base, QColor('#2d2d2d'))
    palette.setColor(palette.ColorRole.AlternateBase, QColor('#3d3d3d'))
    palette.setColor(palette.ColorRole.ToolTipBase, QColor('#2d2d2d'))
    palette.setColor(palette.ColorRole.ToolTipText, QColor(TEXT_COLOR))
    palette.setColor(palette.ColorRole.Text, QColor(TEXT_COLOR))
    palette.setColor(palette.ColorRole.Button, QColor('#3d3d3d'))
    palette.setColor(palette.ColorRole.ButtonText, QColor(TEXT_COLOR))
    palette.setColor(palette.ColorRole.BrightText, QColor('#ff0000'))
    palette.setColor(palette.ColorRole.Link, QColor('#4a90d9'))
    palette.setColor(palette.ColorRole.Highlight, QColor('#4a90d9'))
    palette.setColor(palette.ColorRole.HighlightedText, QColor('#000000'))
    app.setPalette(palette)

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()