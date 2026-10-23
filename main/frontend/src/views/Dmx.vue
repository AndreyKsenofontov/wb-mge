<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import { useSettings } from '@/common/settings';
import { useInfo } from '@/common/info';
import { useAlerts } from '@/common/alert';
import { useOptimisticToggle } from '@/common/useOptimisticToggle';
import type { DmxLossPolicy, DmxNetSource, DmxState, DmxStats, RsSettings } from '@/common/types';
import { api } from '@/utils/api';
import { formatUptime } from '@/utils/repeaterFormat';
import Button from '@/components/Button.vue';
import Heading from '@/components/Heading.vue';
import InputNumber from '@/components/InputNumber.vue';
import Layout from '@/components/Layout.vue';
import Switch from '@/components/Switch.vue';

type PortNumber = 1 | 2;
type PortKey = 'rs485_1' | 'rs485_2';

// DMX512 limits, mirrored from the firmware (bridge/dmx_frame.h).
const DMX_CHANNELS = 512;
const DMX_VALUE_MAX = 255;
const DMX_SLOTS_MIN = 24;
const DMX_REFRESH_MIN = 1;
const DMX_REFRESH_MAX = 44;
const DMX_BREAK_MIN = 92;
const DMX_BREAK_MAX = 1000;
const DMX_MAB_MIN = 12;
const DMX_MAB_MAX = 1000;
const ARTNET_UNIVERSE_MAX = 32767;
const SACN_UNIVERSE_MIN = 1;
const SACN_UNIVERSE_MAX = 63999;
const LOSS_TIMEOUT_MIN = 1;
const LOSS_TIMEOUT_MAX = 3600;
const LOSS_POLICIES: DmxLossPolicy[] = ['hold', 'blackout'];
// Channels shown before the user asks for the whole universe.
const DEFAULT_VISIBLE_CHANNELS = 32;
// The universe is re-read this often so writes from Modbus / the controller show up.
const UNIVERSE_POLL_MS = 3000;

const { t } = useI18n();
const { data: settings, isChanged, isLoading, updateSettings } = useSettings();
const { info } = useInfo();
const { showAlert } = useAlerts();

const port = ref<PortNumber>(1);
const portKey = computed<PortKey>(() => (port.value === 1 ? 'rs485_1' : 'rs485_2'));

// ---- output toggle ---------------------------------------------------------------------

// One toggle per port: the port is ON when its mode is 'dmx'. The toggle is keyed by the
// current port selection, so switching ports does not carry an optimistic override across.
const toggles: Record<PortKey, ReturnType<typeof useOptimisticToggle>> = {
  rs485_1: useOptimisticToggle({
    derive: () => info.value?.rs485_1.port_mode === 'dmx',
    onError: () => showAlert('connection_error'),
  }),
  rs485_2: useOptimisticToggle({
    derive: () => info.value?.rs485_2.port_mode === 'dmx',
    onError: () => showAlert('connection_error'),
  }),
};
const toggle = computed(() => toggles[portKey.value]);
const isEnabled = computed<boolean>(() => toggle.value.value.value);
const isToggleDisabled = computed<boolean>(() => toggle.value.inFlight.value || info.value === undefined);

// ON -> POST /ports/N/mode dmx. OFF -> disabled. Both persist the mode in NVS.
function toggleOutput(): void {
  if (info.value === undefined) return;
  const n = port.value;
  toggle.value.run(async (wasEnabled) => {
    const mode = wasEnabled ? 'disabled' : 'dmx';
    await api<void>(`ports/${n}/mode`, { method: 'POST', json: { mode } });
  });
}

// ---- live stats ------------------------------------------------------------------------

const stats = computed<DmxStats | undefined>(() => info.value?.dmx?.[portKey.value]);
const refreshHz = computed<number>(() => {
  const period = stats.value?.period_us ?? 0;
  return period > 0 ? Math.round(1_000_000 / period) : 0;
});
const uptimeS = computed<number>(() => (stats.value?.uptime_ms ?? 0) / 1000);

// ---- frame settings --------------------------------------------------------------------

const portSettings = computed<RsSettings | undefined>(() => settings.value?.[portKey.value]);
// Firmware without the dmx mode reports no dmx_* fields: hide the form instead of saving NaN.
const hasFrameSettings = computed<boolean>(() => portSettings.value?.dmx_slots !== undefined);
// Likewise for the network sources, which came with a later firmware than the dmx mode itself.
const hasNetSettings = computed<boolean>(() => portSettings.value?.dmx_artnet !== undefined);

