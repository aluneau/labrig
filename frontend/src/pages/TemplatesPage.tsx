import React, { useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Alert,
  AlertActionCloseButton,
  Bullseye,
  Button,
  Card,
  CardBody,
  CardFooter,
  CardHeader,
  CardTitle,
  EmptyState,
  EmptyStateBody,
  EmptyStateHeader,
  ExpandableSection,
  Flex,
  FlexItem,
  Gallery,
  Label,
  LabelGroup,
  PageSection,
  SearchInput,
  Spinner,
  ToggleGroup,
  ToggleGroupItem,
} from '@patternfly/react-core';
import { TemplateInfo } from '../types';
import { templateApi } from '../services/api';
import { usePolling } from '../hooks/usePolling';
import { errorText, formatMiB } from '../utils/format';
import { PageHeader } from '../components/common/PageHeader';
import { ConfirmModal } from '../components/common/ConfirmModal';

const resourcesText = (t: TemplateInfo) => {
  const r = t.resources;
  const parts = [];
  if (r.vcpus) parts.push(`${r.vcpus} vCPU`);
  if (r.memory_mb) parts.push(formatMiB(r.memory_mb));
  if (r.disk_gb) parts.push(`${r.disk_gb} GiB disk`);
  return parts.join(' · ') || '—';
};

const matches = (t: TemplateInfo, q: string) => {
  const text = [t.id, t.title, t.summary, ...t.tags, ...t.requires].join(' ').toLowerCase();
  return q.toLowerCase().split(/\s+/).filter(Boolean).every((w) => text.includes(w));
};

export const TemplatesPage: React.FC = () => {
  const { data, error: loadError, loading, reload } = usePolling(templateApi.list, 60000);
  const [query, setQuery] = useState('');
  const [tag, setTag] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [toDelete, setToDelete] = useState<TemplateInfo | null>(null);
  const navigate = useNavigate();

  const templates = useMemo(() => data?.templates || [], [data]);
  const tags = useMemo(() => {
    const count: Record<string, number> = {};
    templates.forEach((t) => t.tags.forEach((x) => { count[x] = (count[x] || 0) + 1; }));
    return Object.keys(count).sort((a, b) => count[b] - count[a] || a.localeCompare(b)).slice(0, 12);
  }, [templates]);
  const shown = templates.filter((t) => matches(t, query) && (!tag || t.tags.includes(tag)));

  return (
    <>
      <PageHeader title="Templates"
        description="Ready-made labs for customer cases: pick one, enter the case number, review the spec and create it. The group page then shows the case guide." />
      <PageSection>
        {(error || loadError) && (
          <Alert variant="danger" isInline title={error || loadError} style={{ marginBottom: 16 }}
            actionClose={error ? <AlertActionCloseButton onClose={() => setError(null)} /> : undefined} />
        )}
        {!!data?.errors.length && (
          <Alert variant="warning" isInline title={`${data.errors.length} template file(s) could not be loaded`} style={{ marginBottom: 16 }} id="tpl-load-errors">
            <ExpandableSection toggleText="Details">
              <ul>{data.errors.map((e) => <li key={e.file}><code>{e.file}</code>: {e.error}</li>)}</ul>
            </ExpandableSection>
          </Alert>
        )}
        <Flex alignItems={{ default: 'alignItemsCenter' }} style={{ marginBottom: 16 }}>
          <FlexItem style={{ minWidth: 260 }}>
            <SearchInput id="tpl-search" placeholder="Search templates" value={query} onChange={(_e, v) => setQuery(v)} onClear={() => setQuery('')} />
          </FlexItem>
          <FlexItem>
            <ToggleGroup aria-label="Filter by tag">
              <ToggleGroupItem text="All" isSelected={!tag} onChange={() => setTag(null)} />
              {tags.map((x) => <ToggleGroupItem key={x} text={x} isSelected={tag === x} onChange={() => setTag(tag === x ? null : x)} />)}
            </ToggleGroup>
          </FlexItem>
        </Flex>
        {loading && !data && <Bullseye><Spinner size="xl" /></Bullseye>}
        {data && !shown.length && (
          <EmptyState>
            <EmptyStateHeader titleText="No template matches" headingLevel="h2" />
            <EmptyStateBody>Clear the search, or save one of your lab groups as a template (group page → Save as template).</EmptyStateBody>
          </EmptyState>
        )}
        <Gallery hasGutter minWidths={{ default: '300px' }}>
          {shown.map((t) => (
            <Card key={t.id} id={`tpl-${t.id}`} isFlat>
              <CardHeader>
                <CardTitle>
                  {t.title}{' '}
                  {t.custom && <Label color="purple" isCompact>custom</Label>}{' '}
                  {t.heavy && <Label color="orange" isCompact>heavy</Label>}
                </CardTitle>
              </CardHeader>
              <CardBody>
                <p style={{ marginBottom: 12 }}>{t.summary}</p>
                <div style={{ fontSize: 'var(--pf-v5-global--FontSize--sm)', color: 'var(--pf-v5-global--Color--200)', marginBottom: 8 }}>
                  <div><strong>Resources:</strong> {resourcesText(t)}</div>
                  {t.has_cluster && <div><strong>Cluster:</strong> {t.cluster_type}</div>}
                  {!!t.requires.length && <div><strong>Requires:</strong> {t.requires.join(', ')}</div>}
                </div>
                <LabelGroup numLabels={6}>
                  {t.tags.map((x) => <Label key={x} isCompact onClick={() => setTag(x)}>{x}</Label>)}
                </LabelGroup>
              </CardBody>
              <CardFooter>
                <Button size="sm" onClick={() => navigate(`/templates/${t.id}`)}>Use template</Button>
                {t.custom && (
                  <Button size="sm" variant="link" isDanger onClick={() => setToDelete(t)} style={{ marginLeft: 8 }}>Delete</Button>
                )}
              </CardFooter>
            </Card>
          ))}
        </Gallery>
      </PageSection>
      <ConfirmModal title={`Delete template ${toDelete?.title}?`} isOpen={!!toDelete} confirmLabel="Delete"
        onConfirm={async () => {
          try {
            await templateApi.delete(toDelete!.id);
            reload();
          } catch (err) {
            setError(errorText(err));
          }
        }}
        onClose={() => setToDelete(null)}>
        Deletes the template file only: labs created from it keep their guide.
      </ConfirmModal>
    </>
  );
};
