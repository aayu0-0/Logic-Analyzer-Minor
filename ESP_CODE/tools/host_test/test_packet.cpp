// Host-side test for packet.cpp + compress.cpp (no ESP32 needed).
//   g++ -std=c++17 -O2 -I../../include test_packet.cpp ../../src/packet.cpp ../../src/compress.cpp -o test_packet && ./test_packet
// Also writes stream.bin so the PC tool can be checked:  python3 ../la_decode.py stream.bin
#include <cstdio>
#include <cstring>
#include <vector>
#include <random>
#include "compress.h"
#include "packet.h"

static int failures = 0;
#define CHECK(cond, msg) do { if (!(cond)) { printf("FAIL: %s\n", msg); failures++; } } while (0)

struct Link { std::vector<uint8_t> bytes; std::mt19937 rng{7}; int refuse_pct = 0; };
static bool link_send(const uint8_t *f, size_t n, void *ctx) {
  Link *l = (Link *)ctx;
  if ((int)(l->rng() % 100) < l->refuse_pct) return false;
  l->bytes.insert(l->bytes.end(), f, f + n);
  return true;
}

// ---- minimal receiver: rebuild the sample timeline, 0x100 = unknown ----
struct Rx { std::vector<uint16_t> tl; uint64_t crc_bad = 0, frames = 0; };
static uint64_t rd(const uint8_t *p, int n) { uint64_t v = 0; for (int i = 0; i < n; i++) v |= (uint64_t)p[i] << (8 * i); return v; }

static void decode(const std::vector<uint8_t> &b, Rx &rx, size_t total) {
  rx.tl.assign(total, 0x100);
  size_t i = 0;
  while (i + PKT_HDR_BYTES + PKT_CRC_BYTES <= b.size()) {
    if (b[i] != PKT_SYNC0 || b[i + 1] != PKT_SYNC1) { i++; continue; }
    size_t len = rd(&b[i + 6], 2), end = PKT_HDR_BYTES + len;
    if (i + end + PKT_CRC_BYTES > b.size()) break;
    if (pkt_crc16(&b[i + 2], end - 2) != rd(&b[i + end], 2)) { rx.crc_bad++; i++; continue; }
    rx.frames++;
    if (b[i + 2] == PKT_DATA) {
      const uint8_t *pl = &b[i + PKT_HDR_BYTES];
      uint64_t pos = rd(pl, 8);
      size_t q = PKT_DATA_HDR;
      while (q < len) {
        uint8_t v; uint32_t run;
        size_t u = rle_decode_event(pl + q, len - q, &v, &run);
        if (!u) { printf("FAIL: bad event in packet\n"); failures++; break; }
        for (uint32_t k = 0; k < run && pos < total; k++) rx.tl[pos++] = v;
        q += u;
      }
    }
    i += end + PKT_CRC_BYTES;
  }
}

// signal with long idle stretches and bursts, like a real bus
static std::vector<uint8_t> make_signal(size_t n, std::mt19937 &rng) {
  std::vector<uint8_t> s(n);
  uint8_t cur = 0; size_t i = 0;
  while (i < n) {
    size_t run = (rng() % 4 == 0) ? 1 + rng() % 3 : 1 + rng() % 3000;
    if (rng() % 50 == 0) run += 200000;                     // very long idle
    cur = (uint8_t)rng();
    for (size_t k = 0; k < run && i < n; k++) s[i++] = cur;
  }
  return s;
}