// Receiver statistics of the selected port; undefined on firmware without the receivers.
const artnet = computed<DmxNetSource | undefined>(() => stats.value?.artnet);
const sacn = computed<DmxNetSource | undefined>(() => stats.value?.sacn);

const isSaveDisabled = computed(() => isLoading.value || !isChanged([portKey.value]));

function saveFrameSettings(): void {
  if (!portSettings.value) return;
  updateSettings({ [portKey.value]: portSettings.value });
}

// ---- universe --------------------------------------------------------------------------

const values = ref<number[]>(new Array(DMX_CHANNELS).fill(0));
const showAll = ref(false);
const visibleCount = computed<number>(() => (showAll.value ? DMX_CHANNELS : DEFAULT_VISIBLE_CHANNELS));
const visibleChannels = computed<number[]>(() => Array.from({ length: visibleCount.value }, (_, i) => i + 1));
// 1-based channel being dragged; polls do not overwrite the universe while it is set.
const activeChannel = ref<number | null>(null);
let pollTimer: ReturnType<typeof setInterval> | null = null;

async function loadUniverse(): Promise<void> {
  try {
    const state = await api<DmxState>(`ports/${port.value}/dmx`);
    if (activeChannel.value === null && Array.isArray(state.values)) {
      values.value = state.values.slice(0, DMX_CHANNELS);
    }
  } catch {
    // A failed poll leaves the last known universe on screen; the next poll retries.
  }
}

// Write one channel. The slider's `input` event updates the local copy while dragging;
// `change` (pointer released) is what reaches the device.
async function writeChannel(channel: number): Promise<void> {
  const value = values.value[channel - 1];
  try {
    await api<DmxStats>(`ports/${port.value}/dmx`, { method: 'POST', json: { start: channel, values: [value] } });
  } catch {
    showAlert('connection_error');
  } finally {
    activeChannel.value = null;
  }
}

function onSliderInput(channel: number, ev: Event): void {
  activeChannel.value = channel;
  values.value[channel - 1] = Number((ev.target as HTMLInputElement).value);
}

async function writeAllVisible(value: number): Promise<void> {
  const count = visibleCount.value;
  try {
    await api<DmxStats>(`ports/${port.value}/dmx`, {
      method: 'POST',
      json: { start: 1, values: new Array(count).fill(value) },
    });
    values.value = values.value.map((v, i) => (i < count ? value : v));
  } catch {
    showAlert('connection_error');
  }
}

async function blackout(): Promise<void> {
  try {
    await api<DmxStats>(`ports/${port.value}/dmx`, { method: 'POST', json: { blackout: true } });
    values.value = new Array(DMX_CHANNELS).fill(0);
  } catch {
    showAlert('connection_error');
  }
}

function startPolling(): void {
  if (pollTimer) return;
  pollTimer = setInterval(() => {
    if (document.visibilityState === 'visible') loadUniverse();
  }, UNIVERSE_POLL_MS);
}

function stopPolling(): void {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
}

onMounted(() => {
  loadUniverse();
  startPolling();
});

onBeforeUnmount(stopPolling);

// A port switch shows that port's universe at once instead of waiting for the next poll.
watch(port, () => {
  activeChannel.value = null;
  loadUniverse();
});
</script>

