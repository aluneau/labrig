import React, { useEffect, useState } from 'react';
import {
  Alert, Button, Card, CardBody, CardTitle, ClipboardCopy, ExpandableSection, Label, List, ListItem, PageSection,
  Spinner, Stack, StackItem, Tab, Tabs, TabTitleText, Text, TextContent, TextInput,
} from '@patternfly/react-core';
import { Table, Tbody, Td, Th, Thead, Tr } from '@patternfly/react-table';
import { API_BASE, authApi, CSRF_HEADERS } from '../services/api';
import { errorText } from '../utils/format';
import { PageHeader } from '../components/common/PageHeader';
import { useAuth } from '../hooks/useAuth';
import { useCliToken } from '../hooks/useCliToken';

interface McpTool { name: string; description: string }

const EXAMPLES = [
  'Create a lab group "case123" with two Debian 13 members, then give my laptop WireGuard access to it',
  'Deploy an OpenShift SNO with LVMS storage and install the kubernetes-nmstate operator',
  'Add a 50 GiB disk and a second NIC on network vmm-g-case123 to case123-web',
  'Show me the kubeadm init config and a fresh join command of cluster k8s',
  'Run "journalctl -u kubelet -n 50" on k8s-ctlplane-0 and tell me why the node is NotReady',
  'Which customer-case templates exist? Render the proxy one with 2 members, without creating it',
];

const Snippet: React.FC<{ children: string }> = ({ children }) => (
  <ClipboardCopy isReadOnly isCode variant="expansion" isExpanded hoverTip="Copy" clickTip="Copied">
    {children}
  </ClipboardCopy>
);

