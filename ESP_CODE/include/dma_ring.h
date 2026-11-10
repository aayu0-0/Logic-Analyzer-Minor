#pragma once
#include <stdint.h>
#include <stddef.h>

// Called from the consumer task (core 1) once per DMA buffer.
//   samples     : SAMPLES_PER_BUF bytes, one byte per sample, oldest first
//   lost_before : samples that were lost immediately before this block (0 = none)
typedef void (*ring_sink_t)(const uint8_t *samples, size_t n, uint32_t lost_before);

struct RingStats {
  uint32_t produced;      // buffers completed by the DMA
  uint32_t consumed;      // buffers handled by the consumer (good or lost)
  uint32_t lost_buffers;  // buffers overwritten/torn before they were read
  uint32_t max_proc_us;   // slowest per-buffer processing time since last call
};

// Allocate buffers + circular descriptors, install the I2S interrupt, create
// the consumer task pinned to core 1. Returns false on failure.
bool dma_ring_init(ring_sink_t sink);

void dma_ring_start();   // start DMA + I2S capture (call once, after init)
void dma_ring_stop();

RingStats dma_ring_stats();   // resets max_proc_us
