#pragma once

/* Mock tracking state for dmx_output functions — include from tests to access
 * call counters and control variables. */

#include "bridge.h"   /* for BRIDGES_COUNT */
#include "dmx_frame.h"
#include <stdbool.h>

typedef struct {
    int init_called;
    int deinit_called;
    int set_tx_disabled_called;
    bool last_tx_disabled;
    dmx_frame_config_t last_cfg;    /* config passed to the last dmx_output_init_port() */
} mock_dmx_output_calls_t;

extern mock_dmx_output_calls_t mock_dmx_output_calls[BRIDGES_COUNT];
extern bool mock_dmx_output_init_should_fail;
/* dmx_output_init() — the global (non per-port) init that creates the data mutexes. */
extern int mock_dmx_output_global_init_called;

void mock_dmx_output_reset(void);
