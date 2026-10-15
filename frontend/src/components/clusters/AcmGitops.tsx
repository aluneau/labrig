import React, { useState } from 'react';
import {
  Alert,
  Button,
  Card,
  CardBody,
  CardHeader,
  CardTitle,
  ClipboardCopy,
  ClipboardCopyVariant,
  ExpandableSection,
  Flex,
  FlexItem,
  Label,
  LabelProps,
  Spinner,
  Title,
  Tooltip,
} from '@patternfly/react-core';
import {
  CheckCircleIcon, CodeBranchIcon, ExclamationCircleIcon, ExclamationTriangleIcon, ExternalLinkAltIcon, InProgressIcon,
  OutlinedCircleIcon, SyncAltIcon,
} from '@patternfly/react-icons';
import { Table, Tbody, Td, Th, Thead, Tr } from '@patternfly/react-table';
import { AcmGitCredentials, AcmGitops as Gitops, AcmSpoke, AddonState, Cluster } from '../../types';
import { clusterApi } from '../../services/api';
import { errorText } from '../../utils/format';
import { ConfirmModal } from '../common/ConfirmModal';

const muted: React.CSSProperties = { fontSize: 'var(--pf-v5-global--FontSize--sm)', color: 'var(--pf-v5-global--Color--200)' };
const tile: React.CSSProperties = {
  border: '1px solid var(--pf-v5-global--BorderColor--100)', borderRadius: 6, padding: '10px 12px', height: '100%',
  minWidth: 0, overflowWrap: 'anywhere',
};
const SPOKE_FILES = ['manifests.yaml', 'siteconfig.yaml', 'kustomization.yaml'];
/** How often Argo CD looks at git by itself (its default timeout.reconciliation) */
const ARGO_POLL = '3 minutes';

/** "5 min ago" from an ISO date */
export const ago = (iso?: string | null): string => {
  if (!iso) return '';
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (Number.isNaN(s)) return '';
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
};
const sameCommit = (a?: string | null, b?: string | null) => !!a && !!b && (a.startsWith(b) || b.startsWith(a));

/** The one-word state of the whole GitOps path, driving the summary line and its action */
export type GitopsPhase = 'off' | 'installing' | 'install-failed' | 'connect' | 'gitea-down' | 'error' | 'syncing'
  | 'behind' | 'synced' | 'ready';

export function gitopsPhase(g: Gitops, addon?: AddonState | null): GitopsPhase {
  if (!g.argocd_installed) {
    if (addon && ['pending', 'installing'].includes(addon.state)) return 'installing';
    if (addon && addon.state === 'error') return 'install-failed';
    return 'off';
  }
  const app = g.application;
  if (!app) return 'connect';
  if (g.git_server.error || (g.git_server.bootstrapped && !g.git_server.running)) return 'gitea-down';
  const op = app.operation?.phase;
  if (op === 'Failed' || op === 'Error' || app.health === 'Degraded') return 'error';
  if (op === 'Running' || app.health === 'Progressing' || app.sync === 'OutOfSync') return 'syncing';
  if (g.commits.length && app.revision && !sameCommit(app.revision, g.commits[0].commit)) return 'behind';
  return g.spokes.length ? 'synced' : 'ready';
}

export const PHASE_LABEL: Record<GitopsPhase, { text: string; color: LabelProps['color'] }> = {
  off: { text: 'Off', color: 'grey' },
  installing: { text: 'Installing', color: 'blue' },
  'install-failed': { text: 'Install failed', color: 'red' },
  connect: { text: 'Not connected', color: 'orange' },
  'gitea-down': { text: 'Git server down', color: 'red' },
  error: { text: 'Sync failed', color: 'red' },
  syncing: { text: 'Syncing', color: 'blue' },
  behind: { text: 'Behind git', color: 'orange' },
  synced: { text: 'In sync', color: 'green' },
  ready: { text: 'Ready', color: 'green' },
};

const syncColor = (v?: string | null): LabelProps['color'] => (v === 'Synced' ? 'green' : v === 'OutOfSync' ? 'orange' : 'grey');
const healthColor = (v?: string | null): LabelProps['color'] =>
  v === 'Healthy' ? 'green' : v === 'Progressing' ? 'blue' : v === 'Degraded' || v === 'Missing' ? 'red' : 'grey';

