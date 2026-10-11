import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert, Button, ExpandableSection, FlexItem, FormSelect, FormSelectOption, Label, Spinner, Tab, Tabs, TabTitleText,
} from '@patternfly/react-core';
import { logsApi } from '../../services/api';
import { LogLevel, LogParams, LogResult, LogRules } from '../../types/logs';
import { errorText } from '../../utils/format';
import { useAuth } from '../../hooks/useAuth';
import { CommitInput, LogPreset, LogViewer } from './LogViewer';

type Source = 'journal' | 'dmesg' | 'cloud-init' | 'file';
const SOURCES: { key: Source; label: string; help: string }[] = [
  { key: 'journal', label: 'Journal', help: 'journalctl: services and system' },
  { key: 'dmesg', label: 'Kernel', help: 'dmesg: drivers, hardware, OOM' },
  { key: 'cloud-init', label: 'cloud-init', help: 'first boot provisioning' },
  { key: 'file', label: 'File', help: 'any file in the VM (tail)' },
];
const SCANNED: Source[] = ['journal', 'dmesg', 'cloud-init'];

/** Quick scenarios: source options + filters set in one click */
interface Scenario {
  id: string;
  label: string;
  help: string;
  source: Source;
  unit?: string;
  boot?: string;
  filters: Omit<LogPreset, 'key'>;
}
const SCENARIOS: Scenario[] = [
  { id: 'errors', label: 'Errors this boot', help: 'Journal, errors only', source: 'journal', filters: { level: 'error' } },
  { id: 'previous', label: 'Previous boot (crash?)', help: 'Warnings and errors of the boot before this one: why did it reboot / hang?', source: 'journal', boot: '-1', filters: { level: 'warning' } },
  { id: 'k8s', label: 'Kubernetes', help: 'kubelet, container runtimes, k3s', source: 'journal', unit: 'kubelet, crio, containerd, k3s, k3s-agent', filters: { level: 'warning' } },
  { id: 'network', label: 'Network', help: 'NetworkManager, networkd, DNS / DHCP services', source: 'journal', unit: 'NetworkManager, systemd-networkd, systemd-resolved, dnsmasq, chronyd', filters: { level: 'warning' } },
  { id: 'storage', label: 'Storage', help: 'Kernel: disks, file systems, I/O errors', source: 'dmesg', filters: { search: '/(sd[a-z]|vd[a-z]|nvme|xfs|ext4|i\\/o|blk|scsi|ata)/' } },
  { id: 'nic', label: 'SR-IOV / NIC', help: 'Kernel: NIC drivers, VFs, VFIO / IOMMU, link state', source: 'dmesg', filters: { search: '/(sriov|\\bvf\\b|vfio|iommu|dmar|igb|ixgbe|i40e|ice|mlx|bnxt|link (is )?(up|down)|eth\\d|enp)/' } },
];

interface Finding {
  category: string;
  label: string;
  hint: string;
  level: LogLevel;
  count: number;
  sources: Source[];
  example?: string;
  exampleSource?: Source;
}

