import React, { useMemo, useState } from 'react';
import { Flex, FlexItem, FormSelect, FormSelectOption, ToggleGroup, ToggleGroupItem } from '@patternfly/react-core';
import { LogParams } from '../../types/logs';
import { CommitInput, LogViewer } from './LogViewer';

type Source = 'journal' | 'dmesg' | 'cloud-init' | 'file';
const SOURCES: { key: Source; label: string; help: string }[] = [
  { key: 'journal', label: 'Journal', help: 'journalctl' },
  { key: 'dmesg', label: 'Kernel', help: 'dmesg -T' },
  { key: 'cloud-init', label: 'cloud-init', help: 'status --long + its logs' },
  { key: 'file', label: 'File', help: 'any file (tail)' },
];

/** Logs of a VM through the guest agent: source picker + the shared LogViewer */
export const VmLogs: React.FC<{ vmId: number; vmName: string }> = ({ vmId, vmName }) => {
  const [source, setSource] = useState<Source>('journal');
  const [unit, setUnit] = useState('');
  const [boot, setBoot] = useState('0');
  const [since, setSince] = useState('');
  const [grep, setGrep] = useState('');
  const [path, setPath] = useState('/var/log/messages');
  const [lines, setLines] = useState('1000');

  const params: LogParams = useMemo(() => {
    const p: LogParams = { source, lines };
    if (source === 'journal') {
      p.boot = boot;
      p.since = since || null;
      p.unit = unit ? unit.split(/[\s,]+/).filter(Boolean) : null;
      p.grep = grep || null;
    }
    if (source === 'dmesg') {
      p.boot = boot === '-1' ? '-1' : null; // current boot: the live ring buffer
      p.since = since || null;
    }
    if (source === 'file') p.path = path;
    return p;
  }, [source, unit, boot, since, grep, path, lines]);

  return (
    <>
      <Flex spaceItems={{ default: 'spaceItemsSm' }} alignItems={{ default: 'alignItemsCenter' }} style={{ marginBottom: 8 }}>
        <FlexItem>
          <ToggleGroup aria-label="Log source">
            {SOURCES.map((s) => (
              <ToggleGroupItem key={s.key} text={s.label} buttonId={`log-source-${s.key}`} isSelected={source === s.key}
                onChange={() => setSource(s.key)} aria-label={`${s.label}: ${s.help}`} />
            ))}
          </ToggleGroup>
        </FlexItem>
        {source === 'journal' && (
          <FlexItem><CommitInput id="log-unit" value={unit} onCommit={setUnit} placeholder="Units (kubelet, crio…)" width={190} /></FlexItem>
        )}
        {(source === 'journal' || source === 'dmesg') && (
          <>
            <FlexItem>
              <FormSelect id="log-boot" aria-label="Boot" value={boot} onChange={(_e, v) => setBoot(v)} style={{ width: 150 }}>
                <FormSelectOption value="0" label="Current boot" />
                <FormSelectOption value="-1" label="Previous boot" />
                {source === 'journal' && <FormSelectOption value="all" label="All boots" />}
              </FormSelect>
            </FlexItem>
            <FlexItem><CommitInput id="log-since" value={since} onCommit={setSince} placeholder="Since (-1h, today)" width={150} /></FlexItem>
          </>
        )}
        {source === 'journal' && (
          <FlexItem><CommitInput id="log-grep" value={grep} onCommit={setGrep} placeholder="journalctl --grep" width={160} /></FlexItem>
        )}
        {source === 'file' && (
          <FlexItem>
            <CommitInput id="log-path" value={path} onCommit={setPath} placeholder="/var/log/messages" width={280} label="File path in the VM" />
          </FlexItem>
        )}
        <FlexItem>
          <FormSelect id="log-lines" aria-label="Lines to read" value={lines} onChange={(_e, v) => setLines(v)} style={{ width: 140 }}>
            {['200', '1000', '5000', '20000'].map((n) => <FormSelectOption key={n} value={n} label={`last ${n} lines`} />)}
          </FormSelect>
        </FlexItem>
      </Flex>
      <LogViewer path={`/vms/${vmId}/logs`} sourceParams={params} filename={`${vmName}-${source}`} />
    </>
  );
};
