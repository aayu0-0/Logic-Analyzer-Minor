#!/usr/bin/env python3
"""
Unit tests for the timeline model.
"""

import unittest
from timeline import Timeline, TimelineBuilder, Edge, Gap
from parser import FrameParser, DataFrame, StatsFrame, rle_iter_events


class TestTimeline(unittest.TestCase):
    """Test the Timeline class."""

    def setUp(self):
        self.timeline = Timeline(sample_hz=5_000_000, channels=8)

    def test_initial_state(self):
        """Test initial timeline state."""
        self.assertEqual(self.timeline.sample_hz, 5_000_000)
        self.assertEqual(self.timeline.channels, 8)
        self.assertEqual(len(self.timeline.channel_timelines), 8)
        self.assertEqual(self.timeline.get_sample_count(), 0)

    def test_attach(self):
        """Test mid-stream join."""
        self.timeline.attach(start_sample=10000, lost_before=5000)
        self.assertEqual(self.timeline._current_sample, 5000)
        self.assertTrue(self.timeline._attached)

    def test_process_events_basic(self):
        """Test processing simple RLE events."""
        # Attach first
        self.timeline.attach(1000, 0)

        # Events: CH0 high for 10 samples, then low for 20 samples
        # State byte 0x01 (CH0=1), run=10
        # State byte 0x00 (CH0=0), run=20
        events = bytes([0x01, 0x0A, 0x00, 0x14])

        self.timeline.process_events(events, start_sample=1000)

        # Check CH0 edges
        ch0_edges = self.timeline.channel_timelines[0].edges
        self.assertEqual(len(ch0_edges), 2)
        self.assertEqual(ch0_edges[0].level, 1)
        self.assertEqual(ch0_edges[0].sample_index, 1000)
        self.assertEqual(ch0_edges[1].level, 0)
        self.assertEqual(ch0_edges[1].sample_index, 1010)

        # Other channels should have no edges (stay at initial 0)
        for ch in range(1, 8):
            self.assertEqual(len(self.timeline.channel_timelines[ch].edges), 0)

    def test_process_events_multi_channel(self):
        """Test events affecting multiple channels."""
        self.timeline.attach(0, 0)

        # State 0x03 (CH0=1, CH1=1), run=5
        # State 0x00, run=5
        events = bytes([0x03, 0x05, 0x00, 0x05])

        self.timeline.process_events(events, start_sample=0)

        # CH0: 0->1 at 0, 1->0 at 5
        ch0 = self.timeline.channel_timelines[0].edges
        self.assertEqual(len(ch0), 2)
        self.assertEqual(ch0[0].level, 1)
        self.assertEqual(ch0[1].level, 0)

        # CH1: same as CH0
        ch1 = self.timeline.channel_timelines[1].edges
        self.assertEqual(len(ch1), 2)
        self.assertEqual(ch1[0].level, 1)
        self.assertEqual(ch1[1].level, 0)

        # CH2-7: no edges
        for ch in range(2, 8):
            self.assertEqual(len(self.timeline.channel_timelines[ch].edges), 0)

    def test_merge_same_level(self):
        """Test that consecutive same-level events are merged."""
        self.timeline.attach(0, 0)

        # Two consecutive events with same state for CH0
        # State 0x01 (CH0=1), run=5
        # State 0x01 (CH0=1), run=5  <- should merge
        # State 0x00 (CH0=0), run=5
        events = bytes([0x01, 0x05, 0x01, 0x05, 0x00, 0x05])

        self.timeline.process_events(events, start_sample=0)

        ch0 = self.timeline.channel_timelines[0].edges
        # Should only have 2 edges: 0->1 at 0, 1->0 at 10
        self.assertEqual(len(ch0), 2)
        self.assertEqual(ch0[0].sample_index, 0)
        self.assertEqual(ch0[0].level, 1)
        self.assertEqual(ch0[1].sample_index, 10)
        self.assertEqual(ch0[1].level, 0)

    def test_gap_handling(self):
        """Test gap recording."""
        self.timeline.attach(0, 0)

        # Add a gap
        self.timeline.add_gap(100, 50, "reported")

        self.assertEqual(len(self.timeline.gaps), 1)
        gap = self.timeline.gaps[0]
        self.assertEqual(gap.start_sample, 100)
        self.assertEqual(gap.end_sample, 150)
        self.assertEqual(gap.reason, "reported")
        # After gap, state should be unknown
        self.assertIsNone(self.timeline._last_state)

    def test_restart_handling(self):
        """Test board restart."""
        self.timeline.attach(10000, 0)
        self.timeline.process_events(bytes([0x01, 0x64]), 10000)  # 100 samples

        # Restart: sample index goes back to 0
        self.timeline.handle_restart(10100, 0)

        self.assertEqual(len(self.timeline.restarts), 1)
        self.assertEqual(self.timeline.restarts[0], 10100)
        self.assertEqual(self.timeline._base_sample, 10100)
        self.assertEqual(self.timeline._current_sample, 0)
        self.assertIsNone(self.timeline._last_state)

    def test_get_time_range(self):
        """Test getting capture time range."""
        self.timeline.attach(0, 0)
        self.timeline.process_events(bytes([0x01, 0x64, 0x00, 0x64]), 0)  # 100+100 samples

        start, end = self.timeline.get_time_range()
        self.assertEqual(start, 0)
        self.assertEqual(end, int(200 * 1e9 / 5_000_000))  # 200 samples at 5MHz

    def test_get_edges_in_range(self):
        """Test querying edges in time range."""
        self.timeline.attach(0, 0)
        self.timeline.process_events(bytes([0x01, 0x64, 0x00, 0x64]), 0)

        # Get edges in first half
        edges = self.timeline.get_channel_edges(0, 0, int(100 * 1e9 / 5_000_000))
        self.assertEqual(len(edges), 1)  # Only rising edge at 0
        self.assertEqual(edges[0].level, 1)

        # Get edges in second half
        edges = self.timeline.get_channel_edges(0, int(100 * 1e9 / 5_000_000), None)
        self.assertEqual(len(edges), 1)  # Only falling edge at 100
        self.assertEqual(edges[0].level, 0)


