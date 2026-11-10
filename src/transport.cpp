#include <Arduino.h>
#include "freertos/message_buffer.h"
#include "config.h"
#include "packet.h"
#include "transport.h"

#if !USE_WIFI

static MessageBufferHandle_t s_mb;

// Core 0: pull one frame at a time and push it out of the UART.
// Serial.write() may block while the UART FIFO drains. That is fine here.
static void tx_task(void *) {
  static uint8_t frame[PKT_FRAME_MAX];
  for (;;) {
    size_t n = xMessageBufferReceive(s_mb, frame, sizeof frame, portMAX_DELAY);
    if (n) Serial.write(frame, n);
  }
}

bool transport_init() {
  s_mb = xMessageBufferCreate(TX_QUEUE_BYTES);
  if (!s_mb) return false;
  Serial.begin(TX_BAUD);
  return xTaskCreatePinnedToCore(tx_task, "la_tx", 4096, NULL, 5, NULL, 0) == pdPASS;
}

bool transport_send(const uint8_t *frame, size_t len, void *) {
  // single producer, zero wait: a message buffer either takes the whole frame or nothing
  return xMessageBufferSend(s_mb, frame, len, 0) == len;
}

#endif  // !USE_WIFI
