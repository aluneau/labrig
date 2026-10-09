import React, { useCallback, useEffect, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import {
  ActionGroup,
  Alert,
  Breadcrumb,
  BreadcrumbItem,
  Bullseye,
  Button,
  Checkbox,
  ClipboardCopy,
  DescriptionList,
  DescriptionListDescription,
  DescriptionListGroup,
  DescriptionListTerm,
  Form,
  FormGroup,
  FormHelperText,
  FormSelect,
  FormSelectOption,
  HelperText,
  HelperTextItem,
  Label,
  PageSection,
  ProgressStep,
  ProgressStepper,
  Spinner,
  Tab,
  Tabs,
  TabTitleText,
  TextArea,
  TextInput,
  Title,
} from '@patternfly/react-core';
import { CloudImage, TemplateDetail, TemplateParam, TemplateRender, TemplateEstimate } from '../types';
import { storageApi, templateApi } from '../services/api';
import { errorText, formatMiB } from '../utils/format';
import { Markdown } from '../components/common/Markdown';
import { imageSlug } from '../components/groups/CreateGroupModal';
import { defaultKeyboard } from '../components/vms/CreateVMModal';

type Values = Record<string, string | boolean>;

const initialValue = (p: TemplateParam): string | boolean => {
  if (p.type === 'bool') return p.default === true;
  if (p.name === 'keyboard' && p.default == null) return defaultKeyboard();
  return p.default == null ? '' : String(p.default);
};

/** Client-side checks (the server checks again): required, pattern, int bounds */
const paramError = (p: TemplateParam, v: string | boolean): string | null => {
  if (p.type === 'bool') return null;
  const s = String(v).trim();
  if (!s) return p.required ? 'Required' : null;
  if (p.type === 'int') {
    if (!/^-?\d+$/.test(s)) return 'A whole number';
    const n = Number(s);
    if (p.min != null && n < p.min) return `At least ${p.min}`;
    if (p.max != null && n > p.max) return `At most ${p.max}`;
  }
  if (p.pattern) {
    try {
      if (!new RegExp(`^(?:${p.pattern})$`).test(s)) return `Must match ${p.pattern}`;
    } catch { /* a Python-only regex: the server checks it */ }
  }
  return null;
};

const ParamField: React.FC<{
  p: TemplateParam; value: string | boolean; images: CloudImage[]; onChange: (v: string | boolean) => void;
}> = ({ p, value, images, onChange }) => {
  const id = `tp-${p.name}`;
  const label = p.label || p.name;
  const err = paramError(p, value);
  const helper = (err && String(value) !== '') || (err && p.required && value === '')
    ? <FormHelperText><HelperText><HelperTextItem variant={String(value) ? 'error' : 'default'}>{String(value) ? err : (p.help || 'Required')}</HelperTextItem></HelperText></FormHelperText>
    : p.help ? <FormHelperText><HelperText><HelperTextItem>{p.help}</HelperTextItem></HelperText></FormHelperText> : null;
  if (p.type === 'bool') {
    return (
      <FormGroup fieldId={id}>
        <Checkbox id={id} label={label} isChecked={value === true} onChange={(_e, v) => onChange(v)} description={p.help || undefined} />
      </FormGroup>
    );
  }
  let input: React.ReactNode;
  if (p.type === 'choice') {
    input = (
      <FormSelect id={id} value={String(value)} onChange={(_e, v) => onChange(v)}>
        {!p.required && p.default == null && <FormSelectOption value="" label="—" />}
        {p.choices.map((c) => <FormSelectOption key={String(c)} value={String(c)} label={String(c)} />)}
      </FormSelect>
    );
  } else if (p.type === 'cloud_image') {
    const slugs = images.map(imageSlug);
    input = (
      <FormSelect id={id} value={String(value)} onChange={(_e, v) => onChange(v)}>
        {String(value) && !slugs.includes(String(value)) && <FormSelectOption value={String(value)} label={`${value} (not downloaded)`} />}
        {images.map((i) => <FormSelectOption key={i.id} value={imageSlug(i)} label={`${i.distribution} ${i.version}`} />)}
      </FormSelect>
    );
  } else {
    input = (
      <TextInput id={id} value={String(value)} type={p.type === 'int' ? 'number' : 'text'}
        placeholder={p.type === 'cidr' ? 'auto or 10.42.7.0/24' : undefined}
        validated={err && String(value) ? 'error' : 'default'} onChange={(_e, v) => onChange(v)} />
    );
  }
  return <FormGroup label={label} isRequired={p.required} fieldId={id}>{input}{helper}</FormGroup>;
};

const Estimate: React.FC<{ est: TemplateEstimate }> = ({ est }) => (
  <DescriptionList isHorizontal isCompact id="tp-estimate">
    <DescriptionListGroup><DescriptionListTerm>Resources</DescriptionListTerm>
      <DescriptionListDescription>
        {est.vcpus} vCPU · {formatMiB(est.memory_mb)} RAM · {est.disk_gb} GiB disk (thin){' '}
        {est.host_memory_free_mb != null && (
          <Label color={est.fits ? 'green' : 'red'} isCompact>
            host: {formatMiB(est.host_memory_free_mb)} available
          </Label>
        )}
      </DescriptionListDescription></DescriptionListGroup>
    {!!est.detail.length && (
      <DescriptionListGroup><DescriptionListTerm>Detail</DescriptionListTerm>
        <DescriptionListDescription>{est.detail.join(' · ')}</DescriptionListDescription></DescriptionListGroup>
    )}
  </DescriptionList>
);

export const TemplateWizardPage: React.FC = () => {
  const templateId = useParams().id || '';
  const navigate = useNavigate();
  const [tpl, setTpl] = useState<TemplateDetail | null>(null);
  const [images, setImages] = useState<CloudImage[]>([]);
  const [values, setValues] = useState<Values>({});
  const [step, setStep] = useState<'params' | 'review'>('params');
  const [render, setRender] = useState<TemplateRender | null>(null);
  const [yaml, setYaml] = useState('');
  const [edited, setEdited] = useState(false);
  const [tab, setTab] = useState<string | number>('spec');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    templateApi.get(templateId).then((t) => {
      setTpl(t);
      setValues(Object.fromEntries(t.params.map((p) => [p.name, initialValue(p)])));
    }).catch((err) => setError(errorText(err)));
    storageApi.listCloudImages().then((imgs) => setImages(imgs.filter((i) => i.status === 'ready'))).catch(() => {});
  }, [templateId]);

  const params = useCallback(() => {
    const out: Record<string, unknown> = {};
    (tpl?.params || []).forEach((p) => {
      const v = values[p.name];
      if (p.type === 'bool') out[p.name] = v === true;
      else if (String(v ?? '').trim() !== '') out[p.name] = String(v).trim();
    });
    return out;
  }, [tpl, values]);

  const preview = async (text?: string | null) => {
    setBusy(true);
    setError(null);
    try {
      const r = await templateApi.render(templateId, params(), text);
      setRender(r);
      if (!text) { setYaml(r.yaml); setEdited(false); }
      setStep('review');
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const create = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await templateApi.create(templateId, params(), edited ? yaml : null);
      navigate(`/groups/${r.group_id}?tab=guide`);
    } catch (err) {
      setError(errorText(err));
      setBusy(false);
    }
  };

  if (!tpl) return error ? <PageSection><Alert variant="danger" isInline title={error} /></PageSection> : <Bullseye><Spinner size="xl" /></Bullseye>;
  const invalid = tpl.params.some((p) => paramError(p, values[p.name] ?? '') !== null);
  const est = render && 'memory_mb' in render.estimate ? render.estimate as TemplateEstimate : null;

  return (
    <>
      <PageSection variant="light">
        <Breadcrumb>
          <BreadcrumbItem><Link to="/templates">Templates</Link></BreadcrumbItem>
          <BreadcrumbItem isActive>{tpl.title}</BreadcrumbItem>
        </Breadcrumb>
        <Title headingLevel="h1" style={{ marginTop: 8 }}>
          {tpl.title} {tpl.custom && <Label color="purple" isCompact>custom</Label>} {tpl.heavy && <Label color="orange" isCompact>heavy</Label>}
        </Title>
        <p style={{ color: 'var(--pf-v5-global--Color--200)' }}>{tpl.summary}</p>
        <ProgressStepper style={{ marginTop: 16 }} aria-label="Template steps">
          <ProgressStep id="tp-step-params" titleId="tp-step-params-t" variant={step === 'params' ? 'info' : 'success'} isCurrent={step === 'params'}>Parameters</ProgressStep>
          <ProgressStep id="tp-step-review" titleId="tp-step-review-t" variant={step === 'review' ? 'info' : 'pending'} isCurrent={step === 'review'}>Review</ProgressStep>
          <ProgressStep id="tp-step-create" titleId="tp-step-create-t" variant="pending">Create</ProgressStep>
        </ProgressStepper>
      </PageSection>
      <PageSection>
        {error && <Alert variant="danger" isInline title={error} style={{ marginBottom: 16 }} />}
        {step === 'params' && (
          <PageSection variant="light">
            <Form id="tp-form" style={{ maxWidth: 640 }} onSubmit={(e) => { e.preventDefault(); if (!invalid) preview(); }}>
              {tpl.params.map((p) => (
                <ParamField key={p.name} p={p} value={values[p.name] ?? ''} images={images}
                  onChange={(v) => setValues((cur) => ({ ...cur, [p.name]: v }))} />
              ))}
              <ActionGroup>
                <Button type="submit" isDisabled={invalid || busy} isLoading={busy}>Next: review</Button>
                <Button variant="link" onClick={() => navigate('/templates')}>Cancel</Button>
              </ActionGroup>
            </Form>
          </PageSection>
        )}
        {step === 'review' && render && (
          <PageSection variant="light">
            {!!render.errors.length && (
              <Alert variant="danger" isInline title="Can't create this lab yet" style={{ marginBottom: 16 }} id="tp-errors">
                <ul>{render.errors.map((e) => <li key={e}>{e}</li>)}</ul>
              </Alert>
            )}
            {!!render.warnings.length && (
              <Alert variant="warning" isInline title="Warnings" style={{ marginBottom: 16 }} id="tp-warnings">
                <ul>{render.warnings.map((e) => <li key={e}>{e}</li>)}</ul>
              </Alert>
            )}
            {render.ok && <Alert variant="success" isInline title={`Ready to create lab group ${render.group?.name} (${render.group?.cidr})`} style={{ marginBottom: 16 }} id="tp-ok" />}
            {est && <Estimate est={est} />}
            <Tabs activeKey={tab} onSelect={(_e, k) => setTab(k)} style={{ marginTop: 16 }}>
              <Tab eventKey="spec" title={<TabTitleText>Spec (YAML)</TabTitleText>}>
                <p style={{ margin: '12px 0' }}>
                  The lab group (and cluster) to create. You can edit it: <strong>Check</strong> validates the edited spec on this host.
                </p>
                <TextArea id="tp-yaml" aria-label="Spec YAML" value={yaml} rows={Math.min(40, Math.max(12, yaml.split('\n').length + 1))}
                  style={{ fontFamily: 'var(--pf-v5-global--FontFamily--monospace)', fontSize: 13 }}
                  onChange={(_e, v) => { setYaml(v); setEdited(true); }} resizeOrientation="vertical" />
                <div style={{ marginTop: 8 }}>
                  <Button variant="secondary" onClick={() => preview(edited ? yaml : null)} isDisabled={busy} id="tp-check">Check</Button>{' '}
                  {edited && <Button variant="link" onClick={() => preview()} isDisabled={busy}>Reset to the template</Button>}
                </div>
              </Tab>
              <Tab eventKey="hcl" title={<TabTitleText>OpenTofu</TabTitleText>}>
                <p style={{ margin: '12px 0' }}>The same lab with the vm-manager OpenTofu provider (examples/opentofu).</p>
                <ClipboardCopy id="tp-hcl" isCode isReadOnly variant="expansion" isExpanded hoverTip="Copy" clickTip="Copied">{render.hcl}</ClipboardCopy>
              </Tab>
              <Tab eventKey="guide" title={<TabTitleText>Case guide</TabTitleText>}>
                <div style={{ marginTop: 12 }}><Markdown source={render.guide || '_No guide in this template._'} /></div>
              </Tab>
            </Tabs>
            <ActionGroup style={{ marginTop: 24 }}>
              <Button onClick={create} isDisabled={!render.ok || busy || (edited && render.yaml !== yaml)} isLoading={busy} id="tp-create">Create lab</Button>
              <Button variant="secondary" onClick={() => setStep('params')}>Back</Button>
              {edited && render.yaml !== yaml && <span style={{ alignSelf: 'center' }}>Check the edited spec before creating.</span>}
            </ActionGroup>
          </PageSection>
        )}
      </PageSection>
    </>
  );
};
