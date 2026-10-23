#include "dmx_modbus.h"
#include "dmx_output.h"
#include "bridge.h"
#include "mb_device.h"
#include "modbus_helpers.h"
#include "mbtcp_reasm.h"
#include "tcp_server.h"
#include "tcp_desc.h"

#include "esp_log.h"

#include <arpa/inet.h>
#include <string.h>

static const char *TAG = "dmx_mb_srv";

#define DMX_MB_FC_WRITE_SINGLE_REG      0x06u
#define DMX_MB_FC_WRITE_MULTIPLE_REGS   0x10u
#define DMX_MB_MAX_WRITE_REGISTERS      123u    // Modbus spec limit for FC16
#define DMX_MB_CHANNEL_MAX_VALUE        255u
#define DMX_MB_READ_REQ_LEN             (sizeof(mb_tcp_header_t) + 4u)
#define DMX_MB_WRITE_SINGLE_REQ_LEN     (sizeof(mb_tcp_header_t) + 4u)
#define DMX_MB_WRITE_MULTI_MIN_REQ_LEN  (sizeof(mb_tcp_header_t) + 5u)
#define DMX_MB_US_PER_S                 1000000u

typedef struct {
    tcp_desc_t    *tcp_desc;
    int            port;
    mbtcp_reasm_t  reasm;
} dmx_modbus_ctx_t;

static dmx_modbus_ctx_t dmx_mb_ctx[BRIDGES_COUNT];

static unsigned ctx_index_from_desc(const tcp_desc_t *desc)
{
    for (unsigned i = 0; i < BRIDGES_COUNT; i++) {
        if ((dmx_mb_ctx[i].tcp_desc != NULL) && (dmx_mb_ctx[i].tcp_desc == desc)) {
            return i;
        }
    }
    return BRIDGES_COUNT;
}

// Fill the MBAP header of a response; length is set by the caller once the PDU size is known.
static void fill_header(uint8_t *resp, const mb_tcp_header_t *req_hdr, size_t pdu_len_after_unit)
{
    mb_tcp_header_t *hdr = (mb_tcp_header_t *)resp;
    hdr->transaction_id = req_hdr->transaction_id;
    hdr->protocol_id = MODBUS_TCP_PROTOCOL_ID;
    hdr->unit_id = req_hdr->unit_id;
    hdr->function = req_hdr->function;
    hdr->length = htons((uint16_t)(1u + pdu_len_after_unit));   // unit id + fc + data
}

static uint16_t status_register(unsigned port_index, uint16_t addr, const dmx_output_stats_t *st)
{
    switch (addr) {
    case DMX_MB_REG_BLACKOUT:    return 0;
    case DMX_MB_REG_ACTIVE:      return st->active ? 1 : 0;
    case DMX_MB_REG_SLOTS:       return st->slots;
    case DMX_MB_REG_REFRESH_HZ:  return (st->period_us != 0) ? (uint16_t)(DMX_MB_US_PER_S / st->period_us) : 0;
    case DMX_MB_REG_FRAMES_LO:   return (uint16_t)(st->frames_sent & 0xFFFFu);
    case DMX_MB_REG_FRAMES_HI:   return (uint16_t)((st->frames_sent >> 16) & 0xFFFFu);
    case DMX_MB_REG_TX_DISABLED: return st->tx_disabled ? 1 : 0;
    default:                     (void)port_index; return 0;
    }
}

static bool range_in_channels(uint16_t start, uint16_t count)
{
    return ((uint32_t)start + count) <= (uint32_t)(DMX_MB_REG_CHANNEL_BASE + DMX_MB_REG_CHANNEL_COUNT);
}

static bool range_in_status(uint16_t start, uint16_t count)
{
    return (start >= DMX_MB_REG_BLACKOUT) &&
           (((uint32_t)start + count) <= (uint32_t)(DMX_MB_REG_BLACKOUT + DMX_MB_REG_STATUS_COUNT));
}

