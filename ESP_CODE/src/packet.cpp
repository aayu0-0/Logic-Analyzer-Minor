#include "packet.h"
#include "compress.h"
#include <string.h>

uint16_t pkt_crc16(const uint8_t *d, size_t n) {
  uint16_t crc = 0xFFFF;
  while (n--) {
    crc ^= (uint16_t)(*d++) << 8;
    for (int i = 0; i < 8; i++)
      crc = (crc & 0x8000) ? (uint16_t)((crc << 1) ^ 0x1021) : (uint16_t)(crc << 1);
  }
  return crc;
}

static inline void put16(uint8_t *o, uint16_t v) { o[0] = v; o[1] = v >> 8; }
static inline void put32(uint8_t *o, uint32_t v) { for (int i = 0; i < 4; i++) o[i] = v >> (8 * i); }
static inline void put64(uint8_t *o, uint64_t v) { for (int i = 0; i < 8; i++) o[i] = (uint8_t)(v >> (8 * i)); }

void pkt_init(Packetizer *p, pkt_send_fn send, void *ctx) {
  memset(p, 0, sizeof *p);
  p->send = send;
  p->ctx  = ctx;
}

// Fill in header + CRC around a payload that already sits at buf[PKT_HDR_BYTES..]
static size_t finish_frame(Packetizer *p, uint8_t type, uint8_t flags, size_t payload_len) {
  uint8_t *f = p->buf;
  f[0] = PKT_SYNC0; f[1] = PKT_SYNC1;
  f[2] = type;      f[3] = flags;
  put16(f + 4, p->seq++);
  put16(f + 6, (uint16_t)payload_len);
  size_t end = PKT_HDR_BYTES + payload_len;
  put16(f + end, pkt_crc16(f + 2, end - 2));
  return end + PKT_CRC_BYTES;
}

static void close_packet(Packetizer *p, uint64_t samples_in_pkt) {
  if (!p->open) return;
  uint8_t *pl = p->buf + PKT_HDR_BYTES;
  put64(pl, p->pkt_start);
  put32(pl + 8, p->pkt_lost);
  size_t n = finish_frame(p, PKT_DATA, p->pkt_lost ? PKT_F_GAP : 0, PKT_DATA_HDR + p->ev_len);

  if (!p->send(p->buf, n, p->ctx)) {
    // Transport full: this block of samples is gone. Fold it (and any loss it carried)
    // into the loss we report in the next packet; start_sample stays continuous.
    p->drop_pkts++;
    p->drop_samples += (uint32_t)samples_in_pkt;
    p->pending_lost += p->pkt_lost + (uint32_t)samples_in_pkt;
  }
  p->open   = false;
  p->ev_len = 0;
}

// samples covered by the open packet = next_sample - (pkt_start), kept via next_sample
void pkt_add_events(Packetizer *p, const uint8_t *ev, size_t len) {
  size_t pos = 0;
  while (pos < len) {
    uint8_t  v;
    uint32_t run;
    size_t used = rle_decode_event(ev + pos, len - pos, &v, &run);
    if (!used) break;                                   // partial event: should never happen

    if (p->open && p->ev_len + used > PKT_EVENTS_MAX)
      close_packet(p, p->next_sample - p->pkt_start);

    if (!p->open) {
      p->open      = true;
      p->pkt_start = p->next_sample;
      p->pkt_lost  = p->pending_lost;
      p->pending_lost = 0;
      p->ev_len    = 0;
    }
    memcpy(p->buf + PKT_HDR_BYTES + PKT_DATA_HDR + p->ev_len, ev + pos, used);
    p->ev_len     += used;
    p->next_sample += run;
    pos += used;
  }
}

void pkt_add_lost(Packetizer *p, uint32_t samples) {
  if (!samples) return;
  pkt_flush(p);
  p->pending_lost += samples;
  p->next_sample  += samples;
}

void pkt_flush(Packetizer *p) {
  if (p->open) close_packet(p, p->next_sample - p->pkt_start);
}

// STATS payload: ver u8 | channels u8 | sample_hz u32 | produced u32 | consumed u32 |
//                lost_buffers u32 | max_proc_us u32 | tx_drop_pkts u32 | tx_drop_samples u32
void pkt_send_stats(Packetizer *p, const PktStats *s) {
  // Building the stats frame reuses buf, so the open DATA packet must go out first.
  pkt_flush(p);
  uint8_t *pl = p->buf + PKT_HDR_BYTES;
  pl[0] = 1;
  pl[1] = s->channels;
  put32(pl + 2,  s->sample_hz);
  put32(pl + 6,  s->produced);
  put32(pl + 10, s->consumed);
  put32(pl + 14, s->lost_buffers);
  put32(pl + 18, s->max_proc_us);
  put32(pl + 22, p->drop_pkts);
  put32(pl + 26, p->drop_samples);
  size_t n = finish_frame(p, PKT_STATS, 0, 30);
  p->send(p->buf, n, p->ctx);                           // stats loss is harmless
}
