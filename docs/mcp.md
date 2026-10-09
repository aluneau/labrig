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
| VMs | `list_vms`, `get_vm`, `create_vm` (cloud image `debian-13`-style or ISO), `vm_power`, `delete_vm`, `add_disk`, `remove_disk` (+ `delete_volume`), `add_nic`, `remove_nic` |
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

## Troubleshooting

- `claude mcp list` says **Failed to connect — Dynamic Client Registration rejected (HTTP 404)**: the request had no
  valid token (missing / revoked / mistyped `Authorization` header). Claude Code then tries OAuth, which vm-manager
  doesn't offer. Re-add the server with `--header "Authorization: Bearer vmm_…"`.
- A tool answers *Unknown tool*: it is admin-only (`vm_exec`, `kubectl`…) and the token belongs to a viewer.
- `vm_exec` fails with *guest agent*: the VM is still booting (the agent starts with the guest, ~30-60 s for a cloud
  image), or it is an EL VM that isn't a cluster node / router (EL's qemu-ga forbids guest-exec): use `ssh_exec`.

## Protocol

Streamable HTTP, stateless, JSON responses (no SSE, no `Mcp-Session-Id`). Protocol versions 2025-11-25, 2025-06-18,
2025-03-26, 2024-11-05 (`initialize` echoes the client's if supported, else the newest). `POST` only: `GET` / `DELETE`
answer 405 (no server-initiated stream, no session). Notifications and client responses get 202 with no body; JSON-RPC
batches are accepted. An unsupported `MCP-Protocol-Version` header gets 400: Claude Code (2.1.x) first probes a newer
stateless revision (`server/discover` with version 2026-07-28) and falls back to `initialize` on that 400.

Errors: protocol problems are JSON-RPC errors (parse error -32700 with HTTP 400, unknown method -32601, unknown tool or
`params` / `arguments` not an object -32602). A tool that fails (API error with its HTTP status, VM not found, guest
agent not ready) or gets wrong arguments (missing / unexpected ones, reported with the tool's input schema) returns a
normal result with `isError: true`, which the model reads and can correct.

Test: `cd e2e && node mcp.js` (protocol and error checks, then a VM create / guest exec / file write+read / disk
add+remove / delete round trip through the tools; `E2E_VIEWER_TOKEN` also checks the viewer view).

## Design

`backend/app/mcp/`: `registry.py` (a tool = an async function; its signature and docstring become the JSON schema),
`tools.py`, `server.py` (JSON-RPC: initialize, ping, tools/list, tools/call). No `mcp` SDK: it needs Python >= 3.10.

Tools don't duplicate logic: they call the REST API **in-process** (httpx ASGI transport) with the caller's
`Authorization` header, so validation, roles, secret-read rules and the audit log are the same as for the UI and the
OpenTofu provider. Only `admin=True` tools act directly, through the existing services (`libvirt_client.agent_exec`,
`cluster_service`, `openshift_service.oc`). A new endpoint is usable at once through `api_request`; add a dedicated
tool only for frequent tasks. The auth middleware lets viewers POST to `/api/mcp` (each tool's API call is then
checked with their role) and hides admin tools from them.
