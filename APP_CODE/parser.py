#!/usr/bin/env python3
"""
Frame parser for ESP32 Logic Analyzer TCP stream.
Port of la_decode.py logic into a reusable class.
"""

import binascii
import struct
import socket
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional, Callable, List, Tuple


# --- Constants ---
SYNC = b"\xA5\x5A"
PKT_DATA = 1
PKT_STATS = 2
PKT_F_GAP = 0x01

HDR_BYTES = 8
CRC_BYTES = 2
DATA_HDR = 12
PAYLOAD_MAX = DATA_HDR + 1024  # 1036
FRAME_MAX = HDR_BYTES + PAYLOAD_MAX + CRC_BYTES  # 1046


class FrameType(IntEnum):
    DATA = 1
    STATS = 2


@dataclass
class DataFrame:
    """Parsed DATA frame."""
    seq: int
    flags: int
    start_sample: int
    lost_before: int
    events: bytes  # Raw RLE events


@dataclass
class StatsFrame:
    """Parsed STATS frame."""
    seq: int
    version: int
    channels: int
    sample_hz: int
    produced: int
    consumed: int
    lost_buffers: int
    max_proc_us: int
    tx_drop_pkts: int
    tx_drop_samples: int


@dataclass
class DecoderStats:
    """Running decoder statistics."""
    frames: int = 0
    crc_bad: int = 0
    seq_gaps: int = 0
    lost_total: int = 0
    gaps: List[Tuple[int, int]] = field(default_factory=list)  # (sample_index, count)


