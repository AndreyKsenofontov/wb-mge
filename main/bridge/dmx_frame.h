#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

// DMX512 (ANSI E1.11) line parameters: 250 kbit/s, 8N2, so one slot is 11 bits = 44 us.
#define DMX_FRAME_BAUDRATE          250000
#define DMX_FRAME_SLOT_US           44
#define DMX_FRAME_START_CODE        0x00

#define DMX_FRAME_MIN_SLOTS         24
#define DMX_FRAME_MAX_SLOTS         512

// E1.11 transmitter limits: BREAK >= 92 us is recommended (88 us is the receiver minimum),
// MAB >= 12 us recommended (8 us minimum). Upper bounds are ours, to keep the line busy.
#define DMX_FRAME_BREAK_MIN_US      92
#define DMX_FRAME_BREAK_MAX_US      1000
#define DMX_FRAME_MAB_MIN_US        12
#define DMX_FRAME_MAB_MAX_US        1000

#define DMX_FRAME_REFRESH_MIN_HZ    1
#define DMX_FRAME_REFRESH_MAX_HZ    44

// E1.11 minimum break-to-break time.
#define DMX_FRAME_MIN_PERIOD_US     1204

typedef struct {
    uint16_t slots;         // Data slots per frame (start code not counted)
    uint16_t refresh_hz;    // Requested frame rate; capped by the frame duration
    uint16_t break_us;
    uint16_t mab_us;
} dmx_frame_config_t;

bool dmx_frame_config_valid(const dmx_frame_config_t *cfg);

// Time on the wire for one packet: BREAK + MAB + start code + slots.
uint32_t dmx_frame_duration_us(const dmx_frame_config_t *cfg);

// Break-to-break period actually used: the requested refresh period, but never shorter than the
// frame itself or the E1.11 minimum.
uint32_t dmx_frame_period_us(const dmx_frame_config_t *cfg);

// Copy values into a universe buffer starting at 1-based DMX channel start_channel.
// Values past channel DMX_FRAME_MAX_SLOTS are dropped. Returns the number of channels written
// (0 if start_channel is out of range).
size_t dmx_frame_write_channels(uint8_t *universe, uint16_t start_channel, const uint8_t *values, size_t count);
