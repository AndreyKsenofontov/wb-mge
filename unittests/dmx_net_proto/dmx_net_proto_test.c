#include "unity.h"
#include "dmx_net_proto.h"

#include <string.h>

void setUp(void) {}
void tearDown(void) {}

/* ---- Art-Net ------------------------------------------------------------------------------ */

static size_t make_artdmx(uint8_t *buf, uint16_t port_address, uint8_t seq, const uint8_t *data, uint16_t len)
{
    memcpy(buf, "Art-Net", 8);
    buf[8] = 0x00; buf[9] = 0x50;                 /* OpDmx little-endian */
    buf[10] = 0; buf[11] = 14;                    /* ProtVer 14 */
    buf[12] = seq; buf[13] = 0;
    buf[14] = (uint8_t)(port_address & 0xFF);
    buf[15] = (uint8_t)(port_address >> 8);
    buf[16] = (uint8_t)(len >> 8); buf[17] = (uint8_t)(len & 0xFF);
    memcpy(buf + 18, data, len);
    return 18 + len;
}

void test_artdmx_is_parsed(void)
{
    uint8_t data[512];
    for (int i = 0; i < 512; i++) data[i] = (uint8_t)i;
    uint8_t buf[600];
    size_t n = make_artdmx(buf, 0x1234, 7, data, 512);
    artnet_packet_t p;
    TEST_ASSERT_TRUE(artnet_parse(buf, n, &p));
    TEST_ASSERT_EQUAL(ARTNET_PACKET_DMX, p.type);
    TEST_ASSERT_EQUAL_UINT16(0x1234, p.port_address);
    TEST_ASSERT_EQUAL_UINT8(7, p.sequence);
    TEST_ASSERT_EQUAL_UINT16(512, p.length);
    TEST_ASSERT_EQUAL_UINT8(255, p.data[255]);
}

void test_artdmx_short_payload_is_rejected(void)
{
    uint8_t data[24] = {0};
    uint8_t buf[64];
    size_t n = make_artdmx(buf, 0, 0, data, 24);
    artnet_packet_t p;
    TEST_ASSERT_FALSE(artnet_parse(buf, n - 1, &p));   /* one byte short of the declared length */
    buf[16] = 0; buf[17] = 0;
    TEST_ASSERT_FALSE(artnet_parse(buf, n, &p));       /* zero length */
}

void test_artpoll_and_unknown_opcodes(void)
{
    uint8_t buf[14] = {'A', 'r', 't', '-', 'N', 'e', 't', 0, 0x00, 0x20, 0, 14, 0, 0};
    artnet_packet_t p;
    TEST_ASSERT_TRUE(artnet_parse(buf, sizeof(buf), &p));
    TEST_ASSERT_EQUAL(ARTNET_PACKET_POLL, p.type);
    buf[9] = 0x21;                                    /* OpPollReply from another node */
    TEST_ASSERT_TRUE(artnet_parse(buf, sizeof(buf), &p));
    TEST_ASSERT_EQUAL(ARTNET_PACKET_OTHER, p.type);
    buf[0] = 'X';
    TEST_ASSERT_FALSE(artnet_parse(buf, sizeof(buf), &p));
    TEST_ASSERT_FALSE(artnet_parse(buf, 5, &p));
}

void test_poll_reply_layout(void)
{
    uint8_t buf[ARTNET_POLL_REPLY_LEN];
    const uint8_t ip[4] = {192, 168, 13, 172};
    const uint8_t mac[6] = {0xF0, 0x24, 0xF9, 0x30, 0x7E, 0x3F};
    artnet_build_poll_reply(buf, ip, mac, 0x1234, "WB-MGU", "WB-MGU DMX gateway");
    TEST_ASSERT_EQUAL_MEMORY("Art-Net", buf, 8);
    TEST_ASSERT_EQUAL_UINT8(0x00, buf[8]); TEST_ASSERT_EQUAL_UINT8(0x21, buf[9]);
    TEST_ASSERT_EQUAL_MEMORY(ip, buf + 10, 4);
    TEST_ASSERT_EQUAL_UINT8(0x36, buf[14]); TEST_ASSERT_EQUAL_UINT8(0x19, buf[15]);   /* 6454 LE */
    TEST_ASSERT_EQUAL_UINT8(0x12, buf[18]);           /* NetSwitch */
    TEST_ASSERT_EQUAL_UINT8(0x03, buf[19]);           /* SubSwitch */
    TEST_ASSERT_EQUAL_STRING("WB-MGU", (const char *)buf + 26);
    TEST_ASSERT_EQUAL_UINT8(1, buf[173]);             /* one port */
    TEST_ASSERT_EQUAL_UINT8(0x80, buf[174]);          /* DMX output */
    TEST_ASSERT_EQUAL_UINT8(0x04, buf[190]);          /* SwOut = universe nibble */
    TEST_ASSERT_EQUAL_MEMORY(mac, buf + 201, 6);
}

/* ---- sACN ---------------------------------------------------------------------------------- */

