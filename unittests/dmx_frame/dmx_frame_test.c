#include "unity.h"
#include "dmx_frame.h"

#include <string.h>

static const dmx_frame_config_t default_cfg = {
    .slots = 512, .refresh_hz = 40, .break_us = 176, .mab_us = 16,
};

void setUp(void) {}
void tearDown(void) {}

void test_config_valid_defaults(void)
{
    TEST_ASSERT_TRUE(dmx_frame_config_valid(&default_cfg));
}

void test_config_valid_rejects_null(void)
{
    TEST_ASSERT_FALSE(dmx_frame_config_valid(NULL));
}

void test_config_valid_limits(void)
{
    dmx_frame_config_t cfg = default_cfg;

    cfg.slots = DMX_FRAME_MIN_SLOTS;
    TEST_ASSERT_TRUE(dmx_frame_config_valid(&cfg));
    cfg.slots = DMX_FRAME_MIN_SLOTS - 1;
    TEST_ASSERT_FALSE(dmx_frame_config_valid(&cfg));
    cfg.slots = DMX_FRAME_MAX_SLOTS + 1;
    TEST_ASSERT_FALSE(dmx_frame_config_valid(&cfg));

    cfg = default_cfg;
    cfg.refresh_hz = 0;
    TEST_ASSERT_FALSE(dmx_frame_config_valid(&cfg));
    cfg.refresh_hz = DMX_FRAME_REFRESH_MAX_HZ + 1;
    TEST_ASSERT_FALSE(dmx_frame_config_valid(&cfg));

    cfg = default_cfg;
    cfg.break_us = DMX_FRAME_BREAK_MIN_US - 1;
    TEST_ASSERT_FALSE(dmx_frame_config_valid(&cfg));
    cfg.break_us = DMX_FRAME_BREAK_MAX_US + 1;
    TEST_ASSERT_FALSE(dmx_frame_config_valid(&cfg));

    cfg = default_cfg;
    cfg.mab_us = DMX_FRAME_MAB_MIN_US - 1;
    TEST_ASSERT_FALSE(dmx_frame_config_valid(&cfg));
    cfg.mab_us = DMX_FRAME_MAB_MAX_US + 1;
    TEST_ASSERT_FALSE(dmx_frame_config_valid(&cfg));
}

/* A full 512-slot packet: 176 + 16 + 513 * 44 = 22764 us. */
void test_duration_full_universe(void)
{
    TEST_ASSERT_EQUAL_UINT32(22764, dmx_frame_duration_us(&default_cfg));
}

/* 40 Hz asks for 25000 us, which is longer than the 22764 us frame: honoured as is. */
void test_period_uses_requested_refresh_when_frame_fits(void)
{
    TEST_ASSERT_EQUAL_UINT32(25000, dmx_frame_period_us(&default_cfg));
}

/* 44 Hz asks for 22727 us, shorter than the 22764 us frame: capped by the frame. */
void test_period_capped_by_frame_duration(void)
{
    dmx_frame_config_t cfg = default_cfg;
    cfg.refresh_hz = 44;
    TEST_ASSERT_EQUAL_UINT32(22764, dmx_frame_period_us(&cfg));
}

/* 24 slots at 44 Hz: the frame is 1292 us, the request 22727 us; the request wins. */
void test_period_short_frame_keeps_requested_rate(void)
{
    dmx_frame_config_t cfg = { .slots = 24, .refresh_hz = 44, .break_us = 92, .mab_us = 12 };
    TEST_ASSERT_EQUAL_UINT32(22727, dmx_frame_period_us(&cfg));
}

void test_write_channels_copies_at_offset(void)
{
    uint8_t universe[DMX_FRAME_MAX_SLOTS];
    const uint8_t values[3] = {10, 20, 30};
    memset(universe, 0xAA, sizeof(universe));

    TEST_ASSERT_EQUAL_size_t(3, dmx_frame_write_channels(universe, 5, values, 3));
    TEST_ASSERT_EQUAL_UINT8(0xAA, universe[3]);
    TEST_ASSERT_EQUAL_UINT8(10, universe[4]);
    TEST_ASSERT_EQUAL_UINT8(20, universe[5]);
    TEST_ASSERT_EQUAL_UINT8(30, universe[6]);
    TEST_ASSERT_EQUAL_UINT8(0xAA, universe[7]);
}

void test_write_channels_truncates_past_last_slot(void)
{
    uint8_t universe[DMX_FRAME_MAX_SLOTS];
    uint8_t values[8];
    memset(universe, 0, sizeof(universe));
    memset(values, 0xFF, sizeof(values));

    TEST_ASSERT_EQUAL_size_t(2, dmx_frame_write_channels(universe, 511, values, sizeof(values)));
    TEST_ASSERT_EQUAL_UINT8(0, universe[509]);
    TEST_ASSERT_EQUAL_UINT8(0xFF, universe[510]);
    TEST_ASSERT_EQUAL_UINT8(0xFF, universe[511]);
}

void test_write_channels_rejects_bad_arguments(void)
{
    uint8_t universe[DMX_FRAME_MAX_SLOTS] = {0};
    const uint8_t values[1] = {1};

    TEST_ASSERT_EQUAL_size_t(0, dmx_frame_write_channels(universe, 0, values, 1));
    TEST_ASSERT_EQUAL_size_t(0, dmx_frame_write_channels(universe, 513, values, 1));
    TEST_ASSERT_EQUAL_size_t(0, dmx_frame_write_channels(NULL, 1, values, 1));
    TEST_ASSERT_EQUAL_size_t(0, dmx_frame_write_channels(universe, 1, NULL, 1));
    TEST_ASSERT_EQUAL_UINT8(0, universe[0]);
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_config_valid_defaults);
    RUN_TEST(test_config_valid_rejects_null);
    RUN_TEST(test_config_valid_limits);
    RUN_TEST(test_duration_full_universe);
    RUN_TEST(test_period_uses_requested_refresh_when_frame_fits);
    RUN_TEST(test_period_capped_by_frame_duration);
    RUN_TEST(test_period_short_frame_keeps_requested_rate);
    RUN_TEST(test_write_channels_copies_at_offset);
    RUN_TEST(test_write_channels_truncates_past_last_slot);
    RUN_TEST(test_write_channels_rejects_bad_arguments);
    return UNITY_END();
}
