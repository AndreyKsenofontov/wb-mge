#include "dmx_net_proto.h"

#include <string.h>

// ---- Art-Net ---------------------------------------------------------------------------------

static const uint8_t ARTNET_ID[8] = {'A', 'r', 't', '-', 'N', 'e', 't', 0};

#define ARTNET_HEADER_LEN           10u     // id (8) + OpCode (2)
#define ARTNET_DMX_HEADER_LEN       18u     // header + ProtVer (2) + Seq + Phys + SubUni + Net + Length (2)
#define ARTNET_PROT_VERSION         14u

static uint16_t rd16le(const uint8_t *p)
{
    return (uint16_t)(p[0] | (p[1] << 8));
}

static uint16_t rd16be(const uint8_t *p)
{
    return (uint16_t)((p[0] << 8) | p[1]);
}

bool artnet_parse(const uint8_t *buf, size_t len, artnet_packet_t *out)
{
    if ((buf == NULL) || (out == NULL) || (len < ARTNET_HEADER_LEN) || (memcmp(buf, ARTNET_ID, sizeof(ARTNET_ID)) != 0)) {
        return false;
    }
    memset(out, 0, sizeof(*out));
    uint16_t opcode = rd16le(buf + 8);

    if (opcode == ARTNET_OP_POLL) {
        out->type = ARTNET_PACKET_POLL;
        return true;
    }
    if (opcode != ARTNET_OP_DMX) {
        out->type = ARTNET_PACKET_OTHER;
        return true;
    }
    if (len < ARTNET_DMX_HEADER_LEN) {
        return false;
    }
    if (rd16be(buf + 10) < ARTNET_PROT_VERSION) {
        return false;
    }
    uint16_t length = rd16be(buf + 16);
    if ((length == 0) || (length > DMX_NET_MAX_SLOTS) || (len < ARTNET_DMX_HEADER_LEN + length)) {
        return false;
    }
    out->type = ARTNET_PACKET_DMX;
    out->sequence = buf[12];
    out->port_address = (uint16_t)(((buf[15] & 0x7Fu) << 8) | buf[14]);
    out->data = buf + ARTNET_DMX_HEADER_LEN;
    out->length = length;
    return true;
}

void artnet_build_poll_reply(uint8_t *buf, const uint8_t ip[4], const uint8_t mac[6], uint16_t port_address,
                             const char *short_name, const char *long_name)
{
    memset(buf, 0, ARTNET_POLL_REPLY_LEN);
    memcpy(buf, ARTNET_ID, sizeof(ARTNET_ID));
    buf[8] = (uint8_t)(ARTNET_OP_POLL_REPLY & 0xFFu);
    buf[9] = (uint8_t)(ARTNET_OP_POLL_REPLY >> 8);
    memcpy(buf + 10, ip, 4);                        // IP address
    buf[14] = (uint8_t)(DMX_NET_ARTNET_PORT & 0xFFu);   // Port, little-endian
    buf[15] = (uint8_t)(DMX_NET_ARTNET_PORT >> 8);
    buf[16] = 0;                                    // VersInfoH
    buf[17] = 1;                                    // VersInfoL
    buf[18] = (uint8_t)((port_address >> 8) & 0x7Fu);   // NetSwitch
    buf[19] = (uint8_t)((port_address >> 4) & 0x0Fu);   // SubSwitch
    buf[20] = 0;                                    // OemHi
    buf[21] = 0xFF;                                 // OemLo: OemUnknown
    buf[22] = 0;                                    // Ubea version
    buf[23] = 0xD0;                                 // Status1: indicators normal, port-address by network
    buf[24] = 0xFF;                                 // EstaManLo: unregistered
    buf[25] = 0x7F;                                 // EstaManHi
    strncpy((char *)buf + 26, short_name, ARTNET_SHORT_NAME_LEN - 1);
    strncpy((char *)buf + 44, long_name, ARTNET_LONG_NAME_LEN - 1);
    strncpy((char *)buf + 108, "#0001 [0000] OK", 63);   // NodeReport
    buf[172] = 0;                                   // NumPortsHi
    buf[173] = 1;                                   // NumPortsLo
    buf[174] = 0x80;                                // PortTypes[0]: output, DMX512
    buf[178] = 0;                                   // GoodInput[0]
    buf[182] = 0x80;                                // GoodOutput[0]: data being transmitted
    buf[186] = 0;                                   // SwIn[0]
    buf[190] = (uint8_t)(port_address & 0x0Fu);     // SwOut[0]
    buf[200] = 0;                                   // Style: StNode
    memcpy(buf + 201, mac, 6);
    memcpy(buf + 207, ip, 4);                       // BindIp
    buf[211] = 1;                                   // BindIndex
    buf[212] = 0x08;                                // Status2: supports 15-bit port-address
}

