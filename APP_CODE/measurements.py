#!/usr/bin/env python3
"""
Measurement utilities for ESP32 Logic Analyzer.
Provides frequency, period, duty cycle, pulse width, edge count, and cursor measurements.
"""

from dataclasses import dataclass
from typing import List, Optional, Tuple
import numpy as np

from timeline import Timeline, Edge, Gap


@dataclass
class ChannelMeasurements:
    """Measurements for a single channel."""
    channel: int
    edge_count: int = 0
    frequency_hz: Optional[float] = None
    period_s: Optional[float] = None
    duty_cycle_pct: Optional[float] = None
    high_time_s: Optional[float] = None
    low_time_s: Optional[float] = None
    rising_edges: int = 0
    falling_edges: int = 0

    def to_dict(self) -> dict:
        return {
            'channel': self.channel,
            'edge_count': self.edge_count,
            'frequency_hz': self.frequency_hz,
            'period_s': self.period_s,
            'duty_cycle_pct': self.duty_cycle_pct,
            'high_time_s': self.high_time_s,
            'low_time_s': self.low_time_s,
            'rising_edges': self.rising_edges,
            'falling_edges': self.falling_edges,
        }


@dataclass
class CursorMeasurement:
    """Measurement between two cursors."""
    cursor_a_time_s: float
    cursor_b_time_s: float
    delta_time_s: float
    delta_time_ns: int
    frequency_hz: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            'cursor_a_time_s': self.cursor_a_time_s,
            'cursor_b_time_s': self.cursor_b_time_s,
            'delta_time_s': self.delta_time_s,
            'delta_time_ns': self.delta_time_ns,
            'frequency_hz': self.frequency_hz,
        }


def measure_channel(timeline: Timeline, channel: int,
                    start_time_s: float = 0, end_time_s: Optional[float] = None) -> ChannelMeasurements:
    """
    Measure frequency, period, duty cycle, etc. for a channel.

    Args:
        timeline: The timeline to measure
        channel: Channel index (0-7)
        start_time_s: Start time in seconds (default: 0)
        end_time_s: End time in seconds (default: end of capture)

    Returns:
        ChannelMeasurements with all computed values
    """
    edges = timeline.get_channel_edges(channel,
                                       int(start_time_s * 1e9),
                                       int(end_time_s * 1e9) if end_time_s else None)

    if not edges:
        return ChannelMeasurements(channel=channel)

    # Convert to numpy arrays for easier computation
    times = np.array([e.time_ns for e in edges], dtype=np.float64) / 1e9
    levels = np.array([e.level for e in edges], dtype=np.int8)

    # Initial state
    initial_state = timeline.channel_timelines[channel].initial_state

    # If first edge is not at start_time, prepend initial state
    if times[0] > start_time_s:
        times = np.insert(times, 0, start_time_s)
        levels = np.insert(levels, 0, initial_state)

    # If last edge is not at end_time, append final state
    if end_time_s and times[-1] < end_time_s:
        times = np.append(times, end_time_s)
        levels = np.append(levels, levels[-1])

    # Count edges
    edge_count = len(edges)
    rising_edges = int(np.sum(np.diff(levels) > 0))
    falling_edges = int(np.sum(np.diff(levels) < 0))

    # Calculate period and frequency from rising edges
    rising_times = times[np.diff(levels, prepend=initial_state) > 0]
    if len(rising_times) >= 2:
        periods = np.diff(rising_times)
        period_s = float(np.mean(periods))
        frequency_hz = 1.0 / period_s if period_s > 0 else None
    else:
        period_s = None
        frequency_hz = None

    # Calculate duty cycle
    high_time = 0.0
    low_time = 0.0
    for i in range(len(times) - 1):
        dt = times[i + 1] - times[i]
        if levels[i] == 1:
            high_time += dt
        else:
            low_time += dt

    total_time = high_time + low_time
    duty_cycle_pct = (high_time / total_time * 100) if total_time > 0 else None

    return ChannelMeasurements(
        channel=channel,
        edge_count=edge_count,
        frequency_hz=frequency_hz,
        period_s=period_s,
        duty_cycle_pct=duty_cycle_pct,
        high_time_s=high_time if high_time > 0 else None,
        low_time_s=low_time if low_time > 0 else None,
        rising_edges=rising_edges,
        falling_edges=falling_edges,
    )


