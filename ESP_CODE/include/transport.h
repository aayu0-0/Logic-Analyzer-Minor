#pragma once
#include <stdint.h>
#include <stddef.h>

// Byte pipe to the PC. The producer (core 1) never blocks: frames go into a message
// buffer and a task on core 0 writes them to the UART.
bool transport_init();

// All-or-nothing, non-blocking. Returns false if the queue has no room for the frame.
bool transport_send(const uint8_t *frame, size_t len, void *ctx);
