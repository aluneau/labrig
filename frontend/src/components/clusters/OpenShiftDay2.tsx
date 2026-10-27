import React, { useCallback, useEffect, useState } from 'react';
import {
  Alert, Button, Card, CardBody, CardTitle, Checkbox, DescriptionList, DescriptionListDescription, DescriptionListGroup,
  DescriptionListTerm, ExpandableSection, Flex, FlexItem, Form, FormGroup, FormHelperText, FormSelect, FormSelectOption,
  HelperText, HelperTextItem, Label, Modal, Progress, Spinner, Stack, StackItem, TextInput,
} from '@patternfly/react-core';
import { Cluster, ClusterUpgrades } from '../../types';
import { clusterApi } from '../../services/api';
import { useLiveEvents } from '../../hooks/useEvents';
import { errorText } from '../../utils/format';

/** OpenShift: add workers (oc adm node-image create -> node ISO -> VMs, CSRs approved by the app) */
export const AddWorkersModal: React.FC<{ cluster: Cluster; isOpen: boolean; onClose: () => void; onStarted: () => void }> = ({
  cluster, isOpen, onClose, onStarted,
}) => {
  const size = cluster.spec?.worker || {};
  const [count, setCount] = useState('1');
  const [memory, setMemory] = useState(String(Math.round((size.memory || 12288) / 1024)));
  const [vcpu, setVcpu] = useState(String(size.vcpu || 4));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await clusterApi.addWorkers(cluster.id, Number(count), {
        memory: Number(memory) * 1024, vcpu: Number(vcpu), disk_size: size.disk_size || 120,
      });
      onStarted();
      onClose();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal variant="small" title={`Add workers to ${cluster.name}`} isOpen={isOpen} onClose={onClose} id="os-add-workers"
      actions={[
        <Button key="add" variant="primary" onClick={submit} isLoading={busy} isDisabled={busy} id="os-add-workers-submit">Add</Button>,
        <Button key="cancel" variant="link" onClick={onClose}>Cancel</Button>,
      ]}>
      <Form onSubmit={(e) => { e.preventDefault(); submit(); }}>
        {error && <Alert variant="danger" isInline title={error} />}
        <FormGroup label="Workers" fieldId="os-add-count">
          <TextInput id="os-add-count" type="number" min={1} max={10} value={count} onChange={(_e, v) => setCount(v)} />
        </FormGroup>
        <Flex>
          <FlexItem>
            <FormGroup label="Memory (GiB)" fieldId="os-add-memory">
              <TextInput id="os-add-memory" type="number" min={8} value={memory} onChange={(_e, v) => setMemory(v)} />
            </FormGroup>
          </FlexItem>
          <FlexItem>
            <FormGroup label="vCPU" fieldId="os-add-vcpu">
              <TextInput id="os-add-vcpu" type="number" min={2} value={vcpu} onChange={(_e, v) => setVcpu(v)} />
            </FormGroup>
          </FlexItem>
        </Flex>
        <FormHelperText>
          <HelperText>
            <HelperTextItem>
              The cluster builds a node ISO (<code>oc adm node-image create</code>, OpenShift 4.17+); each new VM boots it,
              writes RHCOS to its {size.disk_size || 120} GiB disk and joins. The app approves the node's certificates
              until it is Ready (about 15–30 min), then ejects the ISO. The router's ingress load balancers (80 / 443)
              include the new workers.
            </HelperTextItem>
          </HelperText>
        </FormHelperText>
      </Form>
    </Modal>
  );
};

const STATE_COLORS: Record<string, 'green' | 'blue' | 'red' | 'orange' | 'grey'> = {
  completed: 'green', running: 'blue', failed: 'red', unfollowed: 'orange',
};