// ---- sACN / E1.31 ------------------------------------------------------------------------------

static const uint8_t ACN_PACKET_ID[12] = {'A', 'S', 'C', '-', 'E', '1', '.', '1', '7', 0, 0, 0};

#define SACN_ROOT_PREAMBLE_SIZE     0x0010u
#define SACN_ROOT_VECTOR_DATA       0x00000004u
#define SACN_FRAME_VECTOR_DATA      0x00000002u
#define SACN_DMP_VECTOR             0x02u
#define SACN_DMP_ADDR_TYPE          0xA1u
#define SACN_MIN_LEN                126u    // root (38) + framing (77) + DMP (10) + start code (1)

static uint32_t rd32be(const uint8_t *p)
{
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | p[3];
}

static uint16_t pdu_length(const uint8_t *p)
{
    return (uint16_t)(rd16be(p) & 0x0FFFu);
}

bool sacn_parse(const uint8_t *buf, size_t len, sacn_packet_t *out)
{
    if ((buf == NULL) || (out == NULL) || (len < SACN_MIN_LEN)) {
        return false;
    }
    // Root layer
    if ((rd16be(buf) != SACN_ROOT_PREAMBLE_SIZE) || (rd16be(buf + 2) != 0) ||
        (memcmp(buf + 4, ACN_PACKET_ID, sizeof(ACN_PACKET_ID)) != 0)) {
        return false;
    }
    if (rd32be(buf + 18) != SACN_ROOT_VECTOR_DATA) {
        return false;   // extended packets (sync, discovery) are not data
    }
    if ((size_t)pdu_length(buf + 16) + 16u > len) {
        return false;
    }
    // Framing layer
    const uint8_t *fr = buf + 38;
    if (rd32be(fr + 2) != SACN_FRAME_VECTOR_DATA) {
        return false;
    }
    uint8_t priority = fr[70];
    uint8_t sequence = fr[73];
    uint8_t options = fr[74];
    uint16_t universe = rd16be(fr + 75);
    if ((priority > SACN_PRIORITY_MAX) || (universe < SACN_UNIVERSE_MIN) || (universe > SACN_UNIVERSE_MAX)) {
        return false;
    }
    // DMP layer
    const uint8_t *dmp = buf + 115;
    if ((dmp[2] != SACN_DMP_VECTOR) || (dmp[3] != SACN_DMP_ADDR_TYPE) || (rd16be(dmp + 4) != 0) ||
        (rd16be(dmp + 6) != 1)) {
        return false;
    }
    uint16_t prop_count = rd16be(dmp + 8);      // start code + slots
    if ((prop_count == 0) || (prop_count > DMX_NET_MAX_SLOTS + 1u) || (len < 125u + prop_count)) {
        return false;
    }
    memset(out, 0, sizeof(*out));
    out->universe = universe;
    out->priority = priority;
    out->sequence = sequence;
    out->options = options;
    out->cid = buf + 22;
    out->start_code = dmp[10];
    out->data = dmp + 11;
    out->length = (uint16_t)(prop_count - 1u);
    return true;
}

void sacn_multicast_group(uint16_t universe, uint8_t out[4])
{
    out[0] = 239;
    out[1] = 255;
    out[2] = (uint8_t)(universe >> 8);
    out[3] = (uint8_t)(universe & 0xFFu);
}
