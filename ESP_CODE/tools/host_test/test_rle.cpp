// Host-side test for compress.cpp (no ESP32 needed).
//   g++ -std=c++17 -O2 -I../../include test_rle.cpp ../../src/compress.cpp -o test_rle && ./test_rle
#include <cstdio>
#include <cstdlib>
#include <vector>
#include <random>
#include "compress.h"

static int failures = 0;
#define CHECK(cond, msg) do { if (!(cond)) { printf("FAIL: %s\n", msg); failures++; } } while (0)

static void drain(RleEncoder &e, std::vector<uint8_t> &stream) {
  stream.insert(stream.end(), e.out, e.out + e.len);
  rle_clear_out(&e);
}

// feed data in random-sized chunks, drain like the firmware does
static void encode_all(RleEncoder &e, const std::vector<uint8_t> &in,
                       std::vector<uint8_t> &stream, std::mt19937 &rng, size_t max_chunk) {
  size_t pos = 0;
  while (pos < in.size()) {
    size_t chunk = 1 + rng() % max_chunk;
    if (chunk > in.size() - pos) chunk = in.size() - pos;
    size_t done = 0;
    while (done < chunk) {
      done += rle_encode(&e, in.data() + pos + done, chunk - done);
      drain(e, stream);
    }
    pos += chunk;
  }
}

static std::vector<uint8_t> decode_all(const std::vector<uint8_t> &s, uint64_t *total_run = nullptr) {
  std::vector<uint8_t> out;
  uint64_t total = 0;
  size_t pos = 0;
  while (pos < s.size()) {
    uint8_t v; uint32_t run;
    size_t used = rle_decode_event(s.data() + pos, s.size() - pos, &v, &run);
    if (!used) { printf("FAIL: truncated event stream\n"); failures++; break; }
    if (run == 0) { printf("FAIL: zero-length event\n"); failures++; }
    pos += used;
    total += run;
    if (!total_run) out.insert(out.end(), run, v);
  }
  if (total_run) *total_run = total;
  return out;
}

int main() {
  std::mt19937 rng(12345);

  // 1. varint round trip
  for (uint32_t v : {0u, 1u, 127u, 128u, 16383u, 16384u, 2097151u, 2097152u, RLE_MAX_RUN}) {
    uint8_t b[8]; uint32_t back = 0;
    size_t n = rle_varint_put(b, v);
    CHECK(rle_varint_get(b, n, &back) == n && back == v, "varint round trip");
  }

  // 2. random signals, several densities, several chunk sizes
  for (int density : {1, 5, 50, 1000, 100000}) {
    for (size_t max_chunk : {1u, 7u, 2000u, 5000u}) {
      std::vector<uint8_t> in(200000);
      uint8_t cur = rng();
      for (auto &x : in) { if (rng() % density == 0) cur = rng(); x = cur; }
      RleEncoder e; rle_init(&e);
      std::vector<uint8_t> stream;
      encode_all(e, in, stream, rng, max_chunk);
      while (!rle_flush_pending(&e)) drain(e, stream);
      drain(e, stream);
      CHECK(decode_all(stream) == in, "random round trip");
    }
  }

  // 3. square wave 5 on / 5 off: every event should be run 5
  {
    std::vector<uint8_t> in;
    for (int i = 0; i < 10000; i++) in.push_back((i / 5) & 1);
    RleEncoder e; rle_init(&e);
    std::vector<uint8_t> stream;
    encode_all(e, in, stream, rng, 2000);
    while (!rle_flush_pending(&e)) drain(e, stream);
    drain(e, stream);
    CHECK(decode_all(stream) == in, "square wave round trip");
    CHECK(stream.size() == 2 * 2000, "square wave compresses to 2 bytes/event");
  }

  // 4. long idle run (crosses many chunk boundaries)
  {
    std::vector<uint8_t> in(3000000, 0x55);
    RleEncoder e; rle_init(&e);
    std::vector<uint8_t> stream;
    encode_all(e, in, stream, rng, 4000);
    while (!rle_flush_pending(&e)) drain(e, stream);
    drain(e, stream);
    uint64_t total = 0; decode_all(stream, &total);
    CHECK(total == in.size(), "long run total");
    CHECK(stream.size() <= 5, "long run is one tiny event");
  }

  // 5. run-length limit: forced split, no sample lost
  {
    RleEncoder e; rle_init(&e);
    e.have = true; e.last = 7; e.run = RLE_MAX_RUN - 1;
    std::vector<uint8_t> in(3, 7), stream;
    encode_all(e, in, stream, rng, 3);
    while (!rle_flush_pending(&e)) drain(e, stream);
    drain(e, stream);
    uint64_t total = 0; decode_all(stream, &total);
    CHECK(total == (uint64_t)RLE_MAX_RUN - 1 + 3, "run limit split keeps every sample");
  }

  // 6. worst case: toggling every sample -> exactly 2 bytes per sample
  {
    std::vector<uint8_t> in(10000);
    for (size_t i = 0; i < in.size(); i++) in[i] = i & 1;
    RleEncoder e; rle_init(&e);
    std::vector<uint8_t> stream;
    encode_all(e, in, stream, rng, 2000);
    while (!rle_flush_pending(&e)) drain(e, stream);
    drain(e, stream);
    CHECK(decode_all(stream) == in, "worst case round trip");
    CHECK(stream.size() == 2 * in.size(), "worst case is 2x raw");
  }

  // 7. gap: samples before and after are kept, nothing invented in between
  {
    std::vector<uint8_t> a(1234, 3), b(999, 9), stream;
    RleEncoder e; rle_init(&e);
    encode_all(e, a, stream, rng, 500);
    while (!rle_gap(&e)) drain(e, stream);
    drain(e, stream);
    encode_all(e, b, stream, rng, 500);
    while (!rle_flush_pending(&e)) drain(e, stream);
    drain(e, stream);
    std::vector<uint8_t> want = a; want.insert(want.end(), b.begin(), b.end());
    CHECK(decode_all(stream) == want, "gap keeps both sides intact");
  }

  // 8. heartbeat flush mid-stream must not change the decoded result
  {
    std::vector<uint8_t> in(50000);
    uint8_t cur = 0;
    for (auto &x : in) { if (rng() % 300 == 0) cur = rng(); x = cur; }
    RleEncoder e; rle_init(&e);
    std::vector<uint8_t> stream;
    for (size_t pos = 0; pos < in.size(); pos += 1000) {
      size_t done = 0, n = std::min<size_t>(1000, in.size() - pos);
      while (done < n) { done += rle_encode(&e, in.data() + pos + done, n - done); drain(e, stream); }
      while (!rle_flush_pending(&e)) drain(e, stream);     // heartbeat every 1000 samples
      drain(e, stream);
    }
    CHECK(decode_all(stream) == in, "heartbeat flush round trip");
  }

  printf(failures ? "\n%d FAILURES\n" : "\nALL TESTS PASSED\n", failures);
  return failures != 0;
}