/** OpenShift upgrades: channel, recommended and conditional updates (risks in plain words), progress */
export const UpgradesCard: React.FC<{ cluster: Cluster; onChanged: () => void }> = ({ cluster, onChanged }) => {
  const [data, setData] = useState<ClusterUpgrades | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [channel, setChannel] = useState('');
  const [target, setTarget] = useState('');
  const [accept, setAccept] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const d = await clusterApi.upgrades(cluster.id);
      setData(d);
      setError(null);
    } catch (err) {
      setError(errorText(err));
    }
  }, [cluster.id]);
  useEffect(() => { load(); }, [load, cluster.status, cluster.task_running, cluster.status_message]);
  useEffect(() => {
    if (!data?.updating && !data?.task_running) return undefined;
    const t = setInterval(load, 30000);
    return () => clearInterval(t);
  }, [data?.updating, data?.task_running, load]);
  useLiveEvents(['task'], (e) => { if (e.target_type === 'cluster' && e.target_name === cluster.name && e.status !== 'running') load(); });
  useEffect(() => { if (data) setChannel(data.channel || ''); }, [data?.channel]); // eslint-disable-line react-hooks/exhaustive-deps

  const submit = async (body: { channel?: string; version?: string; accept_risks?: boolean }) => {
    setBusy(true);
    setError(null);
    try {
      setData(await clusterApi.setUpgrade(cluster.id, body));
      setTarget('');
      setAccept(false);
      onChanged();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  if (!data) {
    return <Card id="os-upgrades"><CardTitle>Updates</CardTitle><CardBody>{error ? <Alert variant="danger" isInline title={error} /> : <Spinner size="md" />}</CardBody></Card>;
  }
  const cond = data.conditional.find((c) => c.version === target);
  const risky = !!cond && cond.recommended !== true;
  const channels = Array.from(new Set([...(data.channels || []), ...(data.channel ? [data.channel] : [])]));
  const run = data.upgrade;

  return (
    <Card id="os-upgrades">
      <CardTitle>Updates</CardTitle>
      <CardBody>
        <Stack hasGutter>
          {error && <StackItem><Alert variant="danger" isInline title={error} /></StackItem>}
          {!data.reachable && <StackItem><Alert variant="info" isInline title={data.error || 'The cluster API is not reachable'} /></StackItem>}
          {data.reachable && (
            <StackItem>
              <DescriptionList isHorizontal isCompact>
                <DescriptionListGroup>
                  <DescriptionListTerm>Version</DescriptionListTerm>
                  <DescriptionListDescription id="os-upgrades-current">
                    {data.current}{data.updating && data.desired !== data.current ? <> → <b>{data.desired}</b></> : null}
                  </DescriptionListDescription>
                </DescriptionListGroup>
                <DescriptionListGroup>
                  <DescriptionListTerm>Channel</DescriptionListTerm>
                  <DescriptionListDescription id="os-upgrades-channel">
                    {data.channel || <i>none: no updates are offered (set by the app at install, "disable updates")</i>}
                  </DescriptionListDescription>
                </DescriptionListGroup>
                {run && (
                  <DescriptionListGroup>
                    <DescriptionListTerm>Last upgrade by the app</DescriptionListTerm>
                    <DescriptionListDescription>
                      {run.from} → {run.target}{' '}
                      <Label isCompact color={STATE_COLORS[run.state || ''] || 'grey'} id="os-upgrade-state">{run.state}</Label>
                      {run.message ? <div style={{ fontSize: 'var(--pf-v5-global--FontSize--sm)' }}>{run.message}</div> : null}
                    </DescriptionListDescription>
                  </DescriptionListGroup>
                )}
              </DescriptionList>
            </StackItem>
          )}
          {data.updating && (
            <StackItem>
              <Progress id="os-upgrade-progress" value={data.progress ?? 0}
                title={`Upgrading to ${data.desired}: cluster operators ${data.operators_updated}/${data.operators_total} updated`} />
              {data.message && <div style={{ fontSize: 'var(--pf-v5-global--FontSize--sm)', marginTop: 4 }}>{data.message}</div>}
              {data.failing && <Alert variant="warning" isInline isPlain title={data.failing} style={{ marginTop: 4 }} />}
              <div style={{ fontSize: 'var(--pf-v5-global--FontSize--sm)', marginTop: 4 }}>
                Single-node clusters reboot during the upgrade: the API and the console are away for a few minutes.
                The upgrade goes on in the cluster even if the app restarts.
              </div>
            </StackItem>
          )}
          {data.reachable && !data.updating && data.disconnected && (
            <StackItem><Alert variant="info" isInline title="Upgrading a disconnected cluster is not supported yet: the new release has to be mirrored into the router's registry first." /></StackItem>
          )}
          {data.reachable && !data.updating && !data.disconnected && (
            <>
              <StackItem>
                <Flex alignItems={{ default: 'alignItemsFlexEnd' }}>
                  <FlexItem>
                    <FormGroup label="Channel" fieldId="os-upgrade-channel">
                      <TextInput id="os-upgrade-channel" value={channel} onChange={(_e, v) => setChannel(v)}
                        placeholder={`stable-${(data.current || '4.18').split('.').slice(0, 2).join('.')}`} list="os-upgrade-channels" />
                      <datalist id="os-upgrade-channels">{channels.map((c) => <option key={c} value={c} />)}</datalist>
                    </FormGroup>
                  </FlexItem>
                  <FlexItem>
                    <Button variant="secondary" isDisabled={busy || data.task_running || channel === (data.channel || '')}
                      onClick={() => submit({ channel })} id="os-upgrade-set-channel">Set channel</Button>
                  </FlexItem>
                </Flex>
                {data.channel && !data.retrieved && data.retrieved_message && (
                  <HelperText><HelperTextItem variant="warning">{data.retrieved_message}</HelperTextItem></HelperText>
                )}
              </StackItem>
              {data.channel && (
                <StackItem>
                  {data.available.length === 0 && data.conditional.length === 0 ? (
                    <i id="os-upgrades-none">No update offered in {data.channel}: the cluster runs the latest version of this channel.</i>
                  ) : (
                    <Flex alignItems={{ default: 'alignItemsFlexEnd' }}>
                      <FlexItem>
                        <FormGroup label="Update to" fieldId="os-upgrade-target">
                          <FormSelect id="os-upgrade-target" value={target} onChange={(_e, v) => { setTarget(v); setAccept(false); }} style={{ minWidth: 260 }}>
                            <FormSelectOption value="" label="Pick a version…" isPlaceholder />
                            {data.available.map((v) => <FormSelectOption key={v} value={v} label={`${v} (recommended)`} />)}
                            {data.conditional.map((c) => (
                              <FormSelectOption key={c.version} value={c.version}
                                label={`${c.version} (${c.recommended === true ? 'conditional, risks don\'t apply' : 'known risks'})`} />
                            ))}
                          </FormSelect>
                        </FormGroup>
                      </FlexItem>
                      <FlexItem>
                        <Button variant="primary" isDisabled={!target || busy || data.task_running || (risky && !accept)}
                          isLoading={busy} onClick={() => submit({ version: target, accept_risks: risky ? accept : undefined })}
                          id="os-upgrade-start">Upgrade</Button>
                      </FlexItem>
                    </Flex>
                  )}
                  {!data.upgradeable && data.upgradeable_message && (
                    <HelperText><HelperTextItem variant="warning">Minor upgrades blocked: {data.upgradeable_message}</HelperTextItem></HelperText>
                  )}
                </StackItem>
              )}
              {cond && (
                <StackItem>
                  <Alert variant={risky ? 'warning' : 'info'} isInline id="os-upgrade-risks"
                    title={risky ? `${cond.version} has known risks for this cluster` : `${cond.version}: the known risks don't apply to this cluster`}>
                    {cond.risks.map((r) => (
                      <div key={r.name || ''}><b>{r.name}</b>: {r.message} {r.url && <a href={r.url} target="_blank" rel="noreferrer">details</a>}</div>
                    ))}
                    {risky && <Checkbox id="os-upgrade-accept" label="Upgrade anyway: I accept these risks" isChecked={accept} onChange={(_e, v) => setAccept(v)} />}
                  </Alert>
                </StackItem>
              )}
            </>
          )}
          {data.history.length > 0 && (
            <StackItem>
              <ExpandableSection toggleText="Version history">
                {data.history.map((h) => (
                  <div key={`${h.version}-${h.started}`}>{h.version} — {h.state}{h.completed ? `, ${new Date(h.completed).toLocaleString()}` : ''}</div>
                ))}
              </ExpandableSection>
            </StackItem>
          )}
        </Stack>
      </CardBody>
    </Card>
  );
};
