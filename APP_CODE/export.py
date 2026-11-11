#!/usr/bin/env python3
"""
Export utilities for ESP32 Logic Analyzer.
Supports VCD (Value Change Dump) for PulseView/GTKWave and CSV export.
"""

import csv
from dataclasses import dataclass
from typing import List, Optional, TextIO, BinaryIO
from pathlib import Path

from timeline import Timeline, Edge, Gap


@dataclass
class ExportOptions:
    """Options for export."""
    start_time_s: float = 0
    end_time_s: Optional[float] = None
    channels: Optional[List[int]] = None  # None = all
    include_gaps: bool = True
    timescale: str = "1ns"  # VCD timescale


def export_vcd(timeline: Timeline, output: TextIO,
               options: Optional[ExportOptions] = None) -> None:
    """
    Export timeline to VCD format (compatible with PulseView/GTKWave).

    Args:
        timeline: Timeline to export
        output: File-like object to write to
        options: Export options
    """
    if options is None:
        options = ExportOptions()

    channels = options.channels if options.channels is not None else range(8)
    start_ns = int(options.start_time_s * 1e9)
    end_ns = int(options.end_time_s * 1e9) if options.end_time_s else None

    # Get edges for selected channels in range
    all_edges = {}
    max_time_ns = 0

    for ch in channels:
        edges = timeline.get_channel_edges(ch, start_ns, end_ns)
        all_edges[ch] = edges
        if edges:
            max_time_ns = max(max_time_ns, edges[-1].time_ns)

    # Also consider gaps
    if options.include_gaps:
        gaps = timeline.get_gaps_in_range(start_ns, end_ns)
        for gap in gaps:
            max_time_ns = max(max_time_ns, gap.end_time_ns)

    if max_time_ns == 0:
        max_time_ns = 1

    # VCD header
    output.write(f"$timescale {options.timescale} $end\n")
    output.write("$scope module logic_analyzer $end\n")

    # Variable declarations
    for ch in channels:
        # VCD uses single-character IDs starting from '!'
        var_id = chr(33 + ch)
        output.write(f"$var wire 1 {var_id} CH{ch} $end\n")

    output.write("$upscope $end\n")
    output.write("$enddefinitions $end\n")

    # Initial values at time 0
    output.write(f"#0\n")
    for ch in channels:
        initial_state = timeline.channel_timelines[ch].initial_state
        var_id = chr(33 + ch)
        output.write(f"{initial_state}{var_id}\n")

    # Collect all events (edges + gaps) sorted by time
    events = []

    # Add edges
    for ch in channels:
        for edge in all_edges[ch]:
            events.append((edge.time_ns, 'edge', ch, edge.level))

    # Add gaps as unknown ('x') values
    if options.include_gaps:
        for gap in timeline.get_gaps_in_range(start_ns, end_ns):
            # Gap affects all channels
            for ch in channels:
                events.append((gap.start_time_ns, 'gap_start', ch, None))
                events.append((gap.end_time_ns, 'gap_end', ch, None))

    # Sort by time
    events.sort(key=lambda x: x[0])

    # Write events
    current_time = -1
    for time_ns, event_type, ch, value in events:
        if time_ns != current_time:
            output.write(f"#{time_ns}\n")
            current_time = time_ns

        var_id = chr(33 + ch)
        if event_type == 'edge':
            output.write(f"{value}{var_id}\n")
        elif event_type == 'gap_start':
            output.write(f"x{var_id}\n")
        elif event_type == 'gap_end':
            # After gap, restore last known value
            # Find the last edge before or at gap end
            last_edge = None
            for edge in all_edges[ch]:
                if edge.time_ns <= time_ns:
                    last_edge = edge
                else:
                    break
            if last_edge:
                output.write(f"{last_edge.level}{var_id}\n")
            else:
                output.write(f"{timeline.channel_timelines[ch].initial_state}{var_id}\n")


