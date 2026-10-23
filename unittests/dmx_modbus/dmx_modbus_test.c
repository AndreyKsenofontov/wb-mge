#include "unity.h"
#include "dmx_modbus.h"
#include "dmx_frame.h"
#include "dmx_output.h"
#include "modbus_helpers.h"
#include "bridge.h"

#include <string.h>

/* mocks/dmx_output.c */
extern uint8_t mock_dmx_universe[BRIDGES_COUNT][DMX_FRAME_MAX_SLOTS];
extern dmx_output_stats_t mock_dmx_stats[BRIDGES_COUNT];
extern int mock_dmx_blackout_called[BRIDGES_COUNT];
void mock_dmx_output_reset(void);

/* mocks/tcp_server.c */
extern int mock_tcp_server_init_called;
extern int mock_tcp_server_deinit_called;
extern int mock_tcp_server_last_port;
extern uint32_t mock_tcp_server_max_connections;
void mock_tcp_server_reset(void);

/* mocks/mb_device.c */
extern int mock_mb_device_handle_called;
void mock_mb_device_set_response(const uint8_t *buf, size_t len);
void mock_mb_device_reset(void);

#define HDR 8u

static uint8_t resp[MODBUS_TCP_MAX_ADU_LEN];

void setUp(void)
{
    mock_dmx_output_reset();
    mock_tcp_server_reset();
    mock_mb_device_reset();
    /* Make sure no listener is left from a previous test. */
    dmx_modbus_deinit(0);
    dmx_modbus_deinit(1);
    memset(resp, 0, sizeof(resp));
}

void tearDown(void) {}

/* Build an MBAP header + fc into buf; returns the number of bytes written (8). */
static size_t mbap(uint8_t *buf, uint16_t tid, uint8_t unit, uint8_t fc, uint16_t pdu_len_after_fc)
{
    buf[0] = (uint8_t)(tid >> 8); buf[1] = (uint8_t)tid;
    buf[2] = 0; buf[3] = 0;
    uint16_t len = (uint16_t)(2u + pdu_len_after_fc);   /* unit + fc + rest */
    buf[4] = (uint8_t)(len >> 8); buf[5] = (uint8_t)len;
    buf[6] = unit; buf[7] = fc;
    return HDR;
}

static size_t read_req(uint8_t *buf, uint8_t fc, uint16_t start, uint16_t count)
{
    size_t n = mbap(buf, 0x1234, 1, fc, 4);
    buf[n++] = (uint8_t)(start >> 8); buf[n++] = (uint8_t)start;
    buf[n++] = (uint8_t)(count >> 8); buf[n++] = (uint8_t)count;
    return n;
}

static size_t write_single_req(uint8_t *buf, uint16_t addr, uint16_t value)
{
    size_t n = mbap(buf, 0x0042, 1, 0x06, 4);
    buf[n++] = (uint8_t)(addr >> 8); buf[n++] = (uint8_t)addr;
    buf[n++] = (uint8_t)(value >> 8); buf[n++] = (uint8_t)value;
    return n;
}

static size_t write_multi_req(uint8_t *buf, uint16_t addr, const uint16_t *values, uint16_t count)
{
    size_t n = mbap(buf, 0x0043, 1, 0x10, (uint16_t)(5u + (count * 2u)));
    buf[n++] = (uint8_t)(addr >> 8); buf[n++] = (uint8_t)addr;
    buf[n++] = (uint8_t)(count >> 8); buf[n++] = (uint8_t)count;
    buf[n++] = (uint8_t)(count * 2u);
    for (uint16_t i = 0; i < count; i++) {
        buf[n++] = (uint8_t)(values[i] >> 8); buf[n++] = (uint8_t)values[i];
    }
    return n;
}

static uint16_t reg_at(const uint8_t *r, unsigned i)
{
    return (uint16_t)((r[HDR + 1 + (i * 2)] << 8) | r[HDR + 2 + (i * 2)]);
}

/* ── reads ─────────────────────────────────────────────────────────────── */