def measure_all_channels(timeline: Timeline,
                         start_time_s: float = 0,
                         end_time_s: Optional[float] = None) -> List[ChannelMeasurements]:
    """Measure all 8 channels."""
    return [measure_channel(timeline, ch, start_time_s, end_time_s)
            for ch in range(8)]


def measure_cursor_delta(timeline: Timeline, channel: int,
                         cursor_a_s: float, cursor_b_s: float) -> CursorMeasurement:
    """
    Measure time delta between two cursors on a specific channel.
    Also counts edges between cursors.
    """
    delta_s = abs(cursor_b_s - cursor_a_s)
    delta_ns = int(delta_s * 1e9)

    # Count edges between cursors
    edges = timeline.get_channel_edges(channel,
                                       int(min(cursor_a_s, cursor_b_s) * 1e9),
                                       int(max(cursor_a_s, cursor_b_s) * 1e9))

    # If there are at least 2 edges, we can compute frequency
    frequency_hz = None
    if len(edges) >= 2:
        # Find rising edges
        rising_times = [e.time_ns / 1e9 for e in edges
                        if e.level == 1 and (len(edges) == 1 or edges[edges.index(e)-1].level == 0)]
        if len(rising_times) >= 2:
            period = rising_times[-1] - rising_times[0]
            if period > 0:
                frequency_hz = (len(rising_times) - 1) / period

    return CursorMeasurement(
        cursor_a_time_s=cursor_a_s,
        cursor_b_time_s=cursor_b_s,
        delta_time_s=delta_s,
        delta_time_ns=delta_ns,
        frequency_hz=frequency_hz,
    )


def measure_gap_duration(gap: Gap) -> Tuple[float, int]:
    """Get gap duration in seconds and nanoseconds."""
    duration_ns = gap.end_time_ns - gap.start_time_ns
    return duration_ns / 1e9, duration_ns


# --- Demo / Test ---

if __name__ == "__main__":
    from parser import FrameParser, decode_file
    from timeline import TimelineBuilder
    import os

    # Load test data
    parser = FrameParser()
    builder = TimelineBuilder()
    parser.on('data', builder.handle_data_frame)
    parser.on('stats', builder.handle_stats_frame)

    stream_path = os.path.join(os.path.dirname(__file__), "stream.bin")
    if os.path.exists(stream_path):
        decode_file(stream_path, parser)
        timeline = builder.get_timeline()

        print("=== Channel Measurements (full capture) ===")
        measurements = measure_all_channels(timeline)
        for m in measurements:
            print(f"CH{m.channel}: edges={m.edge_count}, "
                  f"freq={m.frequency_hz:.1f}Hz" if m.frequency_hz else "freq=N/A, "
                  f"period={m.period_s*1e6:.1f}µs" if m.period_s else "period=N/A, "
                  f"duty={m.duty_cycle_pct:.1f}%" if m.duty_cycle_pct else "duty=N/A")

        print("\n=== Cursor Measurement (CH0, 10ms to 110ms) ===")
        cursor = measure_cursor_delta(timeline, 0, 0.01, 0.11)
        print(f"Delta: {cursor.delta_time_s*1e3:.1f}ms ({cursor.delta_time_ns}ns)")
        print(f"Freq: {cursor.frequency_hz:.1f}Hz" if cursor.frequency_hz else "Freq: N/A")

        print("\n=== Gap Measurements ===")
        for gap in timeline.gaps[:3]:
            dur_s, dur_ns = measure_gap_duration(gap)
            print(f"Gap at {gap.start_time_ns/1e6:.1f}ms: {dur_s*1e6:.1f}µs ({gap.reason})")