<template>
  <Layout>
    <Heading :title="t('title')" :crumbs="t('crumbs')" />

    <div class="main-body">
      <div class="dmx-ports" role="tablist">
        <button
          v-for="n in ([1, 2] as PortNumber[])"
          :key="n"
          type="button"
          role="tab"
          :class="['dmx-port-tab', { 'dmx-port-tabActive': port === n }]"
          :aria-selected="port === n"
          @click="port = n"
        >
          {{ t('port_n', { n }) }}
        </button>
      </div>

      <div class="dmx-top">
        <section class="card">
          <div class="card-header">
            <div class="card-title-wrap">
              <div class="title">{{ t('output') }}</div>
              <div class="sub">{{ t('output_sub') }}</div>
            </div>
          </div>
          <div class="dmx-output">
            <button
              type="button"
              :class="['dmx-toggle', isEnabled ? 'on' : 'off']"
              :disabled="isToggleDisabled"
              :aria-pressed="isEnabled"
              :aria-label="t('enabled')"
              @click="toggleOutput"
            >
              <span class="dmx-toggle-label">{{ isEnabled ? t('enabled') : t('disabled') }}</span>
              <span class="dmx-toggle-hint">{{ isEnabled ? t('click_to_disable') : t('click_to_enable') }}</span>
            </button>
            <dl class="dmx-stats">
              <dt>{{ t('slots') }}</dt>
              <dd class="mono">{{ stats?.slots ?? 0 }}</dd>
              <dt>{{ t('refresh') }}</dt>
              <dd class="mono">{{ refreshHz }} Hz</dd>
              <dt>{{ t('frames') }}</dt>
              <dd class="mono">{{ stats?.frames_sent ?? 0 }}</dd>
              <dt>{{ t('uptime') }}</dt>
              <dd class="mono">{{ formatUptime(uptimeS) }}</dd>
              <template v-if="stats?.tx_disabled">
                <dt>{{ t('tx_disabled') }}</dt>
                <dd class="dmx-warn">{{ t('tx_disabled_hint') }}</dd>
              </template>
            </dl>
          </div>
        </section>

        <section v-if="hasFrameSettings && portSettings" class="card">
          <form @submit.prevent="saveFrameSettings">
            <div class="card-header">
              <div class="card-title-wrap">
                <div class="title">{{ t('frame') }}</div>
                <div class="sub">{{ t('frame_sub') }}</div>
              </div>
              <Button type="submit" :is-loading="isLoading" :disabled="isSaveDisabled">{{ t('save') }}</Button>
            </div>
            <div class="card-body">
              <div class="field">
                <label :for="`${portKey}-dmx_slots`">{{ t('slots') }}</label>
                <InputNumber :id="`${portKey}-dmx_slots`" v-model="portSettings.dmx_slots" :min="DMX_SLOTS_MIN" :max="DMX_CHANNELS" />
                <div class="hint">{{ t('slots_hint') }}</div>
              </div>
              <div class="field">
                <label :for="`${portKey}-dmx_refresh`">{{ t('refresh') }}</label>
                <InputNumber :id="`${portKey}-dmx_refresh`" v-model="portSettings.dmx_refresh" :min="DMX_REFRESH_MIN" :max="DMX_REFRESH_MAX" />
                <div class="hint">{{ t('refresh_hint') }}</div>
              </div>
              <div class="field">
                <label :for="`${portKey}-dmx_break`">{{ t('break') }}</label>
                <InputNumber :id="`${portKey}-dmx_break`" v-model="portSettings.dmx_break" :min="DMX_BREAK_MIN" :max="DMX_BREAK_MAX" />
              </div>
              <div class="field">
                <label :for="`${portKey}-dmx_mab`">{{ t('mab') }}</label>
                <InputNumber :id="`${portKey}-dmx_mab`" v-model="portSettings.dmx_mab" :min="DMX_MAB_MIN" :max="DMX_MAB_MAX" />
              </div>
            </div>
          </form>
        </section>
      </div>

      <section v-if="hasNetSettings && portSettings" class="card dmx-net">
        <form @submit.prevent="saveFrameSettings">
          <div class="card-header">
            <div class="card-title-wrap">
              <div class="title">{{ t('net_sources') }}</div>
              <div class="sub">{{ t('net_sources_sub') }}</div>
            </div>
            <Button type="submit" :is-loading="isLoading" :disabled="isSaveDisabled">{{ t('save') }}</Button>
          </div>
          <div class="card-body">
            <div class="field dmx-net-row">
              <label :for="`${portKey}-dmx_artnet`" class="dmx-net-label">
                <Switch :id="`${portKey}-dmx_artnet`" v-model="portSettings.dmx_artnet" class="dmx-net-switch" />
                <span>{{ t('artnet') }}</span>
              </label>
              <InputNumber :id="`${portKey}-dmx_artnet_universe`" v-model="portSettings.dmx_artnet_universe" :min="0" :max="ARTNET_UNIVERSE_MAX" :aria-label="t('artnet_universe')" />
              <div class="hint">{{ t('artnet_universe_hint') }}</div>
              <div v-if="artnet" class="dmx-net-status">
                <span :class="['dmx-net-dot', { 'dmx-net-dotActive': artnet.active }]"></span>
                <span>{{ artnet.active ? t('src_active') : t('src_idle') }}</span>
                <span class="mono">{{ artnet.fps }} {{ t('src_fps') }}</span>
                <span class="mono">{{ artnet.packets }} {{ t('src_packets') }}</span>
                <span v-if="artnet.last_source" class="mono">{{ artnet.last_source }}</span>
              </div>
            </div>
            <div class="field dmx-net-row">
              <label :for="`${portKey}-dmx_sacn`" class="dmx-net-label">
                <Switch :id="`${portKey}-dmx_sacn`" v-model="portSettings.dmx_sacn" class="dmx-net-switch" />
                <span>{{ t('sacn') }}</span>
              </label>
              <InputNumber :id="`${portKey}-dmx_sacn_universe`" v-model="portSettings.dmx_sacn_universe" :min="SACN_UNIVERSE_MIN" :max="SACN_UNIVERSE_MAX" :aria-label="t('sacn_universe')" />
              <div class="hint">{{ t('sacn_universe_hint') }}</div>
              <div v-if="sacn" class="dmx-net-status">
                <span :class="['dmx-net-dot', { 'dmx-net-dotActive': sacn.active }]"></span>
                <span>{{ sacn.active ? t('src_active') : t('src_idle') }}</span>
                <span class="mono">{{ sacn.fps }} {{ t('src_fps') }}</span>
                <span class="mono">{{ sacn.packets }} {{ t('src_packets') }}</span>
                <span v-if="sacn.last_source" class="mono">{{ sacn.last_source }}</span>
                <span v-if="sacn.active" class="mono">{{ t('src_priority') }} {{ sacn.priority }}</span>
              </div>
            </div>
            <div class="field">
              <label :for="`${portKey}-dmx_loss`">{{ t('loss') }}</label>
              <select :id="`${portKey}-dmx_loss`" v-model="portSettings.dmx_loss">
                <option v-for="policy in LOSS_POLICIES" :key="policy" :value="policy">{{ t(`loss_${policy}`) }}</option>
              </select>
            </div>
            <div class="field">
              <label :for="`${portKey}-dmx_loss_timeout`">{{ t('loss_timeout') }}</label>
              <InputNumber :id="`${portKey}-dmx_loss_timeout`" v-model="portSettings.dmx_loss_timeout" :min="LOSS_TIMEOUT_MIN" :max="LOSS_TIMEOUT_MAX" />
              <div v-if="stats?.signal_lost" class="hint dmx-warn">{{ t('signal_lost') }}</div>
            </div>
          </div>
        </form>
      </section>

      <section class="card">
        <div class="card-header">
          <div class="card-title-wrap">
            <div class="title">{{ t('channels') }}</div>
            <div class="sub">{{ t('channels_sub') }}</div>
          </div>
          <div class="dmx-actions">
            <Button variant="outline" type="button" class="dmx-full" @click="writeAllVisible(DMX_VALUE_MAX)">{{ t('full_on') }}</Button>
            <Button variant="danger" type="button" class="dmx-blackout" @click="blackout">{{ t('blackout') }}</Button>
          </div>
        </div>
        <div class="dmx-grid">
          <div v-for="ch in visibleChannels" :key="ch" class="dmx-ch">
            <label :for="`dmx-ch-${ch}`" class="dmx-ch-label mono">{{ ch }}</label>
            <input
              :id="`dmx-ch-${ch}`"
              type="range"
              class="dmx-ch-slider"
              min="0"
              :max="DMX_VALUE_MAX"
              :value="values[ch - 1]"
              :disabled="!isEnabled"
              :aria-label="t('channel_n', { n: ch })"
              @input="onSliderInput(ch, $event)"
              @change="writeChannel(ch)"
            />
            <span class="dmx-ch-value mono">{{ values[ch - 1] }}</span>
          </div>
        </div>
        <div class="dmx-footer">
          <button type="button" class="dmx-more" @click="showAll = !showAll">
            {{ showAll ? t('show_less', { n: DEFAULT_VISIBLE_CHANNELS }) : t('show_all', { n: DMX_CHANNELS }) }}
          </button>
        </div>
      </section>
    </div>
  </Layout>
