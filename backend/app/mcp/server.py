"""MCP server on the app's own port: Streamable HTTP transport (POST /api/mcp, JSON responses, stateless).

Implemented here instead of using the `mcp` SDK, which needs Python >= 3.10 (RHEL 9 has 3.9). The AuthMiddleware
runs before it (path under /api): clients send `Authorization: Bearer vmm_…`, the tools reuse that identity.

Errors: protocol problems (bad JSON-RPC, unknown method / tool, params not an object) are JSON-RPC errors; a tool
that fails or is called with wrong arguments returns a result with isError: true, so the model can read it and
correct its call.
"""
import inspect
import json
import logging
from typing import Any, Dict, List, Optional

from app.config import settings
from app.mcp import tools as _tools  # noqa: F401  (registers the tools)
from app.mcp.registry import TOOLS
from app.mcp.tools import MAX_TEXT, ApiError, Ctx

logger = logging.getLogger(__name__)

PROTOCOL_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]  # newest first
INSTRUCTIONS = (
    "vm-manager drives KVM/libvirt labs on one host: VMs, lab groups (isolated network + router VM with "
    "DNS/DHCP/NAT, WireGuard, BGP, mirror registry), k3s / kubeadm / OpenShift clusters, customer-case templates. "
    "Start with `overview`. Resources accept a name or an id. Long operations return a task: follow them with "
    "wait_for_task / wait_for_cluster. For anything without a dedicated tool, api_describe then api_request "
    "(the same REST API as the web UI and the OpenTofu provider). OpenShift storage (LVMS / ODF), SR-IOV, MetalLB "
    "and ACM are built-in: create_cluster's openshift options or install_cluster_addon's kind, which also add the "
    "disks / custom resources they need; don't install those operators by hand. Deleting things is irreversible: "
    "confirm first."
)


class InvalidParams(Exception):
    pass


def _error(id_: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _result(id_: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _tool_error(text: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": True}


async def _call_tool(ctx: Ctx, params: Dict[str, Any]) -> Dict[str, Any]:
    name = params.get("name")
    tool = TOOLS.get(name) if isinstance(name, str) else None
    if tool is None or (tool.admin and ctx.is_viewer):  # admin tools are not listed for viewers
        raise InvalidParams(f"Unknown tool: {name}")
    args = params.get("arguments")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise InvalidParams("arguments must be an object")
    try:
        inspect.signature(tool.fn).bind(ctx, **args)
    except TypeError as e:
        return _tool_error(f"Invalid arguments for {name}: {e}. Input schema: {json.dumps(tool.schema)}")
    try:
        out = await tool.fn(ctx, **args)
        text = out if isinstance(out, str) else json.dumps(out, indent=1, default=str)
        if len(text) > MAX_TEXT:
            text = text[:MAX_TEXT] + f"\n… truncated ({len(text)} characters)"
        return {"content": [{"type": "text", "text": text}], "isError": False}
    except (ApiError, ValueError) as e:
        return _tool_error(str(e))
    except Exception as e:  # report, don't break the session
        logger.exception("MCP tool %s", name)
        return _tool_error(f"{type(e).__name__}: {e}")


async def handle(ctx: Ctx, msg: Any) -> Optional[Dict[str, Any]]:
    """One JSON-RPC message -> its response (None for notifications / client responses)"""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _error(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid Request")
    method, id_ = msg.get("method"), msg.get("id")
    if method is None and ("result" in msg or "error" in msg):  # a response: the server never sends requests
        return None
    if not isinstance(method, str):
        return _error(id_, -32600, "Invalid Request: no method")
    if "id" not in msg:  # notification (initialized, cancelled...): no response
        return None
    params = msg.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return _error(id_, -32602, "Invalid params: params must be an object")
    if method == "initialize":
        asked = params.get("protocolVersion")
        return _result(id_, {
            "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "vm-manager", "version": settings.APP_VERSION},
            "instructions": INSTRUCTIONS,
        })
    if method == "ping":
        return _result(id_, {})
    if method == "tools/list":
        return _result(id_, {"tools": [t.describe() for t in TOOLS.values() if not (ctx.is_viewer and t.admin)]})
    if method == "tools/call":
        try:
            return _result(id_, await _call_tool(ctx, params))
        except InvalidParams as e:
            return _error(id_, -32602, str(e))
    if method in ("resources/list", "prompts/list"):
        return _result(id_, {method.split("/")[0]: []})
    if method == "resources/templates/list":
        return _result(id_, {"resourceTemplates": []})
    return _error(id_, -32601, f"Method not found: {method}")


async def _send(send, status: int, body: Optional[bytes] = None) -> None:
    headers = [(b"content-type", b"application/json")] if body is not None else []
    if status == 405:
        headers.append((b"allow", b"POST"))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body or b""})


class MCPEndpoint:
    """ASGI app for the /api/mcp route (a class instance: Starlette passes scope/receive/send, not a Request)"""

    async def __call__(self, scope, receive, send) -> None:
        await mcp_app(scope, receive, send)


async def mcp_app(scope, receive, send) -> None:
    if scope["type"] != "http":
        return
    if scope["method"] != "POST":  # no server-initiated SSE stream, no sessions to DELETE
        return await _send(send, 405)
    chunks: List[bytes] = []
    while True:
        event = await receive()
        chunks.append(event.get("body", b""))
        if not event.get("more_body"):
            break
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
    version = headers.get("mcp-protocol-version")
    # Spec: 400 for an unsupported version header. Claude Code first probes a newer stateless revision
    # (`server/discover`, MCP-Protocol-Version 2026-07-28): the 400 makes it fall back to initialize.
    if version and version not in PROTOCOL_VERSIONS:
        return await _send(send, 400, json.dumps(_error(None, -32600, f"Unsupported MCP-Protocol-Version {version} "
                                                        f"(supported: {', '.join(PROTOCOL_VERSIONS)})")).encode())
    try:
        payload = json.loads(b"".join(chunks) or b"null")
    except ValueError:
        return await _send(send, 400, json.dumps(_error(None, -32700, "Parse error")).encode())
    ctx = Ctx(scope.get("state", {}).get("user"), headers)
    if isinstance(payload, list):  # batch (protocol 2025-03-26)
        if not payload:
            return await _send(send, 400, json.dumps(_error(None, -32600, "Invalid Request: empty batch")).encode())
        replies = [r for r in [await handle(ctx, m) for m in payload] if r is not None]
        if not replies:
            return await _send(send, 202)
        return await _send(send, 200, json.dumps(replies, default=str).encode())
    reply = await handle(ctx, payload)
    if reply is None:
        return await _send(send, 202)
    await _send(send, 200, json.dumps(reply, default=str).encode())