const StateIcon: React.FC<{ s: 'ok' | 'busy' | 'warn' | 'bad' | 'none' }> = ({ s }) => {
  const style = { marginRight: 6, verticalAlign: '-0.125em' };
  if (s === 'ok') return <CheckCircleIcon style={{ ...style, color: 'var(--pf-v5-global--success-color--100)' }} />;
  if (s === 'busy') return <InProgressIcon style={{ ...style, color: 'var(--pf-v5-global--info-color--100)' }} />;
  if (s === 'warn') return <ExclamationTriangleIcon style={{ ...style, color: 'var(--pf-v5-global--warning-color--100)' }} />;
  if (s === 'bad') return <ExclamationCircleIcon style={{ ...style, color: 'var(--pf-v5-global--danger-color--100)' }} />;
  return <OutlinedCircleIcon style={{ ...style, color: 'var(--pf-v5-global--Color--200)' }} />;
};

const ExtLink: React.FC<{ id?: string; href?: string | null; children: React.ReactNode; disabledWhy?: string }> = ({ id, href, children, disabledWhy }) => {
  const btn = (
    <Button id={id} variant="link" isInline component="a" href={href || undefined} target="_blank" rel="noreferrer"
      icon={<ExternalLinkAltIcon />} iconPosition="end" isAriaDisabled={!href}>{children}</Button>
  );
  return !href && disabledWhy ? <Tooltip content={disabledWhy}>{btn}</Tooltip> : btn;
};

interface Props {
  cluster: Cluster;
  gitops: Gitops;
  addon?: AddonState | null; // the `gitops` add-on (OpenShift GitOps operator) install state, when one runs
  spokes: AcmSpoke[]; // the hub's spokes (cluster state, deployed from git or not)
  canAct: boolean; // hub ready, no task running, no action in flight
  busy: boolean;
  onInstall: () => void;
  onSync: () => void;
  onShowYaml: (title: string, manifest: string) => void;
}

