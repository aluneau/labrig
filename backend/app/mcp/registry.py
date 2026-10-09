"""MCP tool registry: a tool is an async function whose signature and docstring become its JSON schema.

    @tool
    async def add_disk(ctx: Ctx, vm: str, size_gb: int, bus: Literal["virtio", "sata"] = "virtio") -> Any:
        '''Summary line(s).

        Args:
            vm: VM name or id
            size_gb: size in GiB
        '''
"""
import inspect
import re
import typing
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    from typing import Literal
except ImportError:  # pragma: no cover
    from typing_extensions import Literal  # type: ignore

TOOLS: Dict[str, "Tool"] = {}


class Tool:
    def __init__(self, fn: Callable, name: str, description: str, schema: Dict[str, Any], admin: bool):
        self.fn = fn
        self.name = name
        self.description = description
        self.schema = schema
        self.admin = admin

    def describe(self) -> Dict[str, Any]:
        return {"name": self.name, "description": self.description, "inputSchema": self.schema}


def _type_schema(tp: Any) -> Tuple[Dict[str, Any], bool]:
    """(JSON schema, optional?) of a type hint"""
    origin = typing.get_origin(tp)
    args = typing.get_args(tp)
    if origin is typing.Union:
        rest = [a for a in args if a is not type(None)]
        optional = len(rest) < len(args)
        if len(rest) == 1:
            return _type_schema(rest[0])[0], optional
        types = [_type_schema(a)[0].get("type") for a in rest]
        return {"type": [t for t in types if t]}, optional
    if origin is Literal:
        return {"type": "string" if all(isinstance(a, str) for a in args) else "integer", "enum": list(args)}, False
    if origin in (list, List):
        return {"type": "array", "items": _type_schema(args[0])[0] if args else {}}, False
    if origin in (dict, Dict) or tp in (dict, Dict):
        return {"type": "object"}, False
    simple = {str: "string", int: "integer", float: "number", bool: "boolean"}
    if tp in simple:
        return {"type": simple[tp]}, False
    return {}, False  # Any


def _doc(fn: Callable) -> Tuple[str, Dict[str, str]]:
    """(description, {param: description}) from a Google-style docstring"""
    doc = inspect.cleandoc(fn.__doc__ or "")
    head, _, args = doc.partition("\nArgs:\n")
    params: Dict[str, str] = {}
    current = None
    for line in args.splitlines():
        m = re.match(r"^\s{0,8}(\w+): (.*)$", line)
        if m and line.startswith("    ") and not line.startswith("        "):
            current = m.group(1)
            params[current] = m.group(2).strip()
        elif current and line.strip():
            params[current] += " " + line.strip()
    return head.strip(), params


def tool(fn: Optional[Callable] = None, *, admin: bool = False):
    """Register a tool. admin=True: refused to viewers (it bypasses the REST API's role checks)."""
    def register(f: Callable) -> Callable:
        hints = typing.get_type_hints(f)
        description, param_docs = _doc(f)
        props: Dict[str, Any] = {}
        required: List[str] = []
        for name, p in inspect.signature(f).parameters.items():
            if name == "ctx":
                continue
            schema, optional = _type_schema(hints.get(name, Any))
            if name in param_docs:
                schema["description"] = param_docs[name]
            if p.default is inspect.Parameter.empty and not optional:
                required.append(name)
            elif p.default is not inspect.Parameter.empty and p.default is not None:
                schema["default"] = p.default
            props[name] = schema
        schema = {"type": "object", "properties": props}
        if required:
            schema["required"] = required
        TOOLS[f.__name__] = Tool(f, f.__name__, description, schema, admin)
        return f
    return register(fn) if fn is not None else register
