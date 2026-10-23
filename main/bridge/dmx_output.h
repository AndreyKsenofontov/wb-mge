#pragma once

#include "dmx_frame.h"
#include "serial.h"
#include "esp_err.h"

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef struct {
    bool     active;            // true while the port is running in DMX mode
    bool     tx_disabled;       // true while DE is held LOW (frames are generated but not driven onto the bus)
    uint16_t slots;
    uint32_t period_us;         // break-to-break period in use
    uint64_t frames_sent;       // 64-bit: no practical overflow
    uint64_t uptime_ms;         // milliseconds since the output started (0 if inactive)
} dmx_output_stats_t;

// Create the per-port data mutexes once. Idempotent. Call before any port concurrency starts.
void dmx_output_init(void);

// Start DMX output on a port. Only port_num, tx_pin, rx_pin and dir_pin of pins are used: the line
// settings are fixed by DMX512. The direction pin is taken away from the UART and held HIGH for the
// whole session, because the transmitter must keep driving the line during MAB and between packets.
// The universe is all zeros after boot and keeps its values across port restarts.
esp_err_t dmx_output_init_port(unsigned index, const serial_config_t *pins, const dmx_frame_config_t *cfg,
                               bool tx_disabled);

// Stop the frame task, release DE (LOW = receive) and delete the UART driver.
esp_err_t dmx_output_deinit_port(unsigned index);

// Hold DE LOW (disabled) or HIGH. Has no effect when the port is not active.
esp_err_t dmx_output_set_tx_disabled(unsigned index, bool disabled);

// Update channel values (1-based start channel). Takes effect from the next packet.
// Values may be written while the port is inactive; they are kept for the next start.
esp_err_t dmx_output_set_channels(unsigned index, uint16_t start_channel, const uint8_t *values, size_t count);

// Copy the whole universe (DMX_FRAME_MAX_SLOTS bytes) into out.
esp_err_t dmx_output_get_channels(unsigned index, uint8_t *out);

// Set every channel to 0.
esp_err_t dmx_output_blackout(unsigned index);

void dmx_output_get_stats(unsigned index, dmx_output_stats_t *out);
