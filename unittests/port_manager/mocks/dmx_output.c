// Mock implementation of dmx_output for the port_manager unit tests.
// DMX mode owns no serial_desc, so unlike the repeater mock nothing is handed back
// to the shared serial.c mock. All functions are simple call-tracking stubs.

#include "unity.h"
#include "dmx_output.h"
#include "bridge.h"        // BRIDGES_COUNT
#include "dmx_output_mock.h"
#include <string.h>

mock_dmx_output_calls_t mock_dmx_output_calls[BRIDGES_COUNT] = {0};
bool mock_dmx_output_init_should_fail = false;
int mock_dmx_output_global_init_called = 0;

void dmx_output_init(void)
{
    mock_dmx_output_global_init_called++;
}

esp_err_t dmx_output_init_port(unsigned index, const serial_config_t *pins, const dmx_frame_config_t *cfg,
                               bool tx_disabled)
{
    TEST_ASSERT_LESS_THAN_UINT_MESSAGE(BRIDGES_COUNT, index, "dmx_output_init_port: invalid index");
    TEST_ASSERT_NOT_NULL(pins);
    TEST_ASSERT_NOT_NULL(cfg);
    mock_dmx_output_calls[index].init_called++;
    mock_dmx_output_calls[index].last_cfg = *cfg;
    mock_dmx_output_calls[index].last_tx_disabled = tx_disabled;
    if (mock_dmx_output_init_should_fail) {
        return ESP_FAIL;
    }
    return ESP_OK;
}

esp_err_t dmx_output_deinit_port(unsigned index)
{
    TEST_ASSERT_LESS_THAN_UINT_MESSAGE(BRIDGES_COUNT, index, "dmx_output_deinit_port: invalid index");
    mock_dmx_output_calls[index].deinit_called++;
    return ESP_OK;
}

esp_err_t dmx_output_set_tx_disabled(unsigned index, bool disabled)
{
    TEST_ASSERT_LESS_THAN_UINT_MESSAGE(BRIDGES_COUNT, index, "dmx_output_set_tx_disabled: invalid index");
    mock_dmx_output_calls[index].set_tx_disabled_called++;
    mock_dmx_output_calls[index].last_tx_disabled = disabled;
    return ESP_OK;
}

esp_err_t dmx_output_set_channels(unsigned index, uint16_t start_channel, const uint8_t *values, size_t count)
{
    (void)index; (void)start_channel; (void)values; (void)count;
    return ESP_OK;
}

esp_err_t dmx_output_get_channels(unsigned index, uint8_t *out)
{
    (void)index;
    memset(out, 0, DMX_FRAME_MAX_SLOTS);
    return ESP_OK;
}

esp_err_t dmx_output_blackout(unsigned index)
{
    (void)index;
    return ESP_OK;
}

void dmx_output_get_stats(unsigned index, dmx_output_stats_t *out)
{
    (void)index;
    if (out != NULL) {
        memset(out, 0, sizeof(*out));
    }
}

void mock_dmx_output_reset(void)
{
    memset(mock_dmx_output_calls, 0, sizeof(mock_dmx_output_calls));
    mock_dmx_output_init_should_fail = false;
    mock_dmx_output_global_init_called = 0;
}
