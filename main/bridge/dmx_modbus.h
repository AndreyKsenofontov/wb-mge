#pragma once

#include "esp_err.h"

#include <stddef.h>
#include <stdint.h>

/*
 * Modbus TCP server of a port running in DMX mode.
 *
 * Listens on the port's bridge_port_N (the same TCP port the Modbus TCP gateway
 * would use in tcp_bridge mode) and maps the DMX universe onto holding registers,
 * so a controller sees the gateway as an ordinary Modbus TCP device.
 *
 * Register map (0-based addresses; FC03 and FC04 read the same map):
 *   0..511      DMX channels 1..512, value 0..255 (FC06 / FC16 writable)
 *   1000        blackout: write 1 to zero every channel (reads 0)
 *   1001        active        (RO) 1 while the port transmits DMX
 *   1002        slots         (RO)
 *   1003        refresh, Hz   (RO) derived from the break-to-break period
 *   1004        frames_sent   (RO) low 16 bits
 *   1005        frames_sent   (RO) high 16 bits
 *   1006        tx_disabled   (RO)
 * A read range must stay within one of the two blocks. Writes to the RO block
 * are answered with ILLEGAL DATA ADDRESS, channel values above 255 with
 * ILLEGAL DATA VALUE. Unit ID 0xFF is served by mb_device (gateway info map);
 * every other unit id is accepted.
 */

#define DMX_MB_REG_CHANNEL_BASE     0
#define DMX_MB_REG_CHANNEL_COUNT    512
#define DMX_MB_REG_BLACKOUT         1000
#define DMX_MB_REG_ACTIVE           1001
#define DMX_MB_REG_SLOTS            1002
#define DMX_MB_REG_REFRESH_HZ       1003
#define DMX_MB_REG_FRAMES_LO        1004
#define DMX_MB_REG_FRAMES_HI        1005
#define DMX_MB_REG_TX_DISABLED      1006
#define DMX_MB_REG_STATUS_COUNT     (DMX_MB_REG_TX_DISABLED - DMX_MB_REG_BLACKOUT + 1)

// Start the server for a port. Idempotent for the same tcp_port; a different port restarts it.
esp_err_t dmx_modbus_init(unsigned port_index, int tcp_port);

// Stop the server of a port (no-op if it is not running).
esp_err_t dmx_modbus_deinit(unsigned port_index);

// TCP port the server of a port listens on, 0 if it is not running.
int dmx_modbus_get_port(unsigned port_index);

// Build the response ADU for one complete request ADU. Exposed for the unit tests;
// production traffic reaches it through the TCP receive path.
// Returns the response length, or 0 when the request is dropped (bad framing).
size_t dmx_modbus_handle_request(unsigned port_index, const uint8_t *req, size_t req_len, uint8_t *resp);
