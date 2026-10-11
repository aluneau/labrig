import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert, Button, Flex, FlexItem, FormSelect, FormSelectOption, Label, LabelGroup, Popover, Spinner, Switch,
  TextInput, ToggleGroup, ToggleGroupItem, Tooltip,
} from '@patternfly/react-core';
import { AngleDownIcon, AngleRightIcon, DownloadIcon, SyncAltIcon } from '@patternfly/react-icons';
import { logsApi } from '../../services/api';
import { LogEntry, LogLevel, LogParams, LogResult, LogRules } from '../../types/logs';
import { errorText } from '../../utils/format';
import { useAuth } from '../../hooks/useAuth';
import './logs.css';

/** Text input that applies its value on Enter or when it loses focus (no request per keystroke) */
export const CommitInput: React.FC<{
  id: string; value: string; onCommit: (v: string) => void; placeholder?: string; label?: string; width?: number;
}> = ({ id, value, onCommit, placeholder, label, width }) => {
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);
  return (
    <TextInput id={id} aria-label={label || placeholder || id} value={draft} placeholder={placeholder}
      style={width ? { width } : undefined}
      onChange={(_e, v) => setDraft(v)}
      onKeyDown={(e) => { if (e.key === 'Enter') onCommit(draft.trim()); }}
      onBlur={() => { if (draft.trim() !== value) onCommit(draft.trim()); }} />
  );
};

let rulesCache: Promise<LogRules> | null = null;
const loadRules = () => {
  if (!rulesCache) rulesCache = logsApi.rules().catch((e) => { rulesCache = null; throw e; });
  return rulesCache;
};

const LEVEL_COLOR: Record<LogLevel, string> = {
  error: 'var(--pf-v5-global--danger-color--100)',
  warning: 'var(--pf-v5-global--warning-color--100)',
  info: 'transparent',
};

function compile(pattern: string): RegExp | null {
  try {
    return new RegExp(pattern, 'gi');
  } catch {
    return null; // Python-only syntax: no highlight, the server still matched it
  }
}

/** Split text into plain / highlighted parts for every regex match */
function highlight(text: string, regexes: RegExp[]): React.ReactNode {
  const ranges: [number, number][] = [];
  regexes.forEach((rx) => {
    rx.lastIndex = 0;
    let m: RegExpExecArray | null;
    let guard = 0;
    while ((m = rx.exec(text)) && guard++ < 50) {
      if (m[0].length === 0) { rx.lastIndex++; continue; }
      ranges.push([m.index, m.index + m[0].length]);
    }
  });
  if (!ranges.length) return text;
  ranges.sort((a, b) => a[0] - b[0]);
  const merged: [number, number][] = [];
  ranges.forEach((r) => {
    const last = merged[merged.length - 1];
    if (last && r[0] <= last[1]) last[1] = Math.max(last[1], r[1]);
    else merged.push([r[0], r[1]]);
  });
  const out: React.ReactNode[] = [];
  let pos = 0;
  merged.forEach(([a, b], i) => {
    if (a > pos) out.push(text.slice(pos, a));
    out.push(<mark key={i} className="vmm-log-mark">{text.slice(a, b)}</mark>);
    pos = b;
  });
  if (pos < text.length) out.push(text.slice(pos));
  return out;
}

export interface LogViewerProps {
  /** /vms/{id}/logs or /clusters/{id}/logs */
  path: string;
  /** source options (journal unit, pod, …): a change reloads */
  sourceParams: LogParams;
  /** base name of the downloaded file */
  filename: string;
  /** don't load yet (e.g. no pod chosen) and show this instead */
  idleMessage?: string | null;
}

