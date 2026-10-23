#include "dmx_frame.h"

#include <string.h>

#define DMX_FRAME_US_PER_S          1000000U

bool dmx_frame_config_valid(const dmx_frame_config_t *cfg)
{
    if (cfg == NULL) {
        return false;
    }
    if ((cfg->slots < DMX_FRAME_MIN_SLOTS) || (cfg->slots > DMX_FRAME_MAX_SLOTS)) {
        return false;
    }
    if ((cfg->refresh_hz < DMX_FRAME_REFRESH_MIN_HZ) || (cfg->refresh_hz > DMX_FRAME_REFRESH_MAX_HZ)) {
        return false;
    }
    if ((cfg->break_us < DMX_FRAME_BREAK_MIN_US) || (cfg->break_us > DMX_FRAME_BREAK_MAX_US)) {
        return false;
    }
    if ((cfg->mab_us < DMX_FRAME_MAB_MIN_US) || (cfg->mab_us > DMX_FRAME_MAB_MAX_US)) {
        return false;
    }
    return true;
}

uint32_t dmx_frame_duration_us(const dmx_frame_config_t *cfg)
{
    return (uint32_t)cfg->break_us + cfg->mab_us + ((1U + cfg->slots) * DMX_FRAME_SLOT_US);
}

uint32_t dmx_frame_period_us(const dmx_frame_config_t *cfg)
{
    uint32_t period = DMX_FRAME_US_PER_S / cfg->refresh_hz;
    uint32_t duration = dmx_frame_duration_us(cfg);

    if (period < duration) {
        period = duration;
    }
    if (period < DMX_FRAME_MIN_PERIOD_US) {
        period = DMX_FRAME_MIN_PERIOD_US;
    }
    return period;
}

size_t dmx_frame_write_channels(uint8_t *universe, uint16_t start_channel, const uint8_t *values, size_t count)
{
    if ((universe == NULL) || (values == NULL) || (start_channel < 1) || (start_channel > DMX_FRAME_MAX_SLOTS)) {
        return 0;
    }
    size_t room = (size_t)DMX_FRAME_MAX_SLOTS - (start_channel - 1);
    if (count > room) {
        count = room;
    }
    memcpy(&universe[start_channel - 1], values, count);
    return count;
}