static size_t build_read_response(unsigned port_index, const mb_tcp_header_t *req_hdr, const uint8_t *pdu,
                                  uint8_t *resp)
{
    uint16_t start = 0;
    uint16_t count = 0;
    modbus_pdu_parse_read_request(pdu, &start, &count);

    if ((count == 0) || (count > MODBUS_MAX_READ_REGISTERS)) {
        return modbus_pdu_build_exception(resp, req_hdr->transaction_id, req_hdr->unit_id, req_hdr->function,
                                          MODBUS_EXC_ILLEGAL_DATA_VALUE);
    }

    uint8_t *payload = resp + sizeof(mb_tcp_header_t);
    payload[0] = (uint8_t)(count * 2u);

    if (range_in_channels(start, count)) {
        uint8_t universe[DMX_FRAME_MAX_SLOTS];
        dmx_output_get_channels(port_index, universe);
        for (uint16_t i = 0; i < count; i++) {
            payload[1 + (i * 2)] = 0;
            payload[2 + (i * 2)] = universe[start - DMX_MB_REG_CHANNEL_BASE + i];
        }
    } else if (range_in_status(start, count)) {
        dmx_output_stats_t st = {0};
        dmx_output_get_stats(port_index, &st);
        for (uint16_t i = 0; i < count; i++) {
            uint16_t value = status_register(port_index, (uint16_t)(start + i), &st);
            payload[1 + (i * 2)] = (uint8_t)(value >> 8);
            payload[2 + (i * 2)] = (uint8_t)(value & 0xFFu);
        }
    } else {
        return modbus_pdu_build_exception(resp, req_hdr->transaction_id, req_hdr->unit_id, req_hdr->function,
                                          MODBUS_EXC_ILLEGAL_ADDRESS);
    }

    fill_header(resp, req_hdr, 1u + 1u + (size_t)payload[0]);
    return sizeof(mb_tcp_header_t) + 1u + (size_t)payload[0];
}

static size_t build_write_single_response(unsigned port_index, const mb_tcp_header_t *req_hdr, const uint8_t *pdu,
                                          uint8_t *resp)
{
    uint16_t addr = (uint16_t)((pdu[0] << 8) | pdu[1]);
    uint16_t value = (uint16_t)((pdu[2] << 8) | pdu[3]);

    if (addr == DMX_MB_REG_BLACKOUT) {
        if (value == 1) {
            dmx_output_blackout(port_index);
        }
    } else if (range_in_channels(addr, 1)) {
        if (value > DMX_MB_CHANNEL_MAX_VALUE) {
            return modbus_pdu_build_exception(resp, req_hdr->transaction_id, req_hdr->unit_id, req_hdr->function,
                                              MODBUS_EXC_ILLEGAL_DATA_VALUE);
        }
        uint8_t v = (uint8_t)value;
        dmx_output_set_channels(port_index, (uint16_t)(addr - DMX_MB_REG_CHANNEL_BASE + 1), &v, 1);
    } else {
        return modbus_pdu_build_exception(resp, req_hdr->transaction_id, req_hdr->unit_id, req_hdr->function,
                                          MODBUS_EXC_ILLEGAL_ADDRESS);
    }

    // The response echoes the request PDU.
    memcpy(resp + sizeof(mb_tcp_header_t), pdu, 4);
    fill_header(resp, req_hdr, 1u + 4u);
    return sizeof(mb_tcp_header_t) + 4u;
}