static size_t make_e131(uint8_t *buf, uint16_t universe, uint8_t priority, uint8_t seq, uint8_t options,
                        const uint8_t *data, uint16_t slots)
{
    memset(buf, 0, 638);
    buf[0] = 0x00; buf[1] = 0x10;                             /* preamble size */
    memcpy(buf + 4, "ASC-E1.17\0\0\0", 12);
    uint16_t root_len = (uint16_t)(0x7000 | (110 + slots));   /* from the flags/length field to the end */
    buf[16] = (uint8_t)(root_len >> 8); buf[17] = (uint8_t)root_len;
    buf[21] = 0x04;                                           /* VECTOR_ROOT_E131_DATA */
    for (int i = 0; i < 16; i++) buf[22 + i] = (uint8_t)(0xA0 + i);   /* CID */
    uint8_t *fr = buf + 38;
    uint16_t fr_len = (uint16_t)(0x7000 | (88 + slots));
    fr[0] = (uint8_t)(fr_len >> 8); fr[1] = (uint8_t)fr_len;
    fr[5] = 0x02;                                             /* VECTOR_E131_DATA_PACKET */
    memcpy(fr + 6, "test source", 11);
    fr[70] = priority; fr[73] = seq; fr[74] = options;
    fr[75] = (uint8_t)(universe >> 8); fr[76] = (uint8_t)universe;
    uint8_t *dmp = buf + 115;
    uint16_t dmp_len = (uint16_t)(0x7000 | (11 + slots));
    dmp[0] = (uint8_t)(dmp_len >> 8); dmp[1] = (uint8_t)dmp_len;
    dmp[2] = 0x02; dmp[3] = 0xA1;                             /* vector, address type */
    dmp[6] = 0; dmp[7] = 1;                                   /* address increment 1 */
    dmp[8] = (uint8_t)((slots + 1) >> 8); dmp[9] = (uint8_t)(slots + 1);
    dmp[10] = 0x00;                                           /* start code */
    memcpy(dmp + 11, data, slots);
    return 126 + slots;
}

void test_e131_data_is_parsed(void)
{
    uint8_t data[512];
    for (int i = 0; i < 512; i++) data[i] = (uint8_t)(255 - i);
    uint8_t buf[640];
    size_t n = make_e131(buf, 7, 150, 9, 0, data, 512);
    sacn_packet_t p;
    TEST_ASSERT_TRUE(sacn_parse(buf, n, &p));
    TEST_ASSERT_EQUAL_UINT16(7, p.universe);
    TEST_ASSERT_EQUAL_UINT8(150, p.priority);
    TEST_ASSERT_EQUAL_UINT8(9, p.sequence);
    TEST_ASSERT_EQUAL_UINT8(0, p.start_code);
    TEST_ASSERT_EQUAL_UINT16(512, p.length);
    TEST_ASSERT_EQUAL_UINT8(255, p.data[0]);
    TEST_ASSERT_EQUAL_UINT8(0xA5, p.cid[5]);
}

void test_e131_terminated_flag_and_short_universe(void)
{
    uint8_t data[24] = {1, 2, 3};
    uint8_t buf[640];
    size_t n = make_e131(buf, 1, 100, 0, SACN_OPT_STREAM_TERMINATED, data, 24);
    sacn_packet_t p;
    TEST_ASSERT_TRUE(sacn_parse(buf, n, &p));
    TEST_ASSERT_EQUAL_UINT16(24, p.length);
    TEST_ASSERT_TRUE(p.options & SACN_OPT_STREAM_TERMINATED);
}

void test_e131_rejects_bad_packets(void)
{
    uint8_t data[24] = {0};
    uint8_t buf[640];
    size_t n = make_e131(buf, 1, 100, 0, 0, data, 24);
    sacn_packet_t p;
    TEST_ASSERT_FALSE(sacn_parse(buf, n - 1, &p));          /* truncated */
    buf[21] = 0x08;                                           /* root vector: extended (sync/discovery) */
    TEST_ASSERT_FALSE(sacn_parse(buf, n, &p));
    buf[21] = 0x04;
    buf[38 + 70] = 201;                                       /* priority above 200 */
    TEST_ASSERT_FALSE(sacn_parse(buf, n, &p));
    buf[38 + 70] = 100;
    buf[38 + 75] = 0; buf[38 + 76] = 0;                       /* universe 0 */
    TEST_ASSERT_FALSE(sacn_parse(buf, n, &p));
    TEST_ASSERT_FALSE(sacn_parse(buf, 10, &p));
}

void test_multicast_group(void)
{
    uint8_t g[4];
    sacn_multicast_group(1, g);
    TEST_ASSERT_EQUAL_UINT8(239, g[0]); TEST_ASSERT_EQUAL_UINT8(255, g[1]);
    TEST_ASSERT_EQUAL_UINT8(0, g[2]); TEST_ASSERT_EQUAL_UINT8(1, g[3]);
    sacn_multicast_group(0x1234, g);
    TEST_ASSERT_EQUAL_UINT8(0x12, g[2]); TEST_ASSERT_EQUAL_UINT8(0x34, g[3]);
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_artdmx_is_parsed);
    RUN_TEST(test_artdmx_short_payload_is_rejected);
    RUN_TEST(test_artpoll_and_unknown_opcodes);
    RUN_TEST(test_poll_reply_layout);
    RUN_TEST(test_e131_data_is_parsed);
    RUN_TEST(test_e131_terminated_flag_and_short_universe);
    RUN_TEST(test_e131_rejects_bad_packets);
    RUN_TEST(test_multicast_group);
    return UNITY_END();
}
