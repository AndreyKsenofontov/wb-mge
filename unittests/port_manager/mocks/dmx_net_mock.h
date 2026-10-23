#pragma once

/* Mock tracking state for dmx_net functions. */

#include "bridge.h"   /* for BRIDGES_COUNT */
#include "dmx_net.h"
#include <stdbool.h>

typedef struct {
    int start_called;
    int stop_called;
    dmx_net_port_config_t last_cfg;   /* config passed to the last dmx_net_port_start() */
} mock_dmx_net_calls_t;

extern mock_dmx_net_calls_t mock_dmx_net_calls[BRIDGES_COUNT];
extern bool mock_dmx_net_start_should_fail;

void mock_dmx_net_reset(void);
