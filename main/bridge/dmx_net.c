#include "dmx_net.h"
#include "dmx_net_proto.h"
#include "dmx_output.h"
#include "bridge.h"
#include "setting_items.h"

#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "lwip/sockets.h"

#include <string.h>

static const char *TAG = "dmx_net";

#define DMX_NET_TASK_STACK_SIZE     (1024 * 5)
#define DMX_NET_TASK_PRIORITY       6           // above the TCP servers, below the frame task
#define DMX_NET_SELECT_TIMEOUT_MS   250
#define DMX_NET_ACTIVE_WINDOW_US    2500000LL   // E1.31 source timeout: 2.5 s
#define DMX_NET_STOP_TIMEOUT_MS     3000
#define DMX_NET_RX_BUF_SIZE         700         // the largest E1.31 data packet is 638 bytes
#define DMX_NET_US_PER_S            1000000LL
#define DMX_NET_LONG_NAME           "WB-MGU DMX gateway"
#define EVENT_TASK_FINISHED         BIT0

typedef struct {
    bool                  registered;
    dmx_net_port_config_t cfg;
    dmx_net_stats_t       stats;
    int64_t               artnet_last_rx_us;
    int64_t               sacn_last_rx_us;
    int64_t               last_rx_us;         // either protocol
    bool                  had_signal;         // a packet arrived since the port started
    uint8_t               sacn_cid[SACN_CID_LEN];
    int64_t               second_start_us;    // fps window
    uint32_t              artnet_in_window;
    uint32_t              sacn_in_window;
} dmx_net_port_t;

static dmx_net_port_t s_ports[BRIDGES_COUNT];
static SemaphoreHandle_t s_lock;
static TaskHandle_t s_task;
static EventGroupHandle_t s_task_events;
static bool s_stop_requested;                 // atomic builtins only
static int s_artnet_sock = -1;
static int s_sacn_sock = -1;
static uint16_t s_sacn_groups[BRIDGES_COUNT]; // universes joined on s_sacn_sock, 0 = none

static void dmx_net_task(void *arg);

void dmx_net_init(void)
{
    if (s_lock == NULL) {
        s_lock = xSemaphoreCreateMutex();
    }
}

bool dmx_net_config_equal(const dmx_net_port_config_t *a, const dmx_net_port_config_t *b)
{
    return (a->artnet_enabled == b->artnet_enabled) && (a->artnet_universe == b->artnet_universe) &&
           (a->sacn_enabled == b->sacn_enabled) && (a->sacn_universe == b->sacn_universe) &&
           (a->loss_policy == b->loss_policy) && (a->loss_timeout_s == b->loss_timeout_s);
}

static void lock(void)
{
    if (s_lock == NULL) {
        dmx_net_init();
    }
    xSemaphoreTake(s_lock, portMAX_DELAY);
}

static void unlock(void)
{
    xSemaphoreGive(s_lock);
}

// ---- sockets -----------------------------------------------------------------------------------

static int open_udp_socket(uint16_t port)
{
    int sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_IP);
    if (sock < 0) {
        ESP_LOGE(TAG, "socket() failed: errno %d", errno);
        return -1;
    }
    int opt = 1;
    setsockopt(sock, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));
    struct sockaddr_in addr = {
        .sin_family = AF_INET,
        .sin_port = htons(port),
        .sin_addr.s_addr = htonl(INADDR_ANY),
    };
    if (bind(sock, (struct sockaddr *)&addr, sizeof(addr)) != 0) {
        ESP_LOGE(TAG, "bind(%u) failed: errno %d", port, errno);
        close(sock);
        return -1;
    }
    return sock;
}