</template>

<style scoped>
.dmx-ports {
  display: flex;
  gap: 6px;
  margin-bottom: 12px;
}

.dmx-port-tab {
  height: 30px;
  padding: 0 14px;
  border-radius: var(--r-md);
  border: 1px solid var(--border-strong);
  background: var(--bg-surface);
  color: var(--text-secondary);
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
}

.dmx-port-tabActive {
  background: var(--brand-soft);
  border-color: var(--brand-soft-border);
  color: var(--primary-color);
}

.dmx-top {
  display: grid;
  grid-template-columns: minmax(260px, 1fr) minmax(300px, 1.4fr);
  gap: 16px;
  margin-bottom: 16px;

  @media (max-width: 860px) {
    grid-template-columns: 1fr;
  }
}

.dmx-output {
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 18px;
}

.dmx-toggle {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  width: 100%;
  height: auto;
  padding: 16px 14px;
  border-radius: var(--r-lg);
  border: 1px solid var(--border-strong);
  background: var(--bg-surface);
  color: var(--text-secondary);
  cursor: pointer;
  transition: background 0.12s, border-color 0.12s, color 0.12s;
  white-space: normal;
}

.dmx-toggle:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.dmx-toggle.on {
  background: var(--brand-soft);
  border-color: var(--brand-soft-border);
  color: var(--primary-color);
}

