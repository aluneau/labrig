import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert, Button, Flex, FlexItem, Label, LabelGroup, Popover, SearchInput, Spinner, Switch,
  TextInput, ToggleGroup, ToggleGroupItem, Tooltip,
} from '@patternfly/react-core';
import { AngleDownIcon, AngleRightIcon, DownloadIcon, FilterIcon, SyncAltIcon } from '@patternfly/react-icons';
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

/** Search box text -> include regex: plain text is matched literally, /…/ is a regex */
export function searchToRegex(search: string): string {
  const t = search.trim();
  if (t.length > 2 && t.startsWith('/') && t.endsWith('/')) return t.slice(1, -1);
  return t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/** Filters a parent sets in one go (quick scenarios, "Show lines" of a finding); `key` changes to re-apply */
export interface LogPreset {
  key: number;
  level?: '' | 'warning' | 'error';
  search?: string;
  problemsOnly?: boolean;
  categories?: string[];
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
  preset?: LogPreset | null;
  /** source-specific options shown under "More filters" (units, boot, since, lines…) */
  moreFilters?: React.ReactNode;
  /** how many of those are set (badge on the button) */
  moreFiltersActive?: number;
}

const CONTEXT = 3; // lines kept around each match, shown with the line's expander

export const LogViewer: React.FC<LogViewerProps> = ({
  path, sourceParams, filename, idleMessage, preset, moreFilters, moreFiltersActive = 0,
}) => {
  const { readOnly } = useAuth();
  const [level, setLevel] = useState<'' | 'warning' | 'error'>('');
  const [search, setSearch] = useState('');
  const [searchDraft, setSearchDraft] = useState('');
  const [exclude, setExclude] = useState('');
  const [problemsOnly, setProblemsOnly] = useState(false);
  const [categories, setCategories] = useState<string[]>([]);
  const [showMore, setShowMore] = useState(false);
  const [follow, setFollow] = useState(false);
  const [result, setResult] = useState<LogResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [rules, setRules] = useState<LogRules | null>(null);
  const boxRef = useRef<HTMLDivElement>(null);
  const seq = useRef(0);

  useEffect(() => { loadRules().then(setRules).catch(() => undefined); }, []);

  useEffect(() => {
    if (!preset) return;
    setLevel(preset.level ?? '');
    setSearch(preset.search ?? '');
    setSearchDraft(preset.search ?? '');
    setProblemsOnly(preset.problemsOnly ?? false);
    setCategories(preset.categories ?? []);
    setExclude('');
    setExpanded(new Set());
  }, [preset]);

  const include = useMemo(() => searchToRegex(search), [search]);
  const params: LogParams = useMemo(() => ({
    ...sourceParams, level: level || null, include: include || null, exclude: exclude || null,
    problems_only: problemsOnly, category: categories.join(',') || null, context: CONTEXT,
  }), [sourceParams, level, include, exclude, problemsOnly, categories]);
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
  // newest lines at the end, like `tail`: land there on every new result (and while following)
  useEffect(() => {
    if (boxRef.current) boxRef.current.scrollTop = boxRef.current.scrollHeight;
  }, [result]);

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

  const filtering = !!(level || search || exclude || problemsOnly || categories.length);
  const renderLine = (e: LogEntry) => {
    const rxs: RegExp[] = [];
    e.rules.forEach((id) => { const r = ruleById[id]?.rx; if (r) rxs.push(r); });
    if (includeRx) rxs.push(includeRx);
    const hasCtx = !!(e.before?.length || e.after?.length);
    const open = expanded.has(e.n);
    const ctxLine = (t: string, n: number) => (
      <div key={`c${n}`} className="vmm-log-line vmm-log-ctx"><span className="vmm-log-n">{n}</span><span className="vmm-log-text">{t}</span></div>
    );
    return (
      <div key={e.n} className="vmm-log-entry" data-level={e.level}>
        {open && e.before?.map((t, i) => ctxLine(t, e.n - (e.before?.length || 0) + i))}
        <div className="vmm-log-line" style={{ borderLeftColor: LEVEL_COLOR[e.level] }}>
          <span className="vmm-log-n">
            {hasCtx ? (
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
        <FlexItem style={{ minWidth: 260, flex: '1 1 260px', maxWidth: 420 }}>
          <SearchInput id="log-search" placeholder="Search lines (text, or /regex/)" value={searchDraft}
            onChange={(_e, v) => setSearchDraft(v)}
            onSearch={(_e, v) => setSearch(v)}
            onClear={() => { setSearchDraft(''); setSearch(''); }} />
        </FlexItem>
        <FlexItem>
          <ToggleGroup aria-label="Level">
            <ToggleGroupItem text="All" buttonId="log-level-all" isSelected={level === ''} onChange={() => setLevel('')} />
            <ToggleGroupItem text="Warnings+" buttonId="log-level-warning" isSelected={level === 'warning'} onChange={() => setLevel('warning')} />
            <ToggleGroupItem text="Errors" buttonId="log-level-error" isSelected={level === 'error'} onChange={() => setLevel('error')} />
          </ToggleGroup>
        </FlexItem>
        <FlexItem>
          <Tooltip content="Only lines matching a known problem (OOM, x509, DNS, image pulls, SR-IOV…)">
            <Switch id="log-problems" label="Problems only" isChecked={problemsOnly} onChange={(_e, v) => setProblemsOnly(v)} />
          </Tooltip>
        </FlexItem>
        <FlexItem>
          <Button variant="link" id="log-more" icon={<FilterIcon />} onClick={() => setShowMore(!showMore)} aria-expanded={showMore}>
            More filters{moreFiltersActive + (exclude ? 1 : 0) ? ` (${moreFiltersActive + (exclude ? 1 : 0)})` : ''}
          </Button>
        </FlexItem>
        {filtering && (
          <FlexItem>
            <Button variant="link" isInline id="log-clear" onClick={() => {
              setLevel(''); setSearch(''); setSearchDraft(''); setExclude(''); setProblemsOnly(false); setCategories([]);
            }}>Clear filters</Button>
          </FlexItem>
        )}
        <FlexItem align={{ default: 'alignRight' }}>
          <Flex spaceItems={{ default: 'spaceItemsSm' }} alignItems={{ default: 'alignItemsCenter' }}>
            <Tooltip content="Reload every 5 s and stay at the bottom">
              <Switch id="log-follow" label="Follow" isChecked={follow} onChange={(_e, v) => setFollow(v)} />
            </Tooltip>
            <Tooltip content="Reload">
              <Button variant="plain" aria-label="Reload" icon={<SyncAltIcon />} onClick={() => load()} isDisabled={!!idleMessage} />
            </Tooltip>
            <Tooltip content="Download the lines shown (as text)">
              <Button variant="plain" aria-label="Download" icon={<DownloadIcon />} onClick={download} isDisabled={!result || !!idleMessage} />
            </Tooltip>
          </Flex>
        </FlexItem>
      </Flex>
      {showMore && (
        <div className="vmm-log-more" id="log-more-panel">
          <Flex spaceItems={{ default: 'spaceItemsSm' }} alignItems={{ default: 'alignItemsCenter' }}>
            {moreFilters}
            <FlexItem><CommitInput id="log-exclude" value={exclude} onCommit={setExclude} placeholder="Hide lines matching (regex)" width={220} /></FlexItem>
          </Flex>
        </div>
      )}

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
            {loading ? <Spinner size="sm" /> : null} {result.matched} of {result.total_lines} lines
            {result.truncated ? ` (the ${result.returned} most recent shown)` : ''}
            {filtering && result.entries.some((x) => x.before?.length || x.after?.length) ? ' · ▸ on a line shows the lines around it' : ''}
            {' · '}
            <Popover bodyContent={<code>{result.command}</code>} headerContent="Command run">
              <Button variant="link" isInline style={{ fontSize: 'inherit' }}>command</Button>
            </Popover>
          </div>
        </>
      )}
      {loading && !result && !idleMessage && <Spinner size="lg" />}
      {result && !idleMessage && (
        <div className="vmm-log-box" ref={boxRef} id="log-box">
          {result.entries.length ? result.entries.map(renderLine)
            : (
              <div className="vmm-log-meta" style={{ padding: 8 }}>
                {problemsOnly && !search && !level && !categories.length
                  ? '✓ No known problem in these lines.' : 'No line matches these filters.'}
                {filtering && <> <Button variant="link" isInline onClick={() => {
                  setLevel(''); setSearch(''); setSearchDraft(''); setExclude(''); setProblemsOnly(false); setCategories([]);
                }}>Show all lines</Button></>}
              </div>
            )}
        </div>
      )}
    </div>
  );
};
