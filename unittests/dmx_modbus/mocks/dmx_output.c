/* Functional dmx_output mock for the dmx_modbus unit tests: keeps an in-memory universe
 * per port and a settable stats snapshot, so the register handler can be driven end to end. */

#include "dmx_output.h"
#include "bridge.h"
#include <string.h>

uint8_t mock_dmx_universe[BRIDGES_COUNT][DMX_FRAME_MAX_SLOTS];
dmx_output_stats_t mock_dmx_stats[BRIDGES_COUNT];
int mock_dmx_blackout_called[BRIDGES_COUNT];

void dmx_output_init(void) {}

esp_err_t dmx_output_init_port(unsigned index, const serial_config_t *pins, const dmx_frame_config_t *cfg,
                               bool tx_disabled)
{
    (void)index; (void)pins; (void)cfg; (void)tx_disabled;
    return ESP_OK;
}

esp_err_t dmx_output_deinit_port(unsigned index) { (void)index; return ESP_OK; }
esp_err_t dmx_output_set_tx_disabled(unsigned index, bool disabled) { (void)index; (void)disabled; return ESP_OK; }

esp_err_t dmx_output_set_channels(unsigned index, uint16_t start_channel, const uint8_t *values, size_t count)
{
    if (index >= BRIDGES_COUNT) {
        return ESP_ERR_INVALID_ARG;
    }
    dmx_frame_write_channels(mock_dmx_universe[index], start_channel, values, count);
    return ESP_OK;
}

esp_err_t dmx_output_get_channels(unsigned index, uint8_t *out)
{
    if (index >= BRIDGES_COUNT) {
        return ESP_ERR_INVALID_ARG;
    }
    memcpy(out, mock_dmx_universe[index], DMX_FRAME_MAX_SLOTS);
    return ESP_OK;
}

esp_err_t dmx_output_blackout(unsigned index)
{
    if (index >= BRIDGES_COUNT) {
        return ESP_ERR_INVALID_ARG;
    }
    mock_dmx_blackout_called[index]++;
    memset(mock_dmx_universe[index], 0, DMX_FRAME_MAX_SLOTS);
    return ESP_OK;
}

void dmx_output_get_stats(unsigned index, dmx_output_stats_t *out)
{
    if (out == NULL) {
        return;
    }
    if (index >= BRIDGES_COUNT) {
        memset(out, 0, sizeof(*out));
        return;
    }
    *out = mock_dmx_stats[index];
}

void mock_dmx_output_reset(void)
{
    memset(mock_dmx_universe, 0, sizeof(mock_dmx_universe));
    memset(mock_dmx_stats, 0, sizeof(mock_dmx_stats));
    memset(mock_dmx_blackout_called, 0, sizeof(mock_dmx_blackout_called));
}