class TestTimelineBuilder(unittest.TestCase):
    """Test the TimelineBuilder integration with parser frames."""

    def setUp(self):
        self.builder = TimelineBuilder(sample_hz=5_000_000, channels=8)

    def _make_frame(self, seq: int, start: int, events: list, lost_before: int = 0):
        """Helper to create a DataFrame-like object."""
        frame = DataFrame(
            seq=seq, flags=0, start_sample=start,
            lost_before=lost_before, events=b""
        )
        # Build events bytes
        payload = bytearray()
        for state, run in events:
            payload.append(state)
            val = run
            while val >= 0x80:
                payload.append((val & 0x7F) | 0x80)
                val >>= 7
            payload.append(val)
        frame.events = bytes(payload)
        return frame

    def test_simple_capture(self):
        """Test building timeline from frames."""
        # Frame 1: attach at sample 0
        frame1 = self._make_frame(0, 0, [(0x01, 100), (0x00, 100)], lost_before=0)
        self.builder.handle_data_frame(frame1)

        # Frame 2: continue at sample 200
        frame2 = self._make_frame(1, 200, [(0x01, 50), (0x00, 50)], lost_before=0)
        self.builder.handle_data_frame(frame2)

        timeline = self.builder.get_timeline()

        # CH0 should have edges at 0, 100, 200, 250
        ch0 = timeline.channel_timelines[0].edges
        self.assertEqual(len(ch0), 4)
        self.assertEqual([e.sample_index for e in ch0], [0, 100, 200, 250])
        self.assertEqual([e.level for e in ch0], [1, 0, 1, 0])

    def test_gap_in_frames(self):
        """Test gap detection between frames."""
        frame1 = self._make_frame(0, 0, [(0x01, 100)], lost_before=0)
        self.builder.handle_data_frame(frame1)

        # Frame 2 has lost_before=50, starts at 150 (gap of 50)
        frame2 = self._make_frame(1, 150, [(0x00, 100)], lost_before=50)
        self.builder.handle_data_frame(frame2)

        timeline = self.builder.get_timeline()

        # Should have one gap
        self.assertEqual(len(timeline.gaps), 1)
        self.assertEqual(timeline.gaps[0].start_sample, 150)
        self.assertEqual(timeline.gaps[0].end_sample, 200)
        self.assertEqual(timeline.gaps[0].reason, "reported")

    def test_unreported_gap(self):
        """Test detection of unreported gap (frame lost on link)."""
        frame1 = self._make_frame(0, 0, [(0x01, 100)], lost_before=0)
        self.builder.handle_data_frame(frame1)

        # Frame 2: seq=1, but start=200 with lost_before=0
        # Expected pos = 100, but start=200 -> extra 100 samples missing
        frame2 = self._make_frame(1, 200, [(0x00, 100)], lost_before=0)
        self.builder.handle_data_frame(frame2)

        timeline = self.builder.get_timeline()

        # Should have one unreported gap
        self.assertEqual(len(timeline.gaps), 1)
        self.assertEqual(timeline.gaps[0].start_sample, 200)
        self.assertEqual(timeline.gaps[0].end_sample, 300)
        self.assertEqual(timeline.gaps[0].reason, "unreported")

    def test_restart_in_frames(self):
        """Test restart detection."""
        frame1 = self._make_frame(0, 10000, [(0x01, 100)], lost_before=0)
        self.builder.handle_data_frame(frame1)

        # Frame 2: sample index went backwards to 0
        frame2 = self._make_frame(1, 0, [(0x00, 100)], lost_before=0)
        self.builder.handle_data_frame(frame2)

        timeline = self.builder.get_timeline()

        self.assertEqual(len(timeline.restarts), 1)
        self.assertEqual(timeline.restarts[0], 10100)
        self.assertEqual(timeline._base_sample, 10100)

    def test_sequence_gap(self):
        """Test sequence number gap detection."""
        frame1 = self._make_frame(0, 0, [(0x01, 100)], lost_before=0)
        self.builder.handle_data_frame(frame1)

        # Skip seq=1, go to seq=2
        frame2 = self._make_frame(2, 100, [(0x00, 100)], lost_before=0)
        self.builder.handle_data_frame(frame2)

        # Builder doesn't track seq gaps directly, but timeline should still work
        timeline = self.builder.get_timeline()
        # Frame1: 100 samples, Frame2: starts at 100, adds 100 = 200 total
        self.assertEqual(timeline.get_sample_count(), 200)


