#include "dmx_http.h"
#include "dmx_output.h"
#include "bridge.h"
#include "auth.h"
#include "json_utils.h"

#include "cJSON.h"
#include "esp_check.h"
#include "esp_log.h"

#include <stdlib.h>

#define DMX_HTTP_VALUE_MAX          255

static const char *TAG = "dmx_http";

static cJSON *dmx_http_build_state(unsigned port_index, bool with_values)
{
    dmx_output_stats_t stats = {0};
    dmx_output_get_stats(port_index, &stats);

    cJSON *resp = cJSON_CreateObject();
    if (resp == NULL) {
        return NULL;
    }
    cJSON_AddBoolToObject(resp, "active", stats.active);
    cJSON_AddBoolToObject(resp, "tx_disabled", stats.tx_disabled);
    cJSON_AddNumberToObject(resp, "slots", stats.slots);
    cJSON_AddNumberToObject(resp, "period_us", (double)stats.period_us);
    cJSON_AddNumberToObject(resp, "frames_sent", (double)stats.frames_sent);
    cJSON_AddNumberToObject(resp, "uptime_ms", (double)stats.uptime_ms);

    if (with_values) {
        uint8_t universe[DMX_FRAME_MAX_SLOTS];
        if (dmx_output_get_channels(port_index, universe) == ESP_OK) {
            int values[DMX_FRAME_MAX_SLOTS];
            for (size_t i = 0; i < DMX_FRAME_MAX_SLOTS; i++) {
                values[i] = universe[i];
            }
            cJSON *values_json = cJSON_CreateIntArray(values, DMX_FRAME_MAX_SLOTS);
            if (values_json != NULL) {
                cJSON_AddItemToObject(resp, "values", values_json);
            }
        }
    }
    return resp;
}

static esp_err_t dmx_get_handler(httpd_req_t *req, unsigned port_index)
{
    if (!auth_middleware_check(req)) {
        return ESP_OK;
    }
    cJSON *resp = dmx_http_build_state(port_index, true);
    if (resp == NULL) {
        return json_utils_send_error_status(req, "500 Internal Server Error", "Out of memory");
    }
    json_utils_send_response(req, NULL, resp);
    return ESP_OK;
}

// Parse "values" into out; returns the count, or -1 with an error sent to the client.
static int dmx_http_parse_values(httpd_req_t *req, cJSON *req_json, cJSON *values_item, uint8_t *out)
{
    if (!cJSON_IsArray(values_item)) {
        cJSON_Delete(req_json);
        json_utils_send_error(req, "'values' must be an array");
        return -1;
    }
    int count = cJSON_GetArraySize(values_item);
    if ((count < 1) || (count > DMX_FRAME_MAX_SLOTS)) {
        cJSON_Delete(req_json);
        json_utils_send_error(req, "'values' must hold 1..512 items");
        return -1;
    }
    int i = 0;
    cJSON *item = NULL;
    cJSON_ArrayForEach(item, values_item) {
        if (!cJSON_IsNumber(item) || (item->valuedouble < 0) || (item->valuedouble > DMX_HTTP_VALUE_MAX)) {
            cJSON_Delete(req_json);
            json_utils_send_error(req, "'values' items must be integers 0..255");
            return -1;
        }
        out[i++] = (uint8_t)item->valueint;
    }
    return count;
}

static esp_err_t dmx_post_handler(httpd_req_t *req, unsigned port_index)
{
    if (!auth_middleware_check(req)) {
        return ESP_OK;
    }

    cJSON *req_json = json_utils_receive_json(req);
    if (req_json == NULL) {
        return json_utils_send_error(req, "Invalid JSON");
    }

    cJSON *blackout_item = cJSON_GetObjectItem(req_json, "blackout");
    cJSON *values_item = cJSON_GetObjectItem(req_json, "values");
    if ((blackout_item == NULL) && (values_item == NULL)) {
        cJSON_Delete(req_json);
        return json_utils_send_error(req, "Expected 'values' and/or 'blackout'");
    }

    if (blackout_item != NULL) {
        if (!cJSON_IsBool(blackout_item)) {
            cJSON_Delete(req_json);
            return json_utils_send_error(req, "'blackout' must be a boolean");
        }
        if (cJSON_IsTrue(blackout_item)) {
            dmx_output_blackout(port_index);
        }
    }

    if (values_item != NULL) {
        int start = 1;
        cJSON *start_item = cJSON_GetObjectItem(req_json, "start");
        if (start_item != NULL) {
            if (!cJSON_IsNumber(start_item) || (start_item->valueint < 1) ||
                (start_item->valueint > DMX_FRAME_MAX_SLOTS)) {
                cJSON_Delete(req_json);
                return json_utils_send_error(req, "'start' must be 1..512");
            }
            start = start_item->valueint;
        }

        uint8_t values[DMX_FRAME_MAX_SLOTS];
        int count = dmx_http_parse_values(req, req_json, values_item, values);
        if (count < 0) {
            return ESP_OK;   // error already sent
        }
        if (start - 1 + count > DMX_FRAME_MAX_SLOTS) {
            cJSON_Delete(req_json);
            return json_utils_send_error(req, "'start' + 'values' length exceeds 512 channels");
        }
        dmx_output_set_channels(port_index, (uint16_t)start, values, (size_t)count);
    }

    cJSON *resp = dmx_http_build_state(port_index, false);
    json_utils_send_response(req, req_json, resp);
    return ESP_OK;
}

static esp_err_t port1_dmx_get_handler(httpd_req_t *req)  { return dmx_get_handler(req, 0); }
static esp_err_t port2_dmx_get_handler(httpd_req_t *req)  { return dmx_get_handler(req, 1); }
static esp_err_t port1_dmx_post_handler(httpd_req_t *req) { return dmx_post_handler(req, 0); }
static esp_err_t port2_dmx_post_handler(httpd_req_t *req) { return dmx_post_handler(req, 1); }

static const httpd_uri_t uri_port1_dmx_get = {
    .uri = "/ports/1/dmx", .method = HTTP_GET, .handler = port1_dmx_get_handler,
};
static const httpd_uri_t uri_port2_dmx_get = {
    .uri = "/ports/2/dmx", .method = HTTP_GET, .handler = port2_dmx_get_handler,
};
static const httpd_uri_t uri_port1_dmx_post = {
    .uri = "/ports/1/dmx", .method = HTTP_POST, .handler = port1_dmx_post_handler,
};
static const httpd_uri_t uri_port2_dmx_post = {
    .uri = "/ports/2/dmx", .method = HTTP_POST, .handler = port2_dmx_post_handler,
};

esp_err_t dmx_http_register_handlers(httpd_handle_t server)
{
    ESP_RETURN_ON_ERROR(httpd_register_uri_handler(server, &uri_port1_dmx_get),
                        TAG, "Failed to register GET /ports/1/dmx");
    ESP_RETURN_ON_ERROR(httpd_register_uri_handler(server, &uri_port2_dmx_get),
                        TAG, "Failed to register GET /ports/2/dmx");
    ESP_RETURN_ON_ERROR(httpd_register_uri_handler(server, &uri_port1_dmx_post),
                        TAG, "Failed to register POST /ports/1/dmx");
    ESP_RETURN_ON_ERROR(httpd_register_uri_handler(server, &uri_port2_dmx_post),
                        TAG, "Failed to register POST /ports/2/dmx");
    ESP_LOGI(TAG, "HTTP handlers registered");
    return ESP_OK;
}
