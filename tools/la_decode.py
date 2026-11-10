#!/usr/bin/env python3
"""PC side of the ESP32 logic analyzer stream (frame format: include/packet.h).

  python la_decode.py COM5                 # live from the board (needs: pip install pyserial)
  python la_decode.py /dev/ttyUSB0 --vcd out.vcd
  python la_decode.py capture.bin          # decode a saved raw dump

Prints one line per STATS frame, reports every gap, optionally writes a VCD file
(open in PulseView / GTKWave). Samples that were lost are written as 'x'.
"""
import argparse, binascii, struct, sys, os

SYNC = b"\xA5\x5A"
PKT_DATA, PKT_STATS = 1, 2
HDR, CRC, DATA_HDR = 8, 2, 12
PAYLOAD_MAX = DATA_HDR + 1024


def crc16(b):                       # CRC-16/CCITT-FALSE, same as the firmware
    return binascii.crc_hqx(b, 0xFFFF)


class Decoder:
    def __init__(self, vcd=None, channels=8, sample_hz=5_000_000):
        self.buf = bytearray()
        self.channels, self.hz = channels, sample_hz
        self.expect_seq = None
        self.crc_bad = self.seq_gaps = self.frames = 0
        self.pos = 0                 # next sample index we expect
        self.state = None            # last known state byte
        self.lost_total = 0
        self.vcd = VcdWriter(vcd, channels, sample_hz) if vcd else None

    def feed(self, data):
        self.buf += data
        while True:
            i = self.buf.find(SYNC)
            if i < 0:
                if len(self.buf) > 1:
                    self._noise(bytes(self.buf[:-1])); del self.buf[:-1]   # keep a possible half sync
                return
            if i:
                self._noise(bytes(self.buf[:i])); del self.buf[:i]
            if len(self.buf) < HDR:
                return
            ln = struct.unpack_from("<H", self.buf, 6)[0]
            if ln > PAYLOAD_MAX:                       # not a real header
                del self.buf[:1]; continue
            total = HDR + ln + CRC
            if len(self.buf) < total:
                return
            body = bytes(self.buf[2:HDR + ln])
            if crc16(body) != struct.unpack_from("<H", self.buf, HDR + ln)[0]:
                self.crc_bad += 1
                del self.buf[:1]; continue              # resync on the next sync pattern
            typ, flags, seq = self.buf[2], self.buf[3], struct.unpack_from("<H", self.buf, 4)[0]
            payload = bytes(self.buf[HDR:HDR + ln])
            del self.buf[:total]
            self._frame(typ, flags, seq, payload)

    def _noise(self, b):             # boot messages etc.
        txt = b.decode("ascii", "ignore").strip()
        if txt and txt.isprintable():
            print("[board]", txt)

    def _frame(self, typ, flags, seq, pl):
        self.frames += 1
        if self.expect_seq is not None and seq != self.expect_seq:
            self.seq_gaps += (seq - self.expect_seq) & 0xFFFF
            print(f"[link] {(seq - self.expect_seq) & 0xFFFF} frame(s) missing before seq {seq}")
        self.expect_seq = (seq + 1) & 0xFFFF

        if typ == PKT_STATS:
            (ver, ch, hz, prod, cons, lostb, maxus, dpk, dsm) = struct.unpack_from("<BBIIIIIII", pl)
            self.channels, self.hz = ch, hz
            print(f"prod={prod} cons={cons} lost_buf={lostb} maxproc={maxus}us "
                  f"tx_dropped={dpk} pkts/{dsm} samples | rx: frames={self.frames} "
                  f"crc_bad={self.crc_bad} lost_total={self.lost_total}")
        elif typ == PKT_DATA:
            start, lost = struct.unpack_from("<QI", pl)
            ev = pl[DATA_HDR:]
            if start != self.pos + lost:               # hole the board did not describe (e.g. CRC loss)
                extra = start - self.pos - lost
                print(f"[gap] {extra} samples missing before sample {start} (frame lost on the link)")
                lost += extra
            if lost:
                self.lost_total += lost
                print(f"[gap] {lost} samples lost before sample {start}")
                if self.vcd: self.vcd.unknown(start - lost)
                self.state = None
            self.pos = start
            q = 0
            while q < len(ev):
                v = ev[q]; run = 0; shift = 0; q += 1
                while True:
                    b = ev[q]; q += 1
                    run |= (b & 0x7F) << shift; shift += 7
                    if not b & 0x80: break
                if self.vcd and v != self.state: self.vcd.change(self.pos, v)
                self.state = v
                self.pos += run


class VcdWriter:
    def __init__(self, path, ch, hz):
        self.f = open(path, "w"); self.ch = ch; self.ns = 1e9 / hz; self.last = None
        self.f.write("$timescale 1ns $end\n$scope module la $end\n")
        for i in range(ch): self.f.write(f"$var wire 1 {chr(33 + i)} CH{i} $end\n")
        self.f.write("$upscope $end\n$enddefinitions $end\n")

    def _t(self, s): self.f.write(f"#{int(s * self.ns)}\n")

    def change(self, s, v):
        self._t(s)
        for i in range(self.ch):
            if self.last is None or self.last == "x" or ((v ^ self.last) >> i) & 1:
                self.f.write(f"{(v >> i) & 1}{chr(33 + i)}\n")
        self.last = v

    def unknown(self, s):
        self._t(s)
        for i in range(self.ch): self.f.write(f"x{chr(33 + i)}\n")
        self.last = "x"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source", help="serial port or raw dump file")
    ap.add_argument("--baud", type=int, default=921600)
    ap.add_argument("--vcd", help="write a VCD waveform file")
    ap.add_argument("--save", help="also save the raw stream to this file")
    a = ap.parse_args()

    dec = Decoder(a.vcd)
    raw = open(a.save, "wb") if a.save else None
    try:
        if os.path.isfile(a.source):
            data = open(a.source, "rb").read()
            dec.feed(data)
            print(f"decoded {dec.frames} frames, {dec.pos} samples, lost {dec.lost_total}, crc_bad {dec.crc_bad}")
        else:
            import serial
            with serial.Serial(a.source, a.baud, timeout=0.2) as s:
                while True:
                    d = s.read(4096)
                    if d:
                        if raw: raw.write(d)
                        dec.feed(d)
    except KeyboardInterrupt:
        pass
    finally:
        if dec.vcd: dec.vcd.f.close()
        if raw: raw.close()


if __name__ == "__main__":
    main()