static void sacn_membership(int sock, uint16_t universe, bool join)
{
    uint8_t group[4];
    sacn_multicast_group(universe, group);
    struct ip_mreq mreq = {0};
    memcpy(&mreq.imr_multiaddr.s_addr, group, 4);
    mreq.imr_interface.s_addr = htonl(INADDR_ANY);
    int opt = join ? IP_ADD_MEMBERSHIP : IP_DROP_MEMBERSHIP;
    if (setsockopt(sock, IPPROTO_IP, opt, &mreq, sizeof(mreq)) != 0) {
        ESP_LOGW(TAG, "%s multicast group for universe %u failed: errno %d", join ? "joining" : "leaving",
                 universe, errno);
    } else {
        ESP_LOGI(TAG, "%s 239.255.%u.%u (sACN universe %u)", join ? "joined" : "left", group[2], group[3], universe);
    }
}

// Bring the sockets and multicast memberships in line with the registered ports. Called under
// the lock from the receiver task, so the task's own select() never races a socket change.
static void sync_sockets_locked(void)
{
    bool want_artnet = false;
    bool want_sacn = false;
    for (unsigned i = 0; i < BRIDGES_COUNT; i++) {
        if (s_ports[i].registered && s_ports[i].cfg.artnet_enabled) {
            want_artnet = true;
        }
        if (s_ports[i].registered && s_ports[i].cfg.sacn_enabled) {
            want_sacn = true;
        }
    }
    if (want_artnet && (s_artnet_sock < 0)) {
        s_artnet_sock = open_udp_socket(DMX_NET_ARTNET_PORT);
    } else if (!want_artnet && (s_artnet_sock >= 0)) {
        close(s_artnet_sock);
        s_artnet_sock = -1;
    }
    if (want_sacn && (s_sacn_sock < 0)) {
        s_sacn_sock = open_udp_socket(DMX_NET_SACN_PORT);
        memset(s_sacn_groups, 0, sizeof(s_sacn_groups));
    } else if (!want_sacn && (s_sacn_sock >= 0)) {
        close(s_sacn_sock);
        s_sacn_sock = -1;
        memset(s_sacn_groups, 0, sizeof(s_sacn_groups));
    }
    if (s_sacn_sock >= 0) {
        for (unsigned i = 0; i < BRIDGES_COUNT; i++) {
            uint16_t want = (s_ports[i].registered && s_ports[i].cfg.sacn_enabled) ? s_ports[i].cfg.sacn_universe : 0;
            if (want == s_sacn_groups[i]) {
                continue;
            }
            if (s_sacn_groups[i] != 0) {
                sacn_membership(s_sacn_sock, s_sacn_groups[i], false);
            }
            if (want != 0) {
                sacn_membership(s_sacn_sock, want, true);
            }
            s_sacn_groups[i] = want;
        }
    }
}

static bool s_resync_requested;               // atomic builtins only

// ---- packet handling ---------------------------------------------------------------------------

static void note_source(dmx_net_proto_stats_t *st, const struct sockaddr_in *from)
{
    char ip[16];
    inet_ntoa_r(from->sin_addr, ip, sizeof(ip));
    snprintf(st->last_source, sizeof(st->last_source), "%s:%u", ip, (unsigned)ntohs(from->sin_port));
}

static void accept_slots(unsigned index, dmx_net_port_t *port, dmx_net_proto_stats_t *st, const uint8_t *data,
                         uint16_t len, const struct sockaddr_in *from, int64_t now_us)
{
    dmx_output_set_channels(index, 1, data, len);
    st->packets++;
    note_source(st, from);
    port->last_rx_us = now_us;
    port->had_signal = true;
    port->stats.signal_lost = false;
}

