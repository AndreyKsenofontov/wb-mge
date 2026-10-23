#include "setting_items.h"
#include "bridge.h"  /* for BRIDGES_COUNT */
#include <string.h>
#include <stdlib.h>

/* Stored mock values for port modes (index 0 = port_mode_1, index 1 = port_mode_2) */
static char mock_port_mode[BRIDGES_COUNT][SETTING_ITEM_MAX_STR_LEN];
bool mock_cache_server_enabled = false;
int mock_cache_port = 0;

/* Stored mock values for the per-port DMX parameters (dmx_*_1 / dmx_*_2). Default to the
 * production defaults so a port can enter DMX mode without every test setting them. */
int mock_dmx_slots[BRIDGES_COUNT] = {512, 512};
int mock_dmx_refresh[BRIDGES_COUNT] = {40, 40};
int mock_dmx_break[BRIDGES_COUNT] = {176, 176};
int mock_dmx_mab[BRIDGES_COUNT] = {16, 16};

/* Stored mock values for the per-port TCP listener port (bridge_port_1 / bridge_port_2). */
int mock_bridge_port[BRIDGES_COUNT] = {502, 503};

/* Stored mock values for the network DMX sources of a port. */
bool mock_dmx_artnet[BRIDGES_COUNT] = {false, false};
int mock_dmx_artnet_univ[BRIDGES_COUNT] = {0, 0};
bool mock_dmx_sacn[BRIDGES_COUNT] = {false, false};
int mock_dmx_sacn_univ[BRIDGES_COUNT] = {1, 1};
int mock_dmx_loss_tout[BRIDGES_COUNT] = {3, 3};

/* Stored mock values for per-port cache overlay (cache_en_1 / cache_en_2) */
bool mock_cache_en[BRIDGES_COUNT] = {false, false};

/* Count how many times save was called (for diagnostics) */
int mock_setting_items_save_called = 0;
int mock_setting_items_save_bool_called = 0;

/* When set, the corresponding save returns an error (persist-6 tests). The
 * stored value is NOT updated, mimicking an NVS write failure. */
bool mock_setting_items_save_should_fail = false;
bool mock_setting_items_save_bool_should_fail = false;

/* When set to a key name, setting_items_save_bool() fails for THAT key only and
 * succeeds for every other one. Needed to isolate a single write inside a call that
 * does several — e.g. the release half of a cache-overlay move, whose result would
 * otherwise be masked by the target port's write failing too. */
const char *mock_setting_items_save_bool_fail_key = NULL;

static int index_from_port_mode_key(const char *key)
{
    if (strcmp(key, KEY_PORT_MODE1) == 0) {
        return 0;
    }
    if (strcmp(key, KEY_PORT_MODE2) == 0) {
        return 1;
    }
    return -1;
}

static int index_from_cache_en_key(const char *key)
{
    if (strcmp(key, KEY_CACHE_EN_1) == 0) {
        return 0;
    }
    if (strcmp(key, KEY_CACHE_EN_2) == 0) {
        return 1;
    }
    return -1;
}

/* Test helper: directly set the stored port-mode string (e.g. legacy values). */
void mock_setting_items_set_port_mode(unsigned index, const char *value)
{
    if (index >= BRIDGES_COUNT) return;
    strncpy(mock_port_mode[index], value, SETTING_ITEM_MAX_STR_LEN - 1);
    mock_port_mode[index][SETTING_ITEM_MAX_STR_LEN - 1] = '\0';
}

/* Test helper: read the stored port-mode string. */
const char *mock_setting_items_get_port_mode(unsigned index)
{
    if (index >= BRIDGES_COUNT) return "";
    return mock_port_mode[index];
}

esp_err_t setting_items_read(const char *key, char *value)
{
    int idx = index_from_port_mode_key(key);
    if (idx >= 0) {
        strncpy(value, mock_port_mode[idx], SETTING_ITEM_MAX_STR_LEN);
        return ESP_OK;
    }
    /* Unknown key — return a sensible default rather than failing */
    value[0] = '\0';
    return ESP_OK;
}

esp_err_t setting_items_save(const char *key, const char *value)
{
    mock_setting_items_save_called++;
    if (mock_setting_items_save_should_fail) {
        return ESP_FAIL;  /* NVS write failed: stored value left unchanged */
    }
    int idx = index_from_port_mode_key(key);
    if (idx >= 0) {
        strncpy(mock_port_mode[idx], value, SETTING_ITEM_MAX_STR_LEN - 1);
        mock_port_mode[idx][SETTING_ITEM_MAX_STR_LEN - 1] = '\0';
    }
    return ESP_OK;
}

bool setting_items_read_bool(const char *key)
{
    if (strcmp(key, KEY_CACHE_MODBUS_SERVER_ENABLED) == 0) {
        return mock_cache_server_enabled;
    }
    int ci = index_from_cache_en_key(key);
    if (ci >= 0) {
        return mock_cache_en[ci];
    }
    if (strcmp(key, KEY_DMX_ARTNET_1) == 0) return mock_dmx_artnet[0];
    if (strcmp(key, KEY_DMX_ARTNET_2) == 0) return mock_dmx_artnet[1];
    if (strcmp(key, KEY_DMX_SACN_1) == 0) return mock_dmx_sacn[0];
    if (strcmp(key, KEY_DMX_SACN_2) == 0) return mock_dmx_sacn[1];
    return false;
}

