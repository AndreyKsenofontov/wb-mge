#pragma once

#include "esp_err.h"
#include "esp_http_server.h"

/**
 * @brief Register the DMX REST handlers.
 *
 *   GET  /ports/N/dmx — output state (active, slots, period_us, frames_sent, tx_disabled)
 *                       and the current universe as "values" (DMX_FRAME_MAX_SLOTS numbers).
 *   POST /ports/N/dmx — write channels: {"start": 1, "values": [0..255, ...]} (start is the
 *                       1-based first channel, default 1), or {"blackout": true}. Both may be
 *                       combined; blackout is applied first. Values persist across port
 *                       restarts and may be written while the port is not in DMX mode, but
 *                       then the response carries "active": false.
 *
 * N is 1 or 2 (RS-485 Port 1 / Port 2). Both endpoints require an authenticated session.
 */
esp_err_t dmx_http_register_handlers(httpd_handle_t server);
