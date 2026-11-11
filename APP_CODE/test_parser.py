#!/usr/bin/env python3
"""
Unit tests for the frame parser.
Tests the worked examples from HANDOFF.md section 5.5 and the sample stream.
"""

import unittest
import os
from parser import FrameParser, rle_varint_get, rle_decode_event, rle_iter_events


class TestWorkedExamples(unittest.TestCase):
    """Test the worked examples from HANDOFF.md section 5.5."""

    def test_data_frame_example(self):
        """Test the DATA frame worked example."""
        # From HANDOFF.md section 5.5:
        # DATA frame, events: state 0x01 for 3 samples, then state 0x00 for 200 samples
        data_frame = bytes([
            0xA5, 0x5A, 0x01, 0x00, 0x00, 0x00, 0x11, 0x00,  # header: type=1 flags=0 seq=0 len=17
            0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,  # start_sample = 0
            0x00, 0x00, 0x00, 0x00,                           # lost_before = 0
            0x01, 0x03,                                       # state 0x01, run 3
            0x00, 0xC8, 0x01,                                 # state 0x00, run 200 (200 = 0xC8 0x01 varint)
            0x4E, 0xF3                                        # CRC16 = 0xF34E (little-endian)
        ])

        parser = FrameParser()
        received_frames = []
        parser.on('data', lambda f: received_frames.append(f))

        parser.feed(data_frame)

        self.assertEqual(parser.stats.frames, 1)
        self.assertEqual(parser.stats.crc_bad, 0)
        self.assertEqual(len(received_frames), 1)

        frame = received_frames[0]
        self.assertEqual(frame.seq, 0)
        self.assertEqual(frame.flags, 0)
        self.assertEqual(frame.start_sample, 0)
        self.assertEqual(frame.lost_before, 0)

        # Verify events decode correctly
        events = list(rle_iter_events(frame.events))
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0], (0x01, 3))
        self.assertEqual(events[1], (0x00, 200))

        # Position should be 203 (3 + 200)
        self.assertEqual(parser.get_timeline_position(), 203)

    def test_varint_encoding(self):
        """Test varint encoding/decoding."""
        # Test values from compress.cpp
        test_values = [0, 1, 127, 128, 16383, 16384, 2097151, 2097152, 0x0FFFFFFF]

        for v in test_values:
            # Encode manually (same as rle_varint_put)
            encoded = bytearray()
            val = v
            while val >= 0x80:
                encoded.append((val & 0x7F) | 0x80)
                val >>= 7
            encoded.append(val)

            # Decode
            decoded, consumed = rle_varint_get(bytes(encoded))
            self.assertEqual(decoded, v, f"Varint round-trip failed for {v}")
            self.assertEqual(consumed, len(encoded), f"Consumed bytes mismatch for {v}")

    def test_rle_event_decoding(self):
        """Test RLE event decoding."""
        # Event: state=0x01, run=3
        event1 = bytes([0x01, 0x03])
        state, run, consumed = rle_decode_event(event1)
        self.assertEqual(state, 0x01)
        self.assertEqual(run, 3)
        self.assertEqual(consumed, 2)

        # Event: state=0x00, run=200 (varint: C8 01)
        event2 = bytes([0x00, 0xC8, 0x01])
        state, run, consumed = rle_decode_event(event2)
        self.assertEqual(state, 0x00)
        self.assertEqual(run, 200)
        self.assertEqual(consumed, 3)

        # Multiple events
        data = bytes([0x01, 0x03, 0x00, 0xC8, 0x01])
        events = list(rle_iter_events(data))
        self.assertEqual(events, [(0x01, 3), (0x00, 200)])


class TestSampleStream(unittest.TestCase):
    """Test decoding the generated sample stream."""

    def test_sample_stream(self):
        """Test the stream.bin matches expected output from la_decode.py."""
        stream_path = os.path.join(os.path.dirname(__file__), "stream.bin")
        if not os.path.exists(stream_path):
            self.skipTest("stream.bin not found")

        parser = FrameParser()

        with open(stream_path, "rb") as f:
            data = f.read()
        parser.feed(data)

        # Expected from la_decode.py output:
        # decoded 140 frames, 3352000 samples, lost 152000, crc_bad 0
        self.assertEqual(parser.stats.frames, 140)
        self.assertEqual(parser.stats.crc_bad, 0)
        self.assertEqual(parser.stats.lost_total, 152000)
        self.assertEqual(parser.get_timeline_position(), 3352000)

        # Check specific gaps (from la_decode.py output)
        expected_gaps = [
            (296000, 8000),
            (608000, 16000),
            (928000, 24000),
            (1232000, 8000),
            (1544000, 16000),
            (1864000, 24000),
            (2168000, 8000),
            (2480000, 16000),
            (2800000, 24000),
            (3104000, 8000),
        ]

        # Total lost should be sum of all gaps
        total_gap = sum(g[1] for g in expected_gaps)
        self.assertEqual(total_gap, 152000)

        # Verify gap events were emitted
        self.assertEqual(len(parser.stats.gaps), len(expected_gaps))
        for i, (sample, count) in enumerate(expected_gaps):
            self.assertEqual(parser.stats.gaps[i][0], sample)
            self.assertEqual(parser.stats.gaps[i][1], count)