int setting_items_read_int(const char *key)
{
    if (strcmp(key, KEY_CACHE_MODBUS_PORT) == 0) {
        return mock_cache_port;
    }
    static const struct { const char *key; int *store; } dmx_keys[] = {
        {KEY_BRIDGE_PORT1, &mock_bridge_port[0]},   {KEY_BRIDGE_PORT2, &mock_bridge_port[1]},
        {KEY_DMX_ARTNET_UNIV_1, &mock_dmx_artnet_univ[0]}, {KEY_DMX_ARTNET_UNIV_2, &mock_dmx_artnet_univ[1]},
        {KEY_DMX_SACN_UNIV_1, &mock_dmx_sacn_univ[0]},     {KEY_DMX_SACN_UNIV_2, &mock_dmx_sacn_univ[1]},
        {KEY_DMX_LOSS_TOUT_1, &mock_dmx_loss_tout[0]},     {KEY_DMX_LOSS_TOUT_2, &mock_dmx_loss_tout[1]},
        {KEY_DMX_SLOTS_1, &mock_dmx_slots[0]},     {KEY_DMX_SLOTS_2, &mock_dmx_slots[1]},
        {KEY_DMX_REFRESH_1, &mock_dmx_refresh[0]}, {KEY_DMX_REFRESH_2, &mock_dmx_refresh[1]},
        {KEY_DMX_BREAK_1, &mock_dmx_break[0]},     {KEY_DMX_BREAK_2, &mock_dmx_break[1]},
        {KEY_DMX_MAB_1, &mock_dmx_mab[0]},         {KEY_DMX_MAB_2, &mock_dmx_mab[1]},
    };
    for (size_t i = 0; i < sizeof(dmx_keys) / sizeof(dmx_keys[0]); i++) {
        if (strcmp(key, dmx_keys[i].key) == 0) {
            return *dmx_keys[i].store;
        }
    }
    return 0;
}

/* Stubs for functions not used by port_manager directly */
esp_err_t setting_items_init(void)                                    { return ESP_OK; }
esp_err_t setting_items_init_with_storage(const setting_storage_iface_t *s) { (void)s; return ESP_OK; }
esp_err_t setting_items_save_bool(const char *key, bool value)
{
    mock_setting_items_save_bool_called++;
    if (mock_setting_items_save_bool_should_fail ||
        (mock_setting_items_save_bool_fail_key != NULL &&
         strcmp(key, mock_setting_items_save_bool_fail_key) == 0)) {
        return ESP_FAIL;  /* NVS write failed: stored value left unchanged */
    }
    int ci = index_from_cache_en_key(key);
    if (ci >= 0) {
        mock_cache_en[ci] = value;
    }
    return ESP_OK;
}
esp_err_t setting_items_save_int(const char *key, int value)          { (void)key; (void)value; return ESP_OK; }
esp_err_t setting_items_set_defaults(bool only_uninitialized)         { (void)only_uninitialized; return ESP_OK; }
size_t setting_items_get_count(void)                                  { return 0; }
const char *setting_items_get_key_at(size_t index)                    { (void)index; return NULL; }
const char *setting_items_get_default_value(const char *key)          { (void)key; return NULL; }
setting_item_type_t setting_items_get_type(const char *key)           { (void)key; return SETTING_ITEM_TYPE_INVALID; }
const char *setting_items_type_to_string(setting_item_type_t type)    { (void)type; return "unknown"; }
esp_err_t setting_items_validate(const char *key, const char *value)  { (void)key; (void)value; return ESP_OK; }

void mock_setting_items_reset(void)
{
    for (unsigned i = 0; i < BRIDGES_COUNT; i++) {
        /* Default: disabled */
        strncpy(mock_port_mode[i], PORT_MODE_DISABLED_STR, SETTING_ITEM_MAX_STR_LEN - 1);
        mock_port_mode[i][SETTING_ITEM_MAX_STR_LEN - 1] = '\0';
    }
    mock_cache_server_enabled = false;
    mock_cache_port = 0;
    mock_cache_en[0] = false;
    mock_cache_en[1] = false;
    for (unsigned i = 0; i < BRIDGES_COUNT; i++) {
        mock_dmx_slots[i] = 512;
        mock_dmx_refresh[i] = 40;
        mock_dmx_break[i] = 176;
        mock_dmx_mab[i] = 16;
    }
    mock_bridge_port[0] = 502;
    mock_bridge_port[1] = 503;
    for (unsigned i = 0; i < BRIDGES_COUNT; i++) {
        mock_dmx_artnet[i] = false;
        mock_dmx_artnet_univ[i] = 0;
        mock_dmx_sacn[i] = false;
        mock_dmx_sacn_univ[i] = 1;
        mock_dmx_loss_tout[i] = 3;
    }
    mock_setting_items_save_called = 0;
    mock_setting_items_save_bool_called = 0;
    mock_setting_items_save_should_fail = false;
    mock_setting_items_save_bool_should_fail = false;
    mock_setting_items_save_bool_fail_key = NULL;
}
