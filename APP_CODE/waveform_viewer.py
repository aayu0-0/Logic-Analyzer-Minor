#!/usr/bin/env python3
"""
Waveform viewer for ESP32 Logic Analyzer using PyQtGraph.
Shows 8 stacked digital traces with zoom, pan, and gap visualization.
"""

import sys
from typing import Optional, List, Tuple
from dataclasses import dataclass

import numpy as np
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QLabel, QSlider, QPushButton,
                             QSpinBox, QCheckBox, QSplitter, QFrame)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QRectF
from PyQt6.QtGui import QColor, QPen, QBrush, QFont

import pyqtgraph as pg
from pyqtgraph import PlotWidget, ViewBox, UIGraphicsItem as GraphicsItem

from parser import FrameParser, DataFrame, StatsFrame, decode_file
from timeline import Timeline, TimelineBuilder, Gap


# --- Configuration ---
CHANNEL_COLORS = [
    '#FF6B6B',  # CH0 - Red
    '#4ECDC4',  # CH1 - Teal
    '#FFE66D',  # CH2 - Yellow
    '#95E1D3',  # CH3 - Mint
    '#F38181',  # CH4 - Pink
    '#AA96DA',  # CH5 - Purple
    '#FCBAD3',  # CH6 - Light Pink
    '#A8D8EA',  # CH7 - Light Blue
]

CHANNEL_NAMES = [f'CH{i}' for i in range(8)]
GAP_COLOR = '#888888'
GAP_ALPHA = 80
BACKGROUND_COLOR = '#1e1e1e'
GRID_COLOR = '#333333'
TEXT_COLOR = '#ffffff'


class DigitalWaveformItem(GraphicsItem):
    """Custom graphics item for drawing digital waveforms efficiently."""

    def __init__(self, channel: int, timeline: Timeline, sample_period_ns: float):
        super().__init__()
        self.channel = channel
        self.timeline = timeline
        self.sample_period_ns = sample_period_ns
        self.color = QColor(CHANNEL_COLORS[channel % len(CHANNEL_COLORS)])
        self.pen = QPen(self.color, 1.5)
        self.brush = QBrush(self.color)

        gap_color = QColor(GAP_COLOR)
        gap_color.setAlpha(GAP_ALPHA)
        self.gap_pen = QPen(QColor(GAP_COLOR), 1, Qt.PenStyle.DotLine)
        self.gap_brush = QBrush(gap_color)

        # Cached geometry
        self._path = None
        self._gap_rects = []
        self._view_range = (0, 0)
        self._needs_update = True

    def set_view_range(self, x_min: float, x_max: float):
        """Update visible range and trigger rebuild if needed."""
        if (x_min, x_max) != self._view_range:
            self._view_range = (x_min, x_max)
            self._needs_update = True
            self.prepareGeometryChange()
            self.update()

    def boundingRect(self) -> QRectF:
        return QRectF(self._view_range[0], -0.5,
                      self._view_range[1] - self._view_range[0], 1.0)

    def paint(self, painter, option, widget=None):
        if self._needs_update:
            self._rebuild_geometry()

        # Draw gaps first (background)
        painter.setPen(self.gap_pen)
        painter.setBrush(self.gap_brush)
        for rect in self._gap_rects:
            painter.drawRect(rect)

        # Draw waveform
        if self._path:
            painter.setPen(self.pen)
            painter.drawPath(self._path)

    def _rebuild_geometry(self):
        """Rebuild the waveform path and gap rectangles for current view."""
        from pyqtgraph import QtGui
        self._path = QtGui.QPainterPath()
        self._gap_rects = []

        x_min, x_max = self._view_range
        if x_min >= x_max:
            self._needs_update = False
            return

        # Get edges and gaps in view range
        edges = self.timeline.get_channel_edges(
            self.channel,
            int(x_min * 1e9),
            int(x_max * 1e9) if x_max > 0 else None
        )
        gaps = self.timeline.get_gaps_in_range(
            int(x_min * 1e9),
            int(x_max * 1e9) if x_max > 0 else None
        )

        # Convert to seconds for drawing
        sample_period_s = self.sample_period_ns / 1e9

        # Draw gaps as rectangles
        for gap in gaps:
            x1 = gap.start_time_ns / 1e9
            x2 = gap.end_time_ns / 1e9
            # Clamp to view
            x1 = max(x1, x_min)
            x2 = min(x2, x_max)
            if x2 > x1:
                rect = QRectF(x1, -0.5, x2 - x1, 1.0)
                self._gap_rects.append(rect)

        if not edges:
            self._needs_update = False
            return

        # Build digital waveform path
        # Start from initial state at left edge of view
        current_level = self.timeline.channel_timelines[self.channel].initial_state

        # Find state at x_min
        for edge in edges:
            if edge.time_ns / 1e9 <= x_min:
                current_level = edge.level
            else:
                break

        # Start path at left edge
        y_high = 0.4
        y_low = -0.4
        self._path.moveTo(x_min, y_high if current_level else y_low)

        # Draw edges within view
        for edge in edges:
            x = edge.time_ns / 1e9
            if x < x_min or x > x_max:
                continue

            y = y_high if edge.level else y_low

            # Vertical transition
            self._path.lineTo(x, y_high if current_level else y_low)
            self._path.lineTo(x, y)
            current_level = edge.level

        # Extend to right edge
        self._path.lineTo(x_max, y_high if current_level else y_low)

        self._needs_update = False