void test_read_holding_returns_channel_values(void)
{
    mock_dmx_universe[0][0] = 255;
    mock_dmx_universe[0][1] = 128;
    mock_dmx_universe[0][9] = 7;
    uint8_t req[16];
    size_t n = read_req(req, 0x03, 0, 10);

    size_t rl = dmx_modbus_handle_request(0, req, n, resp);
    TEST_ASSERT_EQUAL_size_t(HDR + 1 + 20, rl);
    TEST_ASSERT_EQUAL_HEX8(0x12, resp[0]);           /* transaction id echoed */
    TEST_ASSERT_EQUAL_HEX8(0x34, resp[1]);
    TEST_ASSERT_EQUAL_HEX8(0x03, resp[7]);
    TEST_ASSERT_EQUAL_UINT8(20, resp[HDR]);          /* byte count */
    TEST_ASSERT_EQUAL_UINT16(255, reg_at(resp, 0));
    TEST_ASSERT_EQUAL_UINT16(128, reg_at(resp, 1));
    TEST_ASSERT_EQUAL_UINT16(0, reg_at(resp, 2));
    TEST_ASSERT_EQUAL_UINT16(7, reg_at(resp, 9));
    /* MBAP length = unit + fc + byte count + data */
    TEST_ASSERT_EQUAL_UINT16(1 + 1 + 1 + 20, (uint16_t)((resp[4] << 8) | resp[5]));
}

void test_read_input_registers_uses_same_map(void)
{
    mock_dmx_universe[1][511] = 42;
    uint8_t req[16];
    size_t n = read_req(req, 0x04, 511, 1);

    size_t rl = dmx_modbus_handle_request(1, req, n, resp);
    TEST_ASSERT_EQUAL_size_t(HDR + 1 + 2, rl);
    TEST_ASSERT_EQUAL_UINT16(42, reg_at(resp, 0));
}

void test_read_status_block(void)
{
    mock_dmx_stats[0].active = true;
    mock_dmx_stats[0].slots = 512;
    mock_dmx_stats[0].period_us = 25000;
    mock_dmx_stats[0].frames_sent = 0x00012345ull;
    mock_dmx_stats[0].tx_disabled = true;
    uint8_t req[16];
    size_t n = read_req(req, 0x03, DMX_MB_REG_BLACKOUT, DMX_MB_REG_STATUS_COUNT);

    size_t rl = dmx_modbus_handle_request(0, req, n, resp);
    TEST_ASSERT_EQUAL_size_t(HDR + 1 + (DMX_MB_REG_STATUS_COUNT * 2), rl);
    TEST_ASSERT_EQUAL_UINT16(0, reg_at(resp, 0));        /* blackout reads 0 */
    TEST_ASSERT_EQUAL_UINT16(1, reg_at(resp, 1));        /* active */
    TEST_ASSERT_EQUAL_UINT16(512, reg_at(resp, 2));      /* slots */
    TEST_ASSERT_EQUAL_UINT16(40, reg_at(resp, 3));       /* refresh Hz */
    TEST_ASSERT_EQUAL_UINT16(0x2345, reg_at(resp, 4));   /* frames lo */
    TEST_ASSERT_EQUAL_UINT16(0x0001, reg_at(resp, 5));   /* frames hi */
    TEST_ASSERT_EQUAL_UINT16(1, reg_at(resp, 6));        /* tx_disabled */
}

void test_read_range_crossing_blocks_is_illegal_address(void)
{
    uint8_t req[16];
    size_t n = read_req(req, 0x03, 510, 4);              /* 510..513 leaves the channel block */

    size_t rl = dmx_modbus_handle_request(0, req, n, resp);
    TEST_ASSERT_EQUAL_size_t(HDR + 1, rl);
    TEST_ASSERT_EQUAL_HEX8(0x83, resp[7]);
    TEST_ASSERT_EQUAL_HEX8(MODBUS_EXC_ILLEGAL_ADDRESS, resp[8]);
}

