#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/*
 * Wire formats of the two network DMX protocols the gateway receives. Pure parsing and packet
 * building, no sockets: dmx_net.c owns the UDP side, the host unit tests drive these directly.
 *
 * Art-Net 4 (Artistic Licence): UDP 6454, "Art-Net\0" + little-endian OpCode. The gateway
 * answers ArtPoll with ArtPollReply and consumes ArtDmx addressed to its port-address
 * (15-bit: Net << 8 | SubNet << 4 | Universe).
 *
 * sACN / ANSI E1.31: UDP 5568, root layer (ACN "ASC-E1.17"), E1.31 framing layer and DMP layer.
 * Multicast group of a universe is 239.255.<hi>.<lo>.
 */

#define DMX_NET_ARTNET_PORT         6454
#define DMX_NET_SACN_PORT           5568
#define DMX_NET_MAX_SLOTS           512

#define ARTNET_OP_POLL              0x2000u
#define ARTNET_OP_POLL_REPLY        0x2100u
#define ARTNET_OP_DMX               0x5000u
#define ARTNET_PORT_ADDRESS_MAX     32767u
#define ARTNET_POLL_REPLY_LEN       239u
#define ARTNET_SHORT_NAME_LEN       18u
#define ARTNET_LONG_NAME_LEN        64u

#define SACN_UNIVERSE_MIN           1u
#define SACN_UNIVERSE_MAX           63999u
#define SACN_PRIORITY_DEFAULT       100u
#define SACN_PRIORITY_MAX           200u
#define SACN_OPT_STREAM_TERMINATED  0x40u
#define SACN_OPT_PREVIEW_DATA       0x80u
#define SACN_CID_LEN                16u

typedef enum {
    ARTNET_PACKET_OTHER = 0,    // valid Art-Net, an OpCode the gateway ignores
    ARTNET_PACKET_POLL,
    ARTNET_PACKET_DMX,
} artnet_packet_type_t;

typedef struct {
    artnet_packet_type_t type;
    uint16_t             port_address;  // ArtDmx: 15-bit port-address
    uint8_t              sequence;      // ArtDmx: 0 = not used
    const uint8_t       *data;          // ArtDmx: slot values (channel 1 first)
    uint16_t             length;        // ArtDmx: 1..512 (odd lengths are accepted)
} artnet_packet_t;

typedef struct {
    uint16_t       universe;
    uint8_t        priority;            // 0..200
    uint8_t        sequence;
    uint8_t        options;             // SACN_OPT_* bits
    uint8_t        start_code;
    const uint8_t *cid;                 // 16 bytes, source id
    const uint8_t *data;                // slot values (channel 1 first)
    uint16_t       length;              // 0..512
} sacn_packet_t;

// Parse an Art-Net datagram. Returns false when it is not Art-Net or is malformed.
bool artnet_parse(const uint8_t *buf, size_t len, artnet_packet_t *out);

// Build an ArtPollReply (ARTNET_POLL_REPLY_LEN bytes) describing one DMX output port.
// ip/mac are the gateway's own; port_address is the output's 15-bit address.
void artnet_build_poll_reply(uint8_t *buf, const uint8_t ip[4], const uint8_t mac[6], uint16_t port_address,
                             const char *short_name, const char *long_name);

// Parse an E1.31 data packet. Returns false for anything else (sync, discovery, malformed).
bool sacn_parse(const uint8_t *buf, size_t len, sacn_packet_t *out);

// Multicast group of an sACN universe, as 4 octets.
void sacn_multicast_group(uint16_t universe, uint8_t out[4]);