class WaveformView(PlotWidget):
    """Main waveform plotting widget with zoom/pan."""

    range_changed = pyqtSignal(float, float)  # x_min, x_max in seconds

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

        # Disable mouse on Y axis
        self.setMouseEnabled(x=True, y=False)

        # Create waveform items for each channel
        self.waveform_items = []
        for ch in range(8):
            item = DigitalWaveformItem(ch, timeline, self.sample_period_ns)
            self.addItem(item)
            self.waveform_items.append(item)

        # Set initial range
        start_ns, end_ns = timeline.get_time_range()
        if end_ns > start_ns:
            self.setXRange(start_ns / 1e9, end_ns / 1e9, padding=0)
        else:
            self.setXRange(0, 1, padding=0)

        # Connect view range changes
        self.vb = self.getViewBox()
        self.vb.sigXRangeChanged.connect(self._on_xrange_changed)

        # Time cursor
        self.cursor_line = pg.InfiniteLine(
            pos=0, angle=90, pen=pg.mkPen('#FFD700', width=1, style=Qt.PenStyle.DashLine)
        )
        self.addItem(self.cursor_line)
        self.cursor_visible = False

        # Measurement cursors
        self.cursor_a = None
        self.cursor_b = None
        self.cursor_a_pos = 0
        self.cursor_b_pos = 0

    def _on_xrange_changed(self, viewbox, ranges):
        try:
            # ranges can be different formats depending on pyqtgraph version
            if isinstance(ranges, (list, tuple)) and len(ranges) > 0:
                x_range = ranges[0]
            else:
                x_range = viewbox.viewRange()[0]

            if isinstance(x_range, (list, tuple)) and len(x_range) >= 2:
                self.range_changed.emit(float(x_range[0]), float(x_range[1]))

                # Update all waveform items
                for item in self.waveform_items:
                    item.set_view_range(float(x_range[0]), float(x_range[1]))
        except Exception as e:
            pass

    def set_time_range(self, start_s: float, end_s: float):
        """Set visible time range in seconds."""
        self.setXRange(start_s, end_s, padding=0)

    def set_cursor(self, time_s: float):
        """Set cursor position."""
        self.cursor_line.setPos(time_s)
        self.cursor_visible = True

    def hide_cursor(self):
        self.cursor_visible = False
        self.cursor_line.setPos(-1e9)  # Move off screen

    def add_measurement_cursor(self, label: str, time_s: float, color: str):
        """Add a measurement cursor."""
        line = pg.InfiniteLine(
            pos=time_s, angle=90,
            pen=pg.mkPen(color, width=1, style=Qt.PenStyle.DashLine),
            label=label, labelOpts={'color': color, 'fill': BACKGROUND_COLOR}
        )
        self.addItem(line)
        return line

    def get_visible_time_range(self) -> Tuple[float, float]:
        """Get current visible time range in seconds."""
        x_range = self.vb.viewRange()[0]
        return (x_range[0], x_range[1])