/** The hub's GitOps / ZTP card: one status line, the three moving parts, what's in git, how to work in it. */
export const AcmGitopsCard: React.FC<Props> = ({ cluster, gitops: g, addon, spokes, canAct, busy, onInstall, onSync, onShowYaml }) => {
  const [creds, setCreds] = useState<AcmGitCredentials | null>(null);
  const [credsBusy, setCredsBusy] = useState(false);
  const [credsError, setCredsError] = useState<string | null>(null);
  const [confirmReset, setConfirmReset] = useState(false);
  const phase = gitopsPhase(g, addon);
  const [aboutOpen, setAboutOpen] = useState(phase === 'off'); // first visit: explain before asking to install
  const [howOpen, setHowOpen] = useState(false);

  const app = g.application;
  const srv = g.git_server;
  const repoWeb = srv.repo_web_url || null;
  const head = g.commits[0];
  const op = app?.operation;
  const lastSync = op?.finished_at || app?.reconciled_at || null;

  const showCreds = async (reset: boolean) => {
    setCredsBusy(true);
    try {
      setCreds(reset ? await clusterApi.acmGitResetCredentials(cluster.id) : await clusterApi.acmGitCredentials(cluster.id));
      setCredsError(null);
    } catch (err) {
      setCredsError(errorText(err));
    } finally {
      setCredsBusy(false);
    }
  };

  const syncButton = (label: string, variant: 'primary' | 'secondary' = 'secondary') => (
    <Tooltip content="Pushes the app's copy of the repository to the git server and asks Argo CD to apply it now.">
      <Button id="acm-gitops-deploy" variant={variant} icon={<SyncAltIcon />} isAriaDisabled={!canAct}
        isLoading={busy} onClick={onSync}>{label}</Button>
    </Tooltip>
  );

  // ---- 1. The summary: one sentence saying where things are and the one thing to do next
  const summary = (() => {
    const actions = (node: React.ReactNode) => <div style={{ marginTop: 8 }}>{node}</div>;
    switch (phase) {
      case 'off':
        return (
          <Alert isInline variant="custom" title="GitOps is off: spokes are created by the app applying their manifests directly">
            Install OpenShift GitOps (Argo CD) on this hub to deploy spokes from git instead: the app commits each
            spoke to a repository and Argo CD applies it. Takes about 5 to 10 minutes.
            {actions(
              <Button id="acm-gitops-install" variant="primary" isAriaDisabled={!canAct} isLoading={busy} onClick={onInstall}>
                Install OpenShift GitOps
              </Button>,
            )}
          </Alert>
        );
      case 'installing':
        return (
          <Alert isInline variant="info" customIcon={<Spinner size="md" aria-label="Installing" />}
            title="Installing OpenShift GitOps on the hub (5 to 10 minutes)">
            {addon?.message || 'Installing the operator, then waiting for its Argo CD instance.'} This card updates by itself.
          </Alert>
        );
      case 'install-failed':
        return (
          <Alert isInline variant="danger" title="OpenShift GitOps did not install">
            {addon?.message || 'See the cluster task for details.'}
            {actions(
              <Button id="acm-gitops-install" variant="secondary" isAriaDisabled={!canAct} isLoading={busy} onClick={onInstall}>Try again</Button>,
            )}
          </Alert>
        );
      case 'connect':
        return (
          <Alert isInline variant="warning" title="Argo CD is installed but not connected to the spokes repository yet">
            Connecting pushes the repository to the git server on this host and creates the Argo CD Application
            <code> {cluster.name}-spokes</code> that applies its <code>clusters/</code> folder to the hub. Provisioning a
            spoke with <em>Deploy from git</em> also does it.
            {actions(syncButton('Connect Argo CD to git', 'primary'))}
          </Alert>
        );
      case 'gitea-down':
        return (
          <Alert isInline variant="danger" title="The git server is not running: Argo CD can't read the repository">
            {srv.error ? <div><code style={{ overflowWrap: 'anywhere' }}>{srv.error}</code></div> : null}
            Nothing changes on the hub while it is down. <em>Sync now</em> starts it again and pushes the repository.
            {actions(syncButton('Sync now', 'primary'))}
          </Alert>
        );
      case 'error':
        return (
          <Alert isInline variant="danger" title={`Argo CD could not apply ${app?.revision ? `commit ${app.revision}` : 'the repository'}`}>
            {op?.message ? <div style={{ overflowWrap: 'anywhere' }}>{op.message}</div> : <div>The Application is {app?.health}.</div>}
            <div style={muted}>Open it in Argo CD to see which resource failed. <em>Sync now</em> tries again from the latest commit.</div>
            {actions(<Flex spaceItems={{ default: 'spaceItemsSm' }}>{syncButton('Sync now', 'primary')}
              {app?.url && <FlexItem alignSelf={{ default: 'alignSelfCenter' }}><ExtLink href={app.url}>View in Argo CD</ExtLink></FlexItem>}</Flex>)}
          </Alert>
        );
      case 'syncing':
        return (
          <Alert isInline variant="info" customIcon={<Spinner size="md" aria-label="Syncing" />}
            title={`Argo CD is applying ${app?.revision ? `commit ${app.revision}` : 'the repository'} to the hub`}>
            {op?.message ? <div style={{ overflowWrap: 'anywhere' }}>{op.message}</div> : null}
            <div style={muted}>A new spoke takes 1 to 2 minutes to be applied, then the hub installs it (the spoke row below shows the progress).</div>
          </Alert>
        );
      case 'behind':
        return (
          <Alert isInline variant="warning" title={`Git has a newer commit (${head?.commit}) than the one on the hub (${app?.revision})`}>
            Argo CD looks at git every {ARGO_POLL}; <em>Sync now</em> applies it at once.
            {actions(syncButton('Sync now'))}
          </Alert>
        );
      case 'synced':
        return (
          <Alert isInline variant="success" title={`In sync: the hub runs commit ${app?.revision}, the latest in git`}>
            {g.spokes.length} spoke{g.spokes.length > 1 ? 's' : ''} defined in git{lastSync ? `, last applied ${ago(lastSync)}` : ''}.
            Changes pushed to the repository are applied within {ARGO_POLL}.
          </Alert>
        );
      default:
        return (
          <Alert isInline variant="success" title="Ready: Argo CD watches the spokes repository">
            No spoke in git yet. Provision a bare-metal spoke above with <strong>Deploy from git (GitOps)</strong> ticked:
            its manifests are committed here and Argo CD applies them to the hub.
          </Alert>
        );
    }
  })();

  // ---- 2. The three moving parts, at a glance
  const serverTile = (
    <div style={tile} id="acm-gitops-server">
      <div style={muted}>1 · Git server on this host</div>
      <div style={{ fontWeight: 600, margin: '2px 0 4px' }}>
        {srv.running ? <><StateIcon s="ok" />Gitea {srv.version} running</>
          : srv.error || srv.bootstrapped ? <><StateIcon s="bad" />Stopped</>
            : <><StateIcon s="none" />Not started yet</>}
      </div>
      <div style={muted}>
        {srv.running || srv.bootstrapped ? 'Holds the spokes repository Argo CD reads.' : 'Starts by itself on the first sync or GitOps spoke.'}
      </div>
      <div style={{ marginTop: 6 }}>
        <ExtLink id="acm-gitea-open" href={srv.running ? repoWeb : null} disabledWhy="Available once the git server runs">Open in Gitea</ExtLink>
      </div>
    </div>
  );
  const argoTile = (
    <div style={tile} id="acm-gitops-argocd">
      <div style={muted}>2 · Argo CD on the hub</div>
      <div style={{ fontWeight: 600, margin: '2px 0 4px' }}>
        {!g.argocd_installed ? (phase === 'installing' ? <><StateIcon s="busy" />Installing</> : <><StateIcon s="none" />Not installed</>)
          : app ? <><StateIcon s="ok" />Watching the repository</> : <><StateIcon s="warn" />Installed, not connected</>}
      </div>
      <div style={muted}>{g.argocd_installed ? 'OpenShift GitOps operator' : 'OpenShift GitOps operator, an add-on of the hub'}</div>
      <div style={{ marginTop: 6 }}>
        <ExtLink id="acm-argocd-open" href={g.argocd_url}
          disabledWhy={g.argocd_installed ? 'Waiting for the openshift-gitops-server route' : 'Install OpenShift GitOps first'}>Open Argo CD</ExtLink>
      </div>
    </div>
  );
  const syncTile = (
    <div style={tile} id="acm-gitops-application">
      <div style={muted}>3 · Last sync</div>
      <div style={{ fontWeight: 600, margin: '2px 0 4px' }}>
        {!app ? <><StateIcon s="none" />Never</>
          : phase === 'error' || phase === 'gitea-down' ? <><StateIcon s="bad" />{op?.phase === 'Error' || op?.phase === 'Failed' ? 'Failed' : app.health}</>
            : phase === 'syncing' ? <><StateIcon s="busy" />Applying</>
              : phase === 'behind' ? <><StateIcon s="warn" />Behind git</>
                : <><StateIcon s="ok" />{app.sync || 'Synced'}</>}
        {lastSync && <span style={{ ...muted, fontWeight: 400 }}> · {ago(lastSync)}</span>}
      </div>
      {app ? (
        <div>
          <Label isCompact color={syncColor(app.sync)}>{app.sync || 'Unknown'}</Label>{' '}
          <Label isCompact color={healthColor(app.health)}>{app.health || 'Unknown'}</Label>
          {app.revision && <span style={muted}> at <code>{app.revision}</code></span>}
        </div>
      ) : <div style={muted}>Argo CD has not applied anything yet.</div>}
      <div style={{ marginTop: 6 }}>
        <ExtLink id="acm-argocd-app" href={app?.url} disabledWhy="Once Argo CD is connected">View in Argo CD</ExtLink>
      </div>
    </div>
  );

  // ---- 3. What's in git: one row per spoke folder + recent commits
  const spokeRows = Array.from(new Set([...g.spokes, ...spokes.filter((s) => s.gitops).map((s) => s.name)])).sort();
  const fileLink = (spoke: string, f: string) => (repoWeb && srv.running ? (
    <a key={f} href={`${repoWeb}/src/branch/main/clusters/${spoke}/${f}`} target="_blank" rel="noreferrer" style={{ marginRight: 10, whiteSpace: 'nowrap', display: 'inline-block' }}>{f}</a>
  ) : <code key={f} style={{ marginRight: 10, whiteSpace: 'nowrap', display: 'inline-block' }}>{f}</code>);
  const files = (
    <div id="acm-gitops-files">
      <Title headingLevel="h3" size="md" style={{ marginBottom: 4 }}>
        <CodeBranchIcon style={{ marginRight: 6 }} />What&apos;s in git
      </Title>
      <div style={{ ...muted, marginBottom: 8 }}>
        Branch <code>main</code>, folder <code>clusters/</code>: one folder per spoke. The pull secret and BMC passwords
        are never committed: the app creates them on the hub.
      </div>
      {spokeRows.length === 0 ? (
        <div style={{ ...tile, height: 'auto', textAlign: 'center', padding: 16 }} id="acm-gitops-empty">
          <div style={{ fontWeight: 600 }}>No spoke in git yet</div>
          <div style={muted}>
            Spokes provisioned with <em>Deploy from git</em> appear here with their files and their sync state.
            Spokes applied directly stay out of git.
          </div>
        </div>
      ) : (
        <Table aria-label="Spokes in git" variant="compact" gridBreakPoint="grid-md">
          <Thead><Tr><Th>Spoke</Th><Th>On the hub (Argo CD)</Th><Th>Cluster</Th><Th>Files</Th></Tr></Thead>
          <Tbody>
            {spokeRows.map((name) => {
              const a = app?.spokes?.[name];
              const s = spokes.find((x) => x.name === name);
              const inGit = g.spokes.includes(name);
              return (
                <Tr key={name}>
                  <Td dataLabel="Spoke"><strong>{name}</strong></Td>
                  <Td dataLabel="On the hub">
                    {!inGit ? <span style={muted}>removed from git</span>
                      : a ? (
                        <div>
                          <Label isCompact color={syncColor(a.sync)}>{a.sync}</Label>{' '}
                          <Label isCompact color={healthColor(a.health)}>{a.health}</Label>
                          {a.out_of_sync ? <div style={muted}>{a.out_of_sync} of {a.resources} resources differ from git</div> : null}
                        </div>
                      ) : <span style={muted}>{app ? 'not applied yet' : 'Argo CD not connected'}</span>}
                  </Td>
                  <Td dataLabel="Cluster">
                    {s ? <div><Label isCompact color={s.state === 'ready' ? 'green' : s.state === 'error' ? 'red' : 'blue'}>{s.state}</Label></div>
                      : <span style={muted}>no cluster row</span>}
                  </Td>
                  <Td dataLabel="Files">{inGit ? <div>{SPOKE_FILES.map((f) => fileLink(name, f))}</div> : '—'}</Td>
                </Tr>
              );
            })}
          </Tbody>
        </Table>
      )}
      <Title headingLevel="h4" size="md" style={{ margin: '16px 0 6px' }}>Recent commits</Title>
      {g.commits.length === 0 ? (
        <div style={muted}>No commit yet: the repository is created with the first sync or GitOps spoke.</div>
      ) : (
        <ul id="acm-gitops-commits" style={{ listStyle: 'none', margin: 0, padding: 0 }}>
          {g.commits.slice(0, 6).map((c) => (
            <li key={c.commit} style={{ display: 'flex', flexWrap: 'wrap', gap: '2px 8px', alignItems: 'baseline', padding: '3px 0',
              borderBottom: '1px solid var(--pf-v5-global--BorderColor--100)' }}>
              {repoWeb && srv.running
                ? <a href={`${repoWeb}/commit/${c.commit}`} target="_blank" rel="noreferrer"><code>{c.commit}</code></a>
                : <code>{c.commit}</code>}
              <span style={{ flex: '1 1 160px', minWidth: 0 }}>{c.message}</span>
              {sameCommit(app?.revision, c.commit) && <Label isCompact color={phase === 'error' ? 'red' : 'green'}>on the hub</Label>}
              <span style={muted}>{ago(c.date)}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );

  // ---- 4. Working in git yourself
  const cloneCmd = g.clone_url ? `git clone ${g.clone_url}` : '';
  const howto = (
    <ExpandableSection id="acm-gitops-howto" toggleText="Work in the repository yourself (clone, edit, push)"
      isExpanded={howOpen} onToggle={(_e, v) => setHowOpen(v)}>
      <ol style={{ paddingLeft: 20, margin: 0 }}>
        <li style={{ marginBottom: 8 }}>
          Clone it (anonymous read, what Argo CD uses too):
          {g.clone_url ? (
            <ClipboardCopy isReadOnly hoverTip="Copy" clickTip="Copied" style={{ marginTop: 4 }}>{cloneCmd}</ClipboardCopy>
          ) : <div style={muted}>The clone URL appears once the git server has started.</div>}
          {srv.firewall && <div style={muted}>{srv.firewall}</div>}
        </li>
        <li style={{ marginBottom: 8 }}>
          Edit <code>clusters/&lt;spoke&gt;/manifests.yaml</code>, commit, and push with the Gitea login:
          <div style={{ marginTop: 4, display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 8 }}>
            <span>user <code>{srv.admin_user || 'vmmanager'}</code></span>
            {!creds ? (
              <Button id="acm-gitea-creds" variant="link" isInline isLoading={credsBusy} isAriaDisabled={credsBusy || !srv.bootstrapped}
                onClick={() => showCreds(false)}>{srv.bootstrapped ? 'Show password' : 'Password: once the git server has started'}</Button>
            ) : (
              <>
                <ClipboardCopy isReadOnly hoverTip="Copy" clickTip="Copied" variant={ClipboardCopyVariant.inlineCompact}>{creds.password}</ClipboardCopy>
                <Button variant="link" isInline onClick={() => setCreds(null)}>Hide</Button>
                <Button variant="link" isInline isDanger isAriaDisabled={credsBusy} onClick={() => setConfirmReset(true)}>New password</Button>
              </>
            )}
          </div>
          {credsError && <Alert isInline isPlain variant="danger" title={credsError} />}
        </li>
        <li style={{ marginBottom: 8 }}>Argo CD applies the new commit within {ARGO_POLL} (or press <em>Sync now</em>).</li>
      </ol>
      <Alert isInline isPlain variant="warning" title="The app's own copy wins">
        The app keeps the repository on this host (<code>{g.repo_path}</code>) and pushes it, replacing the branch, each
        time it provisions or deletes a GitOps spoke and on <em>Sync now</em>. Commits pushed from elsewhere are then
        lost: keep them for experiments, or make the change in the app.
      </Alert>
    </ExpandableSection>
  );

  const about = (
    <ExpandableSection id="acm-gitops-about" toggleText="What changes when a spoke is deployed from git?"
      isExpanded={aboutOpen} onToggle={(_e, v) => setAboutOpen(v)}>
      <Flex direction={{ default: 'column', md: 'row' }} spaceItems={{ default: 'spaceItemsMd' }} flexWrap={{ default: 'nowrap' }}>
        <FlexItem flex={{ default: 'flex_1' }} style={{ ...tile, height: 'auto' }}>
          <strong>Directly (default)</strong>
          <div style={muted}>The app applies the spoke&apos;s manifests to the hub with <code>oc</code>. Nothing is kept in git.
            Fastest, and what most labs need.</div>
        </FlexItem>
        <FlexItem flex={{ default: 'flex_1' }} style={{ ...tile, height: 'auto' }}>
          <strong>From git (GitOps / ZTP)</strong>
          <div style={muted}>The app commits the same manifests to <code>clusters/&lt;spoke&gt;/</code>, pushes the repository
            to the git server on this host, and Argo CD on the hub applies them, as in a customer&apos;s zero touch
            provisioning setup. Deleting the spoke removes its folder; Argo CD then deletes its resources.</div>
        </FlexItem>
      </Flex>
      <div style={{ marginTop: 8 }}>
        <Button variant="link" isInline onClick={() => onShowYaml('Argo CD Application', g.application_manifest)}>
          View the Argo CD Application
        </Button>
      </div>
    </ExpandableSection>
  );

  const showSyncInHeader = !!app && !['connect', 'gitea-down', 'error'].includes(phase);
  return (
    <Card id="acm-gitops">
      <CardHeader actions={showSyncInHeader ? { actions: syncButton('Sync now'), hasNoOffset: true } : undefined}>
        <CardTitle>
          Deploy spokes from git (GitOps){' '}
          <Label id="acm-gitops-phase" isCompact color={PHASE_LABEL[phase].color} style={{ verticalAlign: 'middle' }}>
            {PHASE_LABEL[phase].text}
          </Label>
        </CardTitle>
      </CardHeader>
      <CardBody>
        <div style={{ marginBottom: 16 }}>{summary}</div>
        <Flex spaceItems={{ default: 'spaceItemsMd' }} alignItems={{ default: 'alignItemsStretch' }} style={{ marginBottom: 16 }}>
          {[serverTile, argoTile, syncTile].map((t, i) => (
            <FlexItem key={i} flex={{ default: 'flex_1' }} style={{ minWidth: 220 }}>{t}</FlexItem>
          ))}
        </Flex>
        {(g.argocd_installed || g.commits.length > 0 || spokeRows.length > 0) && <div style={{ marginBottom: 16 }}>{files}</div>}
        {howto}
        <div style={{ marginTop: 8 }}>{about}</div>
      </CardBody>
      <ConfirmModal title="Set a new Gitea password?" isOpen={confirmReset} confirmLabel="New password"
        onConfirm={() => showCreds(true)} onClose={() => setConfirmReset(false)}>
        The old password stops working at once (saved git credentials too). Argo CD is not affected: it clones anonymously.
      </ConfirmModal>
    </Card>
  );
};
