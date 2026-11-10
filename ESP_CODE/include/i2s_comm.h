#pragma once
#include <stdint.h>

// Sample clock + optional self-test signal (LEDC)
void sample_clock_start(uint32_t hz);
void test_signal_start(uint32_t hz);

// Route pins into I2S0 and configure it: slave RX, parallel (camera) mode, 16-bit
void i2s_comm_init();

// Low-level control used by the DMA layer
void i2s_reset();
void i2s_rx_start();
void i2s_rx_stop();
