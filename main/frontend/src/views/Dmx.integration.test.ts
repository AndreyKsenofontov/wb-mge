/**
 * Integration tests for Dmx.vue — the DMX512 output page.
 *
 * DMX-I-001 — the output toggle reflects the selected port's mode: 'dmx' → ON, anything else → OFF.
 * DMX-I-002 — toggling ON posts ports/1/mode { mode: 'dmx' }; toggling OFF posts { mode: 'disabled' }.
 * DMX-I-003 — the universe is read from GET ports/1/dmx on mount and rendered on the sliders.
 * DMX-I-004 — releasing a slider posts ports/1/dmx { start: <channel>, values: [<value>] }.
 * DMX-I-005 — Blackout posts { blackout: true } and zeroes the sliders.
 * DMX-I-006 — info without a `dmx` object (older firmware) renders without throwing.
 * DMX-I-007 — switching to Port 2 reads ports/2/dmx and derives the toggle from rs485_2.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { ref } from 'vue';
import { createI18n } from 'vue-i18n';
import { createRouter, createMemoryHistory } from 'vue-router';
import type { DmxState, DmxStats, Info, PortMode, Settings } from '@/common/types';
import { api } from '@/utils/api';

const infoRef = ref<Info | undefined>(undefined);
const fetchInfoMock = vi.fn().mockResolvedValue(undefined);
const showAlertMock = vi.fn();
const updateSettingsMock = vi.fn();

vi.mock('@/common/info', () => ({
  useInfo: () => ({ info: infoRef, fetchInfo: fetchInfoMock }),
}));

vi.mock('@/common/alert', () => ({
  useAlerts: () => ({ showAlert: showAlertMock }),
}));

vi.mock('@/utils/api', () => ({
  api: vi.fn().mockResolvedValue(undefined),
}));

vi.mock('@/common/hostname', () => ({
  useHostname: () => ({ hostname: ref('') }),
  setHostname: vi.fn(),
}));

function makeSettings(): Settings {
  const rs = () => ({
    term: true,
    fail_safe: false,
    tx_disabled: false,
    baudrate: 9600 as const,
    stopbits: '2' as const,
    parity: 'none' as const,
    databits: '8' as const,
    bridge: { mode: 'server' as const, ip: '0.0.0.0', port: 502, modbus: true },
    dmx_slots: 512,
    dmx_refresh: 40,
    dmx_break: 176,
    dmx_mab: 16,
  });
  return {
    hostname: 'test',
    login: 'admin',
    web_port: 80,
    io_bus: false,
    vout: false,
    cache_modbus_port: 504,
    cache_modbus_server_enabled: true,
    cache_value_timeout_s: 60,
    update_channel: 'stable',
    ethernet: { ip_static: '', mask_static: '', gw_static: '', dhcpc: true },
    rs485_1: rs(),
    rs485_2: { ...rs(), bridge: { ...rs().bridge, port: 503 } },
  };
}

const settingsData = ref<Settings | undefined>(makeSettings());

vi.mock('@/common/settings', () => ({
  useSettings: () => ({
    data: settingsData,
    initData: settingsData,
    isChanged: () => false,
    isLoading: ref(false),
    updateSettings: updateSettingsMock,
  }),
}));

vi.mock('@unhead/vue', () => ({
  injectHead: () => ({}),
  useHead: vi.fn(),
  createUnhead: vi.fn(() => ({})),
  headSymbol: Symbol('head'),
}));

function makeStats(active: boolean, extra: Partial<DmxStats> = {}): DmxStats {
  return {
    active,
    tx_disabled: false,
    slots: active ? 512 : 0,
    period_us: active ? 25000 : 0,
    frames_sent: active ? 1234 : 0,
    uptime_ms: active ? 61_000 : 0,
    ...extra,
  };
}

function makeInfo(opts: { port1Mode: PortMode; port2Mode: PortMode; omitDmx?: boolean }): Info {
  return {
    device_name: 'test',
    serial_num: 0,
    firmware: '0.0.0',
    hardware: '0',
    system_voltage: 0,
    heap_total: 0,
    heap_free: 0,
    heap_min_free: 0,
    ethernet: { con_eth: false, ip: '', mask: '', gw: '', mac: '' },
    wifi: {
      mode: 'none',
      con_ap: 0,
      con_sta: false,
      con_sta_ssid: '',
      enabled: false,
      sta_ip: '',
      sta_mask: '',
      sta_gw: '',
      sta_mac: '',
      ap_ip: '',
      ap_channel: 0,
      ap_mac: '',
    },
    rs485_1: { is_busy: false, error_percentage: 0, server_connections_count: 0, port_mode: opts.port1Mode, cache_enabled: false },
    rs485_2: { is_busy: false, error_percentage: 0, server_connections_count: 0, port_mode: opts.port2Mode, cache_enabled: false },
    dmx: opts.omitDmx
      ? undefined
      : { rs485_1: makeStats(opts.port1Mode === 'dmx'), rs485_2: makeStats(opts.port2Mode === 'dmx') },
    cache_modbus_port: 504,
    cache_modbus_server_enabled: true,
    cache_value_timeout_s: 60,
    psram_available: false,
    psram_size_kb: 0,
  };
}

function makeUniverse(values: Record<number, number> = {}): DmxState {
  const universe = new Array(512).fill(0);
  for (const [ch, v] of Object.entries(values)) universe[Number(ch) - 1] = v;
  return { ...makeStats(true), values: universe };
}

// The api mock answers GET ports/N/dmx with a universe and everything else with undefined.
function mockApiWithUniverse(state: DmxState) {
  vi.mocked(api).mockImplementation(((url: string, options?: { method?: string }) => {
    if (/^ports\/[12]\/dmx$/.test(url) && (options?.method ?? 'GET') === 'GET') {
      return Promise.resolve(state);
    }
    return Promise.resolve(undefined);
  }) as never);
}

function toggleButton(wrapper: ReturnType<typeof mount>) {
  return wrapper.find('.dmx-toggle');
}

function slider(wrapper: ReturnType<typeof mount>, channel: number) {
  return wrapper.find(`#dmx-ch-${channel}`);
}

function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { template: '<div/>' } },
      { path: '/logout', component: { template: '<div/>' } },
    ],
  });
}

const i18n = createI18n({ legacy: false, locale: 'en', messages: { en: {} } });

async function mountPage() {
  const { default: Dmx } = await import('@/views/Dmx.vue');
  const wrapper = mount(Dmx, { global: { plugins: [i18n, makeRouter()] } });
  await flushPromises();
  return wrapper;
}

beforeEach(() => {
  infoRef.value = undefined;
  settingsData.value = makeSettings();
  fetchInfoMock.mockClear();
  showAlertMock.mockClear();
  updateSettingsMock.mockClear();
  vi.resetModules();
  vi.mocked(api).mockReset();
  mockApiWithUniverse(makeUniverse());
});

describe('DMX-I-001: toggle reflects the port mode', () => {
  it('port 1 in dmx mode → ON; any other mode → OFF', async () => {
    infoRef.value = makeInfo({ port1Mode: 'dmx', port2Mode: 'disabled' });
    const wrapper = await mountPage();

    expect(toggleButton(wrapper).classes()).toContain('on');
    expect(toggleButton(wrapper).attributes('aria-pressed')).toBe('true');

    infoRef.value = makeInfo({ port1Mode: 'tcp_bridge', port2Mode: 'disabled' });
    await flushPromises();

    expect(toggleButton(wrapper).classes()).toContain('off');
    expect(toggleButton(wrapper).attributes('aria-pressed')).toBe('false');

    wrapper.unmount();
  });
});

describe('DMX-I-002: toggle posts the port mode', () => {
  it('ON posts ports/1/mode { mode: dmx } then fetchInfo(low)', async () => {
    infoRef.value = makeInfo({ port1Mode: 'disabled', port2Mode: 'disabled' });
    const wrapper = await mountPage();
    vi.mocked(api).mockClear();
    fetchInfoMock.mockClear();

    await toggleButton(wrapper).trigger('click');
    await flushPromises();

    expect(vi.mocked(api)).toHaveBeenCalledWith('ports/1/mode', { method: 'POST', json: { mode: 'dmx' } });
    expect(vi.mocked(api)).not.toHaveBeenCalledWith('ports/2/mode', expect.anything());
    expect(fetchInfoMock).toHaveBeenCalledWith('low');

    wrapper.unmount();
  });

  it('OFF posts ports/1/mode { mode: disabled }', async () => {
    infoRef.value = makeInfo({ port1Mode: 'dmx', port2Mode: 'disabled' });
    const wrapper = await mountPage();
    vi.mocked(api).mockClear();

    await toggleButton(wrapper).trigger('click');
    await flushPromises();

    expect(vi.mocked(api)).toHaveBeenCalledWith('ports/1/mode', { method: 'POST', json: { mode: 'disabled' } });

    wrapper.unmount();
  });
});

describe('DMX-I-003: the universe is read on mount', () => {
  it('GET ports/1/dmx values land on the sliders and the stats render', async () => {
    mockApiWithUniverse(makeUniverse({ 1: 255, 3: 77, 32: 9 }));
    infoRef.value = makeInfo({ port1Mode: 'dmx', port2Mode: 'disabled' });
    const wrapper = await mountPage();

    expect(vi.mocked(api)).toHaveBeenCalledWith('ports/1/dmx');
    expect((slider(wrapper, 1).element as HTMLInputElement).value).toBe('255');
    expect((slider(wrapper, 3).element as HTMLInputElement).value).toBe('77');
    expect((slider(wrapper, 32).element as HTMLInputElement).value).toBe('9');
    // Only the first 32 channels are shown until the user asks for all of them.
    expect(slider(wrapper, 33).exists()).toBe(false);

    const text = wrapper.text().replace(/\s+/g, ' ');
    expect(text).toContain('40 Hz');
    expect(text).toContain('1234');
    expect(text).toContain('00:01:01');

    wrapper.unmount();
  });

  it('"show all" reveals channel 512', async () => {
    infoRef.value = makeInfo({ port1Mode: 'dmx', port2Mode: 'disabled' });
    const wrapper = await mountPage();

    await wrapper.find('.dmx-more').trigger('click');
    expect(slider(wrapper, 512).exists()).toBe(true);

    wrapper.unmount();
  });
});

describe('DMX-I-004: a slider writes its channel', () => {
  it('change on channel 5 posts { start: 5, values: [200] }', async () => {
    infoRef.value = makeInfo({ port1Mode: 'dmx', port2Mode: 'disabled' });
    const wrapper = await mountPage();
    vi.mocked(api).mockClear();

    const input = slider(wrapper, 5);
    (input.element as HTMLInputElement).value = '200';
    await input.trigger('input');
    await input.trigger('change');
    await flushPromises();

    expect(vi.mocked(api)).toHaveBeenCalledWith('ports/1/dmx', { method: 'POST', json: { start: 5, values: [200] } });

    wrapper.unmount();
  });

  it('sliders are disabled while the output is off', async () => {
    infoRef.value = makeInfo({ port1Mode: 'disabled', port2Mode: 'disabled' });
    const wrapper = await mountPage();

    expect(slider(wrapper, 1).attributes('disabled')).toBeDefined();

    wrapper.unmount();
  });
});

describe('DMX-I-005: blackout', () => {
  it('posts { blackout: true } and zeroes the sliders', async () => {
    mockApiWithUniverse(makeUniverse({ 1: 255, 2: 128 }));
    infoRef.value = makeInfo({ port1Mode: 'dmx', port2Mode: 'disabled' });
    const wrapper = await mountPage();
    expect((slider(wrapper, 1).element as HTMLInputElement).value).toBe('255');
    vi.mocked(api).mockClear();

    await wrapper.find('.dmx-blackout').trigger('click');
    await flushPromises();

    expect(vi.mocked(api)).toHaveBeenCalledWith('ports/1/dmx', { method: 'POST', json: { blackout: true } });
    expect((slider(wrapper, 1).element as HTMLInputElement).value).toBe('0');
    expect((slider(wrapper, 2).element as HTMLInputElement).value).toBe('0');

    wrapper.unmount();
  });
});

describe('DMX-I-006: firmware without dmx stats', () => {
  it('renders with zero placeholders and no frame-settings card', async () => {
    settingsData.value = makeSettings();
    delete settingsData.value.rs485_1.dmx_slots;
    infoRef.value = makeInfo({ port1Mode: 'disabled', port2Mode: 'disabled', omitDmx: true });

    const wrapper = await mountPage();

    expect(toggleButton(wrapper).classes()).toContain('off');
    expect(wrapper.text().replace(/\s+/g, ' ')).toContain('0 Hz');
    expect(wrapper.find('#rs485_1-dmx_slots').exists()).toBe(false);

    wrapper.unmount();
  });
});

describe('DMX-I-007: port selection', () => {
  it('Port 2 reads ports/2/dmx and derives the toggle from rs485_2', async () => {
    infoRef.value = makeInfo({ port1Mode: 'disabled', port2Mode: 'dmx' });
    const wrapper = await mountPage();
    expect(toggleButton(wrapper).classes()).toContain('off');
    vi.mocked(api).mockClear();

    const tabs = wrapper.findAll('.dmx-port-tab');
    await tabs[1].trigger('click');
    await flushPromises();

    expect(vi.mocked(api)).toHaveBeenCalledWith('ports/2/dmx');
    expect(toggleButton(wrapper).classes()).toContain('on');

    await toggleButton(wrapper).trigger('click');
    await flushPromises();
    expect(vi.mocked(api)).toHaveBeenCalledWith('ports/2/mode', { method: 'POST', json: { mode: 'disabled' } });

    wrapper.unmount();
  });
});
