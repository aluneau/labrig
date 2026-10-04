import { useCallback, useEffect, useRef, useState } from 'react';
import { VM, VMPowerAction } from '../types';
import { vmApi } from '../services/api';
import { errorText } from '../utils/format';
import { LiveEvent } from './useEvents';

export const PENDING_LABELS: Record<VMPowerAction, string> = {
  start: 'Starting…',
  stop: 'Shutting down…',
  force_stop: 'Powering off…',
  reboot: 'Rebooting…',
  suspend: 'Pausing…',
  resume: 'Resuming…',
};

// ACPI shutdown/reboot only *ask* the guest; give up waiting after these delays
const TIMEOUTS_MS: Partial<Record<VMPowerAction, number>> = { stop: 120_000, reboot: 60_000 };

interface Pending {
  action: VMPowerAction;
  name: string;
  fromStatus: string;
  since: number;
}

/**
 * Power actions with a "pending" state per VM that lasts until libvirt reports
 * the new state through a live event (shutdown can take a while).
 */
export function useVmPower(onError: (message: string) => void) {
  const [pending, setPending] = useState<Record<number, Pending>>({});
  const onErrorRef = useRef(onError);
  onErrorRef.current = onError;

  const clear = useCallback((vmId: number) => {
    setPending((prev) => {
      if (!(vmId in prev)) return prev;
      const next = { ...prev };
      delete next[vmId];
      return next;
    });
  }, []);

  const run = useCallback(async (vm: VM, action: VMPowerAction) => {
    setPending((prev) => ({ ...prev, [vm.id]: { action, name: vm.name, fromStatus: vm.status, since: Date.now() } }));
    try {
      const updated = await vmApi.power(vm.id, action);
      // stop/reboot return before the guest acts; the others are done when the call returns
      if (!TIMEOUTS_MS[action] && updated.status !== vm.status) clear(vm.id);
      return updated;
    } catch (err) {
      clear(vm.id);
      onErrorRef.current(`${vm.name}: ${errorText(err)}`);
      return null;
    }
  }, [clear]);

  /** Feed live VM events here (vmId resolved by the caller from the event uuid) */
  const onVmEvent = useCallback((vmId: number, event: LiveEvent) => {
    setPending((prev) => {
      const p = prev[vmId];
      if (!p) return prev;
      const done = p.action === 'reboot'
        ? event.event === 'rebooted' || event.state !== 'running'
        : event.state !== p.fromStatus;
      if (!done) return prev;
      const next = { ...prev };
      delete next[vmId];
      return next;
    });
  }, []);

  useEffect(() => {
    if (!Object.keys(pending).length) return;
    const timer = setInterval(() => {
      const now = Date.now();
      Object.entries(pending).forEach(([id, p]) => {
        const timeout = TIMEOUTS_MS[p.action];
        if (timeout && now - p.since > timeout) {
          clear(Number(id));
          if (p.action === 'stop') {
            onErrorRef.current(
              `${p.name} did not shut down after ${timeout / 1000}s. The guest may ignore ACPI shutdown requests: use Force off.`,
            );
          }
        }
      });
    }, 2000);
    return () => clearInterval(timer);
  }, [pending, clear]);

  return { pending, run, onVmEvent };
}
