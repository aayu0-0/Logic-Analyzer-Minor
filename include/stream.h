#pragma once
#include <stdint.h>
#include <stddef.h>

// Glue: samples -> RLE -> packets -> transport.
bool stream_init();   // call before dma_ring_init
// ring_sink_t for dma_ring_init()
void stream_on_samples(const uint8_t *samples, size_t n, uint32_t lost_before);
