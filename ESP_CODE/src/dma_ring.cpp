#include <Arduino.h>
#include "rom/lldesc.h"
#include "soc/i2s_struct.h"
#include "soc/periph_defs.h"
#include "esp_intr_alloc.h"

#include "config.h"
#include "i2s_comm.h"
#include "dma_ring.h"

static lldesc_t   s_desc[NBUF];
static uint32_t  *s_buf[NBUF];
static intr_handle_t s_intr;
static TaskHandle_t  s_task;
static ring_sink_t   s_sink;

static volatile uint32_t s_produced;       // written only by the ISR
static volatile uint32_t s_consumed;       // written only by the consumer
static volatile uint32_t s_lost_buffers;
static volatile uint32_t s_max_us;

// ---------------------------------------------------------------------------
// ISR: one "in_done" per filled descriptor. Count it and wake the consumer.
// ---------------------------------------------------------------------------
static void IRAM_ATTR i2s_isr(void *) {
  if (I2S0.int_raw.in_done) {
    s_produced = s_produced + 1;
    BaseType_t woken = pdFALSE;
    if (s_task) vTaskNotifyGiveFromISR(s_task, &woken);
    I2S0.int_clr.val = I2S0.int_raw.val;
    if (woken) portYIELD_FROM_ISR();
    return;
  }
  I2S0.int_clr.val = I2S0.int_raw.val;
}

// ---------------------------------------------------------------------------
// Copy one DMA buffer into a private 8-bit scratch array.
// Each 32-bit word holds two 16-bit samples; the FIRST sample in time is the
// upper half (seen in the earlier test). Keep only the low 8 channels.
// ---------------------------------------------------------------------------
static void extract_8bit(uint32_t k, uint8_t *out) {
  const volatile uint32_t *src = s_buf[k % NBUF];
  for (int i = 0; i < WORDS_PER_BUF; i++) {
    uint32_t w = src[i];
    out[2 * i]     = (w >> 16) & 0xFF;
    out[2 * i + 1] = w & 0xFF;
  }
}

// ---------------------------------------------------------------------------
// Consumer task.
// Rule: buffer k is only valid while (produced - k) < NBUF. Once produced
// reaches k + NBUF the DMA is overwriting it. So: copy first, then re-check.
// ---------------------------------------------------------------------------
static void consumer_task(void *) {
  static uint8_t scratch[SAMPLES_PER_BUF];
  uint32_t next = 0;
  uint32_t lost_pending = 0;     // samples lost since the last delivered block

  for (;;) {
    ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(100));

    for (;;) {
      uint32_t p = s_produced;
      if (next == p) break;                                  // nothing new

      // already lapped? skip buffers that can no longer be trusted
      uint32_t oldest_valid = (p >= NBUF - 1) ? p - (NBUF - 1) : 0;
      if (next < oldest_valid) {
        uint32_t skipped = oldest_valid - next;
        s_lost_buffers = s_lost_buffers + skipped;
        lost_pending += skipped * SAMPLES_PER_BUF;
        next = oldest_valid;
      }

      uint32_t t0 = micros();
      extract_8bit(next, scratch);

      if ((uint32_t)(s_produced - next) >= NBUF) {           // torn during copy
        s_lost_buffers = s_lost_buffers + 1;
        lost_pending += SAMPLES_PER_BUF;
      } else {
        s_sink(scratch, SAMPLES_PER_BUF, lost_pending);
        lost_pending = 0;
      }

      next++;
      s_consumed = next;
      uint32_t dt = micros() - t0;
      if (dt > s_max_us) s_max_us = dt;
    }
  }
}

// ---------------------------------------------------------------------------
bool dma_ring_init(ring_sink_t sink) {
  s_sink = sink;
  s_produced = s_consumed = s_lost_buffers = s_max_us = 0;

  for (int i = 0; i < NBUF; i++) {
    s_buf[i] = (uint32_t *)heap_caps_malloc(BUF_BYTES, MALLOC_CAP_DMA);
    if (!s_buf[i]) return false;
    memset(s_buf[i], 0, BUF_BYTES);

    lldesc_t *d = &s_desc[i];
    d->size   = BUF_BYTES;
    d->length = BUF_BYTES / 2;
    d->owner  = 1;
    d->sosf   = 1;
    d->eof    = 0;                                 // never signal "end": ring runs forever
    d->buf    = (uint8_t *)s_buf[i];
    d->offset = 0;
    d->empty  = 0;
    d->qe.stqe_next = &s_desc[(i + 1) % NBUF];     // last one links back to the first
  }

  if (xTaskCreatePinnedToCore(consumer_task, "ring_rx", 4096, NULL, 10, &s_task, 1) != pdPASS)
    return false;

  esp_err_t err = esp_intr_alloc(ETS_I2S0_INTR_SOURCE,
                                 ESP_INTR_FLAG_INTRDISABLED | ESP_INTR_FLAG_LEVEL1 | ESP_INTR_FLAG_IRAM,
                                 i2s_isr, NULL, &s_intr);
  return err == ESP_OK;
}

void dma_ring_start() {
  esp_intr_disable(s_intr);
  i2s_reset();

  I2S0.in_link.addr = (uint32_t)&s_desc[0];
  I2S0.in_link.start = 1;
  I2S0.int_clr.val = I2S0.int_raw.val;
  I2S0.int_ena.val = 0;
  I2S0.int_ena.in_done = 1;
  I2S0.rx_eof_num = BUF_BYTES;                     // large on purpose (as in the reference)

  esp_intr_enable(s_intr);
  i2s_rx_start();
}

void dma_ring_stop() {
  esp_intr_disable(s_intr);
  i2s_rx_stop();
}

RingStats dma_ring_stats() {
  RingStats st;
  st.produced     = s_produced;
  st.consumed     = s_consumed;
  st.lost_buffers = s_lost_buffers;
  st.max_proc_us  = s_max_us;
  s_max_us = 0;
  return st;
}