void test_read_too_many_registers_is_illegal_value(void)
{
    uint8_t req[16];
    size_t n = read_req(req, 0x03, 0, 126);

    size_t rl = dmx_modbus_handle_request(0, req, n, resp);
    TEST_ASSERT_EQUAL_size_t(HDR + 1, rl);
    TEST_ASSERT_EQUAL_HEX8(MODBUS_EXC_ILLEGAL_DATA_VALUE, resp[8]);
}

/* ── writes ────────────────────────────────────────────────────────────── */

void test_write_single_sets_channel_and_echoes_request(void)
{
    uint8_t req[16];
    size_t n = write_single_req(req, 4, 200);          /* register 4 = channel 5 */

    size_t rl = dmx_modbus_handle_request(0, req, n, resp);
    TEST_ASSERT_EQUAL_size_t(HDR + 4, rl);
    TEST_ASSERT_EQUAL_UINT8(200, mock_dmx_universe[0][4]);
    TEST_ASSERT_EQUAL_MEMORY(req, resp, HDR + 4);
}

void test_write_single_value_above_255_is_illegal_value(void)
{
    uint8_t req[16];
    size_t n = write_single_req(req, 0, 256);

    size_t rl = dmx_modbus_handle_request(0, req, n, resp);
    TEST_ASSERT_EQUAL_size_t(HDR + 1, rl);
    TEST_ASSERT_EQUAL_HEX8(0x86, resp[7]);
    TEST_ASSERT_EQUAL_HEX8(MODBUS_EXC_ILLEGAL_DATA_VALUE, resp[8]);
    TEST_ASSERT_EQUAL_UINT8(0, mock_dmx_universe[0][0]);
}

void test_write_single_blackout_zeroes_universe(void)
{
    memset(mock_dmx_universe[0], 0x55, DMX_FRAME_MAX_SLOTS);
    uint8_t req[16];
    size_t n = write_single_req(req, DMX_MB_REG_BLACKOUT, 1);

    size_t rl = dmx_modbus_handle_request(0, req, n, resp);
    TEST_ASSERT_EQUAL_size_t(HDR + 4, rl);
    TEST_ASSERT_EQUAL(1, mock_dmx_blackout_called[0]);
    TEST_ASSERT_EQUAL_UINT8(0, mock_dmx_universe[0][100]);
}

void test_write_single_blackout_zero_is_noop(void)
{
    memset(mock_dmx_universe[0], 0x55, DMX_FRAME_MAX_SLOTS);
    uint8_t req[16];
    size_t n = write_single_req(req, DMX_MB_REG_BLACKOUT, 0);

    dmx_modbus_handle_request(0, req, n, resp);
    TEST_ASSERT_EQUAL(0, mock_dmx_blackout_called[0]);
    TEST_ASSERT_EQUAL_UINT8(0x55, mock_dmx_universe[0][100]);
}

void test_write_single_to_read_only_register_is_illegal_address(void)
{
    uint8_t req[16];
    size_t n = write_single_req(req, DMX_MB_REG_ACTIVE, 1);

    size_t rl = dmx_modbus_handle_request(0, req, n, resp);
    TEST_ASSERT_EQUAL_size_t(HDR + 1, rl);
    TEST_ASSERT_EQUAL_HEX8(MODBUS_EXC_ILLEGAL_ADDRESS, resp[8]);
}

void test_write_multiple_sets_channels(void)
{
    const uint16_t values[4] = {1, 2, 3, 255};
    uint8_t req[32];
    size_t n = write_multi_req(req, 100, values, 4);

    size_t rl = dmx_modbus_handle_request(0, req, n, resp);
    TEST_ASSERT_EQUAL_size_t(HDR + 4, rl);
    TEST_ASSERT_EQUAL_HEX8(0x10, resp[7]);
    TEST_ASSERT_EQUAL_UINT16(100, (uint16_t)((resp[8] << 8) | resp[9]));
    TEST_ASSERT_EQUAL_UINT16(4, (uint16_t)((resp[10] << 8) | resp[11]));
    TEST_ASSERT_EQUAL_UINT8(1, mock_dmx_universe[0][100]);
    TEST_ASSERT_EQUAL_UINT8(255, mock_dmx_universe[0][103]);
    TEST_ASSERT_EQUAL_UINT8(0, mock_dmx_universe[0][104]);
}