export const LogViewer: React.FC<LogViewerProps> = ({ path, sourceParams, filename, idleMessage }) => {
  const { readOnly } = useAuth();
  const [level, setLevel] = useState<'' | 'warning' | 'error'>('');
  const [include, setInclude] = useState('');
  const [exclude, setExclude] = useState('');
  const [problemsOnly, setProblemsOnly] = useState(false);
  const [categories, setCategories] = useState<string[]>([]);
  const [context, setContext] = useState(3);
  const [showContext, setShowContext] = useState(false);
  const [follow, setFollow] = useState(false);
  const [result, setResult] = useState<LogResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [rules, setRules] = useState<LogRules | null>(null);
  const boxRef = useRef<HTMLDivElement>(null);
  const seq = useRef(0);

  useEffect(() => { loadRules().then(setRules).catch(() => undefined); }, []);

  const params: LogParams = useMemo(() => ({
    ...sourceParams, level: level || null, include: include || null, exclude: exclude || null,
    problems_only: problemsOnly, category: categories.join(',') || null, context,
  }), [sourceParams, level, include, exclude, problemsOnly, categories, context]);
  const key = JSON.stringify(params);

  const load = useCallback(async (quiet = false) => {
    if (idleMessage) return;
    const mine = ++seq.current;
    if (!quiet) setLoading(true);
    try {
      const r = await logsApi.get(path, params);
      if (mine !== seq.current) return;
      setResult(r);
      setError(null);
    } catch (e) {
      if (mine === seq.current) setError(errorText(e));
    } finally {
      if (mine === seq.current) setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, key, idleMessage]);

  useEffect(() => { if (!readOnly) load(); }, [load, readOnly]);
  useEffect(() => {
    if (!follow) return undefined;
    const t = window.setInterval(() => load(true), 5000);
    return () => window.clearInterval(t);
  }, [follow, load]);
  useEffect(() => {
    if (follow && boxRef.current) boxRef.current.scrollTop = boxRef.current.scrollHeight;
  }, [result, follow]);

  const ruleById = useMemo(() => {
    const m: Record<string, { category: string; hint: string; level: LogLevel; rx: RegExp | null }> = {};
    (rules?.rules || []).forEach((r) => { m[r.id] = { category: r.category, hint: r.hint, level: r.level, rx: compile(r.pattern) }; });
    return m;
  }, [rules]);
  const includeRx = useMemo(() => (include ? compile(include) : null), [include]);
  const catLabel = (c: string) => rules?.categories[c] || c;

  if (readOnly) {
    return <Alert variant="info" isInline title="Logs are for admins only: they can hold secrets (passwords, tokens in command lines)." />;
  }

  const toggleCategory = (c: string) =>
    setCategories((prev) => (prev.includes(c) ? prev.filter((x) => x !== c) : [...prev, c]));
  const toggleEntry = (n: number) =>
    setExpanded((prev) => { const s = new Set(prev); s.has(n) ? s.delete(n) : s.add(n); return s; });

  const download = async () => {
    try {
      const text = await logsApi.text(path, { ...params, context: 0, max_entries: 5000 });
      const url = URL.createObjectURL(new Blob([text], { type: 'text/plain' }));
      const a = document.createElement('a');
      a.href = url;
      a.download = `${filename}.log`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e) {
      setError(errorText(e));
    }
  };

  const filtering = !!(level || include || exclude || problemsOnly || categories.length);
  const renderLine = (e: LogEntry) => {
    const rxs: RegExp[] = [];
    e.rules.forEach((id) => { const r = ruleById[id]?.rx; if (r) rxs.push(r); });
    if (includeRx) rxs.push(includeRx);
    const hasCtx = !!(e.before?.length || e.after?.length);
    const open = showContext || expanded.has(e.n);
    const ctxLine = (t: string, n: number) => (
      <div key={`c${n}`} className="vmm-log-line vmm-log-ctx"><span className="vmm-log-n">{n}</span><span className="vmm-log-text">{t}</span></div>
    );
    return (
      <div key={e.n} className="vmm-log-entry" data-level={e.level}>
        {open && e.before?.map((t, i) => ctxLine(t, e.n - (e.before?.length || 0) + i))}
        <div className="vmm-log-line" style={{ borderLeftColor: LEVEL_COLOR[e.level] }}>
          <span className="vmm-log-n">
            {hasCtx && filtering ? (
              <button type="button" className="vmm-log-ctx-btn" aria-label={open ? 'Hide context' : 'Show context'}
                title={open ? 'Hide context' : 'Show context lines'} onClick={() => toggleEntry(e.n)}>
                {open ? <AngleDownIcon /> : <AngleRightIcon />}
              </button>
            ) : null}
            {e.n}
          </span>
          <span className="vmm-log-text">{highlight(e.text, rxs)}</span>
          {e.rules.length > 0 && (
            <span className="vmm-log-labels">
              {e.rules.map((id) => {
                const r = ruleById[id];
                return (
                  <Popover key={id} headerContent={`${catLabel(r?.category || id)} (${id})`} bodyContent={r?.hint || id}>
                    <Label isCompact color={r?.level === 'error' ? 'red' : 'orange'} className="vmm-log-rule"
                      onClick={() => undefined} data-rule={id}>
                      {catLabel(r?.category || id)}
                    </Label>
                  </Popover>
                );
              })}
            </span>
          )}
        </div>
        {open && e.after?.map((t, i) => ctxLine(t, e.n + 1 + i))}
      </div>
    );
  };

  const selectedHints = (result?.summary || []).filter((s) => categories.includes(s.category));

  return (
    <div className="vmm-logs">
      <Flex spaceItems={{ default: 'spaceItemsSm' }} alignItems={{ default: 'alignItemsCenter' }} style={{ marginBottom: 8 }}>
        <FlexItem>
          <ToggleGroup aria-label="Level">
            <ToggleGroupItem text="All" isSelected={level === ''} onChange={() => setLevel('')} />
            <ToggleGroupItem text="Warnings+" isSelected={level === 'warning'} onChange={() => setLevel('warning')} />
            <ToggleGroupItem text="Errors" isSelected={level === 'error'} onChange={() => setLevel('error')} />
          </ToggleGroup>
        </FlexItem>
        <FlexItem><CommitInput id="log-include" value={include} onCommit={setInclude} placeholder="Include regex" width={180} /></FlexItem>
        <FlexItem><CommitInput id="log-exclude" value={exclude} onCommit={setExclude} placeholder="Exclude regex" width={160} /></FlexItem>
        <FlexItem>
          <Switch id="log-problems" label="Known problems only" isChecked={problemsOnly} onChange={(_e, v) => setProblemsOnly(v)} />
        </FlexItem>
        <FlexItem>
          <Tooltip content="Lines of context kept around each match">
            <FormSelect id="log-context" aria-label="Context lines" value={String(context)} style={{ width: 110 }}
              onChange={(_e, v) => setContext(Number(v))}>
              {[0, 1, 3, 5, 10, 20].map((n) => <FormSelectOption key={n} value={String(n)} label={`${n} context`} />)}
            </FormSelect>
          </Tooltip>
        </FlexItem>
        <FlexItem>
          <Switch id="log-show-context" label="Show context" isChecked={showContext} onChange={(_e, v) => setShowContext(v)} />
        </FlexItem>
        <FlexItem align={{ default: 'alignRight' }}>
          <Flex spaceItems={{ default: 'spaceItemsSm' }} alignItems={{ default: 'alignItemsCenter' }}>
            <Switch id="log-follow" label="Follow (5 s)" isChecked={follow} onChange={(_e, v) => setFollow(v)} />
            <Button variant="secondary" icon={<SyncAltIcon />} onClick={() => load()} isDisabled={!!idleMessage}>Refresh</Button>
            <Button variant="secondary" icon={<DownloadIcon />} onClick={download} isDisabled={!result || !!idleMessage}>Download</Button>
          </Flex>
        </FlexItem>
      </Flex>

      {idleMessage ? <Alert variant="info" isInline isPlain title={idleMessage} /> : null}
      {error && <Alert variant="danger" isInline title={error} style={{ marginBottom: 8 }} />}
      {result && !idleMessage && (
        <>
          <Flex spaceItems={{ default: 'spaceItemsSm' }} alignItems={{ default: 'alignItemsCenter' }} style={{ marginBottom: 8 }}>
            <FlexItem>
              <LabelGroup categoryName="Levels" numLabels={3}>
                <Label color="red" variant={level === 'error' ? 'filled' : 'outline'} onClick={() => setLevel(level === 'error' ? '' : 'error')}>
                  Errors {result.levels.error}
                </Label>
                <Label color="orange" variant={level === 'warning' ? 'filled' : 'outline'} onClick={() => setLevel(level === 'warning' ? '' : 'warning')}>
                  Warnings {result.levels.warning}
                </Label>
              </LabelGroup>
            </FlexItem>
            <FlexItem>
              {result.summary.length ? (
                <LabelGroup categoryName="Known problems" numLabels={20} id="log-summary">
                  {result.summary.map((s) => {
                    const isErr = s.rules.some((r) => r.level === 'error');
                    return (
                      <Tooltip key={s.category} content={s.hint}>
                        <Label color={isErr ? 'red' : 'orange'} variant={categories.includes(s.category) ? 'filled' : 'outline'}
                          onClick={() => toggleCategory(s.category)} data-category={s.category}>
                          {s.label} {s.count}
                        </Label>
                      </Tooltip>
                    );
                  })}
                </LabelGroup>
              ) : <span className="vmm-log-meta">No known problem in these lines.</span>}
            </FlexItem>
          </Flex>
          {selectedHints.map((s) => (
            <Alert key={s.category} variant="info" isInline isPlain title={s.label} style={{ marginBottom: 6 }}>
              {s.rules.map((r) => <div key={r.id}><strong>{r.id}</strong> ({r.count}): {r.hint}</div>)}
            </Alert>
          ))}
          {(result.notes || []).map((n) => <Alert key={n} variant="warning" isInline isPlain title={n} style={{ marginBottom: 4 }} />)}
          <div className="vmm-log-meta" style={{ marginBottom: 4 }}>
            {loading ? <Spinner size="sm" /> : null} {result.total_lines} lines read, {result.matched} shown
            {result.truncated ? ` (the ${result.returned} most recent)` : ''}
            {' · '}<code>{result.command}</code>
          </div>
        </>
      )}
      {loading && !result && !idleMessage && <Spinner size="lg" />}
      {result && !idleMessage && (
        <div className="vmm-log-box" ref={boxRef} id="log-box">
          {result.entries.length ? result.entries.map(renderLine)
            : <div className="vmm-log-meta" style={{ padding: 8 }}>No line matches these filters.</div>}
        </div>
      )}
    </div>
  );
};