static void send_poll_reply(const struct sockaddr_in *to)
{
    uint8_t ip[4] = {0};
    uint8_t mac[6] = {0};
    esp_netif_t *netif = esp_netif_get_default_netif();
    if (netif != NULL) {
        esp_netif_ip_info_t info;
        if (esp_netif_get_ip_info(netif, &info) == ESP_OK) {
            memcpy(ip, &info.ip.addr, 4);
        }
    }
    esp_read_mac(mac, ESP_MAC_ETH);
    char hostname[SETTING_ITEM_MAX_STR_LEN] = {0};
    setting_items_read(KEY_HOSTNAME, hostname);

    // One reply per registered Art-Net port, so a controller sees every output.
    for (unsigned i = 0; i < BRIDGES_COUNT; i++) {
        if (!s_ports[i].registered || !s_ports[i].cfg.artnet_enabled) {
            continue;
        }
        uint8_t reply[ARTNET_POLL_REPLY_LEN];
        artnet_build_poll_reply(reply, ip, mac, s_ports[i].cfg.artnet_universe, hostname, DMX_NET_LONG_NAME);
        reply[211] = (uint8_t)(i + 1);   // BindIndex distinguishes the two ports
        sendto(s_artnet_sock, reply, sizeof(reply), 0, (const struct sockaddr *)to, sizeof(*to));
    }
}

static void handle_artnet(const uint8_t *buf, size_t len, const struct sockaddr_in *from, int64_t now_us)
{
    artnet_packet_t pkt;
    if (!artnet_parse(buf, len, &pkt)) {
        return;
    }
    if (pkt.type == ARTNET_PACKET_POLL) {
        send_poll_reply(from);
        return;
    }
    if (pkt.type != ARTNET_PACKET_DMX) {
        return;
    }
    for (unsigned i = 0; i < BRIDGES_COUNT; i++) {
        dmx_net_port_t *port = &s_ports[i];
        if (!port->registered || !port->cfg.artnet_enabled || (port->cfg.artnet_universe != pkt.port_address)) {
            continue;
        }
        accept_slots(i, port, &port->stats.artnet, pkt.data, pkt.length, from, now_us);
        port->artnet_last_rx_us = now_us;
        port->artnet_in_window++;
    }
}

static void handle_sacn(const uint8_t *buf, size_t len, const struct sockaddr_in *from, int64_t now_us)
{
    sacn_packet_t pkt;
    if (!sacn_parse(buf, len, &pkt) || (pkt.start_code != 0) || (pkt.options & SACN_OPT_PREVIEW_DATA)) {
        return;
    }
    for (unsigned i = 0; i < BRIDGES_COUNT; i++) {
        dmx_net_port_t *port = &s_ports[i];
        if (!port->registered || !port->cfg.sacn_enabled || (port->cfg.sacn_universe != pkt.universe)) {
            continue;
        }
        dmx_net_proto_stats_t *st = &port->stats.sacn;
        bool same_source = (memcmp(port->sacn_cid, pkt.cid, SACN_CID_LEN) == 0);
        bool source_expired = (now_us - port->sacn_last_rx_us) > DMX_NET_ACTIVE_WINDOW_US;
        if (pkt.options & SACN_OPT_STREAM_TERMINATED) {
            if (same_source) {
                port->sacn_last_rx_us = 0;   // release: the next source takes over at once
            }
            continue;
        }
        // Follow one source per universe: the current one, or a higher-priority / fresh one.
        if (!same_source && !source_expired && (pkt.priority <= st->priority)) {
            continue;
        }
        if (!same_source) {
            memcpy(port->sacn_cid, pkt.cid, SACN_CID_LEN);
            st->priority = pkt.priority;
        }
        accept_slots(i, port, st, pkt.data, pkt.length, from, now_us);
        port->sacn_last_rx_us = now_us;
        port->sacn_in_window++;
    }
}