class TestEdgeCases(unittest.TestCase):
    """Test edge cases and error handling."""

    def test_resync_after_garbage(self):
        """Parser should resync after garbage bytes."""
        garbage = b"\x00\x11\x22\x33\x44"
        valid_frame = self._make_data_frame(seq=0, start=0, events=[(0x01, 5)], lost_before=0)

        data = garbage + valid_frame
        parser = FrameParser()
        parser.feed(data)

        self.assertEqual(parser.stats.frames, 1)
        self.assertEqual(parser.stats.crc_bad, 0)

    def test_crc_error_handling(self):
        """Parser should detect and skip CRC errors."""
        valid_frame = self._make_data_frame(seq=0, start=0, events=[(0x01, 5)], lost_before=0)

        # Corrupt the frame
        corrupted = bytearray(valid_frame)
        corrupted[10] ^= 0xFF  # Flip a bit in payload

        parser = FrameParser()
        parser.feed(bytes(corrupted))

        self.assertEqual(parser.stats.crc_bad, 1)
        self.assertEqual(parser.stats.frames, 0)

    def test_sequence_gap_detection(self):
        """Parser should detect missing sequence numbers."""
        # Frame with seq=0
        frame0 = self._make_data_frame(seq=0, start=0, events=[(0x01, 100)])
        # Frame with seq=2 (missing seq=1)
        frame2 = self._make_data_frame(seq=2, start=100, events=[(0x00, 100)])

        parser = FrameParser()
        seq_gaps = []
        parser.on('seq_gap', lambda gap, seq: seq_gaps.append((gap, seq)))

        parser.feed(frame0)
        parser.feed(frame2)

        self.assertEqual(len(seq_gaps), 1)
        self.assertEqual(seq_gaps[0], (1, 2))  # 1 frame missing before seq 2

    def test_board_restart(self):
        """Parser should handle board restart (sample index goes backwards)."""
        # First capture
        frame1 = self._make_data_frame(seq=0, start=10000, events=[(0x01, 5000)])
        # Board restarts, sample index goes back to 0
        frame2 = self._make_data_frame(seq=1, start=0, events=[(0x00, 100)])

        parser = FrameParser()
        restarts = []
        parser.on('restart', lambda old_pos, new_start, lost: restarts.append((old_pos, new_start, lost)))

        parser.feed(frame1)
        parser.feed(frame2)

        self.assertEqual(len(restarts), 1)
        self.assertEqual(restarts[0][0], 15000)  # old position
        self.assertEqual(restarts[0][1], 0)      # new start
        self.assertEqual(parser.get_timeline_position(), 15100)  # base + new pos

    def test_midstream_join(self):
        """Parser should baseline on first DATA frame without counting earlier as lost."""
        # Simulate joining mid-stream: first frame has start_sample=10000, lost_before=5000
        frame = self._make_data_frame(seq=0, start=10000, events=[(0x01, 100)], lost_before=5000)

        parser = FrameParser()
        attaches = []
        parser.on('attach', lambda start, lost: attaches.append((start, lost)))

        parser.feed(frame)

        self.assertEqual(len(attaches), 1)
        self.assertEqual(attaches[0], (10000, 5000))
        # Position: attach sets pos=5000, then set to start=10000, then +run(100) = 10100
        self.assertEqual(parser.get_timeline_position(), 10100)

    def _make_data_frame(self, seq: int, start: int, events: list, lost_before: int = 0, flags: int = 0):
        """Helper to create a valid DATA frame."""
        import binascii

        # Build payload
        payload = bytearray()
        payload += start.to_bytes(8, 'little')
        payload += lost_before.to_bytes(4, 'little')

        for state, run in events:
            payload.append(state)
            # Encode run as varint
            val = run
            while val >= 0x80:
                payload.append((val & 0x7F) | 0x80)
                val >>= 7
            payload.append(val)

        # Build frame
        frame = bytearray()
        frame += b'\xA5\x5A'  # sync
        frame.append(1)       # type = DATA
        frame.append(flags)   # flags
        frame += seq.to_bytes(2, 'little')
        frame += len(payload).to_bytes(2, 'little')
        frame += payload

        # CRC over bytes [2 .. end of payload]
        crc = binascii.crc_hqx(bytes(frame[2:]), 0xFFFF)
        frame += crc.to_bytes(2, 'little')

        return bytes(frame)


if __name__ == "__main__":
    unittest.main(verbosity=2)