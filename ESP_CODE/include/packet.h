#pragma once
#include <stdint.h>
#include <stddef.h>

// Packet framing for the encoded sample stream. No Arduino dependency: builds on a PC too.
//
// Frame (all multi-byte fields little-endian):
//   [0]    0xA5                 sync
//   [1]    0x5A                 sync
//   [2]    type                 PKT_DATA / PKT_STATS
//   [3]    flags                PKT_F_GAP = samples were lost right before this packet
//   [4..5] seq                  increments for every frame built, even one that is dropped
//   [6..7] len                  payload bytes
//   [8..]  payload
//   [..]   crc16                CRC-16/CCITT-FALSE over bytes [2 .. end of payload]
//
// DATA payload:  start_sample u64 | lost_before u32 | RLE events (whole events only)
//   start_sample = index (since start) of the first sample of the first event.
//   lost_before  = samples lost between the previous packet and this one. They are
//                  already counted in start_sample, so the PC can fill the hole.
// STATS payload: see pkt_send_stats().

#define PKT_SYNC0        0xA5
#define PKT_SYNC1        0x5A
#define PKT_HDR_BYTES    8
#define PKT_CRC_BYTES    2
#define PKT_DATA_HDR     12
#define PKT_EVENTS_MAX   1024                                   // RLE bytes per DATA packet
#define PKT_PAYLOAD_MAX  (PKT_DATA_HDR + PKT_EVENTS_MAX)
#define PKT_FRAME_MAX    (PKT_HDR_BYTES + PKT_PAYLOAD_MAX + PKT_CRC_BYTES)

#define PKT_DATA   1
#define PKT_STATS  2
#define PKT_F_GAP  0x01

// Hand one finished frame to the transport. Return false if it could not be queued
// (the packetizer then counts the packet as dropped and reports the loss later).
typedef bool (*pkt_send_fn)(const uint8_t *frame, size_t len, void *ctx);

struct Packetizer {
  pkt_send_fn send;
  void       *ctx;
  uint8_t     buf[PKT_FRAME_MAX];
  size_t      ev_len;           // RLE bytes currently in buf
  bool        open;             // a DATA packet is being filled
  uint64_t    pkt_start;        // start_sample of the open packet
  uint32_t    pkt_lost;         // lost_before of the open packet
  uint64_t    next_sample;      // index right after the last sample we know about
  uint32_t    pending_lost;     // lost samples to report in the next packet
  uint16_t    seq;
  uint32_t    drop_pkts;        // packets the transport refused
  uint32_t    drop_samples;     // samples inside those packets
};

void pkt_init(Packetizer *p, pkt_send_fn send, void *ctx);

// Append whole RLE events (exactly what RleEncoder.out holds). Opens/closes packets as needed.
void pkt_add_events(Packetizer *p, const uint8_t *ev, size_t len);

// Samples were lost (DMA overrun): close the open packet so the hole sits between packets.
void pkt_add_lost(Packetizer *p, uint32_t samples);

// Send the open packet now (time-based flush / end of capture). No-op if nothing is open.
void pkt_flush(Packetizer *p);

// Periodic health frame. Call from the same context as the other pkt_* functions.
struct PktStats {
  uint32_t sample_hz;
  uint8_t  channels;
  uint32_t produced, consumed, lost_buffers, max_proc_us;
};
void pkt_send_stats(Packetizer *p, const PktStats *s);

uint16_t pkt_crc16(const uint8_t *d, size_t n);