static size_t build_write_multiple_response(unsigned port_index, const mb_tcp_header_t *req_hdr, const uint8_t *pdu,
                                            size_t pdu_len, uint8_t *resp)
{
    uint16_t addr = (uint16_t)((pdu[0] << 8) | pdu[1]);
    uint16_t count = (uint16_t)((pdu[2] << 8) | pdu[3]);
    uint8_t byte_count = pdu[4];

    if ((count == 0) || (count > DMX_MB_MAX_WRITE_REGISTERS) || (byte_count != (count * 2u)) ||
        (pdu_len < 5u + byte_count)) {
        return modbus_pdu_build_exception(resp, req_hdr->transaction_id, req_hdr->unit_id, req_hdr->function,
                                          MODBUS_EXC_ILLEGAL_DATA_VALUE);
    }
    if (!range_in_channels(addr, count)) {
        return modbus_pdu_build_exception(resp, req_hdr->transaction_id, req_hdr->unit_id, req_hdr->function,
                                          MODBUS_EXC_ILLEGAL_ADDRESS);
    }

    uint8_t values[DMX_MB_MAX_WRITE_REGISTERS];
    for (uint16_t i = 0; i < count; i++) {
        uint16_t value = (uint16_t)((pdu[5 + (i * 2)] << 8) | pdu[6 + (i * 2)]);
        if (value > DMX_MB_CHANNEL_MAX_VALUE) {
            return modbus_pdu_build_exception(resp, req_hdr->transaction_id, req_hdr->unit_id, req_hdr->function,
                                              MODBUS_EXC_ILLEGAL_DATA_VALUE);
        }
        values[i] = (uint8_t)value;
    }
    dmx_output_set_channels(port_index, (uint16_t)(addr - DMX_MB_REG_CHANNEL_BASE + 1), values, count);

    // The response echoes the start address and quantity.
    memcpy(resp + sizeof(mb_tcp_header_t), pdu, 4);
    fill_header(resp, req_hdr, 1u + 4u);
    return sizeof(mb_tcp_header_t) + 4u;
}

size_t dmx_modbus_handle_request(unsigned port_index, const uint8_t *req, size_t req_len, uint8_t *resp)
{
    if ((port_index >= BRIDGES_COUNT) || (modbus_tcp_check_request(req, req_len) != ESP_OK)) {
        return 0;
    }

    mb_tcp_header_t req_hdr;
    memcpy(&req_hdr, req, sizeof(req_hdr));

    if (mb_device_is_self(req_hdr.unit_id)) {
        return mb_device_handle_self_request(req, req_len, resp);
    }

    const uint8_t *pdu = req + sizeof(mb_tcp_header_t);
    size_t pdu_len = req_len - sizeof(mb_tcp_header_t);

    switch (req_hdr.function) {
    case MODBUS_FC_READ_HOLDING_REGS:
    case MODBUS_FC_READ_INPUT_REGS:
        if (req_len < DMX_MB_READ_REQ_LEN) {
            break;
        }
        return build_read_response(port_index, &req_hdr, pdu, resp);

    case DMX_MB_FC_WRITE_SINGLE_REG:
        if (req_len < DMX_MB_WRITE_SINGLE_REQ_LEN) {
            break;
        }
        return build_write_single_response(port_index, &req_hdr, pdu, resp);

    case DMX_MB_FC_WRITE_MULTIPLE_REGS:
        if (req_len < DMX_MB_WRITE_MULTI_MIN_REQ_LEN) {
            break;
        }
        return build_write_multiple_response(port_index, &req_hdr, pdu, pdu_len, resp);

    default:
        return modbus_pdu_build_exception(resp, req_hdr.transaction_id, req_hdr.unit_id, req_hdr.function,
                                          MODBUS_EXC_ILLEGAL_FUNCTION);
    }

    // A supported function code with a PDU too short for it.
    return modbus_pdu_build_exception(resp, req_hdr.transaction_id, req_hdr.unit_id, req_hdr.function,
                                      MODBUS_EXC_ILLEGAL_DATA_VALUE);
}

// ---- TCP receive path --------------------------------------------------------

