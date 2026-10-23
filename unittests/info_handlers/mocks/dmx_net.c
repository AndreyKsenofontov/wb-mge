/* dmx_net mock for the info_handlers unit test: zeroed statistics. */

#include "bridge/dmx_net.h"
#include <string.h>

void dmx_net_get_stats(unsigned index, dmx_net_stats_t *out)
{
    (void)index;
    if (out != NULL) {
        memset(out, 0, sizeof(*out));
    }
}
