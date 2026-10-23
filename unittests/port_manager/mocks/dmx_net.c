// Mock implementation of dmx_net for the port_manager unit tests: call-tracking stubs.
// dmx_net_config_equal() is the real comparison, so the settings-change tests exercise it.

#include "unity.h"
#include "dmx_net.h"
#include "bridge.h"        // BRIDGES_COUNT
#include "dmx_net_mock.h"
#include <string.h>

mock_dmx_net_calls_t mock_dmx_net_calls[BRIDGES_COUNT] = {0};
bool mock_dmx_net_start_should_fail = false;

void dmx_net_init(void) {}

esp_err_t dmx_net_port_start(unsigned index, const dmx_net_port_config_t *cfg)
{
    TEST_ASSERT_LESS_THAN_UINT_MESSAGE(BRIDGES_COUNT, index, "dmx_net_port_start: invalid index");
    TEST_ASSERT_NOT_NULL(cfg);
    mock_dmx_net_calls[index].start_called++;
    mock_dmx_net_calls[index].last_cfg = *cfg;
    return mock_dmx_net_start_should_fail ? ESP_FAIL : ESP_OK;
}

void dmx_net_port_stop(unsigned index)
{
    TEST_ASSERT_LESS_THAN_UINT_MESSAGE(BRIDGES_COUNT, index, "dmx_net_port_stop: invalid index");
    mock_dmx_net_calls[index].stop_called++;
}

void dmx_net_get_stats(unsigned index, dmx_net_stats_t *out)
{
    (void)index;
    if (out != NULL) {
        memset(out, 0, sizeof(*out));
    }
}

bool dmx_net_config_equal(const dmx_net_port_config_t *a, const dmx_net_port_config_t *b)
{
    return (a->artnet_enabled == b->artnet_enabled) && (a->artnet_universe == b->artnet_universe) &&
           (a->sacn_enabled == b->sacn_enabled) && (a->sacn_universe == b->sacn_universe) &&
           (a->loss_policy == b->loss_policy) && (a->loss_timeout_s == b->loss_timeout_s);
}

void mock_dmx_net_reset(void)
{
    memset(mock_dmx_net_calls, 0, sizeof(mock_dmx_net_calls));
    mock_dmx_net_start_should_fail = false;
}