class FrameParser:
    """
    Parses the ESP32 logic analyzer TCP stream.

    Handles:
    - Frame synchronization (A5 5A)
    - CRC16-CCITT-FALSE validation
    - Sequence number tracking
    - Gap detection and reporting
    - Board restart detection
    - Mid-stream join (baseline on first DATA frame)
    """

    def __init__(self, channels: int = 8, sample_hz: int = 5_000_000):
        self.buf = bytearray()
        self.channels = channels
        self.sample_hz = sample_hz
        self.expect_seq: Optional[int] = None
        self.stats = DecoderStats()
        self.pos = 0                 # Next sample index we expect (since last board start)
        self.base = 0                # Samples from earlier board runs
        self.state = None            # Last known state byte
        self.attached = False        # First DATA frame sets baseline
        self._callbacks = {
            'data': [],
            'stats': [],
            'gap': [],
            'restart': [],
            'attach': [],
            'seq_gap': [],
            'crc_error': [],
        }

    def on(self, event: str, callback: Callable):
        """Register a callback for an event."""
        if event in self._callbacks:
            self._callbacks[event].append(callback)

    def _emit(self, event: str, *args):
        for cb in self._callbacks.get(event, []):
            cb(*args)

    @staticmethod
    def crc16(data: bytes) -> int:
        """CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF, no reflection)."""
        return binascii.crc_hqx(data, 0xFFFF)

    def feed(self, data: bytes):
        """Feed raw bytes into the parser."""
        self.buf += data
        while True:
            i = self.buf.find(SYNC)
            if i < 0:
                # No sync found, keep at most 1 byte (could be start of sync)
                if len(self.buf) > 1:
                    self.buf = self.buf[-1:]
                return

            if i > 0:
                # Noise before sync - could be boot messages
                noise = bytes(self.buf[:i])
                self._emit('noise', noise)
                del self.buf[:i]

            # Need at least header
            if len(self.buf) < HDR_BYTES:
                return

            # Read payload length
            ln = struct.unpack_from("<H", self.buf, 6)[0]
            if ln > PAYLOAD_MAX:
                # Not a real header, skip one byte and resync
                del self.buf[:1]
                continue

            total = HDR_BYTES + ln + CRC_BYTES
            if len(self.buf) < total:
                # Incomplete frame, wait for more data
                return

            # Verify CRC over bytes [2 .. 8+ln)
            body = bytes(self.buf[2:HDR_BYTES + ln])
            expected_crc = struct.unpack_from("<H", self.buf, HDR_BYTES + ln)[0]
            actual_crc = self.crc16(body)

            if actual_crc != expected_crc:
                self.stats.crc_bad += 1
                self._emit('crc_error', self.stats.crc_bad)
                del self.buf[:1]  # Resync on next byte
                continue

            # Valid frame - parse it
            typ = self.buf[2]
            flags = self.buf[3]
            seq = struct.unpack_from("<H", self.buf, 4)[0]
            payload = bytes(self.buf[HDR_BYTES:HDR_BYTES + ln])

            # Remove processed frame from buffer
            del self.buf[:total]

            self._process_frame(typ, flags, seq, payload)

    def _process_frame(self, typ: int, flags: int, seq: int, payload: bytes):
        self.stats.frames += 1

        # Sequence tracking
        if self.expect_seq is not None and seq != self.expect_seq:
            gap = (seq - self.expect_seq) & 0xFFFF
            self.stats.seq_gaps += gap
            self._emit('seq_gap', gap, seq)
        self.expect_seq = (seq + 1) & 0xFFFF

        if typ == PKT_STATS:
            self._process_stats(seq, payload)
        elif typ == PKT_DATA:
            self._process_data(flags, seq, payload)

    def _process_stats(self, seq: int, payload: bytes):
        if len(payload) < 30:
            return
        (ver, ch, hz, prod, cons, lostb, maxus, dpk, dsm) = struct.unpack_from("<BBIIIIIII", payload)
        self.channels = ch
        self.sample_hz = hz

        frame = StatsFrame(
            seq=seq, version=ver, channels=ch, sample_hz=hz,
            produced=prod, consumed=cons, lost_buffers=lostb,
            max_proc_us=maxus, tx_drop_pkts=dpk, tx_drop_samples=dsm
        )
        self._emit('stats', frame)

    def _process_data(self, flags: int, seq: int, payload: bytes):
        if len(payload) < DATA_HDR:
            return

        start, lost = struct.unpack_from("<QI", payload)
        events = payload[DATA_HDR:]

        # First DATA frame: join mid-stream, don't count earlier samples as lost
        if not self.attached:
            self.attached = True
            self.pos = start - lost
            self._emit('attach', start, lost)

        # Board restart: sample index went backwards
        elif start < self.pos:
            self._emit('restart', self.pos, start, self.stats.lost_total)
            self.stats.lost_total = 0
            self.base += self.pos
            self.pos = 0
            self.state = None
            self.expect_seq = None

        # Check for unreported gap (hole the board didn't describe)
        if start != self.pos + lost:
            extra = start - self.pos - lost
            self.stats.lost_total += extra
            self.stats.gaps.append((start, extra))
            self._emit('gap', extra, start, "unreported")
            self.state = None

        # Apply reported lost_before
        if lost:
            self.stats.lost_total += lost
            self.stats.gaps.append((start, lost))
            self._emit('gap', lost, start, "reported")
            self.state = None

        self.pos = start

        # Advance position by RLE event runs (for accurate timeline position)
        offset = 0
        while offset < len(events):
            state, run, consumed = rle_decode_event(events, offset)
            if consumed == 0:
                break
            self.pos += run
            offset += consumed

        frame = DataFrame(
            seq=seq, flags=flags, start_sample=start,
            lost_before=lost, events=events
        )
        self._emit('data', frame)

    def get_timeline_position(self) -> int:
        """Get current absolute sample position (including base from restarts)."""
        return self.base + self.pos

    def reset(self):
        """Reset parser state for reconnection."""
        self.buf.clear()
        self.expect_seq = None
        self.attached = False
        self.pos = 0
        self.base = 0
        self.state = None
        self.stats = DecoderStats()


# --- RLE Decoding Utilities ---

def rle_varint_get(data: bytes, offset: int = 0) -> Tuple[int, int]:
    """
    Decode a varint from data at offset.
    Returns (value, bytes_consumed).
    """
    result = 0
    shift = 0
    for i in range(offset, min(offset + 5, len(data))):
        b = data[i]
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, i - offset + 1
        shift += 7
    return 0, 0  # Truncated


def rle_decode_event(data: bytes, offset: int = 0) -> Tuple[int, int, int]:
    """
    Decode one RLE event from data at offset.
    Returns (state_byte, run_length, bytes_consumed) or (0, 0, 0) on failure.
    """
    if offset + 1 >= len(data):
        return 0, 0, 0
    state = data[offset]
    run, consumed = rle_varint_get(data, offset + 1)
    if consumed == 0:
        return 0, 0, 0
    return state, run, 1 + consumed