/** Scan journal + kernel + cloud-init of this boot for known problems: one card per category */
const Findings: React.FC<{
  vmId: number; onShow: (source: Source, category: string) => void; reloadKey: number;
}> = ({ vmId, onShow, reloadKey }) => {
  const [results, setResults] = useState<Partial<Record<Source, LogResult | string>> | null>(null);
  const [rules, setRules] = useState<LogRules | null>(null);

  useEffect(() => {
    let live = true;
    setResults(null);
    logsApi.rules().then((r) => live && setRules(r)).catch(() => undefined);
    Promise.all(SCANNED.map((source) => logsApi.get(`/vms/${vmId}/logs`, {
      source, lines: 5000, boot: source === 'journal' ? '0' : null, problems_only: true, context: 0, max_entries: 500,
    }).then((r) => [source, r] as const).catch((e) => [source, errorText(e)] as const)))
      .then((all) => { if (live) setResults(Object.fromEntries(all)); });
    return () => { live = false; };
  }, [vmId, reloadKey]);

  const findings = useMemo(() => {
    if (!results) return [];
    const catOf: Record<string, { category: string; level: LogLevel }> = {};
    (rules?.rules || []).forEach((r) => { catOf[r.id] = { category: r.category, level: r.level }; });
    const byCat: Record<string, Finding> = {};
    SCANNED.forEach((source) => {
      const r = results[source];
      if (!r || typeof r === 'string') return;
      r.summary.forEach((s) => {
        const f = byCat[s.category] || (byCat[s.category] = {
          category: s.category, label: s.label, hint: s.hint, level: 'warning', count: 0, sources: [],
        });
        f.count += s.count;
        f.sources.push(source);
        if (s.rules.some((x) => x.level === 'error')) f.level = 'error';
        const last = [...r.entries].reverse().find((e) => e.rules.some((id) => catOf[id]?.category === s.category));
        if (last) { f.example = last.text; f.exampleSource = source; }
      });
    });
    return Object.values(byCat).sort((a, b) => (a.level === b.level ? b.count - a.count : a.level === 'error' ? -1 : 1));
  }, [results, rules]);

  if (!results) {
    return <div className="vmm-log-meta" style={{ marginBottom: 12 }}><Spinner size="sm" /> Scanning the journal, kernel log and cloud-init of this boot for known problems…</div>;
  }
  const failed = SCANNED.filter((s) => typeof results[s] === 'string');
  if (failed.length === SCANNED.length) {
    return <Alert variant="warning" isInline title="Logs can't be read" style={{ marginBottom: 12 }}>{results.journal as string}</Alert>;
  }
  return (
    <div id="log-findings" style={{ marginBottom: 12 }}>
      {findings.length === 0 ? (
        <Alert variant="success" isInline isPlain title="No known problem in the journal, kernel log and cloud-init of this boot." />
      ) : (
        <div className="vmm-findings">
          {findings.map((f) => (
            <div key={f.category} className="vmm-finding" data-level={f.level} data-category={f.category}>
              <div>
                <Label isCompact color={f.level === 'error' ? 'red' : 'orange'}>{f.label}</Label>{' '}
                <strong>{f.count}</strong> line{f.count > 1 ? 's' : ''}
                <span className="vmm-log-meta"> · {f.sources.map((s) => SOURCES.find((x) => x.key === s)?.label).join(', ')}</span>
              </div>
              <div style={{ fontSize: 'var(--pf-v5-global--FontSize--sm)' }}>{f.hint}</div>
              {f.example && <div className="vmm-finding-line" title={f.example}>{f.example}</div>}
              <div>
                <Button variant="link" isInline onClick={() => onShow(f.exampleSource || f.sources[0], f.category)}>Show lines</Button>
              </div>
            </div>
          ))}
        </div>
      )}
      {failed.length > 0 && (
        <div className="vmm-log-meta">Not scanned: {failed.map((s) => `${s} (${results[s]})`).join('; ')}</div>
      )}
    </div>
  );
};

