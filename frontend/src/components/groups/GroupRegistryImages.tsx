/** Your own images in a group's mirror registry: copy from another registry (skopeo on the router), upload an
 * archive (podman / docker save), podman push from your machine, list / delete (docs/disconnected.md). */
import React, { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  Button,
  ClipboardCopy,
  ClipboardCopyVariant,
  ExpandableSection,
  FileUpload,
  Flex,
  FlexItem,
  Form,
  FormGroup,
  FormHelperText,
  HelperText,
  HelperTextItem,
  Label,
  Progress,
  TextInput,
  Title,
} from '@patternfly/react-core';
import { Table, Tbody, Td, Th, Thead, Tr } from '@patternfly/react-table';
import { GroupDetail, RegistryCredentials, RegistryImages, RegistryStatus, Task } from '../../types';
import { groupApi, taskApi } from '../../services/api';
import { useLiveEvents } from '../../hooks/useEvents';
import { errorText } from '../../utils/format';
import { ConfirmModal } from '../common/ConfirmModal';

const muted: React.CSSProperties = { fontSize: 'var(--pf-v5-global--FontSize--sm)', color: 'var(--pf-v5-global--Color--200)' };
const REPO_RE = /^[a-z0-9]+(?:[._-][a-z0-9]+)*(?:\/[a-z0-9]+(?:[._-][a-z0-9]+)*)*$/;
const TAG_RE = /^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$/;
const MAX_TAGS = 12;

