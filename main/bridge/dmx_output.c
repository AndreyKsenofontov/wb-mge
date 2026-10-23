#include "dmx_output.h"
#include "bridge.h"

#include "driver/gpio.h"
#include "driver/uart.h"
#include "esp_check.h"
#include "esp_log.h"
#include "esp_rom_sys.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#include <string.h>

#define DMX_OUTPUT_TASK_STACK_SIZE      (1024 * 3)
#define DMX_OUTPUT_TASK_PRIORITY        12          // same as the serial event tasks
// RX is unused (/RE is tied to DE, which stays HIGH), but uart_driver_install() needs an RX
// buffer larger than the hardware FIFO.
#define DMX_OUTPUT_UART_RX_BUF_SIZE     (UART_HW_FIFO_LEN(0) * 2)
#define DMX_OUTPUT_STOP_TIMEOUT_MS      2000
#define DMX_OUTPUT_US_PER_MS            1000

static const char *TAG = "dmx_output";

typedef struct {
    bool               active;
    uart_port_t        port_num;
    int                dir_pin;
    dmx_frame_config_t cfg;
    uint32_t           period_us;
    TaskHandle_t       task;
    SemaphoreHandle_t  task_stopped;    // given by the frame task right before it deletes itself
    bool               stop_requested;  // atomic builtins only: written by deinit, read by the task
    bool               tx_disabled;
    int64_t            started_us;
    // Fields below are protected by data_mutex.
    SemaphoreHandle_t  data_mutex;
    uint8_t            universe[DMX_FRAME_MAX_SLOTS];
    uint64_t           frames_sent;
} dmx_output_ctx_t;

static dmx_output_ctx_t dmx_ctx[BRIDGES_COUNT];

// Packet buffer of the frame task: start code + slots. Only the task touches it.
static uint8_t dmx_tx_frame[BRIDGES_COUNT][1 + DMX_FRAME_MAX_SLOTS];

static void dmx_output_task(void *arg);

void dmx_output_init(void)
{
    for (unsigned i = 0; i < BRIDGES_COUNT; i++) {
        if (dmx_ctx[i].data_mutex == NULL) {
            dmx_ctx[i].data_mutex = xSemaphoreCreateMutex();
        }
    }
}

static esp_err_t dmx_output_check_index(unsigned index)
{
    if (index >= BRIDGES_COUNT) {
        return ESP_ERR_INVALID_ARG;
    }
    // Lazily create the mutex too, so callers that skip dmx_output_init() still work.
    if (dmx_ctx[index].data_mutex == NULL) {
        dmx_output_init();
    }
    return (dmx_ctx[index].data_mutex != NULL) ? ESP_OK : ESP_ERR_NO_MEM;
}

static void dmx_output_drive_de(dmx_output_ctx_t *ctx, bool enabled)
{
    // Set the level before the direction so the pin never drives an undefined level.
    gpio_set_level(ctx->dir_pin, enabled ? 1 : 0);
    gpio_set_direction(ctx->dir_pin, GPIO_MODE_OUTPUT);
}

static esp_err_t dmx_output_configure_uart(const serial_config_t *pins)
{
    const uart_config_t uart_config = {
        .baud_rate = DMX_FRAME_BAUDRATE,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_2,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };

    ESP_RETURN_ON_ERROR(uart_param_config(pins->port_num, &uart_config), TAG, "uart_param_config failed");
    // RTS is left unrouted: DE is a plain GPIO in DMX mode (see dmx_output_drive_de()).
    ESP_RETURN_ON_ERROR(uart_set_pin(pins->port_num, pins->tx_pin, pins->rx_pin, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE),
                        TAG, "uart_set_pin failed");
    // Plain UART mode: RS485 half-duplex would drop DE after every packet and leave the line
    // floating during MAB and between packets.
    ESP_RETURN_ON_ERROR(uart_set_mode(pins->port_num, UART_MODE_UART), TAG, "uart_set_mode failed");
    return ESP_OK;
}

