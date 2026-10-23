#pragma once

/* Mock tracking state for dmx_modbus functions. */

#include "bridge.h"   /* for BRIDGES_COUNT */
#include <stdbool.h>

typedef struct {
    int init_called;
    int deinit_called;
    int last_port;      /* tcp_port passed to the last dmx_modbus_init() */
    int running_port;   /* what dmx_modbus_get_port() reports */
} mock_dmx_modbus_calls_t;

extern mock_dmx_modbus_calls_t mock_dmx_modbus_calls[BRIDGES_COUNT];
extern bool mock_dmx_modbus_init_should_fail;

void mock_dmx_modbus_reset(void);
