#pragma once
#include <stdint.h>

// ---------------- pins ----------------
#define CLK_OUT   22          // LEDC sample clock out
#define CLK_IN    23          // jumper from CLK_OUT, feeds I2S clock input
#define TEST_OUT  2           // test signal, jumper to CH0 (GPIO21)

// CH0..CH15 -> GPIO. Only the first NUM_CHANNELS are used by the app.
static const int LA_PINS[16] = {21,19,18,5,4,15,13,12,14,27,26,25,33,32,35,34};

// ---------------- capture ----------------
#define SAMPLE_HZ       5000000UL   // sample clock
#define TEST_SIGNAL_HZ  10000UL     // self-test square wave on TEST_OUT (100 kHz makes ~400 KB/s of RLE: too much for UART)
#define NUM_CHANNELS    8           // I2S runs 16-bit; we keep the low byte

// ---------------- DMA ring ----------------
#define NBUF            8                       // ring size (descriptors)
#define BUF_BYTES       4000                    // bytes per descriptor
#define WORDS_PER_BUF   (BUF_BYTES / 4)         // 32-bit words per buffer
#define SAMPLES_PER_BUF (WORDS_PER_BUF * 2)     // 2 samples per word

// ---------------- streaming ----------------
#define TX_BAUD          921600UL   // UART to the PC. CH340 can do 2000000, CP2102N 3000000
#define TX_QUEUE_BYTES   (24 * 1024)// packets waiting for the UART (heap)
#define FLUSH_MS         20         // max age of buffered data before it is sent anyway
#define STATS_MS         1000       // period of the STATS frame

// ---------------- transport select ----------------
#define USE_WIFI         1          // 1 = send frames over WiFi (TCP), 0 = UART (TX_BAUD)
#define WIFI_SSID        "Net+2.4G 9888062406"
#define WIFI_PASS        "3U498mjcgS"
#define WIFI_TCP_PORT    5000       // ESP32 listens here; the laptop connects
#define WIFI_HOSTNAME    "esp32-la" // laptop can also use esp32-la.local
