import React, { useState } from 'react';
import {
  Alert, Button, Card, CardBody, CardTitle, ClipboardCopy, Form, FormGroup, FormHelperText, HelperText, HelperTextItem,
  Stack, StackItem, TextArea,
} from '@patternfly/react-core';
import { Table, Tbody, Td, Th, Thead, Tr } from '@patternfly/react-table';
import { Cluster } from '../../types';
import { clusterApi } from '../../services/api';
import { errorText } from '../../utils/format';

/** Disconnected kubeadm cluster: what is in the group's mirror registry for it, how pods use it, day-2 mirroring */
export const MirroredImages: React.FC<{ cluster: Cluster; onChanged: () => void }> = ({ cluster, onChanged }) => {
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [started, setStarted] = useState<number | null>(null);
  const reg = cluster.registry;
  if (!reg) return null;
  const images = reg.images || [];
  const refs = text.split('\n').map((l) => l.trim()).filter(Boolean);

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const task = await clusterApi.mirrorImages(cluster.id, refs);
      setStarted(task.id);
      setText('');
      onChanged();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card id="cluster-mirrored-images">
      <CardTitle>Mirrored images ({images.length})</CardTitle>
      <CardBody>
        <Stack hasGutter>
          <StackItem style={{ fontSize: 'var(--pf-v5-global--FontSize--sm)' }}>
            The nodes' containerd sends every pull of registry.k8s.io, docker.io, ghcr.io and quay.io images to{' '}
            <code>{reg.url}</code> (<code>/etc/containerd/certs.d/&lt;registry&gt;/hosts.toml</code>): pods keep their
            usual image names. {reg.egress === 'blocked'
              ? <>The group's egress is <b>blocked</b>: an image missing here ends in <code>ImagePullBackOff</code>.</>
              : <>The group's egress is open again: missing images are pulled from the internet.</>}
            {reg.exempt && reg.exempt.length > 0 && (
              <> Nodes installing their packages (still allowed out): {reg.exempt.join(', ')}.</>
            )}
            {reg.demo_image && (
              <div style={{ marginTop: 8 }}>
                First test (mirrored):
                <ClipboardCopy isCode isReadOnly variant="inline-compact" hoverTip="Copy" clickTip="Copied">
                  {`kubectl create deployment web --image=${reg.demo_image.replace(/^docker\.io\/library\//, '')} && kubectl get pods -w`}
                </ClipboardCopy>
              </div>
            )}
          </StackItem>
          {(reg.failed || []).length > 0 && (
            <StackItem>
              <Alert variant="warning" isInline isPlain title={`Not mirrored (copy failed): ${(reg.failed || []).join(', ')}`} />
            </StackItem>
          )}
          <StackItem>
            <Table aria-label="Mirrored images" variant="compact">
              <Thead><Tr><Th>Image (as pods use it)</Th><Th>In the mirror registry</Th></Tr></Thead>
              <Tbody>
                {images.map((i) => (
                  <Tr key={i.source}>
                    <Td dataLabel="Image"><code>{i.source}</code></Td>
                    <Td dataLabel="Mirror"><code>{i.mirror}</code></Td>
                  </Tr>
                ))}
              </Tbody>
            </Table>
          </StackItem>
          <StackItem>
            <Form onSubmit={(e) => { e.preventDefault(); if (refs.length) submit(); }}>
              <FormGroup label="Mirror more images (one per line)" fieldId="mirror-more">
                <TextArea id="mirror-more" rows={2} value={text} resizeOrientation="vertical"
                  placeholder="docker.io/library/redis:7-alpine" onChange={(_e, v) => setText(v)} />
                <FormHelperText><HelperText><HelperTextItem>
                  skopeo copies them on the router (it keeps its internet access); pods can use them right after.
                </HelperTextItem></HelperText></FormHelperText>
              </FormGroup>
              <div>
                <Button id="mirror-more-submit" onClick={submit} isDisabled={!refs.length || busy || cluster.task_running}
                  isLoading={busy}>Mirror</Button>
              </div>
            </Form>
          </StackItem>
          {error && <StackItem><Alert variant="danger" isInline title={error} /></StackItem>}
          {started && cluster.task_running && cluster.task_id === started && (
            <StackItem><Alert variant="info" isInline isPlain
              title={`Mirroring… ${cluster.task_progress ?? 0}% (task #${started}, progress on the Tasks page)`} /></StackItem>
          )}
        </Stack>
      </CardBody>
    </Card>
  );
};