class TestIntegrationWithParser(unittest.TestCase):
    """Integration test with actual parser and sample stream."""

    def test_stream_bin(self):
        """Test full pipeline with stream.bin."""
        import os
        stream_path = os.path.join(os.path.dirname(__file__), "stream.bin")
        if not os.path.exists(stream_path):
            self.skipTest("stream.bin not found")

        parser = FrameParser()
        builder = TimelineBuilder()

        parser.on('data', builder.handle_data_frame)
        parser.on('stats', builder.handle_stats_frame)

        with open(stream_path, "rb") as f:
            data = f.read()
        parser.feed(data)

        timeline = builder.get_timeline()

        # Verify expected results
        self.assertEqual(parser.stats.frames, 140)
        self.assertEqual(parser.stats.crc_bad, 0)
        self.assertEqual(parser.stats.lost_total, 152000)
        self.assertEqual(timeline.get_sample_count(), 3352000)
        self.assertEqual(len(timeline.gaps), 10)
        self.assertEqual(len(timeline.restarts), 0)

        # Each channel should have edges
        for ch in range(8):
            edges = timeline.channel_timelines[ch].edges
            self.assertGreater(len(edges), 0, f"CH{ch} should have edges")


if __name__ == "__main__":
    unittest.main(verbosity=2)