def rle_iter_events(data: bytes):
    """Iterate over all RLE events in data, yielding (state, run)."""
    offset = 0
    while offset < len(data):
        state, run, consumed = rle_decode_event(data, offset)
        if consumed == 0:
            break
        yield state, run
        offset += consumed


# --- TCP Client ---

class TCPClient:
    """TCP client for connecting to ESP32 logic analyzer."""

    def __init__(self, host: str, port: int, parser: FrameParser):
        self.host = host
        self.port = port
        self.parser = parser
        self.socket: Optional[socket.socket] = None
        self.running = False
        self._reconnect_delay = 2.0

    def connect(self) -> bool:
        """Connect to the ESP32."""
        try:
            self.socket = socket.create_connection((self.host, self.port), timeout=5)
            self.socket.settimeout(1.0)
            print(f"[wifi] Connected to {self.host}:{self.port}")
            return True
        except OSError as e:
            print(f"[wifi] Connect failed ({e})")
            return False

    def run(self):
        """Run the client loop (blocks)."""
        self.running = True
        while self.running:
            if not self.connect():
                time.sleep(self._reconnect_delay)
                continue

            # Re-baseline on reconnect
            self.parser.reset()

            try:
                while self.running:
                    try:
                        data = self.socket.recv(65536)
                    except socket.timeout:
                        continue
                    except OSError:
                        data = b""

                    if not data:
                        print("[wifi] Connection closed; reconnecting")
                        break

                    self.parser.feed(data)

            finally:
                if self.socket:
                    self.socket.close()
                    self.socket = None

    def stop(self):
        self.running = False
        if self.socket:
            self.socket.close()


# --- Offline File Decoding ---

def decode_file(filepath: str, parser: FrameParser):
    """Decode a saved raw stream file."""
    with open(filepath, "rb") as f:
        data = f.read()
    parser.feed(data)
    return parser.stats


# --- Demo / Test ---

if __name__ == "__main__":
    import sys

    # Test with the worked examples from HANDOFF.md section 5.5
    print("=== Testing worked examples ===")

    # Example 1: DATA frame
    data_frame = bytes([
        0xA5, 0x5A, 0x01, 0x00, 0x00, 0x00, 0x11, 0x00,  # header
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,  # start_sample = 0
        0x00, 0x00, 0x00, 0x00,                           # lost_before = 0
        0x01, 0x03,                                       # state 0x01, run 3
        0x00, 0xC8, 0x01,                                 # state 0x00, run 200
        0x4E, 0xF3                                        # CRC16 = 0xF34E
    ])

    parser = FrameParser()
    parser.feed(data_frame)
    print(f"Frames: {parser.stats.frames}, CRC errors: {parser.stats.crc_bad}")
    print(f"Position: {parser.get_timeline_position()}")

    # Example 2: STATS frame
    stats_frame = bytes([
        0xA5, 0x5A, 0x02, 0x00, 0x01, 0x00, 0x1E, 0x00,
        0x01, 0x08, 0x40, 0x4B, 0x4C, 0x00,
        0x64, 0x00, 0x00, 0x00,
        0x63, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00,
        0x86, 0x02, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00,
        0x19, 0x27
    ])

    parser = FrameParser()
    parser.feed(stats_frame)
    print(f"Frames: {parser.stats.frames}, CRC errors: {parser.stats.crc_bad}")

    # Test with generated stream.bin
    if len(sys.argv) > 1:
        print(f"\n=== Decoding {sys.argv[1]} ===")
        parser = FrameParser()
        stats = decode_file(sys.argv[1], parser)
        print(f"Decoded {stats.frames} frames, {parser.get_timeline_position()} samples, "
              f"lost {stats.lost_total}, crc_bad {stats.crc_bad}")
    else:
        # Default test with stream.bin
        import os
        stream_path = os.path.join(os.path.dirname(__file__), "stream.bin")
        if os.path.exists(stream_path):
            print(f"\n=== Decoding {stream_path} ===")
            parser = FrameParser()
            stats = decode_file(stream_path, parser)
            print(f"Decoded {stats.frames} frames, {parser.get_timeline_position()} samples, "
                  f"lost {stats.lost_total}, crc_bad {stats.crc_bad}")