/* dmx_output mock for the info_handlers unit test. Only dmx_output_get_stats() is
 * referenced (by info_get_handler); returns a zeroed snapshot. */

#include "bridge/dmx_output.h"
#include <string.h>

void dmx_output_get_stats(unsigned index, dmx_output_stats_t *out)
{
    (void)index;
    if (out != NULL) {
        memset(out, 0, sizeof(*out));
    }
}