void test_write_multiple_rejects_value_above_255_without_partial_write(void)
{
    const uint16_t values[3] = {10, 300, 20};
    uint8_t req[32];
    size_t n = write_multi_req(req, 0, values, 3);

    size_t rl = dmx_modbus_handle_request(0, req, n, resp);
    TEST_ASSERT_EQUAL_size_t(HDR + 1, rl);
    TEST_ASSERT_EQUAL_HEX8(MODBUS_EXC_ILLEGAL_DATA_VALUE, resp[8]);
    TEST_ASSERT_EQUAL_UINT8(0, mock_dmx_universe[0][0]);
}

void test_write_multiple_past_last_channel_is_illegal_address(void)
{
    const uint16_t values[2] = {1, 1};
    uint8_t req[32];
    size_t n = write_multi_req(req, 511, values, 2);

    size_t rl = dmx_modbus_handle_request(0, req, n, resp);
    TEST_ASSERT_EQUAL_size_t(HDR + 1, rl);
    TEST_ASSERT_EQUAL_HEX8(MODBUS_EXC_ILLEGAL_ADDRESS, resp[8]);
}

void test_write_multiple_bad_byte_count_is_illegal_value(void)
{
    const uint16_t values[2] = {1, 1};
    uint8_t req[32];
    size_t n = write_multi_req(req, 0, values, 2);
    req[HDR + 4] = 3;                                   /* byte count must be 4 */

    size_t rl = dmx_modbus_handle_request(0, req, n, resp);
    TEST_ASSERT_EQUAL_size_t(HDR + 1, rl);
    TEST_ASSERT_EQUAL_HEX8(MODBUS_EXC_ILLEGAL_DATA_VALUE, resp[8]);
}

/* ── framing / dispatch ────────────────────────────────────────────────── */

void test_unsupported_function_is_illegal_function(void)
{
    uint8_t req[16];
    size_t n = mbap(req, 1, 1, 0x05, 4);
    memset(req + n, 0, 4); n += 4;

    size_t rl = dmx_modbus_handle_request(0, req, n, resp);
    TEST_ASSERT_EQUAL_size_t(HDR + 1, rl);
    TEST_ASSERT_EQUAL_HEX8(0x85, resp[7]);
    TEST_ASSERT_EQUAL_HEX8(MODBUS_EXC_ILLEGAL_FUNCTION, resp[8]);
}

void test_short_pdu_is_illegal_value(void)
{
    uint8_t req[16];
    size_t n = mbap(req, 1, 1, 0x03, 2);
    req[n++] = 0; req[n++] = 0;

    size_t rl = dmx_modbus_handle_request(0, req, n, resp);
    TEST_ASSERT_EQUAL_size_t(HDR + 1, rl);
    TEST_ASSERT_EQUAL_HEX8(MODBUS_EXC_ILLEGAL_DATA_VALUE, resp[8]);
}

void test_bad_framing_is_dropped(void)
{
    uint8_t req[16];
    size_t n = read_req(req, 0x03, 0, 1);
    req[2] = 0x12;                                      /* protocol id != 0 */

    TEST_ASSERT_EQUAL_size_t(0, dmx_modbus_handle_request(0, req, n, resp));
    TEST_ASSERT_EQUAL_size_t(0, dmx_modbus_handle_request(BRIDGES_COUNT, req, n, resp));
}

