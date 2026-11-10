#include <Arduino.h>
#include "config.h"

#if USE_WIFI
#include <WiFi.h>
#include <ESPmDNS.h>
#include "freertos/message_buffer.h"
#include "packet.h"
#include "transport.h"

// WiFi transport: the ESP32 is a TCP server, the laptop connects to it.
// Same byte stream as the UART version (frames from packet.h), so la_decode.py
// decodes it unchanged:   python la_decode.py tcp://<esp-ip>:5000
//
// Producer (core 1) never blocks: frames go into a message buffer. A task on
// core 0 owns the socket. With no client connected, frames are discarded; the
// decoder re-baselines on the next frame it sees (sample index keeps counting).

static MessageBufferHandle_t s_mb;
static WiFiServer s_server(WIFI_TCP_PORT);
static WiFiClient s_client;

static void check_client() {
  if (s_client && !s_client.connected()) {
    s_client.stop();
    Serial.println("[wifi] client disconnected");
  }
  if (!s_client) {
#if ESP_ARDUINO_VERSION_MAJOR >= 3
    WiFiClient c = s_server.accept();
#else
    WiFiClient c = s_server.available();
#endif
    if (c) {
      c.setNoDelay(true);
      s_client = c;
      Serial.printf("[wifi] client connected: %s\n", s_client.remoteIP().toString().c_str());
    }
  }
}

static void tx_task(void *) {
  static uint8_t frame[PKT_FRAME_MAX];
  uint32_t last_wifi_try = 0;
  for (;;) {
    size_t n = xMessageBufferReceive(s_mb, frame, sizeof frame, pdMS_TO_TICKS(50));

    if (WiFi.status() != WL_CONNECTED) {                 // keep the link alive
      if (millis() - last_wifi_try > 5000) { WiFi.reconnect(); last_wifi_try = millis(); }
      continue;                                          // frame (if any) is dropped
    }
    check_client();
    if (!n || !s_client) continue;                       // nobody listening: drop

    if (s_client.write(frame, n) != n) {                 // socket died mid-frame
      s_client.stop();
      Serial.println("[wifi] write failed, client dropped");
    }
  }
}

bool transport_init() {
  Serial.begin(115200);                                  // logs only, no frames here
  s_mb = xMessageBufferCreate(TX_QUEUE_BYTES);
  if (!s_mb) return false;

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);                                  // no modem sleep: lower latency, more throughput
  WiFi.setHostname(WIFI_HOSTNAME);
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  Serial.printf("\n[wifi] connecting to %s", WIFI_SSID);
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED) {
    if (millis() - t0 > 20000) { Serial.println("\n[wifi] connect timeout (check SSID/password, 2.4 GHz only)"); return false; }
    delay(250); Serial.print('.');
  }
  Serial.printf("\n[wifi] connected, IP %s\n", WiFi.localIP().toString().c_str());
  if (MDNS.begin(WIFI_HOSTNAME)) MDNS.addService("lalogic", "tcp", WIFI_TCP_PORT);

  s_server.begin();
  Serial.printf("[wifi] listening on port %d\n[wifi] on the laptop: python la_decode.py tcp://%s:%d\n",
                WIFI_TCP_PORT, WiFi.localIP().toString().c_str(), WIFI_TCP_PORT);

  return xTaskCreatePinnedToCore(tx_task, "la_tx", 6144, NULL, 5, NULL, 0) == pdPASS;
}

bool transport_send(const uint8_t *frame, size_t len, void *) {
  return xMessageBufferSend(s_mb, frame, len, 0) == len;
}
#endif  // USE_WIFI
