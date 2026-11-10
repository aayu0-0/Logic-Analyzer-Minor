#include <Arduino.h>
#include "config.h"
#include "compress.h"
#include "packet.h"
#include "transport.h"
#include "dma_ring.h"
#include "stream.h"

// Everything below runs in the ring consumer task (core 1), so no locking is needed.
static RleEncoder   s_enc;
static Packetizer   s_pkt;
static uint32_t     s_last_flush_us, s_last_stats_us;

bool stream_init() {
  rle_init(&s_enc);
  pkt_init(&s_pkt, transport_send, nullptr);
  s_last_flush_us = s_last_stats_us = micros();
  return transport_init();
}

// encoder output -> packetizer
static void drain() {
  if (s_enc.len) pkt_add_events(&s_pkt, s_enc.out, s_enc.len);
  rle_clear_out(&s_enc);
}

void stream_on_samples(const uint8_t *s, size_t n, uint32_t lost_before) {
  if (lost_before) {                              // DMA overrun: mark the hole in the stream
    while (!rle_gap(&s_enc)) drain();
    drain();
    pkt_add_lost(&s_pkt, lost_before);
  }

  size_t done = 0;
  while (done < n) {
    done += rle_encode(&s_enc, s + done, n - done);
    drain();
  }

  uint32_t now = micros();

  // Heartbeat: a line that never changes keeps its run pending forever. Push it out so
  // the PC sees data, and send whatever is waiting in the open packet.
  if ((uint32_t)(now - s_last_flush_us) >= FLUSH_MS * 1000UL) {
    while (!rle_flush_pending(&s_enc)) drain();
    drain();
    pkt_flush(&s_pkt);
    s_last_flush_us = now;
  }

  if ((uint32_t)(now - s_last_stats_us) >= STATS_MS * 1000UL) {
    RingStats rs = dma_ring_stats();
    PktStats  ps = { SAMPLE_HZ, NUM_CHANNELS, rs.produced, rs.consumed, rs.lost_buffers, rs.max_proc_us };
    pkt_send_stats(&s_pkt, &ps);
    s_last_stats_us = now;
  }
}