static void housekeeping(int64_t now_us)
{
    for (unsigned i = 0; i < BRIDGES_COUNT; i++) {
        dmx_net_port_t *port = &s_ports[i];
        if (!port->registered) {
            continue;
        }
        port->stats.artnet.active = (now_us - port->artnet_last_rx_us) < DMX_NET_ACTIVE_WINDOW_US;
        port->stats.sacn.active = (now_us - port->sacn_last_rx_us) < DMX_NET_ACTIVE_WINDOW_US;
        if (now_us - port->second_start_us >= DMX_NET_US_PER_S) {
            port->stats.artnet.fps = port->artnet_in_window;
            port->stats.sacn.fps = port->sacn_in_window;
            port->artnet_in_window = 0;
            port->sacn_in_window = 0;
            port->second_start_us = now_us;
        }
        if (port->had_signal && !port->stats.signal_lost &&
            (now_us - port->last_rx_us) > (int64_t)port->cfg.loss_timeout_s * DMX_NET_US_PER_S) {
            port->stats.signal_lost = true;
            if (port->cfg.loss_policy == DMX_LOSS_BLACKOUT) {
                ESP_LOGW(TAG, "Port[%u]: no network DMX for %u s, blackout", i + 1, port->cfg.loss_timeout_s);
                dmx_output_blackout(i);
            } else {
                ESP_LOGW(TAG, "Port[%u]: no network DMX for %u s, holding last values", i + 1,
                         port->cfg.loss_timeout_s);
            }
        }
    }
}

static void dmx_net_task(void *arg)
{
    (void)arg;
    static uint8_t rx_buf[DMX_NET_RX_BUF_SIZE];
    ESP_LOGI(TAG, "receiver task started");

    while (!__atomic_load_n(&s_stop_requested, __ATOMIC_ACQUIRE)) {
        lock();
        if (__atomic_exchange_n(&s_resync_requested, false, __ATOMIC_ACQ_REL)) {
            sync_sockets_locked();
        }
        int artnet_sock = s_artnet_sock;
        int sacn_sock = s_sacn_sock;
        unlock();

        fd_set readfds;
        FD_ZERO(&readfds);
        int maxfd = -1;
        if (artnet_sock >= 0) {
            FD_SET(artnet_sock, &readfds);
            maxfd = artnet_sock;
        }
        if (sacn_sock >= 0) {
            FD_SET(sacn_sock, &readfds);
            maxfd = (sacn_sock > maxfd) ? sacn_sock : maxfd;
        }
        struct timeval tv = {.tv_sec = 0, .tv_usec = DMX_NET_SELECT_TIMEOUT_MS * 1000};
        int ready = (maxfd >= 0) ? select(maxfd + 1, &readfds, NULL, NULL, &tv) : 0;
        if (maxfd < 0) {
            vTaskDelay(pdMS_TO_TICKS(DMX_NET_SELECT_TIMEOUT_MS));
        }

        int64_t now_us = esp_timer_get_time();
        lock();
        if ((ready > 0) && (artnet_sock >= 0) && FD_ISSET(artnet_sock, &readfds) && (s_artnet_sock == artnet_sock)) {
            struct sockaddr_in from;
            socklen_t from_len = sizeof(from);
            int n = recvfrom(artnet_sock, rx_buf, sizeof(rx_buf), 0, (struct sockaddr *)&from, &from_len);
            if (n > 0) {
                handle_artnet(rx_buf, (size_t)n, &from, now_us);
            }
        }
        if ((ready > 0) && (sacn_sock >= 0) && FD_ISSET(sacn_sock, &readfds) && (s_sacn_sock == sacn_sock)) {
            struct sockaddr_in from;
            socklen_t from_len = sizeof(from);
            int n = recvfrom(sacn_sock, rx_buf, sizeof(rx_buf), 0, (struct sockaddr *)&from, &from_len);
            if (n > 0) {
                handle_sacn(rx_buf, (size_t)n, &from, now_us);
            }
        }
        housekeeping(now_us);
        unlock();
    }

    lock();
    for (unsigned i = 0; i < BRIDGES_COUNT; i++) {
        s_ports[i].registered = false;
    }
    sync_sockets_locked();   // closes everything
    unlock();
    ESP_LOGI(TAG, "receiver task stopped");
    xEventGroupSetBits(s_task_events, EVENT_TASK_FINISHED);
    vTaskDelete(NULL);
}

// ---- lifecycle ---------------------------------------------------------------------------------