static void run_case(const char *name, int refuse_pct, bool inject_dma_loss, bool write_file) {
  std::mt19937 rng(1234);
  const size_t BLK = 8000;                                   // = SAMPLES_PER_BUF
  const size_t BLOCKS = 400;
  std::vector<uint8_t> sig = make_signal(BLK * BLOCKS, rng);

  Link link; link.refuse_pct = refuse_pct;
  Packetizer pk; pkt_init(&pk, link_send, &link);
  RleEncoder enc; rle_init(&enc);

  auto drain = [&] { if (enc.len) pkt_add_events(&pk, enc.out, enc.len); rle_clear_out(&enc); };
  std::vector<bool> dma_lost(sig.size(), false);

  size_t lost_total = 0;
  for (size_t b = 0; b < BLOCKS; b++) {
    uint32_t lost = 0;
    if (inject_dma_loss && b % 37 == 36) lost = BLK * (1 + b % 3);   // pretend DMA overran
    size_t src = b * BLK;
    if (lost) {
      while (!rle_gap(&enc)) drain();
      drain();
      pkt_add_lost(&pk, lost);
      lost_total += lost;
      for (size_t k = 0; k < lost && src - lost + k < sig.size(); k++) {}
    }
    size_t done = 0;
    while (done < BLK) { done += rle_encode(&enc, &sig[src + done], BLK - done); drain(); }
    if (b % 3 == 2) {                                        // heartbeat
      while (!rle_flush_pending(&enc)) drain();
      drain();
      pkt_flush(&pk);
    }
  }
  while (!rle_flush_pending(&enc)) drain();
  drain();
  pkt_flush(&pk);

  // total timeline length = samples fed + samples declared lost
  size_t total = sig.size() + lost_total;
  Rx rx; decode(link.bytes, rx, total);

  // rebuild expected timeline (lost blocks are unknown, dropped packets become unknown)
  // Check 1: packetizer's own bookkeeping is consistent
  CHECK(pk.next_sample == total, "next_sample must equal fed + lost");
  CHECK(rx.crc_bad == 0, "no CRC errors on a clean link");

  // Check 2: every sample the receiver HAS must match the source, every hole must be accounted for
  size_t known = 0, unknown = 0, wrong = 0, src = 0, t = 0;
  for (size_t b = 0; b < BLOCKS; b++) {
    if (inject_dma_loss && b % 37 == 36) {
      size_t lost = BLK * (1 + b % 3);
      for (size_t k = 0; k < lost; k++, t++) if (rx.tl[t] != 0x100) wrong++;
    }
    for (size_t k = 0; k < BLK; k++, t++, src++) {
      if (rx.tl[t] == 0x100) unknown++;
      else { known++; if (rx.tl[t] != sig[src]) wrong++; }
    }
  }
  CHECK(wrong == 0, "received samples must match the source exactly");
  size_t holes = total - known;                              // everything not received
  size_t reported = lost_total + pk.drop_samples;
  CHECK(holes == reported, "unknown samples must equal DMA loss + dropped-packet samples");
  if (refuse_pct == 0) CHECK(pk.drop_pkts == 0 && unknown == 0, "nothing may be lost on a clean link");
  if (refuse_pct > 0)  CHECK(pk.drop_pkts > 0, "test should have exercised the drop path");

  printf("%-26s frames=%llu bytes=%zu raw=%zu known=%zu holes=%zu dropped_pkts=%u\n",
         name, (unsigned long long)rx.frames, link.bytes.size(), sig.size(), known, holes, pk.drop_pkts);
  if (write_file) { FILE *f = fopen("stream.bin", "wb"); fwrite(link.bytes.data(), 1, link.bytes.size(), f); fclose(f); }
}

int main() {
  // CRC-16/CCITT-FALSE check value
  CHECK(pkt_crc16((const uint8_t *)"123456789", 9) == 0x29B1, "crc16 check value");

  run_case("clean link",            0,  false, false);
  run_case("DMA overruns",          0,  true,  true);
  run_case("full queue (30% drop)", 30, false, false);
  run_case("both",                  30, true,  false);

  // corrupt / garbage resilience: flip a byte, prepend noise
  {
    Link link; Packetizer pk; pkt_init(&pk, link_send, &link);
    uint8_t ev[2 * 600]; size_t n = 0;
    for (int i = 0; i < 600; i++) { ev[n++] = (uint8_t)i; n += rle_varint_put(ev + n, 5); }
    pkt_add_events(&pk, ev, n); pkt_flush(&pk);
    std::vector<uint8_t> bad = {0x11, 0xA5, 0x00, 0xA5, 0x5A};   // noise incl. fake sync
    bad.insert(bad.end(), link.bytes.begin(), link.bytes.end());
    bad[bad.size() / 2] ^= 0x40;
    Rx rx; decode(bad, rx, 3000);
    CHECK(rx.crc_bad >= 1, "corrupted frame must fail CRC");
  }

  printf(failures ? "\n%d FAILURE(S)\n" : "\nall packet tests passed\n", failures);
  return failures ? 1 : 0;
}