class StatsPanel(QWidget):
    """Statistics panel showing live values from STATS frames."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setup_ui()

        # Current stats
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

        # Title
        title = QLabel("STATISTICS")
        title.setStyleSheet("font-weight: bold; font-size: 12px; color: #888;")
        layout.addWidget(title)

        # Stats grid
        self.labels = {}
        stats_config = [
            ('Sample Rate', 'sample_hz', lambda v: f"{v/1e6:.1f} MHz"),
            ('Produced', 'produced', str),
            ('Consumed', 'consumed', str),
            ('Lost Buffers', 'lost_buffers', str),
            ('Max Proc (μs)', 'max_proc_us', str),
            ('TX Dropped Pkts', 'tx_drop_pkts', str),
            ('TX Dropped Samples', 'tx_drop_samples', str),
            ('CRC Errors', 'crc_bad', str),
            ('Seq Gaps', 'seq_gaps', str),
            ('Total Lost', 'lost_total', str),
        ]

        for label_text, key, formatter in stats_config:
            row = QHBoxLayout()
            label = QLabel(label_text)
            label.setStyleSheet("color: #aaa; font-size: 11px;")
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
        self.conn_label.setStyleSheet("color: #f66; font-size: 11px;")
        layout.addWidget(self.conn_label)

        layout.addStretch()

    def update_stats(self, stats_frame: StatsFrame):
        """Update from a STATS frame."""
        self.stats['sample_hz'] = stats_frame.sample_hz
        self.stats['produced'] = stats_frame.produced
        self.stats['consumed'] = stats_frame.consumed
        self.stats['lost_buffers'] = stats_frame.lost_buffers
        self.stats['max_proc_us'] = stats_frame.max_proc_us
        self.stats['tx_drop_pkts'] = stats_frame.tx_drop_pkts
        self.stats['tx_drop_samples'] = stats_frame.tx_drop_samples

        self._refresh_labels()

    def update_decoder_stats(self, crc_bad: int, seq_gaps: int, lost_total: int):
        """Update decoder-level stats."""
        self.stats['crc_bad'] = crc_bad
        self.stats['seq_gaps'] = seq_gaps
        self.stats['lost_total'] = lost_total
        self._refresh_labels()

    def set_connected(self, connected: bool):
        """Update connection status."""
        if connected:
            self.conn_label.setText("Connected")
            self.conn_label.setStyleSheet("color: #6f6; font-size: 11px;")
        else:
            self.conn_label.setText("Disconnected")
            self.conn_label.setStyleSheet("color: #f66; font-size: 11px;")

    def _refresh_labels(self):
        for key, (label, formatter) in self.labels.items():
            label.setText(formatter(self.stats[key]))


class ControlPanel(QWidget):
    """Control panel for zoom, pan, capture control."""

    zoom_changed = pyqtSignal(float)  # time per division in seconds
    pan_changed = pyqtSignal(float)   # position in seconds

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # Title
        title = QLabel("CONTROLS")
        title.setStyleSheet("font-weight: bold; font-size: 12px; color: #888;")
        layout.addWidget(title)

        # Time per division
        tdiv_layout = QHBoxLayout()
        tdiv_layout.addWidget(QLabel("Time/Div:"))
        self.tdiv_combo = QSpinBox()
        self.tdiv_combo.setRange(-9, 3)  # 1ns to 1s per div
        self.tdiv_combo.setValue(-3)  # 1ms per div
        self.tdiv_combo.setSuffix(" (10^x s)")
        self.tdiv_combo.valueChanged.connect(self._on_tdiv_changed)
        tdiv_layout.addWidget(self.tdiv_combo)
        layout.addLayout(tdiv_layout)

        # Position slider
        pos_layout = QVBoxLayout()
        pos_layout.addWidget(QLabel("Position:"))
        self.pos_slider = QSlider(Qt.Orientation.Horizontal)
        self.pos_slider.setRange(0, 1000)
        self.pos_slider.valueChanged.connect(self._on_pos_changed)
        pos_layout.addWidget(self.pos_slider)
        layout.addLayout(pos_layout)

        # Buttons
        btn_layout = QHBoxLayout()
        self.fit_btn = QPushButton("Fit All")
        self.fit_btn.clicked.connect(self._on_fit_all)
        btn_layout.addWidget(self.fit_btn)

        self.run_btn = QPushButton("Run")
        self.run_btn.setCheckable(True)
        self.run_btn.setChecked(True)
        btn_layout.addWidget(self.run_btn)
        layout.addLayout(btn_layout)

        # Channel visibility
        layout.addWidget(QLabel("Channels:"))
        self.ch_checkboxes = []
        for i in range(8):
            cb = QCheckBox(f"CH{i}")
            cb.setChecked(True)
            cb.setStyleSheet(f"color: {CHANNEL_COLORS[i]};")
            cb.toggled.connect(lambda checked, ch=i: self._on_channel_toggled(ch, checked))
            self.ch_checkboxes.append(cb)
            layout.addWidget(cb)

        layout.addStretch()

    def _on_tdiv_changed(self, value: int):
        self.zoom_changed.emit(10 ** value)

    def _on_pos_changed(self, value: int):
        self.pan_changed.emit(value / 1000.0)

    def _on_fit_all(self):
        self.fit_all_requested.emit()

    def _on_channel_toggled(self, channel: int, visible: bool):
        self.channel_visibility_changed.emit(channel, visible)

    # Signals
    fit_all_requested = pyqtSignal()
    channel_visibility_changed = pyqtSignal(int, bool)


class WaveformWindow(QMainWindow):
    """Main application window."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("ESP32 Logic Analyzer")
        self.resize(1400, 900)

        # State
        self.timeline = Timeline()
        self.parser = FrameParser()
        self.builder = TimelineBuilder()
        self.tcp_client = None

        # Connect parser to builder
        self.parser.on('data', self.builder.handle_data_frame)
        self.parser.on('stats', self.builder.handle_stats_frame)

        # Setup UI
        self.setup_ui()

        # Update timer for live streaming
        self.update_timer = QTimer()
        self.update_timer.timeout.connect(self.update_view)
        self.update_timer.start(50)  # 20 FPS

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
        main_layout.addWidget(self.waveform_view, stretch=4)

        # Right: Control and stats panels
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(4)

        self.control_panel = ControlPanel()
        self.control_panel.zoom_changed.connect(self.on_zoom_changed)
        self.control_panel.pan_changed.connect(self.on_pan_changed)
        self.control_panel.fit_all_requested.connect(self.fit_all)
        self.control_panel.channel_visibility_changed.connect(self.on_channel_visibility)

        self.stats_panel = StatsPanel()

        right_layout.addWidget(self.control_panel)
        right_layout.addWidget(self.stats_panel)

        main_layout.addWidget(right_panel, stretch=1)

        # Status bar
        self.statusBar().showMessage("Ready")

    def load_test_file(self):
        """Load stream.bin for testing."""
        import os
        stream_path = os.path.join(os.path.dirname(__file__), "stream.bin")
        if os.path.exists(stream_path):
            self.statusBar().showMessage(f"Loading {stream_path}...")
            decode_file(stream_path, self.parser)
            self.timeline = self.builder.get_timeline()
            self.waveform_view.timeline = self.timeline
            self.waveform_view.sample_period_ns = self.timeline.sample_period_ns

            # Update waveform items with new timeline
            for item in self.waveform_view.waveform_items:
                item.timeline = self.timeline
                item.sample_period_ns = self.timeline.sample_period_ns
                item._needs_update = True

            # Fit to data
            self.fit_all()

            # Update stats
            self.stats_panel.update_decoder_stats(
                self.parser.stats.crc_bad,
                self.parser.stats.seq_gaps,
                self.parser.stats.lost_total
            )

            self.statusBar().showMessage(
                f"Loaded: {self.parser.stats.frames} frames, "
                f"{self.timeline.get_sample_count()} samples, "
                f"{self.parser.stats.lost_total} lost, "
                f"{self.parser.stats.crc_bad} CRC errors"
            )

    def update_view(self):
        """Periodic update for live streaming."""
        if self.tcp_client and self.tcp_client.running:
            # Stats are updated via callbacks
            pass

        # Update stats panel with decoder stats
        self.stats_panel.update_decoder_stats(
            self.parser.stats.crc_bad,
            self.parser.stats.seq_gaps,
            self.parser.stats.lost_total
        )

    def on_zoom_changed(self, time_per_div: float):
        """Handle zoom change."""
        # 10 divisions visible
        visible_width = time_per_div * 10
        current_center = sum(self.waveform_view.get_visible_time_range()) / 2
        self.waveform_view.set_time_range(
            current_center - visible_width / 2,
            current_center + visible_width / 2
        )

    def on_pan_changed(self, pos: float):
        """Handle pan change (0-1)."""
        start_ns, end_ns = self.timeline.get_time_range()
        if end_ns <= start_ns:
            return

        total_width = (end_ns - start_ns) / 1e9
        visible_width = self.waveform_view.get_visible_time_range()[1] - \
                        self.waveform_view.get_visible_time_range()[0]

        max_start = (end_ns - start_ns) / 1e9 - visible_width
        new_start = start_ns / 1e9 + pos * max_start
        self.waveform_view.set_time_range(new_start, new_start + visible_width)

    def fit_all(self):
        """Fit view to all data."""
        start_ns, end_ns = self.timeline.get_time_range()
        if end_ns > start_ns:
            self.waveform_view.set_time_range(start_ns / 1e9, end_ns / 1e9)

    def on_channel_visibility(self, channel: int, visible: bool):
        """Toggle channel visibility."""
        item = self.waveform_view.waveform_items[channel]
        item.setVisible(visible)

    def connect_tcp(self, host: str, port: int):
        """Connect to ESP32 via TCP."""
        from parser import TCPClient

        self.tcp_client = TCPClient(host, port, self.parser)
        self.tcp_client.run()
        self.stats_panel.set_connected(True)
        self.statusBar().showMessage(f"Connected to {host}:{port}")


def main():
    app = QApplication(sys.argv)

    # Set dark theme
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

    window = WaveformWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()