/** Setup of the MCP server (POST /api/mcp) for Claude Code, Claude Desktop, Cursor, VS Code */
export const MCPPage: React.FC = () => {
  const { status } = useAuth();
  const authOn = status.enabled;
  const [token, setToken] = useState('');
  const [creating, setCreating] = useState(false);
  const [created, setCreated] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<string | number>('claude-code');
  const [tools, setTools] = useState<McpTool[] | null>(null);
  const [toolsError, setToolsError] = useState<string | null>(null);

  const base = API_BASE || window.location.origin;
  const url = `${base}/api/mcp`;
  const cli = useCliToken();
  // the default CLI token unless a dedicated one is created / pasted
  const tok = token.trim() || cli.token || 'vmm_…';
  const auth = `Bearer ${tok}`;
  const { protocol, hostname } = new URL(base);
  const remoteHttp = protocol === 'http:' && !['localhost', '127.0.0.1', '::1'].includes(hostname);

  useEffect(() => {
    fetch(`${API_BASE}/api/mcp`, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', ...CSRF_HEADERS },
      body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'tools/list' }),
    })
      .then(async (r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const data = await r.json();
        setTools(data.result.tools);
      })
      .catch((e) => setToolsError(errorText(e)));
  }, []);

  const createToken = async () => {
    setCreating(true);
    try {
      const t = await authApi.createToken(`mcp ${new Date().toISOString().slice(0, 10)}`);
      setToken(t.token);
      setCreated(true);
      setError(null);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setCreating(false);
    }
  };

  const headerJson = authOn ? `,\n      "headers": { "Authorization": "${auth}" }` : '';
  const claudeCode = `claude mcp add --transport http --scope user vm-manager ${url}`
    + (authOn ? ` \\\n  --header "Authorization: ${auth}"` : '')
    + '\n\n# check: claude mcp list   (inside a session: /mcp)';
  // literal ${VMM_AUTH}: mcp-remote expands it from "env" (keeps the token out of the args, no Windows quoting issue)
  // eslint-disable-next-line no-template-curly-in-string
  const desktopArgs = ['-y', 'mcp-remote', url, ...(authOn ? ['--header', 'Authorization:${VMM_AUTH}'] : []),
    ...(remoteHttp ? ['--allow-http'] : [])];
  const claudeDesktop = JSON.stringify({
    mcpServers: {
      'vm-manager': {
        command: 'npx', args: desktopArgs, ...(authOn ? { env: { VMM_AUTH: auth } } : {}),
      },
    },
  }, null, 2);
  const cursor = `{\n  "mcpServers": {\n    "vm-manager": {\n      "url": "${url}"${headerJson}\n    }\n  }\n}`;
  const vscode = `{\n  "servers": {\n    "vm-manager": {\n      "type": "http",\n      "url": "${url}"${headerJson}\n    }\n  }\n}`;
  const curlAuth = authOn ? ` -H "Authorization: ${auth}"` : '';
  const curl = `curl -s ${url}${curlAuth} -H 'Content-Type: application/json' \\\n`
    + `  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"overview","arguments":{}}}'`;

  return (
    <>
      <PageHeader title="MCP server"
        description="Let an AI assistant (Claude Code, Claude Desktop, Cursor, VS Code...) drive vm-manager: VMs, disks, NICs, lab groups, WireGuard, clusters, operators, commands inside the VMs. It is served by this app, on this port: nothing else to install or run on the host." />
      <PageSection>
        <Stack hasGutter>
          <StackItem>
            <Card>
              <CardTitle>1. Token</CardTitle>
              <CardBody>
                {!authOn ? (
                  <Alert variant="info" isInline isPlain title="Authentication is off on this server: no token needed." />
                ) : (
                  <Stack hasGutter>
                    <StackItem>
                      The assistant acts as you, with your role ({status.user?.role}){status.user?.role === 'viewer'
                        ? ': read-only tools only' : ''}. The configurations below contain your default CLI token; create a
                      dedicated token instead to revoke the assistant's access on its own (API tokens page).
                    </StackItem>
                    {error && <StackItem><Alert variant="danger" isInline title={error} /></StackItem>}
                    <StackItem>
                      <Button onClick={createToken} isLoading={creating} isDisabled={creating}>
                        Create a token for MCP
                      </Button>{' '}
                      <span style={{ marginLeft: 8 }}>or paste an existing one:</span>
                      <TextInput aria-label="Token" value={token} onChange={(_e, v) => { setToken(v); setCreated(false); }}
                        placeholder="vmm_…" style={{ maxWidth: 520, marginTop: 8 }} />
                    </StackItem>
                    {created && (
                      <StackItem>
                        <Alert variant="warning" isInline isPlain
                          title="Token created and filled in below. Copy your configuration now: the token is not shown again." />
                      </StackItem>
                    )}
                  </Stack>
                )}
              </CardBody>
            </Card>
          </StackItem>

          <StackItem>
            <Card>
              <CardTitle>2. Connect your assistant</CardTitle>
              <CardBody>
                <TextContent style={{ marginBottom: 12 }}>
                  <Text>Server URL: <code>{url}</code> (Streamable HTTP transport). The address must be reachable from
                    the machine running the assistant.</Text>
                </TextContent>
                <Tabs activeKey={tab} onSelect={(_e, k) => setTab(k)}>
                  <Tab eventKey="claude-code" title={<TabTitleText>Claude Code</TabTitleText>}>
                    <Stack hasGutter style={{ marginTop: 16 }}>
                      <StackItem>Run once in a terminal (<code>--scope user</code>: every project; drop it for this project only):</StackItem>
                      <StackItem><Snippet>{claudeCode}</Snippet></StackItem>
                    </Stack>
                  </Tab>
                  <Tab eventKey="claude-desktop" title={<TabTitleText>Claude Desktop</TabTitleText>}>
                    <Stack hasGutter style={{ marginTop: 16 }}>
                      <StackItem>
                        Claude Desktop starts local servers only: the <code>mcp-remote</code> bridge (run by <code>npx</code>,
                        needs Node.js) connects it to this URL. Settings → Developer → Edit Config opens the file:
                        <List>
                          <ListItem>Linux: <code>~/.config/Claude/claude_desktop_config.json</code></ListItem>
                          <ListItem>macOS: <code>~/Library/Application Support/Claude/claude_desktop_config.json</code></ListItem>
                          <ListItem>Windows: <code>%APPDATA%\Claude\claude_desktop_config.json</code></ListItem>
                        </List>
                      </StackItem>
                      <StackItem><Snippet>{claudeDesktop}</Snippet></StackItem>
                      <StackItem>Merge it into an existing <code>mcpServers</code> block, then quit and restart Claude Desktop.
                        {remoteHttp && <> <code>--allow-http</code> is there because this server is plain HTTP on another
                          machine: prefer a VPN or SSH tunnel on untrusted networks.</>}</StackItem>
                    </Stack>
                  </Tab>
                  <Tab eventKey="cursor" title={<TabTitleText>Cursor</TabTitleText>}>
                    <Stack hasGutter style={{ marginTop: 16 }}>
                      <StackItem><code>~/.cursor/mcp.json</code> (every project) or <code>.cursor/mcp.json</code> in a project.
                        Cursor Settings → MCP should then show vm-manager with a green dot and its tools.</StackItem>
                      <StackItem><Snippet>{cursor}</Snippet></StackItem>
                    </Stack>
                  </Tab>
                  <Tab eventKey="vscode" title={<TabTitleText>VS Code</TabTitleText>}>
                    <Stack hasGutter style={{ marginTop: 16 }}>
                      <StackItem><code>.vscode/mcp.json</code> in a workspace (Copilot agent mode):</StackItem>
                      <StackItem><Snippet>{vscode}</Snippet></StackItem>
                    </Stack>
                  </Tab>
                  <Tab eventKey="curl" title={<TabTitleText>Test with curl</TabTitleText>}>
                    <Stack hasGutter style={{ marginTop: 16 }}>
                      <StackItem>Checks the URL and the token from the assistant's machine:</StackItem>
                      <StackItem><Snippet>{curl}</Snippet></StackItem>
                    </Stack>
                  </Tab>
                </Tabs>
              </CardBody>
            </Card>
          </StackItem>

          <StackItem>
            <Card>
              <CardTitle>3. Ask</CardTitle>
              <CardBody>
                <List>{EXAMPLES.map((e) => <ListItem key={e}>{e}</ListItem>)}</List>
                <TextContent style={{ marginTop: 12 }}>
                  <Text component="small">
                    Commands inside VMs go through the QEMU guest agent (no SSH key or route needed); WireGuard: the
                    assistant gets the device config and the nmcli / wg-quick commands, and runs them on your machine if
                    it has a shell there. Everything else goes through the same REST API as this UI and the OpenTofu
                    provider. Deletions are irreversible: assistants usually ask first, read before you approve.
                  </Text>
                </TextContent>
              </CardBody>
            </Card>
          </StackItem>

          <StackItem>
            <Card>
              <CardBody>
                <ExpandableSection toggleText={tools ? `Available tools (${tools.length})` : 'Available tools'}>
                  {toolsError && <Alert variant="danger" isInline title={`Could not list the tools: ${toolsError}`} />}
                  {!tools && !toolsError && <Spinner size="md" />}
                  {tools && (
                    <Table variant="compact" aria-label="MCP tools">
                      <Thead><Tr><Th width={20}>Tool</Th><Th>What it does</Th></Tr></Thead>
                      <Tbody>
                        {tools.map((t) => (
                          <Tr key={t.name}>
                            <Td><code>{t.name}</code>{['api_request', 'api_describe'].includes(t.name)
                              && <Label isCompact color="blue" style={{ marginLeft: 6 }}>any endpoint</Label>}</Td>
                            <Td>{t.description.split('\n\n')[0]}</Td>
                          </Tr>
                        ))}
                      </Tbody>
                    </Table>
                  )}
                </ExpandableSection>
              </CardBody>
            </Card>
          </StackItem>
        </Stack>
      </PageSection>
    </>
  );
};