static bool any_port_registered_locked(void)
{
    for (unsigned i = 0; i < BRIDGES_COUNT; i++) {
        if (s_ports[i].registered) {
            return true;
        }
    }
    return false;
}

static esp_err_t start_task_locked(void)
{
    if (s_task != NULL) {
        return ESP_OK;
    }
    if (s_task_events == NULL) {
        s_task_events = xEventGroupCreate();
        if (s_task_events == NULL) {
            return ESP_ERR_NO_MEM;
        }
    }
    xEventGroupClearBits(s_task_events, EVENT_TASK_FINISHED);
    __atomic_store_n(&s_stop_requested, false, __ATOMIC_RELEASE);
    __atomic_store_n(&s_resync_requested, true, __ATOMIC_RELEASE);
    if (xTaskCreate(dmx_net_task, "dmx_net", DMX_NET_TASK_STACK_SIZE, NULL, DMX_NET_TASK_PRIORITY, &s_task) != pdPASS) {
        s_task = NULL;
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

// Stop the task. Called with the lock NOT held: the task needs it to finish its iteration.
static void stop_task(void)
{
    TaskHandle_t task = s_task;
    if (task == NULL) {
        return;
    }
    __atomic_store_n(&s_stop_requested, true, __ATOMIC_RELEASE);
    if (xEventGroupWaitBits(s_task_events, EVENT_TASK_FINISHED, pdTRUE, pdTRUE,
                            pdMS_TO_TICKS(DMX_NET_STOP_TIMEOUT_MS)) & EVENT_TASK_FINISHED) {
        s_task = NULL;
    } else {
        ESP_LOGE(TAG, "receiver task did not stop");
    }
}

esp_err_t dmx_net_port_start(unsigned index, const dmx_net_port_config_t *cfg)
{
    if ((index >= BRIDGES_COUNT) || (cfg == NULL)) {
        return ESP_ERR_INVALID_ARG;
    }
    lock();
    dmx_net_port_t *port = &s_ports[index];
    memset(port, 0, sizeof(*port));
    port->cfg = *cfg;
    port->registered = cfg->artnet_enabled || cfg->sacn_enabled;
    port->stats.artnet.enabled = cfg->artnet_enabled;
    port->stats.artnet.universe = cfg->artnet_universe;
    port->stats.sacn.enabled = cfg->sacn_enabled;
    port->stats.sacn.universe = cfg->sacn_universe;
    port->second_start_us = esp_timer_get_time();
    esp_err_t ret = ESP_OK;
    bool need_task = any_port_registered_locked();
    if (need_task) {
        ret = start_task_locked();
        __atomic_store_n(&s_resync_requested, true, __ATOMIC_RELEASE);
    }
    unlock();
    if (!need_task) {
        stop_task();
    }
    if (ret == ESP_OK && port->registered) {
        ESP_LOGI(TAG, "Port[%u]: Art-Net %s (universe %u), sACN %s (universe %u), loss: %s after %u s",
                 index + 1, cfg->artnet_enabled ? "on" : "off", cfg->artnet_universe,
                 cfg->sacn_enabled ? "on" : "off", cfg->sacn_universe,
                 (cfg->loss_policy == DMX_LOSS_BLACKOUT) ? "blackout" : "hold", cfg->loss_timeout_s);
    }
    return ret;
}

void dmx_net_port_stop(unsigned index)
{
    if (index >= BRIDGES_COUNT) {
        return;
    }
    lock();
    memset(&s_ports[index], 0, sizeof(s_ports[index]));
    bool need_task = any_port_registered_locked();
    __atomic_store_n(&s_resync_requested, true, __ATOMIC_RELEASE);
    unlock();
    if (!need_task) {
        stop_task();
    }
}

void dmx_net_get_stats(unsigned index, dmx_net_stats_t *out)
{
    if (out == NULL) {
        return;
    }
    memset(out, 0, sizeof(*out));
    if (index >= BRIDGES_COUNT) {
        return;
    }
    lock();
    *out = s_ports[index].stats;
    unlock();
}