static void process_one_frame(tcp_desc_t *desc, int client_sock, const uint8_t *data, size_t len)
{
    unsigned index = ctx_index_from_desc(desc);
    if (index >= BRIDGES_COUNT) {
        return;
    }
    uint8_t resp[MODBUS_TCP_MAX_ADU_LEN];
    size_t resp_len = dmx_modbus_handle_request(index, data, len, resp);
    if (resp_len == 0) {
        ESP_LOGW(TAG, "Port[%u] sock=%d: invalid Modbus TCP framing (%u bytes)", index + 1, client_sock,
                 (unsigned)len);
        return;
    }
    tcp_server_send(desc, client_sock, resp, resp_len);
}

static bool on_reasm_frame(void *user_ctx, int sock, const uint8_t *frame, size_t len)
{
    process_one_frame((tcp_desc_t *)user_ctx, sock, frame, len);
    return true;
}

static void process_data_from_tcp(tcp_desc_t *desc, int client_sock, uint8_t *data, size_t len)
{
    unsigned index = ctx_index_from_desc(desc);
    if (index >= BRIDGES_COUNT) {
        return;
    }
    int rc = mbtcp_reasm_feed(&dmx_mb_ctx[index].reasm, client_sock, data, len, on_reasm_frame, desc);
    if (rc == MBTCP_REASM_NO_SLOT) {
        // Best effort without a reassembly slot: treat the recv() buffer as one frame.
        process_one_frame(desc, client_sock, data, len);
    }
}

static void on_conn_close(tcp_desc_t *desc, int client_sock)
{
    unsigned index = ctx_index_from_desc(desc);
    if (index < BRIDGES_COUNT) {
        mbtcp_reasm_close(&dmx_mb_ctx[index].reasm, client_sock);
    }
}

// ---- Public API ----------------------------------------------------------------

esp_err_t dmx_modbus_init(unsigned port_index, int tcp_port)
{
    if (port_index >= BRIDGES_COUNT) {
        return ESP_ERR_INVALID_ARG;
    }
    dmx_modbus_ctx_t *ctx = &dmx_mb_ctx[port_index];

    if (ctx->tcp_desc != NULL) {
        if (ctx->port == tcp_port) {
            return ESP_OK;
        }
        ESP_LOGW(TAG, "Port[%u]: re-init on TCP port %d -> %d, restarting", port_index + 1, ctx->port, tcp_port);
        esp_err_t de = dmx_modbus_deinit(port_index);
        if (de != ESP_OK) {
            return de;
        }
    }

    ESP_LOGI(TAG, "Port[%u]: starting DMX Modbus TCP server on port %d", port_index + 1, tcp_port);
    if (!mbtcp_reasm_init(&ctx->reasm, TAG)) {
        ESP_LOGE(TAG, "Port[%u]: failed to create the reassembly mutex", port_index + 1);
        return ESP_ERR_NO_MEM;
    }
    esp_err_t ret = tcp_server_init(tcp_port, process_data_from_tcp, &ctx->tcp_desc);
    if (ret != ESP_OK) {
        mbtcp_reasm_deinit(&ctx->reasm);
        ctx->tcp_desc = NULL;
        return ret;
    }
    ctx->port = tcp_port;
    ctx->tcp_desc->close_handler = on_conn_close;
    return ESP_OK;
}

esp_err_t dmx_modbus_deinit(unsigned port_index)
{
    if (port_index >= BRIDGES_COUNT) {
        return ESP_ERR_INVALID_ARG;
    }
    dmx_modbus_ctx_t *ctx = &dmx_mb_ctx[port_index];
    if (ctx->tcp_desc == NULL) {
        return ESP_OK;
    }
    esp_err_t ret = tcp_server_deinit(ctx->tcp_desc);   // waits for the receiver tasks to exit
    if (ret == ESP_OK) {
        mbtcp_reasm_deinit(&ctx->reasm);
        ctx->tcp_desc = NULL;
        ctx->port = 0;
    }
    return ret;
}

int dmx_modbus_get_port(unsigned port_index)
{
    if (port_index >= BRIDGES_COUNT) {
        return 0;
    }
    return dmx_mb_ctx[port_index].port;
}