.dmx-toggle.on:hover:not(:disabled) {
  background: color-mix(in oklch, var(--primary-color) 14%, var(--bg-surface));
  border-color: var(--primary-color);
}

.dmx-toggle.off:hover:not(:disabled) {
  background: var(--bg-surface-subtle);
  border-color: var(--border-strong);
  color: var(--text-color);
}

.dmx-toggle-label {
  font-size: 15px;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.dmx-toggle-hint {
  font-size: 11px;
  color: var(--text-muted);
}

.dmx-stats {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 6px 12px;
  margin: 0;
  font-size: 12.5px;
}

.dmx-stats dt {
  color: var(--text-secondary);
}

.dmx-stats dd {
  margin: 0;
  text-align: right;
}

.dmx-warn {
  color: var(--warn);
}

.dmx-actions {
  display: flex;
  gap: 8px;
}

.dmx-net {
  margin-bottom: 16px;
}

.dmx-net-label {
  display: flex;
  align-items: center;
  gap: 10px;
  cursor: pointer;
}

.dmx-net-status {
  grid-column: 1 / -1;
  display: flex;
  flex-wrap: wrap;
  gap: 4px 14px;
  align-items: center;
  font-size: 12px;
  color: var(--text-secondary);
  margin-top: -4px;
}

.dmx-net-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--text-muted);
}

.dmx-net-dotActive {
  background: var(--primary-color);
}

.dmx-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
  gap: 6px 14px;
  padding: 10px 18px 6px;
}

.dmx-ch {
  display: grid;
  grid-template-columns: 30px 1fr 32px;
  gap: 8px;
  align-items: center;
  font-size: 12px;
}

.dmx-ch-label {
  color: var(--text-muted);
  text-align: right;
}

.dmx-ch-slider {
  width: 100%;
  min-width: 0;
  margin: 0;
  accent-color: var(--primary-color);
}

.dmx-ch-value {
  text-align: right;
  color: var(--text-color);
}

.dmx-footer {
  display: flex;
  justify-content: center;
  padding: 6px 18px 14px;
}

.dmx-more {
  border: 0;
  background: none;
  color: var(--primary-color);
  font-size: 12.5px;
  cursor: pointer;
  text-decoration: underline;
}
</style>