esp_err_t dmx_output_init_port(unsigned index, const serial_config_t *pins, const dmx_frame_config_t *cfg,
                               bool tx_disabled)
{
    ESP_RETURN_ON_ERROR(dmx_output_check_index(index), TAG, "Port[%u]: invalid port", index + 1);
    ESP_RETURN_ON_FALSE((pins != NULL) && dmx_frame_config_valid(cfg), ESP_ERR_INVALID_ARG,
                        TAG, "Port[%u]: invalid DMX config", index + 1);

    dmx_output_ctx_t *ctx = &dmx_ctx[index];
    ESP_RETURN_ON_FALSE(!ctx->active, ESP_ERR_INVALID_STATE, TAG, "Port[%u]: DMX output already running", index + 1);

    ctx->task_stopped = xSemaphoreCreateBinary();
    ESP_RETURN_ON_FALSE(ctx->task_stopped != NULL, ESP_ERR_NO_MEM, TAG, "Port[%u]: no memory", index + 1);

    esp_err_t err = uart_driver_install(pins->port_num, DMX_OUTPUT_UART_RX_BUF_SIZE, 0, 0, NULL, 0);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Port[%u]: uart_driver_install failed: %s", index + 1, esp_err_to_name(err));
        vSemaphoreDelete(ctx->task_stopped);
        ctx->task_stopped = NULL;
        return err;
    }
    err = dmx_output_configure_uart(pins);
    if (err != ESP_OK) {
        uart_driver_delete(pins->port_num);
        vSemaphoreDelete(ctx->task_stopped);
        ctx->task_stopped = NULL;
        return err;
    }

    ctx->port_num = pins->port_num;
    ctx->dir_pin = pins->dir_pin;
    ctx->cfg = *cfg;
    ctx->period_us = dmx_frame_period_us(cfg);
    ctx->tx_disabled = tx_disabled;
    gpio_reset_pin(ctx->dir_pin);
    dmx_output_drive_de(ctx, !tx_disabled);

    xSemaphoreTake(ctx->data_mutex, portMAX_DELAY);
    ctx->frames_sent = 0;
    xSemaphoreGive(ctx->data_mutex);

    __atomic_store_n(&ctx->stop_requested, false, __ATOMIC_RELEASE);
    ctx->started_us = esp_timer_get_time();
    ctx->active = true;

    if (xTaskCreate(dmx_output_task, "dmx_output", DMX_OUTPUT_TASK_STACK_SIZE, (void *)(uintptr_t)index,
                    DMX_OUTPUT_TASK_PRIORITY, &ctx->task) != pdPASS) {
        ESP_LOGE(TAG, "Port[%u]: unable to create frame task", index + 1);
        ctx->active = false;
        dmx_output_drive_de(ctx, false);
        uart_driver_delete(ctx->port_num);
        vSemaphoreDelete(ctx->task_stopped);
        ctx->task_stopped = NULL;
        return ESP_ERR_NO_MEM;
    }

    ESP_LOGI(TAG, "Port[%u]: DMX output started: %u slots, period %lu us, break %u us, MAB %u us%s",
             index + 1, cfg->slots, (unsigned long)ctx->period_us, cfg->break_us, cfg->mab_us,
             tx_disabled ? " (TX disabled)" : "");
    return ESP_OK;
}

esp_err_t dmx_output_deinit_port(unsigned index)
{
    ESP_RETURN_ON_ERROR(dmx_output_check_index(index), TAG, "Port[%u]: invalid port", index + 1);
    dmx_output_ctx_t *ctx = &dmx_ctx[index];
    if (!ctx->active) {
        return ESP_OK;
    }

    __atomic_store_n(&ctx->stop_requested, true, __ATOMIC_RELEASE);
    if (xSemaphoreTake(ctx->task_stopped, pdMS_TO_TICKS(DMX_OUTPUT_STOP_TIMEOUT_MS)) != pdTRUE) {
        // The task is stuck inside the UART driver; deleting the driver under it would crash.
        ESP_LOGE(TAG, "Port[%u]: frame task did not stop", index + 1);
        return ESP_ERR_TIMEOUT;
    }
    vSemaphoreDelete(ctx->task_stopped);
    ctx->task_stopped = NULL;
    ctx->task = NULL;

    dmx_output_drive_de(ctx, false);
    uart_driver_delete(ctx->port_num);
    ctx->active = false;
    ctx->started_us = 0;
    ESP_LOGI(TAG, "Port[%u]: DMX output stopped", index + 1);
    return ESP_OK;
}

esp_err_t dmx_output_set_tx_disabled(unsigned index, bool disabled)
{
    ESP_RETURN_ON_ERROR(dmx_output_check_index(index), TAG, "Port[%u]: invalid port", index + 1);
    dmx_output_ctx_t *ctx = &dmx_ctx[index];
    if (!ctx->active) {
        return ESP_OK;
    }
    dmx_output_drive_de(ctx, !disabled);
    ctx->tx_disabled = disabled;
    return ESP_OK;
}

esp_err_t dmx_output_set_channels(unsigned index, uint16_t start_channel, const uint8_t *values, size_t count)
{
    ESP_RETURN_ON_ERROR(dmx_output_check_index(index), TAG, "Port[%u]: invalid port", index + 1);
    if ((values == NULL) || (start_channel < 1) || (start_channel > DMX_FRAME_MAX_SLOTS)) {
        return ESP_ERR_INVALID_ARG;
    }
    xSemaphoreTake(dmx_ctx[index].data_mutex, portMAX_DELAY);
    dmx_frame_write_channels(dmx_ctx[index].universe, start_channel, values, count);
    xSemaphoreGive(dmx_ctx[index].data_mutex);
    return ESP_OK;
}