def export_csv(timeline: Timeline, output: TextIO,
               options: Optional[ExportOptions] = None) -> None:
    """
    Export timeline edges to CSV format.

    CSV columns: time_ns, time_s, channel, level, sample_index
    """
    if options is None:
        options = ExportOptions()

    channels = options.channels if options.channels is not None else range(8)
    start_ns = int(options.start_time_s * 1e9)
    end_ns = int(options.end_time_s * 1e9) if options.end_time_s else None

    writer = csv.writer(output)
    writer.writerow(['time_ns', 'time_s', 'channel', 'level', 'sample_index'])

    for ch in channels:
        edges = timeline.get_channel_edges(ch, start_ns, end_ns)
        for edge in edges:
            writer.writerow([
                edge.time_ns,
                f"{edge.time_ns / 1e9:.9f}",
                edge.channel,
                edge.level,
                edge.sample_index
            ])


def export_csv_gaps(timeline: Timeline, output: TextIO,
                    options: Optional[ExportOptions] = None) -> None:
    """Export gaps to CSV."""
    if options is None:
        options = ExportOptions()

    start_ns = int(options.start_time_s * 1e9)
    end_ns = int(options.end_time_s * 1e9) if options.end_time_s else None

    writer = csv.writer(output)
    writer.writerow(['start_ns', 'start_s', 'end_ns', 'end_s',
                     'duration_ns', 'duration_s', 'reason'])

    for gap in timeline.get_gaps_in_range(start_ns, end_ns):
        writer.writerow([
            gap.start_time_ns,
            f"{gap.start_time_ns / 1e9:.9f}",
            gap.end_time_ns,
            f"{gap.end_time_ns / 1e9:.9f}",
            gap.end_time_ns - gap.start_time_ns,
            f"{(gap.end_time_ns - gap.start_time_ns) / 1e9:.9f}",
            gap.reason
        ])


def export_raw_stream(parser, output: BinaryIO) -> None:
    """Export raw stream bytes (for replay)."""
    # This would require storing the raw bytes during parsing
    # For now, this is a placeholder
    pass


# --- Demo / Test ---

if __name__ == "__main__":
    from parser import FrameParser, decode_file
    from timeline import TimelineBuilder
    import os
    import io

    # Load test data
    parser = FrameParser()
    builder = TimelineBuilder()
    parser.on('data', builder.handle_data_frame)
    parser.on('stats', builder.handle_stats_frame)

    stream_path = os.path.join(os.path.dirname(__file__), "stream.bin")
    if os.path.exists(stream_path):
        decode_file(stream_path, parser)
        timeline = builder.get_timeline()

        # Test VCD export
        print("=== VCD Export (first 200 chars) ===")
        vcd_buffer = io.StringIO()
        export_vcd(timeline, vcd_buffer, ExportOptions(end_time_s=0.001))  # First 1ms
        vcd_content = vcd_buffer.getvalue()
        print(vcd_content[:500])
        print("...")

        # Test CSV export
        print("\n=== CSV Export (edges, first 10 rows) ===")
        csv_buffer = io.StringIO()
        export_csv(timeline, csv_buffer, ExportOptions(end_time_s=0.001))
        csv_lines = csv_buffer.getvalue().split('\n')
        for line in csv_lines[:12]:
            print(line)

        print("\n=== CSV Gaps Export ===")
        csv_buffer = io.StringIO()
        export_csv_gaps(timeline, csv_buffer)
        print(csv_buffer.getvalue())

        # Save full files
        with open('export_test.vcd', 'w') as f:
            export_vcd(timeline, f)
        print("\nSaved export_test.vcd")

        with open('export_test_edges.csv', 'w') as f:
            export_csv(timeline, f)
        print("Saved export_test_edges.csv")

        with open('export_test_gaps.csv', 'w') as f:
            export_csv_gaps(timeline, f)
        print("Saved export_test_gaps.csv")