<i18n>
{
  "en": {
    "title": "DMX",
    "crumbs": "Drive the RS-485 port as a DMX512 transmitter: 250 kbit/s, up to 512 channels, written from this page, over Modbus TCP or from a controller.",
    "port_n": "Port {n}",
    "output": "DMX output",
    "output_sub": "Switches the port into dmx mode and back",
    "enabled": "Enabled",
    "disabled": "Disabled",
    "click_to_disable": "Click to disable",
    "click_to_enable": "Click to enable",
    "slots": "Channels per frame",
    "slots_hint": "Data slots transmitted in every packet (24–512). Fewer slots allow a faster refresh.",
    "refresh": "Refresh rate",
    "refresh_hint": "Requested packet rate in Hz (1–44). A full 512-channel packet caps it at about 44 Hz.",
    "frames": "Frames sent",
    "uptime": "Uptime",
    "tx_disabled": "Transmitter",
    "tx_disabled_hint": "TX is disabled on the port settings page: packets are generated but not driven onto the bus",
    "frame": "Frame timing",
    "frame_sub": "BREAK and mark-after-break in microseconds",
    "break": "BREAK, µs",
    "mab": "Mark after break, µs",
    "save": "Save",
    "channels": "Channels",
    "channels_sub": "Values take effect from the next packet; the universe is re-read every few seconds",
    "channel_n": "Channel {n}",
    "full_on": "Visible to 255",
    "blackout": "Blackout",
    "show_all": "Show all {n} channels",
    "show_less": "Show the first {n} channels",
    "net_sources": "Network sources",
    "net_sources_sub": "Pull packets from a console or software into this port",
    "artnet": "Art-Net (UDP 6454)",
    "artnet_universe": "Art-Net port-address",
    "artnet_universe_hint": "15-bit port-address: Net × 256 + Sub-Net × 16 + Universe (0–32767). The gateway answers ArtPoll, so consoles find it.",
    "sacn": "sACN / E1.31 (UDP 5568)",
    "sacn_universe": "sACN universe",
    "sacn_universe_hint": "Universe 1–63999; the gateway joins the multicast group 239.255.x.y and follows the highest-priority source.",
    "loss": "When the network stops",
    "loss_hold": "Keep the last values",
    "loss_blackout": "Blackout",
    "loss_timeout": "Signal loss timeout, s",
    "src_active": "receiving",
    "src_idle": "no packets",
    "src_fps": "pkt/s",
    "src_packets": "total",
    "src_priority": "priority",
    "signal_lost": "No network packets for longer than the timeout: the loss policy has been applied"
  },
  "ru": {
    "title": "DMX",
    "crumbs": "Порт RS-485 работает передатчиком DMX512: 250 кбит/с, до 512 каналов, управление с этой страницы, по Modbus TCP или с контроллера.",
    "port_n": "Порт {n}",
    "output": "Выход DMX",
    "output_sub": "Переключает порт в режим dmx и обратно",
    "enabled": "Включён",
    "disabled": "Отключён",
    "click_to_disable": "Нажмите, чтобы отключить",
    "click_to_enable": "Нажмите, чтобы включить",
    "slots": "Каналов в кадре",
    "slots_hint": "Слотов данных в каждом пакете (24–512). Чем меньше слотов, тем выше возможная частота.",
    "refresh": "Частота кадров",
    "refresh_hint": "Желаемая частота пакетов, Гц (1–44). Полный пакет на 512 каналов ограничивает её примерно 44 Гц.",
    "frames": "Кадров отправлено",
    "uptime": "Время работы",
    "tx_disabled": "Передатчик",
    "tx_disabled_hint": "TX отключён в настройках порта: кадры формируются, но в шину не выдаются",
    "frame": "Тайминги кадра",
    "frame_sub": "BREAK и пауза после него в микросекундах",
    "break": "BREAK, мкс",
    "mab": "Пауза после BREAK, мкс",
    "save": "Сохранить",
    "channels": "Каналы",
    "channels_sub": "Значения применяются со следующего пакета; вселенная перечитывается раз в несколько секунд",
    "channel_n": "Канал {n}",
    "full_on": "Видимые на 255",
    "blackout": "Погасить всё",
    "show_all": "Показать все {n} каналов",
    "show_less": "Показать первые {n} каналов",
    "net_sources": "Сетевые источники",
    "net_sources_sub": "Приём пакетов от пульта или программы в этот порт",
    "artnet": "Art-Net (UDP 6454)",
    "artnet_universe": "Адрес порта Art-Net",
    "artnet_universe_hint": "15-битный адрес порта: Net × 256 + Sub-Net × 16 + Universe (0–32767). Шлюз отвечает на ArtPoll, пульты его находят.",
    "sacn": "sACN / E1.31 (UDP 5568)",
    "sacn_universe": "Universe sACN",
    "sacn_universe_hint": "Universe 1–63999; шлюз вступает в multicast-группу 239.255.x.y и следует источнику с наибольшим приоритетом.",
    "loss": "Если сеть замолчала",
    "loss_hold": "Держать последние значения",
    "loss_blackout": "Погасить всё",
    "loss_timeout": "Таймаут потери сигнала, с",
    "src_active": "приём идёт",
    "src_idle": "пакетов нет",
    "src_fps": "пак/с",
    "src_packets": "всего",
    "src_priority": "приоритет",
    "signal_lost": "Пакетов из сети нет дольше таймаута: применена политика потери сигнала"
  },
  "kk": {
    "title": "DMX",
    "crumbs": "RS-485 порты DMX512 таратқышы ретінде жұмыс істейді: 250 кбит/с, 512 арнаға дейін, осы беттен, Modbus TCP арқылы немесе контроллерден басқару.",
    "port_n": "Порт {n}",
    "output": "DMX шығысы",
    "output_sub": "Портты dmx режиміне және кері ауыстырады",
    "enabled": "Қосулы",
    "disabled": "Өшірілген",
    "click_to_disable": "Өшіру үшін басыңыз",
    "click_to_enable": "Қосу үшін басыңыз",
    "slots": "Кадрдағы арналар",
    "slots_hint": "Әр пакеттегі дерек слоттары (24–512). Слот аз болса, жиілік жоғары болады.",
    "refresh": "Кадр жиілігі",
    "refresh_hint": "Пакеттердің қажетті жиілігі, Гц (1–44). 512 арналық толық пакет оны шамамен 44 Гц-пен шектейді.",
    "frames": "Жіберілген кадрлар",
    "uptime": "Жұмыс уақыты",
    "tx_disabled": "Таратқыш",
    "tx_disabled_hint": "TX порт параметрлерінде өшірілген: кадрлар жасалады, бірақ шинаға берілмейді",
    "frame": "Кадр уақыттары",
    "frame_sub": "BREAK және одан кейінгі үзіліс, микросекунд",
    "break": "BREAK, мкс",
    "mab": "BREAK кейінгі үзіліс, мкс",
    "save": "Сақтау",
    "channels": "Арналар",
    "channels_sub": "Мәндер келесі пакеттен бастап қолданылады; ғалам бірнеше секунд сайын қайта оқылады",
    "channel_n": "Арна {n}",
    "full_on": "Көрінетіндерді 255-ке",
    "blackout": "Бәрін сөндіру",
    "show_all": "Барлық {n} арнаны көрсету",
    "show_less": "Алғашқы {n} арнаны көрсету",
    "net_sources": "Желілік көздер",
    "net_sources_sub": "Пульттен немесе бағдарламадан пакеттерді осы портқа қабылдау",
    "artnet": "Art-Net (UDP 6454)",
    "artnet_universe": "Art-Net порт мекенжайы",
    "artnet_universe_hint": "15 биттік порт мекенжайы: Net × 256 + Sub-Net × 16 + Universe (0–32767). Шлюз ArtPoll-ға жауап береді, пульттер оны табады.",
    "sacn": "sACN / E1.31 (UDP 5568)",
    "sacn_universe": "sACN universe",
    "sacn_universe_hint": "Universe 1–63999; шлюз 239.255.x.y multicast тобына қосылады және ең жоғары басымдықты көзге ереді.",
    "loss": "Желі үнсіз қалса",
    "loss_hold": "Соңғы мәндерді сақтау",
    "loss_blackout": "Бәрін сөндіру",
    "loss_timeout": "Сигнал жоғалу таймауты, с",
    "src_active": "қабылдау жүріп жатыр",
    "src_idle": "пакеттер жоқ",
    "src_fps": "пак/с",
    "src_packets": "барлығы",
    "src_priority": "басымдық",
    "signal_lost": "Желіден пакеттер таймауттан ұзақ келмеді: сигнал жоғалу саясаты қолданылды"
  },
  "it": {
    "title": "DMX",
    "crumbs": "La porta RS-485 funziona come trasmettitore DMX512: 250 kbit/s, fino a 512 canali, controllati da questa pagina, via Modbus TCP o da un controller.",
    "port_n": "Porta {n}",
    "output": "Uscita DMX",
    "output_sub": "Porta la porta in modalità dmx e viceversa",
    "enabled": "Abilitata",
    "disabled": "Disabilitata",
    "click_to_disable": "Fai clic per disabilitare",
    "click_to_enable": "Fai clic per abilitare",
    "slots": "Canali per frame",
    "slots_hint": "Slot di dati trasmessi in ogni pacchetto (24–512). Meno slot consentono una frequenza maggiore.",
    "refresh": "Frequenza di aggiornamento",
    "refresh_hint": "Frequenza richiesta dei pacchetti in Hz (1–44). Un pacchetto completo di 512 canali la limita a circa 44 Hz.",
    "frames": "Frame inviati",
    "uptime": "Tempo attivo",
    "tx_disabled": "Trasmettitore",
    "tx_disabled_hint": "TX è disabilitato nelle impostazioni della porta: i pacchetti vengono generati ma non inviati sul bus",
    "frame": "Temporizzazione del frame",
    "frame_sub": "BREAK e mark-after-break in microsecondi",
    "break": "BREAK, µs",
    "mab": "Mark after break, µs",
    "save": "Salva",
    "channels": "Canali",
    "channels_sub": "I valori hanno effetto dal pacchetto successivo; l'universo viene riletto ogni pochi secondi",
    "channel_n": "Canale {n}",
    "full_on": "Visibili a 255",
    "blackout": "Blackout",
    "show_all": "Mostra tutti i {n} canali",
    "show_less": "Mostra i primi {n} canali",
    "net_sources": "Sorgenti di rete",
    "net_sources_sub": "Riceve i pacchetti di una console o di un software su questa porta",
    "artnet": "Art-Net (UDP 6454)",
    "artnet_universe": "Port-address Art-Net",
    "artnet_universe_hint": "Port-address a 15 bit: Net × 256 + Sub-Net × 16 + Universe (0–32767). Il gateway risponde ad ArtPoll, quindi le console lo trovano.",
    "sacn": "sACN / E1.31 (UDP 5568)",
    "sacn_universe": "Universe sACN",
    "sacn_universe_hint": "Universe 1–63999; il gateway entra nel gruppo multicast 239.255.x.y e segue la sorgente con la priorità più alta.",
    "loss": "Quando la rete si ferma",
    "loss_hold": "Mantieni gli ultimi valori",
    "loss_blackout": "Blackout",
    "loss_timeout": "Timeout di perdita del segnale, s",
    "src_active": "in ricezione",
    "src_idle": "nessun pacchetto",
    "src_fps": "pkt/s",
    "src_packets": "totale",
    "src_priority": "priorità",
    "signal_lost": "Nessun pacchetto di rete oltre il timeout: è stata applicata la politica di perdita del segnale"
  },
  "de": {
    "title": "DMX",
    "crumbs": "Der RS-485-Port arbeitet als DMX512-Sender: 250 kbit/s, bis zu 512 Kanäle, gesteuert von dieser Seite, über Modbus TCP oder von einem Controller.",
    "port_n": "Port {n}",
    "output": "DMX-Ausgang",
    "output_sub": "Schaltet den Port in den dmx-Modus und zurück",
    "enabled": "Aktiviert",
    "disabled": "Deaktiviert",
    "click_to_disable": "Zum Deaktivieren klicken",
    "click_to_enable": "Zum Aktivieren klicken",
    "slots": "Kanäle pro Frame",
    "slots_hint": "Datenslots in jedem Paket (24–512). Weniger Slots erlauben eine höhere Wiederholrate.",
    "refresh": "Wiederholrate",
    "refresh_hint": "Gewünschte Paketrate in Hz (1–44). Ein volles 512-Kanal-Paket begrenzt sie auf etwa 44 Hz.",
    "frames": "Gesendete Frames",
    "uptime": "Laufzeit",
    "tx_disabled": "Sender",
    "tx_disabled_hint": "TX ist in den Porteinstellungen deaktiviert: Pakete werden erzeugt, aber nicht auf den Bus getrieben",
    "frame": "Frame-Timing",
    "frame_sub": "BREAK und Mark-after-Break in Mikrosekunden",
    "break": "BREAK, µs",
    "mab": "Mark after Break, µs",
    "save": "Speichern",
    "channels": "Kanäle",
    "channels_sub": "Werte gelten ab dem nächsten Paket; das Universum wird alle paar Sekunden neu gelesen",
    "channel_n": "Kanal {n}",
    "full_on": "Sichtbare auf 255",
    "blackout": "Blackout",
    "show_all": "Alle {n} Kanäle anzeigen",
    "show_less": "Die ersten {n} Kanäle anzeigen",
    "net_sources": "Netzwerkquellen",
    "net_sources_sub": "Pakete eines Pults oder einer Software auf diesem Port empfangen",
    "artnet": "Art-Net (UDP 6454)",
    "artnet_universe": "Art-Net-Port-Adresse",
    "artnet_universe_hint": "15-Bit-Port-Adresse: Net × 256 + Sub-Net × 16 + Universe (0–32767). Das Gateway beantwortet ArtPoll, Pulte finden es.",
    "sacn": "sACN / E1.31 (UDP 5568)",
    "sacn_universe": "sACN-Universe",
    "sacn_universe_hint": "Universe 1–63999; das Gateway tritt der Multicast-Gruppe 239.255.x.y bei und folgt der Quelle mit der höchsten Priorität.",
    "loss": "Wenn das Netzwerk verstummt",
    "loss_hold": "Letzte Werte halten",
    "loss_blackout": "Blackout",
    "loss_timeout": "Signalverlust-Timeout, s",
    "src_active": "empfängt",
    "src_idle": "keine Pakete",
    "src_fps": "Pkt/s",
    "src_packets": "gesamt",
    "src_priority": "Priorität",
    "signal_lost": "Länger als das Timeout keine Netzwerkpakete: die Verlustregel wurde angewendet"
  }
}
</i18n>
