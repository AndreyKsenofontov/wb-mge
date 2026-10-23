import { ref } from 'vue';
import { api } from '@/utils/api';

const hostname = ref<string>('');
// Device model as the firmware reports it ("WB-MGE v.3", "WB-MGU v.1"); '' on firmware that
// does not send it yet, in which case the UI falls back to its WB-MGE wording.
const model = ref<string>('');
let fetched = false;

// The WB-MGU is the DMX gateway build of this firmware; its pages word themselves accordingly.
export const isMgu = (value: string): boolean => value.toUpperCase().startsWith('WB-MGU');

export const setHostname = (value: string) => {
  hostname.value = value;
};

export const useHostname = () => {
  if (!fetched) {
    fetched = true;
    api<{ hostname: string; model?: string }>('hostname')
      .then(data => {
        hostname.value = data.hostname;
        model.value = data.model ?? '';
      })
      .catch(() => {
        fetched = false;
      });
  }
  return { hostname, model };
};
