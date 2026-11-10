#include <Arduino.h>
#include "soc/i2s_struct.h"
#include "driver/periph_ctrl.h"
#include "soc/periph_defs.h"
#include "esp32/rom/gpio.h"
#include "soc/io_mux_reg.h"
#include "soc/gpio_periph.h"

#include "config.h"
#include "i2s_comm.h"

// ---------- pin routing: pad -> GPIO matrix -> I2S signal ----------
static void pin_in(int gpio, int sig) {
  PIN_FUNC_SELECT(GPIO_PIN_MUX_REG[gpio], PIN_FUNC_GPIO);
  gpio_set_direction((gpio_num_t)gpio, (gpio_mode_t)GPIO_MODE_DEF_INPUT);
  gpio_matrix_in(gpio, sig, false);
}

// ---------- LEDC: 50% square wave (1-bit duty) ----------
static void pwm(int pin, int ch, uint32_t hz) {
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  ledcAttach(pin, hz, 1);
  ledcWrite(pin, 1);
#else
  ledcSetup(ch, hz, 1);
  ledcAttachPin(pin, ch);
  ledcWrite(ch, 1);
#endif
}

void sample_clock_start(uint32_t hz) { pwm(CLK_OUT, 0, hz); delay(10); }
void test_signal_start(uint32_t hz)  { pwm(TEST_OUT, 2, hz); delay(10); }

// ---------- reset: pulse each reset bit 1 then 0 ----------
void i2s_reset() {
  I2S0.lc_conf.in_rst = 1;        I2S0.lc_conf.in_rst = 0;
  I2S0.lc_conf.ahbm_rst = 1;      I2S0.lc_conf.ahbm_rst = 0;
  I2S0.lc_conf.ahbm_fifo_rst = 1; I2S0.lc_conf.ahbm_fifo_rst = 0;
  I2S0.conf.rx_reset = 1;         I2S0.conf.rx_reset = 0;
  I2S0.conf.rx_fifo_reset = 1;    I2S0.conf.rx_fifo_reset = 0;
  while (I2S0.state.rx_fifo_reset_back) {}
}

void i2s_rx_start() { I2S0.conf.rx_start = 1; }
void i2s_rx_stop()  { I2S0.conf.rx_start = 0; }

// ---------- one-time I2S0 setup (values taken from the reference) ----------
void i2s_comm_init() {
  // route data pins, clock input, and the three camera sync signals
  for (int i = 0; i < 16; i++) pin_in(LA_PINS[i], I2S0I_DATA_IN0_IDX + i);
  pin_in(CLK_IN, I2S0I_WS_IN_IDX);
  gpio_matrix_in(0x38, I2S0I_V_SYNC_IDX, false);    // constant 1: camera mode only
  gpio_matrix_in(0x38, I2S0I_H_SYNC_IDX, false);    // samples while these three
  gpio_matrix_in(0x38, I2S0I_H_ENABLE_IDX, false);  // are high

  periph_module_enable(PERIPH_I2S0_MODULE);
  i2s_reset();

  I2S0.conf.rx_slave_mod = 1;                 // external sample clock
  I2S0.conf2.val = 0;
  I2S0.conf2.lcd_en = 1;                      // parallel mode
  I2S0.conf2.camera_en = 1;
  I2S0.clkm_conf.val = 0;                     // probably irrelevant in slave mode
  I2S0.clkm_conf.clka_en = 0;
  I2S0.clkm_conf.clkm_div_a = 1;
  I2S0.clkm_conf.clkm_div_b = 0;
  I2S0.clkm_conf.clkm_div_num = 4;
  I2S0.fifo_conf.dscr_en = 1;                 // FIFO -> DMA
  I2S0.fifo_conf.rx_fifo_mod = 1;
  I2S0.fifo_conf.rx_fifo_mod_force_en = 1;
  I2S0.conf_chan.rx_chan_mod = 1;
  I2S0.conf.rx_right_first = 1;
  I2S0.conf.rx_msb_right = 0;
  I2S0.conf.rx_msb_shift = 0;
  I2S0.conf.rx_mono = 1;
  I2S0.conf.rx_short_sync = 1;
  I2S0.timing.val = 0;
  I2S0.sample_rate_conf.val = 0;
  I2S0.sample_rate_conf.rx_bits_mod = 16;     // 16 pins per sample
  I2S0.sample_rate_conf.rx_bck_div_num = 1;
}