void test_unit_255_goes_to_gateway_self_map(void)
{
    static const uint8_t canned[9] = {0, 1, 0, 0, 0, 3, 0xFF, 0x83, 0x02};
    mock_mb_device_set_response(canned, sizeof(canned));
    uint8_t req[16];
    size_t n = read_req(req, 0x03, 0, 1);
    req[6] = 0xFF;

    size_t rl = dmx_modbus_handle_request(0, req, n, resp);
    TEST_ASSERT_EQUAL(1, mock_mb_device_handle_called);
    TEST_ASSERT_EQUAL_size_t(sizeof(canned), rl);
    TEST_ASSERT_EQUAL_MEMORY(canned, resp, sizeof(canned));
}

/* ── listener lifecycle ────────────────────────────────────────────────── */

void test_init_starts_listener_and_deinit_stops_it(void)
{
    TEST_ASSERT_EQUAL(ESP_OK, dmx_modbus_init(0, 502));
    TEST_ASSERT_EQUAL(1, mock_tcp_server_init_called);
    TEST_ASSERT_EQUAL(502, mock_tcp_server_last_port);
    TEST_ASSERT_EQUAL(502, dmx_modbus_get_port(0));
    TEST_ASSERT_EQUAL(0, dmx_modbus_get_port(1));
    /* The listener is capped: extra clients must be refused rather than starve the device. */
    TEST_ASSERT_EQUAL_UINT32(4, mock_tcp_server_max_connections);

    /* Same port again: no second listener. */
    TEST_ASSERT_EQUAL(ESP_OK, dmx_modbus_init(0, 502));
    TEST_ASSERT_EQUAL(1, mock_tcp_server_init_called);

    /* A different port restarts it. */
    TEST_ASSERT_EQUAL(ESP_OK, dmx_modbus_init(0, 1502));
    TEST_ASSERT_EQUAL(2, mock_tcp_server_init_called);
    TEST_ASSERT_EQUAL(1, mock_tcp_server_deinit_called);
    TEST_ASSERT_EQUAL(1502, dmx_modbus_get_port(0));

    TEST_ASSERT_EQUAL(ESP_OK, dmx_modbus_deinit(0));
    TEST_ASSERT_EQUAL(2, mock_tcp_server_deinit_called);
    TEST_ASSERT_EQUAL(0, dmx_modbus_get_port(0));
    /* Deinit of a stopped port is a no-op. */
    TEST_ASSERT_EQUAL(ESP_OK, dmx_modbus_deinit(0));
    TEST_ASSERT_EQUAL(2, mock_tcp_server_deinit_called);
}

void test_invalid_port_index_is_rejected(void)
{
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG, dmx_modbus_init(BRIDGES_COUNT, 502));
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG, dmx_modbus_deinit(BRIDGES_COUNT));
    TEST_ASSERT_EQUAL(0, dmx_modbus_get_port(BRIDGES_COUNT));
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_read_holding_returns_channel_values);
    RUN_TEST(test_read_input_registers_uses_same_map);
    RUN_TEST(test_read_status_block);
    RUN_TEST(test_read_range_crossing_blocks_is_illegal_address);
    RUN_TEST(test_read_too_many_registers_is_illegal_value);
    RUN_TEST(test_write_single_sets_channel_and_echoes_request);
    RUN_TEST(test_write_single_value_above_255_is_illegal_value);
    RUN_TEST(test_write_single_blackout_zeroes_universe);
    RUN_TEST(test_write_single_blackout_zero_is_noop);
    RUN_TEST(test_write_single_to_read_only_register_is_illegal_address);
    RUN_TEST(test_write_multiple_sets_channels);
    RUN_TEST(test_write_multiple_rejects_value_above_255_without_partial_write);
    RUN_TEST(test_write_multiple_past_last_channel_is_illegal_address);
    RUN_TEST(test_write_multiple_bad_byte_count_is_illegal_value);
    RUN_TEST(test_unsupported_function_is_illegal_function);
    RUN_TEST(test_short_pdu_is_illegal_value);
    RUN_TEST(test_bad_framing_is_dropped);
    RUN_TEST(test_unit_255_goes_to_gateway_self_map);
    RUN_TEST(test_init_starts_listener_and_deinit_stops_it);
    RUN_TEST(test_invalid_port_index_is_rejected);
    return UNITY_END();
}