export const GroupRegistryImages: React.FC<{
  group: GroupDetail; status: RegistryStatus; onDone: (msg: string) => void; onError: (msg: string) => void;
}> = ({ group, status, onDone, onError }) => {
  const [images, setImages] = useState<RegistryImages | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [taskId, setTaskId] = useState<number | null>(null);
  const [task, setTask] = useState<Task | null>(null);
  const [busy, setBusy] = useState(false);
  // copy
  const [source, setSource] = useState('');
  const [destRepo, setDestRepo] = useState('');
  const [destTag, setDestTag] = useState('');
  const [user, setUser] = useState('');
  const [password, setPassword] = useState('');
  // upload
  const [file, setFile] = useState<File | null>(null);
  const [upRepo, setUpRepo] = useState('');
  const [upTag, setUpTag] = useState('');
  const [uploaded, setUploaded] = useState<number | null>(null);
  // push from my machine
  const [creds, setCreds] = useState<RegistryCredentials | null>(null);
  const [toDelete, setToDelete] = useState<{ ref: string; added: boolean } | null>(null);

  const loadImages = useCallback(async () => {
    try {
      setImages(await groupApi.registryImages(group.id));
      setListError(null);
    } catch (err) {
      setListError(errorText(err));
    }
  }, [group.id]);
  useEffect(() => { loadImages(); }, [loadImages]);

  const loadTask = useCallback(async (id: number) => {
    const t = (await taskApi.list()).find((x) => x.id === id) || null;
    setTask(t);
    if (t && t.status !== 'running' && t.status !== 'pending') {
      setTaskId(null);
      if (t.status === 'completed') {
        const result = t.result as { image?: string; images?: string[] } | null;
        onDone(`Done: ${result?.image || (result?.images || []).join(', ')}`);
      } else {
        onError(`${t.name}: ${t.error_message || t.status}`);
      }
      loadImages();
    }
  }, [loadImages, onDone, onError]);
  useLiveEvents(['task'], (event) => {
    if (taskId && event.id === taskId) loadTask(taskId);
  });

  const started = (id: number, msg: string) => {
    setTaskId(id);
    loadTask(id);
    onDone(msg);
  };

  const copy = async () => {
    setBusy(true);
    try {
      const { task_id } = await groupApi.copyImage(group.id, {
        source: source.trim(), dest_repo: destRepo.trim() || null, dest_tag: destTag.trim() || null,
        username: user.trim() || null, password: password || null,
      });
      setPassword('');
      started(task_id, `Copying ${source.trim()} (skopeo on the router)`);
    } catch (err) {
      onError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const upload = async () => {
    if (!file) return;
    setBusy(true);
    setUploaded(0);
    try {
      const { task_id } = await groupApi.uploadImage(group.id, file, { repo: upRepo.trim() || undefined, tag: upTag.trim() || undefined },
        (f) => setUploaded(f));
      setFile(null);
      started(task_id, `${file.name} uploaded: pushing it to the registry`);
    } catch (err) {
      onError(errorText(err));
    } finally {
      setBusy(false);
      setUploaded(null);
    }
  };

  const remove = async () => {
    if (!toDelete) return;
    try {
      await groupApi.deleteImage(group.id, toDelete.ref);
      onDone(`${toDelete.ref} deleted`);
      loadImages();
    } catch (err) {
      onError(errorText(err));
    } finally {
      setToDelete(null);
    }
  };

  const showCreds = async () => {
    try {
      setCreds(await groupApi.registryCredentials(group.id));
    } catch (err) {
      onError(errorText(err));
    }
  };

  const running = taskId !== null;
  const badDest = (destRepo.trim() && !REPO_RE.test(destRepo.trim())) || (destTag.trim() && !TAG_RE.test(destTag.trim()));
  const badUp = (upRepo.trim() && !REPO_RE.test(upRepo.trim())) || (upTag.trim() && !TAG_RE.test(upTag.trim()));
  const url = status.url || '';
  const uplink = status.uplink_url || '';
  const login = (host: string) => `podman login ${host} -u ${creds ? creds.username : '<user>'} -p '${creds ? creds.password : '<password>'}'`;
  const caLines = (host: string) => `sudo mkdir -p /etc/containers/certs.d/${host}\n`
    + `curl -s ${window.location.origin}/api/v1/groups/${group.id}/registry/ca.crt | sudo tee /etc/containers/certs.d/${host}/ca.crt\n`;
  const pushCmds = (host: string) => `${caLines(host)}${login(host)}\n`
    + `podman tag localhost/myapp:1.0 ${host}/myteam/myapp:1.0\npodman push ${host}/myteam/myapp:1.0`;

  return (
    <div id="registry-images">
      <Title headingLevel="h4" size="md" style={{ margin: '24px 0 8px' }}>Your own images</Title>
      <div style={{ ...muted, marginBottom: 12, maxWidth: 900 }}>
        Add an image to test in the lab (e.g. a patched build): copy it from a registry (the router downloads it), upload an
        archive from this browser, or push it with podman. Lab machines and clusters pull it as
        {' '}<code>{url}/&lt;repository&gt;:&lt;tag&gt;</code>.
      </div>
      {task && running && (
        <Progress id="image-task" value={task.progress} title={`${task.name}: ${task.description || 'working…'}`} size="sm"
          style={{ marginBottom: 16, maxWidth: 900 }} aria-label="Image task progress" />
      )}

      <Flex alignItems={{ default: 'alignItemsFlexStart' }} spaceItems={{ default: 'spaceItems2xl' }}>
        <FlexItem style={{ minWidth: 320, flex: '1 1 380px', maxWidth: 520 }}>
          <Form>
            <Title headingLevel="h5" size="md">Copy from a registry</Title>
            <FormGroup label="Source image" fieldId="copy-source" isRequired>
              <TextInput id="copy-source" value={source} onChange={(_e, v) => setSource(v)} placeholder="quay.io/myorg/myapp:1.0" />
            </FormGroup>
            <Flex>
              <FlexItem grow={{ default: 'grow' }}>
                <FormGroup label="Repository" fieldId="copy-repo">
                  <TextInput id="copy-repo" value={destRepo} onChange={(_e, v) => setDestRepo(v)} placeholder="same path" validated={badDest ? 'error' : 'default'} />
                </FormGroup>
              </FlexItem>
              <FlexItem>
                <FormGroup label="Tag" fieldId="copy-tag">
                  <TextInput id="copy-tag" value={destTag} onChange={(_e, v) => setDestTag(v)} placeholder="same" style={{ width: 120 }} />
                </FormGroup>
              </FlexItem>
            </Flex>
            <ExpandableSection toggleText="Source registry login (private images)">
              <Flex>
                <FlexItem><TextInput id="copy-user" aria-label="Source username" value={user} onChange={(_e, v) => setUser(v)} placeholder="username" /></FlexItem>
                <FlexItem><TextInput id="copy-password" aria-label="Source password" type="password" value={password} onChange={(_e, v) => setPassword(v)} placeholder="password / token" /></FlexItem>
              </Flex>
              <HelperText><HelperTextItem>Used for this copy only, never stored. Without it, the OpenShift pull secret is used (registry.redhat.io…).</HelperTextItem></HelperText>
            </ExpandableSection>
            <div><Button id="copy-start" onClick={copy} isDisabled={busy || running || !source.trim() || !!badDest}>Copy</Button></div>
          </Form>
        </FlexItem>
        <FlexItem style={{ minWidth: 320, flex: '1 1 380px', maxWidth: 520 }}>
          <Form>
            <Title headingLevel="h5" size="md">Upload an archive</Title>
            <FormGroup label="Image archive" fieldId="upload-file">
              <FileUpload id="upload-file" filename={file?.name || ''} hideDefaultPreview browseButtonText="Choose…"
                filenamePlaceholder="podman save -o myapp.tar myapp:1.0"
                onFileInputChange={(_e, f) => setFile(f)} onClearClick={() => setFile(null)} isDisabled={busy} />
              <FormHelperText><HelperText><HelperTextItem>
                <code>podman save</code>, <code>docker save</code> or an OCI archive (.tar). Streamed to disk, then pushed by the app.
              </HelperTextItem></HelperText></FormHelperText>
            </FormGroup>
            <Flex>
              <FlexItem grow={{ default: 'grow' }}>
                <FormGroup label="Repository" fieldId="upload-repo">
                  <TextInput id="upload-repo" value={upRepo} onChange={(_e, v) => setUpRepo(v)} placeholder="name in the archive" validated={badUp ? 'error' : 'default'} />
                </FormGroup>
              </FlexItem>
              <FlexItem>
                <FormGroup label="Tag" fieldId="upload-tag">
                  <TextInput id="upload-tag" value={upTag} onChange={(_e, v) => setUpTag(v)} placeholder="same" style={{ width: 120 }} />
                </FormGroup>
              </FlexItem>
            </Flex>
            {uploaded !== null && <Progress value={Math.round(uploaded * 100)} title="Uploading" size="sm" aria-label="Upload progress" />}
            <div><Button id="upload-start" onClick={upload} isDisabled={busy || running || !file || !!badUp}>Upload</Button></div>
          </Form>
        </FlexItem>
      </Flex>

      <ExpandableSection toggleText="Push from your machine (podman)" style={{ margin: '16px 0', maxWidth: 900 }} id="push-help">
        <div style={muted}>
          From this host use the router&apos;s uplink address <code>{uplink}</code>; from a laptop on the group&apos;s WireGuard,
          {' '}<code>{url}</code>. Trust the CA once (or add <code>--tls-verify=false</code> to login / push for a quick test).
          The credentials can push and delete: keep them for the lab.
        </div>
        <div style={{ margin: '8px 0' }}>
          {creds ? <Label color="orange">credentials shown below</Label>
            : <Button variant="secondary" size="sm" id="show-credentials" onClick={showCreds}>Show credentials</Button>}
        </div>
        {uplink && <><div style={muted}>On this host:</div>
          <ClipboardCopy isReadOnly isCode variant={ClipboardCopyVariant.expansion} isExpanded hoverTip="Copy" clickTip="Copied">{pushCmds(uplink)}</ClipboardCopy></>}
        <div style={{ ...muted, marginTop: 8 }}>On a WireGuard laptop:</div>
        <ClipboardCopy isReadOnly isCode variant={ClipboardCopyVariant.expansion} hoverTip="Copy" clickTip="Copied">{pushCmds(url)}</ClipboardCopy>
      </ExpandableSection>

      <Flex alignItems={{ default: 'alignItemsCenter' }} style={{ marginBottom: 8 }}>
        <FlexItem><Title headingLevel="h5" size="md">In the registry</Title></FlexItem>
        <FlexItem><Button variant="link" isInline onClick={loadImages}>Refresh</Button></FlexItem>
      </Flex>
      {listError && <Alert variant="warning" isInline isPlain title={listError} />}
      <Table aria-label="Registry images" variant="compact" id="registry-image-list">
        <Thead><Tr><Th>Repository</Th><Th>Tags (click to copy the pull reference)</Th><Th>Origin</Th></Tr></Thead>
        <Tbody>
          {(images?.images || []).map((img) => (
            <Tr key={img.repository}>
              <Td dataLabel="Repository">{img.repository}</Td>
              <Td dataLabel="Tags">
                {img.tags.slice(0, MAX_TAGS).map((tag) => (
                  <span key={tag} style={{ display: 'inline-flex', alignItems: 'center', marginRight: 10 }}>
                    <ClipboardCopy isReadOnly variant="inline-compact" hoverTip="Copy pull reference" clickTip="Copied">
                      {`${url}/${img.repository}:${tag}`}
                    </ClipboardCopy>
                    <Button variant="plain" size="sm" aria-label={`Delete ${img.repository}:${tag}`} title="Delete this tag"
                      onClick={() => setToDelete({ ref: `${img.repository}:${tag}`, added: tag in img.sources })}>×</Button>
                  </span>
                ))}
                {img.tags.length > MAX_TAGS && <span style={muted}>+{img.tags.length - MAX_TAGS} more</span>}
              </Td>
              <Td dataLabel="Origin">{img.added
                ? <Label isCompact color="blue">{Object.values(img.sources)[0]?.split(':')[0] === 'upload' ? 'uploaded' : 'copied'}</Label>
                : <Label isCompact>mirrored / pushed</Label>}</Td>
            </Tr>
          ))}
          {images && !images.images.length && <Tr><Td colSpan={3}>The registry is empty.</Td></Tr>}
        </Tbody>
      </Table>
      {images?.truncated && <div style={muted}>Only the first 500 repositories are listed.</div>}
      {toDelete && (
        <ConfirmModal isOpen title={`Delete ${toDelete.ref}?`} confirmLabel="Delete" onConfirm={remove} onClose={() => setToDelete(null)}>
          {toDelete.added ? 'Removes this tag (and its manifest) from the registry.'
            : 'This image was mirrored by oc-mirror (or pushed outside the app): clusters installed from this registry may need it. '
              + 'Delete it anyway?'}
        </ConfirmModal>
      )}
    </div>
  );
};
