// Mock implementation of dmx_modbus for the port_manager unit tests: call-tracking stubs.

#include "unity.h"
#include "dmx_modbus.h"
#include "bridge.h"        // BRIDGES_COUNT
#include "dmx_modbus_mock.h"
#include <string.h>

mock_dmx_modbus_calls_t mock_dmx_modbus_calls[BRIDGES_COUNT] = {0};
bool mock_dmx_modbus_init_should_fail = false;

esp_err_t dmx_modbus_init(unsigned port_index, int tcp_port)
{
    TEST_ASSERT_LESS_THAN_UINT_MESSAGE(BRIDGES_COUNT, port_index, "dmx_modbus_init: invalid index");
    mock_dmx_modbus_calls[port_index].init_called++;
    mock_dmx_modbus_calls[port_index].last_port = tcp_port;
    if (mock_dmx_modbus_init_should_fail) {
        return ESP_FAIL;
    }
    mock_dmx_modbus_calls[port_index].running_port = tcp_port;
    return ESP_OK;
}

esp_err_t dmx_modbus_deinit(unsigned port_index)
{
    TEST_ASSERT_LESS_THAN_UINT_MESSAGE(BRIDGES_COUNT, port_index, "dmx_modbus_deinit: invalid index");
    mock_dmx_modbus_calls[port_index].deinit_called++;
    mock_dmx_modbus_calls[port_index].running_port = 0;
    return ESP_OK;
}

int dmx_modbus_get_port(unsigned port_index)
{
    return (port_index < BRIDGES_COUNT) ? mock_dmx_modbus_calls[port_index].running_port : 0;
}

size_t dmx_modbus_handle_request(unsigned port_index, const uint8_t *req, size_t req_len, uint8_t *resp)
{
    (void)port_index; (void)req; (void)req_len; (void)resp;
    return 0;
}

void mock_dmx_modbus_reset(void)
{
    memset(mock_dmx_modbus_calls, 0, sizeof(mock_dmx_modbus_calls));
    mock_dmx_modbus_init_should_fail = false;
}
