#pragma once
#include <stdint.h>
#include <stddef.h>

// Run-length encoder for 8-bit samples.
//   event = [state byte][run length as varint]   ("state held for run samples")
//   varint = 7 bits per byte, little-endian, top bit set = more bytes follow
//
// The encoder keeps its pending run between calls, so runs continue correctly
// across DMA buffer boundaries. No Arduino dependency: it builds on a PC too.

#define RLE_OUT_CAP    256              // output buffer inside the encoder
#define RLE_MAX_EVENT  5                // 1 state byte + up to 4 varint bytes
#define RLE_MAX_RUN    0x0FFFFFFFu      // 28 bits -> 4-byte varint

struct RleEncoder {
  uint8_t  last;                        // state of the pending run
  uint32_t run;                         // samples in the pending run
  bool     have;                        // false right after init / gap
  uint8_t  out[RLE_OUT_CAP];            // finished events
  size_t   len;                         // bytes used in out[]
};

void   rle_init(RleEncoder *e);

// Encode up to n samples. Returns how many were consumed; it stops early when
// out[] is nearly full. Caller drains (e->out, e->len), calls rle_clear_out(),
// and calls again with the rest.
size_t rle_encode(RleEncoder *e, const uint8_t *in, size_t n);

// Emit the pending run now (heartbeat). Returns false if out[] has no room.
// The run restarts at 0 but the state is kept, so the PC just merges neighbours.
bool   rle_flush_pending(RleEncoder *e);

// Samples were lost: emit the pending run and break continuity. Returns false
// if out[] has no room (drain and call again).
bool   rle_gap(RleEncoder *e);

void   rle_clear_out(RleEncoder *e);

// helpers (also used by the PC side / debug prints)
size_t rle_varint_put(uint8_t *out, uint32_t v);
size_t rle_varint_get(const uint8_t *in, size_t len, uint32_t *v);   // 0 = incomplete
size_t rle_decode_event(const uint8_t *in, size_t len, uint8_t *value, uint32_t *run); // 0 = incomplete