esp_err_t dmx_output_get_channels(unsigned index, uint8_t *out)
{
    ESP_RETURN_ON_ERROR(dmx_output_check_index(index), TAG, "Port[%u]: invalid port", index + 1);
    if (out == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    xSemaphoreTake(dmx_ctx[index].data_mutex, portMAX_DELAY);
    memcpy(out, dmx_ctx[index].universe, DMX_FRAME_MAX_SLOTS);
    xSemaphoreGive(dmx_ctx[index].data_mutex);
    return ESP_OK;
}

esp_err_t dmx_output_blackout(unsigned index)
{
    ESP_RETURN_ON_ERROR(dmx_output_check_index(index), TAG, "Port[%u]: invalid port", index + 1);
    xSemaphoreTake(dmx_ctx[index].data_mutex, portMAX_DELAY);
    memset(dmx_ctx[index].universe, 0, DMX_FRAME_MAX_SLOTS);
    xSemaphoreGive(dmx_ctx[index].data_mutex);
    return ESP_OK;
}

void dmx_output_get_stats(unsigned index, dmx_output_stats_t *out)
{
    if (out == NULL) {
        return;
    }
    memset(out, 0, sizeof(*out));
    if (dmx_output_check_index(index) != ESP_OK) {
        return;
    }
    dmx_output_ctx_t *ctx = &dmx_ctx[index];
    out->active = ctx->active;
    if (!out->active) {
        return;
    }
    out->tx_disabled = ctx->tx_disabled;
    out->slots = ctx->cfg.slots;
    out->period_us = ctx->period_us;
    out->uptime_ms = (uint64_t)(esp_timer_get_time() - ctx->started_us) / DMX_OUTPUT_US_PER_MS;
    xSemaphoreTake(ctx->data_mutex, portMAX_DELAY);
    out->frames_sent = ctx->frames_sent;
    xSemaphoreGive(ctx->data_mutex);
}

static void dmx_output_send_packet(dmx_output_ctx_t *ctx, const uint8_t *frame, size_t len)
{
    // The previous packet must have left the shift register before BREAK is forced.
    uart_wait_tx_done(ctx->port_num, portMAX_DELAY);

    // BREAK: invert the idle (mark) level of TX to space. A preemption here only stretches
    // BREAK or MAB, which E1.11 allows up to 1 s.
    uart_set_line_inverse(ctx->port_num, UART_SIGNAL_TXD_INV);
    esp_rom_delay_us(ctx->cfg.break_us);
    uart_set_line_inverse(ctx->port_num, UART_SIGNAL_INV_DISABLE);
    esp_rom_delay_us(ctx->cfg.mab_us);

    uart_write_bytes(ctx->port_num, frame, len);
}

static TickType_t dmx_output_ticks_until(int64_t deadline_us)
{
    int64_t remaining_us = deadline_us - esp_timer_get_time();
    if (remaining_us <= 0) {
        return 1;   // always yield at least one tick so lower-priority tasks and IDLE run
    }
    TickType_t ticks = (TickType_t)((remaining_us + (portTICK_PERIOD_MS * DMX_OUTPUT_US_PER_MS) - 1)
                                    / (portTICK_PERIOD_MS * DMX_OUTPUT_US_PER_MS));
    return (ticks > 0) ? ticks : 1;
}

static void dmx_output_task(void *arg)
{
    unsigned index = (unsigned)(uintptr_t)arg;
    dmx_output_ctx_t *ctx = &dmx_ctx[index];
    uint8_t *frame = dmx_tx_frame[index];
    size_t frame_len = 1 + ctx->cfg.slots;
    int64_t deadline_us = esp_timer_get_time();

    frame[0] = DMX_FRAME_START_CODE;

    while (!__atomic_load_n(&ctx->stop_requested, __ATOMIC_ACQUIRE)) {
        xSemaphoreTake(ctx->data_mutex, portMAX_DELAY);
        memcpy(&frame[1], ctx->universe, ctx->cfg.slots);
        ctx->frames_sent++;
        xSemaphoreGive(ctx->data_mutex);

        dmx_output_send_packet(ctx, frame, frame_len);

        deadline_us += ctx->period_us;
        int64_t now_us = esp_timer_get_time();
        if (deadline_us < now_us) {
            // Fell behind (long preemption): restart the schedule instead of bursting.
            deadline_us = now_us;
        }
        vTaskDelay(dmx_output_ticks_until(deadline_us));
    }

    uart_wait_tx_done(ctx->port_num, portMAX_DELAY);
    xSemaphoreGive(ctx->task_stopped);
    vTaskDelete(NULL);
}
