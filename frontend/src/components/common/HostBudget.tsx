import React, { useEffect, useState } from 'react';
import { Checkbox, HelperText, HelperTextItem } from '@patternfly/react-core';
import { BudgetRequest, BudgetResult } from '../../types';
import { hostApi } from '../../services/api';

const gib = (mib: number | null | undefined) => {
  if (mib === null || mib === undefined) return '?';
  const g = mib / 1024;
  return `${g >= 10 || Number.isInteger(g) ? g.toFixed(0) : g.toFixed(1)} GiB`;
};

/** Dry run of the create-time host budget (POST /hosts/budget), re-run 400 ms after the form stops changing */
export function useHostBudget(req: BudgetRequest | null): BudgetResult | null {
  const [result, setResult] = useState<BudgetResult | null>(null);
  const key = req ? JSON.stringify(req) : '';
  useEffect(() => {
    if (!key) {
      setResult(null);
      return undefined;
    }
    let live = true;
    const timer = setTimeout(() => {
      hostApi.budget(JSON.parse(key))
        .then((r) => { if (live) setResult(r); })
        .catch(() => { if (live) setResult(null); }); // a half-typed form: no line rather than an error
    }, 400);
    return () => { live = false; clearTimeout(timer); };
  }, [key]);
  return result;
}

/** True when the create would be refused (409) and "Create anyway" isn't ticked */
export const budgetBlocks = (r: BudgetResult | null, force: boolean) => !!r && r.blocking && !force;

interface Props {
  result: BudgetResult | null;
  force: boolean;
  onForce: (force: boolean) => void;
}

/** "Host resources" line of the create forms: needs vs available, warnings (yellow), problems (red) + Create anyway */
export const HostBudget: React.FC<Props> = ({ result, force, onForce }) => {
  if (!result) return null;
  const { need, host } = result;
  if (!need.memory_mib && !need.vcpus && !need.disk_gib) {
    return result.note ? <HelperText data-testid="host-budget"><HelperTextItem>{result.note}</HelperTextItem></HelperText> : null;
  }
  const pool = host.pools[0];
  const variant = result.problems.length ? 'error' : result.warnings.length ? 'warning' : 'success';
  return (
    <div data-testid="host-budget" data-state={variant} style={{ marginTop: 'var(--pf-v5-global--spacer--sm)' }}>
      <HelperText>
        <HelperTextItem variant={variant === 'success' ? 'default' : variant} hasIcon={variant !== 'success'}>
          <strong>Host resources:</strong> needs {gib(need.memory_mib)} RAM, {need.vcpus} vCPU
          {need.disk_gib ? `, ${need.disk_gib.toFixed(0)} GiB disk` : ''}
          {' / available '}{gib(host.usable_mib !== null ? Math.max(host.usable_mib, 0) : null)} RAM
          {host.memory_total_mib ? ` (host ${gib(host.memory_total_mib)})` : ''}, {host.cpus} CPUs
          {pool && pool.free_gib !== null ? `, ${pool.free_gib.toFixed(0)} GiB free in ${pool.name}` : ''}
        </HelperTextItem>
        {result.problems.map((p) => <HelperTextItem key={p} variant="error" hasIcon>{p}</HelperTextItem>)}
        {result.warnings.map((w) => <HelperTextItem key={w} variant="warning" hasIcon>{w}</HelperTextItem>)}
        {result.note && <HelperTextItem>{result.note}</HelperTextItem>}
      </HelperText>
      {result.blocking && (
        <Checkbox
          id="budget-force"
          label="Create anyway (the host may swap or kill other programs)"
          isChecked={force}
          onChange={(_e, v) => onForce(v)}
        />
      )}
    </div>
  );
};