/** Logs of a VM through the guest agent: known problems first, then the lines (shared LogViewer) */
export const VmLogs: React.FC<{ vmId: number; vmName: string }> = ({ vmId, vmName }) => {
  const { readOnly } = useAuth();
  const [source, setSource] = useState<Source>('journal');
  const [unit, setUnit] = useState('');
  const [boot, setBoot] = useState('0');
  const [since, setSince] = useState('');
  const [grep, setGrep] = useState('');
  const [path, setPath] = useState('/var/log/messages');
  const [lines, setLines] = useState('1000');
  const [preset, setPreset] = useState<LogPreset | null>(null);
  const [scenario, setScenario] = useState<string | null>(null);
  const [findingsOpen, setFindingsOpen] = useState(true);
  const [scanKey, setScanKey] = useState(0);

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

  const reset = useCallback((s: Source) => {
    setSource(s); setUnit(''); setBoot('0'); setSince(''); setGrep('');
  }, []);
  const showFinding = useCallback((s: Source, category: string) => {
    reset(s);
    setScenario(null);
    setPreset({ key: Date.now(), problemsOnly: true, categories: [category] });
    document.getElementById('log-lines-anchor')?.scrollIntoView({ behavior: 'smooth' });
  }, [reset]);
  const runScenario = (sc: Scenario) => {
    reset(sc.source);
    if (sc.unit) setUnit(sc.unit);
    if (sc.boot) setBoot(sc.boot);
    setScenario(sc.id);
    setPreset({ key: Date.now(), ...sc.filters });
  };

  if (readOnly) {
    return <Alert variant="info" isInline title="Logs are for admins only: they can hold secrets (passwords, tokens in command lines)." />;
  }

  const active = [unit, since, grep].filter(Boolean).length + (boot !== '0' ? 1 : 0) + (lines !== '1000' ? 1 : 0);
  const moreFilters = (
    <>
      {source === 'journal' && (
        <FlexItem><CommitInput id="log-unit" value={unit} onCommit={setUnit} placeholder="Units (kubelet, crio…)" width={220} /></FlexItem>
      )}
      {(source === 'journal' || source === 'dmesg') && (
        <>
          <FlexItem>
            <FormSelect id="log-boot" aria-label="Boot" value={boot} onChange={(_e, v) => setBoot(v)} style={{ width: 150 }}>
              <FormSelectOption value="0" label="This boot" />
              <FormSelectOption value="-1" label="Previous boot" />
              {source === 'journal' && <FormSelectOption value="all" label="All boots" />}
            </FormSelect>
          </FlexItem>
          <FlexItem><CommitInput id="log-since" value={since} onCommit={setSince} placeholder="Since (-1h, today)" width={150} /></FlexItem>
        </>
      )}
      {source === 'journal' && (
        <FlexItem><CommitInput id="log-grep" value={grep} onCommit={setGrep} placeholder="journalctl --grep (in the VM)" width={210} /></FlexItem>
      )}
      <FlexItem>
        <FormSelect id="log-lines" aria-label="Lines to read" value={lines} onChange={(_e, v) => setLines(v)} style={{ width: 150 }}>
          {['200', '1000', '5000', '20000'].map((n) => <FormSelectOption key={n} value={n} label={`read last ${n}`} />)}
        </FormSelect>
      </FlexItem>
    </>
  );

  return (
    <>
      <ExpandableSection id="log-findings-section" isExpanded={findingsOpen} onToggle={(_e, v) => setFindingsOpen(v)}
        toggleContent={<strong>Known problems found</strong>}>
        <Findings vmId={vmId} onShow={showFinding} reloadKey={scanKey} />
        <Button variant="link" isInline onClick={() => setScanKey(scanKey + 1)} style={{ marginBottom: 8 }}>Scan again</Button>
      </ExpandableSection>

      <div id="log-lines-anchor" style={{ margin: '8px 0' }}>
        <span className="vmm-log-meta" style={{ marginRight: 8 }}>Quick look:</span>
        {SCENARIOS.map((sc) => (
          <Label key={sc.id} id={`log-scenario-${sc.id}`} color="blue" variant={scenario === sc.id ? 'filled' : 'outline'}
            onClick={() => runScenario(sc)} style={{ marginRight: 6, marginBottom: 4 }} title={sc.help}>
            {sc.label}
          </Label>
        ))}
      </div>

      <Tabs activeKey={source} onSelect={(_e, k) => { reset(k as Source); setScenario(null); }} aria-label="Log source"
        style={{ marginBottom: 8 }}>
        {SOURCES.map((s) => (
          <Tab key={s.key} eventKey={s.key} id={`log-source-${s.key}`}
            title={<TabTitleText>{s.label}</TabTitleText>} aria-label={`${s.label}: ${s.help}`} />
        ))}
      </Tabs>
      {source === 'file' && (
        <div style={{ marginBottom: 8 }}>
          <CommitInput id="log-path" value={path} onCommit={setPath} placeholder="/var/log/messages" width={360} label="File path in the VM" />
        </div>
      )}
      <LogViewer path={`/vms/${vmId}/logs`} sourceParams={params} filename={`${vmName}-${source}`}
        preset={preset} moreFilters={moreFilters} moreFiltersActive={active} />
    </>
  );
};
