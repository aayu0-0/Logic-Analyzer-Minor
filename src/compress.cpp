#include "compress.h"

void rle_init(RleEncoder *e) {
  e->last = 0;
  e->run  = 0;
  e->have = false;
  e->len  = 0;
}

void rle_clear_out(RleEncoder *e) { e->len = 0; }

size_t rle_varint_put(uint8_t *out, uint32_t v) {
  size_t n = 0;
  while (v >= 0x80) {
    out[n++] = (uint8_t)((v & 0x7F) | 0x80);
    v >>= 7;
  }
  out[n++] = (uint8_t)v;
  return n;
}

size_t rle_varint_get(const uint8_t *in, size_t len, uint32_t *v) {
  uint32_t r = 0;
  int shift = 0;
  for (size_t i = 0; i < len && i < 5; i++) {
    r |= (uint32_t)(in[i] & 0x7F) << shift;
    if (!(in[i] & 0x80)) { *v = r; return i + 1; }
    shift += 7;
  }
  return 0;
}

size_t rle_decode_event(const uint8_t *in, size_t len, uint8_t *value, uint32_t *run) {
  if (len < 2) return 0;
  size_t n = rle_varint_get(in + 1, len - 1, run);
  if (!n) return 0;
  *value = in[0];
  return 1 + n;
}

static inline void emit(RleEncoder *e) {
  e->out[e->len++] = e->last;
  e->len += rle_varint_put(e->out + e->len, e->run);
}

static inline bool has_room(const RleEncoder *e) {
  return e->len + RLE_MAX_EVENT <= RLE_OUT_CAP;
}

size_t rle_encode(RleEncoder *e, const uint8_t *in, size_t n) {
  size_t i = 0;
  for (; i < n; i++) {
    uint8_t s = in[i];

    if (!e->have) {                                   // first sample after init/gap
      e->last = s; e->run = 1; e->have = true;
      continue;
    }
    if (s == e->last && e->run < RLE_MAX_RUN) {       // same state: extend the run
      e->run++;
      continue;
    }
    // state changed (or run hit its limit): close the current run
    if (e->run > 0) {
      if (!has_room(e)) return i;                     // sample i NOT consumed
      emit(e);
    }
    e->last = s;
    e->run  = 1;
  }
  return i;
}

bool rle_flush_pending(RleEncoder *e) {
  if (e->have && e->run > 0) {
    if (!has_room(e)) return false;
    emit(e);
    e->run = 0;
  }
  return true;
}

bool rle_gap(RleEncoder *e) {
  if (!rle_flush_pending(e)) return false;
  e->have = false;
  e->run  = 0;
  return true;
}
