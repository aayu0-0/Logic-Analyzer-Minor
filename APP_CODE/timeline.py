#!/usr/bin/env python3
"""
Timeline model for ESP32 Logic Analyzer.
Converts RLE events into per-channel edge lists with timestamps.
Handles gaps (unknown spans), board restarts, and merging of equal neighboring events.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple, Iterator
from enum import IntEnum
import math


class SampleState(IntEnum):
    """State of a sample in the timeline."""
    LOW = 0
    HIGH = 1
    UNKNOWN = 2  # Gap/lost samples


@dataclass
class Edge:
    """A single edge (transition) on a channel."""
    time_ns: int      # Absolute time in nanoseconds
    level: int        # 0 or 1
    channel: int      # 0-7
    sample_index: int # Absolute sample index

    def __lt__(self, other):
        return self.time_ns < other.time_ns


@dataclass
class Gap:
    """A gap (unknown span) in the timeline."""
    start_sample: int
    end_sample: int      # Exclusive
    start_time_ns: int
    end_time_ns: int     # Exclusive
    reason: str          # "reported", "unreported", "restart", "crc"


@dataclass
class ChannelTimeline:
    """Timeline for a single channel."""
    channel: int
    edges: List[Edge] = field(default_factory=list)
    initial_state: int = 0  # State at time 0

    def add_edge(self, edge: Edge):
        """Add an edge, merging with previous if same level."""
        if self.edges and self.edges[-1].level == edge.level:
            # Same level - merge (extend previous edge's duration implicitly)
            return
        self.edges.append(edge)

    def get_state_at(self, sample_index: int, sample_period_ns: float) -> int:
        """Get the state of this channel at a given sample index."""
        if not self.edges:
            return self.initial_state

        # Find the last edge before or at this sample
        for edge in reversed(self.edges):
            if edge.sample_index <= sample_index:
                return edge.level
        return self.initial_state

    def get_edges_in_range(self, start_sample: int, end_sample: int,
                           sample_period_ns: float) -> List[Edge]:
        """Get edges within a sample range."""
        result = []
        for edge in self.edges:
            if start_sample <= edge.sample_index < end_sample:
                result.append(edge)
        return result


@dataclass
class Timeline:
    """
    Complete timeline model for all 8 channels.
    Handles RLE event decoding, gap tracking, and board restarts.
    """
    sample_hz: int = 5_000_000
    channels: int = 8

    # Per-channel timelines
    channel_timelines: List[ChannelTimeline] = field(default_factory=list)

    # Gaps in the timeline
    gaps: List[Gap] = field(default_factory=list)

    # Board restart markers
    restarts: List[int] = field(default_factory=list)  # Sample indices where restarts occurred

    # Current state
    _current_sample: int = 0
    _base_sample: int = 0      # Offset for restarts
    _last_state: int = 0       # Last known 8-bit state byte
    _attached: bool = False    # First frame received

    # Sample period in nanoseconds
    @property
    def sample_period_ns(self) -> float:
        return 1e9 / self.sample_hz

    def __post_init__(self):
        self.channel_timelines = [ChannelTimeline(i) for i in range(self.channels)]

    def reset(self):
        """Reset timeline for new capture."""
        self.channel_timelines = [ChannelTimeline(i) for i in range(self.channels)]
        self.gaps = []
        self.restarts = []
        self._current_sample = 0
        self._base_sample = 0
        self._last_state = 0
        self._attached = False

    def attach(self, start_sample: int, lost_before: int):
        """Called when first DATA frame arrives (mid-stream join)."""
        self._attached = True
        self._current_sample = start_sample - lost_before
        self._base_sample = 0
        self._last_state = None

    def handle_restart(self, old_pos: int, new_start: int):
        """Handle board restart (sample index went backwards)."""
        self.restarts.append(self._base_sample + self._current_sample)
        self._base_sample += self._current_sample
        self._current_sample = 0
        self._last_state = None

    def add_gap(self, start_sample: int, count: int, reason: str):
        """Record a gap in the timeline."""
        abs_start = self._base_sample + start_sample
        abs_end = abs_start + count
        gap = Gap(
            start_sample=abs_start,
            end_sample=abs_end,
            start_time_ns=int(abs_start * self.sample_period_ns),
            end_time_ns=int(abs_end * self.sample_period_ns),
            reason=reason
        )
        self.gaps.append(gap)
        self._last_state = None  # Unknown state after gap

    def process_events(self, events: bytes, start_sample: int):
        """
        Process RLE events and update channel timelines.

        Args:
            events: Raw RLE event bytes
            start_sample: Absolute sample index of first event (relative to current board run)
        """
        # Convert to absolute sample index
        abs_start = self._base_sample + start_sample

        # If we have a last known state and it's the same as first event, merge
        offset = 0
        pos = abs_start

        # Import here to avoid circular dependency
        from parser import rle_iter_events

        for state_byte, run in rle_iter_events(events):
            if run == 0:
                continue

            # Check each channel for transitions
            for ch in range(self.channels):
                new_level = (state_byte >> ch) & 1
                tl = self.channel_timelines[ch]

                # Determine previous level
                if pos == abs_start and self._last_state is not None:
                    prev_level = (self._last_state >> ch) & 1
                elif tl.edges:
                    prev_level = tl.edges[-1].level
                else:
                    prev_level = tl.initial_state

                # If level changed, add edge
                if new_level != prev_level:
                    edge = Edge(
                        time_ns=int(pos * self.sample_period_ns),
                        level=new_level,
                        channel=ch,
                        sample_index=pos
                    )
                    tl.add_edge(edge)

            pos += run

        # Update current position and last state
        self._current_sample = start_sample + sum(run for _, run in rle_iter_events(events))
        self._last_state = state_byte if events else self._last_state

    def get_channel_edges(self, channel: int, start_time_ns: int = 0,
                          end_time_ns: Optional[int] = None) -> List[Edge]:
        """Get edges for a channel within a time range."""
        tl = self.channel_timelines[channel]

        start_sample = int(start_time_ns / self.sample_period_ns)
        if end_time_ns is None:
            # Use current timeline end
            end_sample = self._base_sample + self._current_sample
        else:
            end_sample = int(end_time_ns / self.sample_period_ns)

        return tl.get_edges_in_range(start_sample, end_sample, self.sample_period_ns)

    def get_all_edges(self, start_time_ns: int = 0,
                      end_time_ns: Optional[int] = None) -> Dict[int, List[Edge]]:
        """Get edges for all channels within a time range."""
        return {ch: self.get_channel_edges(ch, start_time_ns, end_time_ns)
                for ch in range(self.channels)}

    def get_gaps_in_range(self, start_time_ns: int = 0,
                          end_time_ns: Optional[int] = None) -> List[Gap]:
        """Get gaps within a time range."""
        if end_time_ns is None:
            end_time_ns = int((self._base_sample + self._current_sample) * self.sample_period_ns)

        return [g for g in self.gaps
                if g.start_time_ns < end_time_ns and g.end_time_ns > start_time_ns]

    def get_time_range(self) -> Tuple[int, int]:
        """Get the total time range of the capture in nanoseconds."""
        if not self.gaps and all(not tl.edges for tl in self.channel_timelines):
            return (0, 0)

        min_time = float('inf')
        max_time = 0

        for tl in self.channel_timelines:
            for edge in tl.edges:
                min_time = min(min_time, edge.time_ns)
                max_time = max(max_time, edge.time_ns)

        for gap in self.gaps:
            min_time = min(min_time, gap.start_time_ns)
            max_time = max(max_time, gap.end_time_ns)

        # Also consider the current timeline position (end of last run)
        end_sample = self._base_sample + self._current_sample
        if end_sample > 0:
            max_time = max(max_time, int(end_sample * self.sample_period_ns))

        if min_time == float('inf'):
            return (0, 0)
        return (int(min_time), int(max_time))

    def get_duration_ns(self) -> int:
        """Get total capture duration in nanoseconds."""
        start, end = self.get_time_range()
        return end - start

    def get_sample_count(self) -> int:
        """Get total number of samples processed."""
        return self._base_sample + self._current_sample


# --- Timeline Builder (convenience class for building from parser events) ---

class TimelineBuilder:
    """
    Builds a Timeline from parser events.
    Handles the receiver rules: gaps, restarts, mid-stream join, etc.
    """

    def __init__(self, sample_hz: int = 5_000_000, channels: int = 8):
        self.timeline = Timeline(sample_hz=sample_hz, channels=channels)
        self._expect_seq: Optional[int] = None

    def handle_data_frame(self, frame) -> None:
        """Process a DataFrame from the parser."""
        from parser import DataFrame

        if not isinstance(frame, DataFrame):
            return

        # Sequence tracking
        if self._expect_seq is not None and frame.seq != self._expect_seq:
            gap = (frame.seq - self._expect_seq) & 0xFFFF
            # Sequence gap implies lost frames - we don't know how many samples
            # This is reported separately by the parser
            pass
        self._expect_seq = (frame.seq + 1) & 0xFFFF

        # First frame: attach
        if not self.timeline._attached:
            self.timeline.attach(frame.start_sample, frame.lost_before)

        # Restart detection
        elif frame.start_sample < self.timeline._current_sample:
            self.timeline.handle_restart(
                self.timeline._current_sample, frame.start_sample)

        # Gap detection (unreported hole)
        expected_pos = self.timeline._current_sample + frame.lost_before
        if frame.start_sample != expected_pos:
            extra = frame.start_sample - expected_pos
            self.timeline.add_gap(frame.start_sample, extra, "unreported")

        # Reported gap (lost_before)
        if frame.lost_before > 0:
            self.timeline.add_gap(frame.start_sample, frame.lost_before, "reported")

        # Process events
        self.timeline.process_events(frame.events, frame.start_sample)

    def handle_stats_frame(self, frame) -> None:
        """Update timeline parameters from STATS frame."""
        from parser import StatsFrame

        if not isinstance(frame, StatsFrame):
            return

        self.timeline.sample_hz = frame.sample_hz
        self.timeline.channels = frame.channels

    def get_timeline(self) -> Timeline:
        """Get the built timeline."""
        return self.timeline


# --- Demo / Test ---

if __name__ == "__main__":
    from parser import FrameParser, decode_file
    import os

    # Test with stream.bin
    stream_path = os.path.join(os.path.dirname(__file__), "stream.bin")
    if os.path.exists(stream_path):
        print("=== Building timeline from stream.bin ===")

        parser = FrameParser()
        builder = TimelineBuilder()

        parser.on('data', builder.handle_data_frame)
        parser.on('stats', builder.handle_stats_frame)

        decode_file(stream_path, parser)

        timeline = builder.get_timeline()

        print(f"Sample rate: {timeline.sample_hz} Hz")
        print(f"Total samples: {timeline.get_sample_count()}")
        print(f"Duration: {timeline.get_duration_ns() / 1e9:.3f} seconds")
        print(f"Gaps: {len(timeline.gaps)}")
        print(f"Restarts: {len(timeline.restarts)}")

        for ch in range(8):
            edges = timeline.channel_timelines[ch].edges
            print(f"CH{ch}: {len(edges)} edges")
            if edges:
                print(f"  First edge: t={edges[0].time_ns}ns, level={edges[0].level}")
                print(f"  Last edge:  t={edges[-1].time_ns}ns, level={edges[-1].level}")

        print("\nGaps:")
        for gap in timeline.gaps:
            print(f"  {gap.start_time_ns/1e6:.1f}-{gap.end_time_ns/1e6:.1f}ms "
                  f"({gap.end_sample-gap.start_sample} samples, {gap.reason})")