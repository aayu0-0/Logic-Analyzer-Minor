#include <Arduino.h>
#include "config.h"
#include "i2s_comm.h"
#include "dma_ring.h"
#include "stream.h"
#include "esp_system.h"

// Capture -> ring -> RLE -> packets -> UART.
// The UART carries binary frames only (see packet.h); decode them with tools/la_decode.py.
// Health numbers travel inside STATS frames, so there are no text prints while streaming.

void setup() {
  if (!stream_init()) {                      // opens Serial at TX_BAUD
    Serial.begin(115200);
    Serial.println("transport init FAILED");
    while (true) delay(1000);
  }

  // text line before any frame; la_decode.py shows it as [board] ...
  // 1=poweron 3=sw 4=panic 5=int_wdt 6=task_wdt 7=wdt 9=brownout
  Serial.printf("\nreset_reason=%d\n", (int)esp_reset_reason());

  sample_clock_start(SAMPLE_HZ);
  test_signal_start(TEST_SIGNAL_HZ);
  i2s_comm_init();

  if (!dma_ring_init(stream_on_samples)) {
    Serial.println("ring init FAILED (out of DMA memory or no interrupt)");
    while (true) delay(1000);
  }
  dma_ring_start();
}

void loop() {
  delay(1000);                               // all the work happens in tasks
}