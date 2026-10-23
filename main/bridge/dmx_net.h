#pragma once

#include "esp_err.h"

#include <stdbool.h>
#include <stdint.h>

/*
 * Network DMX sources of the dmx ports: Art-Net and sACN (E1.31) receivers.
 *
 * One receiver task serves both ports: it owns the UDP sockets (6454 for Art-Net, 5568 for
 * sACN, with one multicast membership per configured sACN universe), matches every packet to
 * the ports whose universe it addresses and writes the slots into that port's universe through
 * dmx_output. The task exists only while at least one port has a protocol enabled.
 *
 * Signal loss: when a port stops receiving for loss_timeout_s after having received, the
 * configured policy applies — keep the last values (hold) or zero the universe (blackout).
 */

#define DMX_NET_SOURCE_STR_LEN      24      // "255.255.255.255:65535" and a terminator

typedef enum {
    DMX_LOSS_HOLD = 0,
    DMX_LOSS_BLACKOUT = 1,
} dmx_loss_policy_t;

typedef struct {
    bool              artnet_enabled;
    uint16_t          artnet_universe;      // 15-bit port-address
    bool              sacn_enabled;
    uint16_t          sacn_universe;        // 1..63999
    dmx_loss_policy_t loss_policy;
    uint16_t          loss_timeout_s;
} dmx_net_port_config_t;

typedef struct {
    bool     enabled;
    uint16_t universe;
    bool     active;                        // a packet arrived within the last 2.5 s
    uint32_t packets;                       // accepted packets since the port started
    uint32_t fps;                           // packets accepted during the previous second
    char     last_source[DMX_NET_SOURCE_STR_LEN];   // "ip:port" of the last accepted packet, "" if none
    uint8_t  priority;                      // sACN: priority of the source being followed
} dmx_net_proto_stats_t;

typedef struct {
    dmx_net_proto_stats_t artnet;
    dmx_net_proto_stats_t sacn;
    bool                  signal_lost;      // the loss policy fired and no packet arrived since
} dmx_net_stats_t;

// Create the lock. Idempotent; call before any port can start.
void dmx_net_init(void);

// Register a port's network sources and (re)start the receiver task as needed.
// A port with neither protocol enabled is simply unregistered.
esp_err_t dmx_net_port_start(unsigned index, const dmx_net_port_config_t *cfg);

// Unregister a port; the receiver task stops when no port is left.
void dmx_net_port_stop(unsigned index);

// Snapshot of the port's receiver statistics (zeros when the port is not registered).
void dmx_net_get_stats(unsigned index, dmx_net_stats_t *out);

// Compare two configurations field by field (serial_config_t-style, no memcmp on padding).
bool dmx_net_config_equal(const dmx_net_port_config_t *a, const dmx_net_port_config_t *b);
