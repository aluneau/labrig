# AI assistants (MCP)

vm-manager is an [MCP](https://modelcontextprotocol.io) server: Claude Code, Claude Desktop, Cursor, VS Code (and any
MCP client) can drive it — VMs, disks, NICs, lab groups, WireGuard, k3s / kubeadm / OpenShift clusters, operators,
commands inside the VMs.

Nothing to install or run on the host: the app serves it on its own port, at `http(s)://<host>:<port>/api/mcp`
(Streamable HTTP transport, JSON responses, stateless). The **AI assistants (MCP)** page of the UI generates every
configuration below with the right URL and a token filled in.

## Setup

1. **Token** (skip when authentication is off): the MCP page's *Create a token for MCP* button, or
   `cd backend && venv/bin/python -m app.cli token create --user $USER --name mcp`. The assistant acts as that user,
   with their role: viewers only see / can run read-only tools. Revoke it on the API tokens page.
2. **Client**:

   | Client | Configuration |
   |---|---|
   | Claude Code | `claude mcp add --transport http --scope user vm-manager http://HOST:8000/api/mcp --header "Authorization: Bearer vmm_…"` |
   | Cursor | `~/.cursor/mcp.json`: `{"mcpServers": {"vm-manager": {"url": "http://HOST:8000/api/mcp", "headers": {"Authorization": "Bearer vmm_…"}}}}` |
   | VS Code | `.vscode/mcp.json`: `{"servers": {"vm-manager": {"type": "http", "url": "…/api/mcp", "headers": {"Authorization": "Bearer vmm_…"}}}}` |
   | Claude Desktop | only starts local servers: bridge with `mcp-remote` (Node.js), see below |

   Claude Desktop (`~/.config/Claude/claude_desktop_config.json`, macOS `~/Library/Application Support/Claude/`,
   Windows `%APPDATA%\Claude\`), then restart it:

   ```json
   {"mcpServers": {"vm-manager": {
     "command": "npx",
     "args": ["-y", "mcp-remote", "http://HOST:8000/api/mcp", "--header", "Authorization:${VMM_AUTH}", "--allow-http"],
     "env": {"VMM_AUTH": "Bearer vmm_…"}}}}
   ```

   (`--allow-http`: plain HTTP to another machine; not needed for localhost or https. The header has no space
   after the colon on purpose: Windows quoting.)
3. **Test** from the client machine:
   `curl -s http://HOST:8000/api/mcp -H "Authorization: Bearer vmm_…" -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'`

## Tools

| Area | Tools |
|---|---|
| Start / anything | `overview`, `api_describe` (OpenAPI of any endpoint, with nested models), `api_request` (any endpoint) |
| VMs | `list_vms`, `get_vm`, `create_vm`, `vm_power`, `delete_vm`, `add_disk`, `remove_disk`, `add_nic`, `remove_nic` |
| Inside VMs (admin) | `vm_exec`, `vm_read_file`, `vm_write_file` (QEMU guest agent: no SSH key / route), `ssh_exec` (from the host) |
| Lab groups | `list_groups`, `get_group` (+ leases), `create_group`, `add_group_member`, `wireguard_device` |
| Clusters | `list_clusters`, `get_cluster`, `create_cluster`, `cluster_power`, `get_kubeconfig`, `kubectl` (admin), `kubeadm_files` (admin), `install_cluster_addon`, `search_operators`, `wait_for_cluster` |
| Other | `list_cloud_images`, `list_networks`, `list_templates`, `create_from_template` (or `dry_run`), `list_tasks`, `wait_for_task` |

- Resources are named by **name or id**. Long operations return a task: `wait_for_task` / `wait_for_cluster` wait up to
  30 min per call.
- Complex parameters (group specs with WireGuard / BGP / egress / registry, OpenShift options, MetalLB, templates):
  `api_describe` gives the exact JSON schema, then `create_group` / `create_cluster` / `api_request`.
- **WireGuard**: `wireguard_device` enables remote access on the group if needed, creates the device and returns its
  config plus ready-to-run `nmcli` / `wg-quick` commands. An assistant with a shell on your laptop (Claude Code,
  Cursor) can run them there: the VPN comes up in one approval.
- **kubeadm files**: `kubeadm_files` returns admin.conf, the init config the app used, the live ClusterConfiguration,
  kubelet config, CA, a fresh join command, static pod manifests, certificate expiry.
- `kubectl` runs on the first running control plane (guest agent) for kubeadm / k3s, with the app's `oc` on the host
  for OpenShift; `stdin` + `apply -f -` applies manifests (e.g. operators on kubeadm).

## Design

`backend/app/mcp/`: `registry.py` (a tool = an async function; its signature and docstring become the JSON schema),
`tools.py`, `server.py` (JSON-RPC: initialize, ping, tools/list, tools/call). No `mcp` SDK: it needs Python >= 3.10.

Tools don't duplicate logic: they call the REST API **in-process** (httpx ASGI transport) with the caller's
`Authorization` header, so validation, roles, secret-read rules and the audit log are the same as for the UI and the
OpenTofu provider. Only `admin=True` tools act directly, through the existing services (`libvirt_client.agent_exec`,
`cluster_service`, `openshift_service.oc`). A new endpoint is usable at once through `api_request`; add a dedicated
tool only for frequent tasks. The auth middleware lets viewers POST to `/api/mcp` (each tool's API call is then
checked with their role) and hides admin tools from them.
