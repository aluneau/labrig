"""Customer-case templates (docs/templates.md)

Built-in templates are `backend/app/templates/*.yaml` (read-only, shipped with the app); user templates
("Save as template" on a group) are `DATA_DIR/templates/*.yaml`. A file is validated when it is loaded:
a bad one is reported by GET /templates (`errors`), never fatal.

Rendering = typed parameters -> `{{param}}` substitution (plain text, no Jinja) -> a GroupSpec dict
(+ an optional ClusterCreate dict), validated by the existing schemas; then the host checks (names,
subnet, images, pools: group_service.normalize as a dry run) and a resource estimate. Creating = the
group through group_service.create_group (task) and, for a cluster, a task that waits for the group
and creates the cluster in it.
"""
import copy
import ipaddress
import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Cluster, Group, Task
from app.schemas import TaskCreate
from app.schemas.cluster import ClusterCreate
from app.schemas.group import GroupSpec, TemplateRef
from app.schemas.template import TemplateFile, TemplateParam, TemplateSave

logger = logging.getLogger(__name__)

BUILTIN_DIR = Path(__file__).resolve().parents[1] / "templates"
PLACEHOLDER = re.compile(r"\{\{\s*([a-z_][a-z0-9_]*(?::\d+)?)\s*\}\}")
IPV4_IN_TEXT = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d/]|\.\d)")
GROUP_WAIT_TIMEOUT = 30 * 60
BUILTINS = ("group", "domain")  # + ip:N (N-th address of the group network)
# Features a template may list in `requires` that the app checks on this host
KNOWN_REQUIRES = {"kubeadm", "k3s", "openshift", "wireguard", "bgp", "registry", "egress", "pull-secret"}
_DROP = object()


class TemplateError(ValueError):
    pass


class Template:
    def __init__(self, data: TemplateFile, path: Path, custom: bool, text: str):
        self.data = data
        self.path = path
        self.custom = custom
        self.text = text

    def info(self) -> Dict[str, Any]:
        d = self.data
        cluster_type = (d.cluster or {}).get("type", "k3s") if d.cluster else None
        return {
            "id": d.id, "title": d.title, "summary": d.summary, "tags": d.tags, "requires": d.requires,
            "resources": d.resources.model_dump(), "params": [p.model_dump() for p in d.params], "heavy": d.heavy,
            "has_cluster": d.cluster is not None, "cluster_type": cluster_type, "custom": self.custom,
            "author": d.author,
        }

    def detail(self) -> Dict[str, Any]:
        d = self.data
        return {**self.info(), "name": d.name, "group": d.group, "cluster": d.cluster, "guide": d.guide,
                "yaml": self.text}


# Parameters and substitution

def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off", "none")
    return bool(value)


def _text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return "" if value is None else str(value)


def coerce_params(params: List[TemplateParam], raw: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """Defaults + type conversion + checks. Returns (values, errors)."""
    values: Dict[str, Any] = {}
    errors: List[str] = []
    known = {p.name for p in params}
    for key in raw:
        if key not in known:
            errors.append(f"Unknown parameter '{key}'")
    for p in params:
        label = p.label or p.name
        v = raw.get(p.name)
        if v is None or (isinstance(v, str) and not v.strip()):
            v = p.default
        if v is None:
            if p.required:
                errors.append(f"{label}: required")
            values[p.name] = None if p.type != "bool" else False
            continue
        try:
            if p.type == "int":
                if isinstance(v, bool):
                    raise ValueError("not a number")
                v = int(str(v).strip())
                if p.min is not None and v < p.min:
                    raise ValueError(f"must be >= {p.min}")
                if p.max is not None and v > p.max:
                    raise ValueError(f"must be <= {p.max}")
            elif p.type == "bool":
                if not isinstance(v, bool):
                    s = str(v).strip().lower()
                    if s not in ("true", "false", "1", "0", "yes", "no", "on", "off"):
                        raise ValueError("must be true or false")
                    v = s in ("true", "1", "yes", "on")
            elif p.type == "choice":
                match = next((c for c in p.choices if _text(c) == _text(v)), None)
                if match is None:
                    raise ValueError(f"must be one of {', '.join(_text(c) for c in p.choices)}")
                v = match
            elif p.type == "cidr":
                v = str(v).strip()
                if v != "auto":
                    v = str(ipaddress.IPv4Network(v, strict=False))
            else:  # string, cloud_image
                v = str(v).strip()
            if p.pattern and not re.fullmatch(p.pattern, _text(v)):
                raise ValueError(f"must match {p.pattern}")
        except ValueError as e:
            errors.append(f"{label}: {e}")
            continue
        values[p.name] = v
    return values, errors


def _condition(expr: Any, values: Dict[str, Any]) -> bool:
    """`_if` value: param | !param | param == value | param != value | a literal bool"""
    if isinstance(expr, bool):
        return expr
    s = str(expr).strip()
    for op in ("==", "!="):
        if op in s:
            left, right = (x.strip() for x in s.split(op, 1))
            if left not in values:
                raise TemplateError(f"_if: unknown parameter '{left}'")
            equal = _text(values[left]) == right.strip("'\"")
            return equal if op == "==" else not equal
    negate = s.startswith("!")
    name = s.lstrip("!").strip()
    if name not in values:
        raise TemplateError(f"_if: unknown parameter '{name}'")
    return _truthy(values[name]) != negate


def substitute(obj: Any, lookup: Callable[[str], Any], values: Dict[str, Any], conditions: bool = True) -> Any:
    """Replace {{key}} in every string. A string that is exactly one placeholder takes the value's
    type (int, bool...). `lookup` raises KeyError for keys left for a later pass. Dicts with
    `_if: <condition>` are dropped when it is false (conditions=True)."""
    if isinstance(obj, dict):
        if conditions and "_if" in obj:
            if not _condition(obj["_if"], values):
                return _DROP
            obj = {k: v for k, v in obj.items() if k != "_if"}
            if set(obj) == {"value"}:  # {_if: x, value: v}: a conditional scalar / list item
                return substitute(obj["value"], lookup, values, conditions)
        out = {}
        for k, v in obj.items():
            r = substitute(v, lookup, values, conditions)
            if r is not _DROP:
                out[k] = r
        return out
    if isinstance(obj, list):
        return [r for r in (substitute(v, lookup, values, conditions) for v in obj) if r is not _DROP]
    if isinstance(obj, str):
        whole = PLACEHOLDER.fullmatch(obj.strip())
        if whole:
            try:
                return lookup(whole.group(1))
            except KeyError:
                return obj

        def one(m: "re.Match") -> str:
            try:
                return _text(lookup(m.group(1)))
            except KeyError:
                return m.group(0)
        return PLACEHOLDER.sub(one, obj)
    return obj


def placeholders(obj: Any) -> List[str]:
    found: List[str] = []
    if isinstance(obj, dict):
        for v in obj.values():
            found += placeholders(v)
    elif isinstance(obj, list):
        for v in obj:
            found += placeholders(v)
    elif isinstance(obj, str):
        found += PLACEHOLDER.findall(obj)
    return found


def _nth_ip(cidr: str, n: int) -> str:
    net = ipaddress.IPv4Network(cidr)
    if not 0 < n < net.num_addresses - 1:
        raise TemplateError(f"address #{n} is outside {cidr}")
    return str(net.network_address + n)


def _pydantic_errors(prefix: str, e: ValidationError) -> List[str]:
    out = []
    for err in e.errors():
        loc = ".".join(str(x) for x in err.get("loc", ()))
        msg = err["msg"].replace("Value error, ", "")
        where = ".".join(x for x in (prefix, loc) if x)
        out.append(f"{where}: {msg}" if where else msg)
    return out


class TemplateService:
    def __init__(self):
        self._lock = threading.Lock()

    @staticmethod
    def user_dir() -> Path:
        return Path(settings.DATA_DIR) / "templates"

    # Loading

    def load(self) -> Tuple[Dict[str, Template], List[Dict[str, str]]]:
        """All templates (built-in first; a user file can't shadow a built-in id) + load errors"""
        templates: Dict[str, Template] = {}
        errors: List[Dict[str, str]] = []
        for directory, custom in ((BUILTIN_DIR, False), (self.user_dir(), True)):
            if not directory.is_dir():
                continue
            for path in sorted(list(directory.glob("*.yaml")) + list(directory.glob("*.yml"))):
                label = path.name if not custom else f"custom/{path.name}"
                try:
                    tpl = self.load_file(path, custom)
                    if tpl.data.id in templates:
                        raise TemplateError(f"id '{tpl.data.id}' is already used by another template")
                    templates[tpl.data.id] = tpl
                except (TemplateError, ValueError, OSError, yaml.YAMLError) as e:
                    errors.append({"file": label, "error": str(e)})
                    logger.warning(f"Template {path}: {e}")
        return templates, errors

    def load_file(self, path: Path, custom: bool = False) -> Template:
        text = path.read_text(encoding="utf-8")
        data = self.parse(text)
        if data.id != path.stem:
            raise TemplateError(f"id '{data.id}' must be the file name ('{path.stem}')")
        return Template(data, path, custom, text)

    def parse(self, text: str) -> TemplateFile:
        """Parse + validate a template document (structure, placeholders, a render with sample values)"""
        try:
            raw = yaml.safe_load(text)
        except yaml.YAMLError as e:
            raise TemplateError(f"invalid YAML: {e}")
        if not isinstance(raw, dict):
            raise TemplateError("a template is a YAML mapping")
        try:
            data = TemplateFile.model_validate(raw)
        except ValidationError as e:
            raise TemplateError("; ".join(_pydantic_errors("", e)))
        names = {p.name for p in data.params}
        cidr_params = {p.name for p in data.params if p.type == "cidr"}
        for key in placeholders([data.name, data.group, data.cluster, data.guide]):
            base = key.split(":", 1)[0]
            if ":" in key:
                if base != "ip" and base not in cidr_params:
                    raise TemplateError(f"{{{{{key}}}}}: ':N' is only for ip or a cidr parameter")
                continue
            if base not in names and base not in BUILTINS:
                raise TemplateError(f"unknown placeholder {{{{{key}}}}} (not a parameter)")
        if any(k.startswith("ip:") for k in placeholders([data.name])):
            raise TemplateError("name can't use {{ip:N}}")
        # Static check: render with sample values (and every bool flipped) through the schemas
        for flip in (False, True):
            sample = self._sample(data.params, flip)
            if sample is None:
                break
            values, errors = coerce_params(data.params, sample)
            if errors:
                raise TemplateError("sample parameters: " + "; ".join(errors))
            result = self._render_doc(data, values, cidr_for_auto="10.99.0.0/24")
            if result["errors"]:
                raise TemplateError("rendered with sample parameters: " + "; ".join(result["errors"]))
        return data

    @staticmethod
    def _sample(params: List[TemplateParam], flip: bool) -> Optional[Dict[str, Any]]:
        sample: Dict[str, Any] = {}
        for p in params:
            if p.type == "bool":
                v = bool(p.default)
                sample[p.name] = (not v) if flip else v
                continue
            if p.default is not None:
                sample[p.name] = p.default
                continue
            if not p.required:
                continue  # left empty, as a user would
            if p.type == "int":
                sample[p.name] = p.min if p.min is not None else 1
            elif p.type == "choice":
                sample[p.name] = p.choices[0]
            elif p.type == "cidr":
                sample[p.name] = "auto"
            elif p.type == "cloud_image":
                sample[p.name] = "debian-13"
            else:
                candidates = ["12345678", "1234", "test", "a", "x1"]
                ok = next((c for c in candidates if not p.pattern or re.fullmatch(p.pattern, c)), None)
                if ok is None:
                    return None  # can't guess a valid value: skip the static check
                sample[p.name] = ok
        if not flip or any(p.type == "bool" for p in params):
            return sample
        return None

    def get(self, template_id: str) -> Template:
        templates, _ = self.load()
        tpl = templates.get(template_id)
        if tpl is None:
            raise LookupError(f"Template '{template_id}' not found")
        return tpl

    def list(self) -> Dict[str, Any]:
        templates, errors = self.load()
        items = sorted(templates.values(), key=lambda t: (t.custom, t.data.title.lower()))
        return {"templates": [t.info() for t in items], "errors": errors}

    # Rendering

    def _render_doc(self, data: TemplateFile, values: Dict[str, Any], cidr_for_auto: Optional[str] = None,
                    pick_cidr: Optional[Callable[[], str]] = None) -> Dict[str, Any]:
        """Substitute and validate (no host checks). Returns {group, cluster, guide, errors}."""
        errors: List[str] = []
        try:
            name = substitute(data.name, lambda k: values[k], values)
            if not isinstance(name, str):
                name = _text(name)
            scope = dict(values, group=name)

            def first(k: str) -> Any:
                if k in scope:
                    return scope[k]
                raise KeyError(k)
            group = substitute(copy.deepcopy(data.group), first, values)
            cluster = substitute(copy.deepcopy(data.cluster), first, values) if data.cluster is not None else None
            group = {"name": name, **{k: v for k, v in group.items() if k != "name"}}
            cidr = group.get("cidr")
            if cidr in (None, "", "auto"):
                cidr = cidr_for_auto or (pick_cidr() if pick_cidr else None)
                if cidr is None:
                    raise TemplateError("no subnet: set the group's cidr")
                group["cidr"] = cidr
            try:
                net = ipaddress.IPv4Network(str(group["cidr"]), strict=False)
                group["cidr"] = str(net)
            except ValueError as e:
                raise TemplateError(f"group.cidr: {e}")
            src = PLACEHOLDER.fullmatch(str(data.group.get("cidr") or "").strip())
            if src and src.group(1) in values:  # the subnet parameter now names the real subnet
                values[src.group(1)] = scope[src.group(1)] = group["cidr"]
            domain = group.get("domain") or f"{name}.lab"
            scope["domain"] = domain

            cidr_params = {p.name for p in data.params if p.type == "cidr"}

            def second(k: str) -> Any:
                if ":" in k:
                    base, n = k.split(":", 1)
                    if base == "ip":
                        return _nth_ip(group["cidr"], int(n))
                    if base in cidr_params and values.get(base) not in (None, "auto"):
                        return _nth_ip(values[base], int(n))
                    raise TemplateError(f"{{{{{k}}}}}: parameter {base} has no network")
                return first(k)
            group = substitute(group, second, values, conditions=False)
            if cluster is not None:
                cluster = substitute(cluster, second, values, conditions=False)
            guide = substitute(data.guide, second, values, conditions=False)
            left = placeholders([group, cluster])
            if left:
                raise TemplateError(f"unresolved placeholders: {', '.join(sorted(set(left)))}")
        except TemplateError as e:
            return {"group": None, "cluster": None, "guide": "", "errors": [str(e)]}
        try:
            GroupSpec.model_validate(group)
        except ValidationError as e:
            errors += _pydantic_errors("group", e)
        if cluster is not None:
            errors += self._check_cluster(cluster)
        return {"group": group, "cluster": cluster, "guide": guide, "errors": errors}

    @staticmethod
    def _guide_for(data: TemplateFile, values: Dict[str, Any], group: Dict[str, Any]) -> str:
        """The guide for an edited spec: group / domain / ip:N follow the edited group"""
        spec = GroupSpec.model_validate(group)
        cidr_params = {p.name for p in data.params if p.type == "cidr"}
        src = PLACEHOLDER.fullmatch(str(data.group.get("cidr") or "").strip())
        scope = dict(values, group=spec.name, domain=spec.domain)
        if src and src.group(1) in scope:
            scope[src.group(1)] = spec.cidr

        def lookup(k: str) -> Any:
            if ":" in k:
                base, n = k.split(":", 1)
                if base == "ip":
                    return _nth_ip(spec.cidr, int(n))
                if base in cidr_params and scope.get(base) not in (None, "auto"):
                    return _nth_ip(scope[base], int(n))
            if k in scope:
                return scope[k]
            raise KeyError(k)
        try:
            return substitute(data.guide, lookup, values, conditions=False)
        except TemplateError:
            return data.guide

    @staticmethod
    def _cluster_request(cluster: Dict[str, Any], group_id: Optional[int] = None) -> ClusterCreate:
        """The template's cluster block -> ClusterCreate. `image` (a cloud image name such as debian-13)
        is an extension: resolved to cloud_image_id at creation."""
        body = {k: v for k, v in cluster.items() if k != "image"}
        if group_id is not None:
            body["group_id"] = group_id
        return ClusterCreate.model_validate(body)

    def _check_cluster(self, cluster: Dict[str, Any]) -> List[str]:
        errors = []
        try:
            req = self._cluster_request(cluster)
        except ValidationError as e:
            return _pydantic_errors("cluster", e)
        if req.type == "k3s":
            errors.append("cluster: k3s clusters use their own network, not a lab group (use kubeadm or openshift)")
        for key in ("group_id", "network", "cidr"):
            if cluster.get(key) is not None:
                errors.append(f"cluster.{key}: set by the template engine (the cluster goes in the template's group)")
        return errors

    def free_cidr(self, db: Session) -> str:
        """First /24 of TEMPLATE_SUBNET_POOL overlapping no libvirt network nor lab group"""
        from app.libvirt_client import libvirt_client
        used = [ipaddress.IPv4Network(n["cidr"]) for n in libvirt_client.network_subnets()]
        used += [ipaddress.IPv4Network(g.cidr) for g in db.query(Group).all() if g.status != "missing"]
        pool = ipaddress.IPv4Network(settings.TEMPLATE_SUBNET_POOL, strict=False)
        for net in pool.subnets(new_prefix=max(24, pool.prefixlen)):
            if not any(net.overlaps(u) for u in used):
                return str(net)
        raise TemplateError(f"No free /24 left in TEMPLATE_SUBNET_POOL ({pool}): set a cidr")

    def render(self, db: Session, template_id: str, raw_params: Dict[str, Any],
               edited: Optional[str] = None, host_checks: bool = True) -> Dict[str, Any]:
        tpl = self.get(template_id)
        data = tpl.data
        values, errors = coerce_params(data.params, raw_params or {})
        warnings: List[str] = []
        result: Dict[str, Any] = {"ok": False, "params": values, "group": None, "cluster": None, "yaml": "",
                                  "guide": "", "hcl": "", "errors": errors, "warnings": warnings,
                                  "estimate": {}}
        if errors:
            return result
        pick = (lambda: self.free_cidr(db)) if host_checks else (lambda: "10.99.0.0/24")
        doc = self._render_doc(data, values, pick_cidr=pick)
        group, cluster, guide = doc["group"], doc["cluster"], doc["guide"]
        if edited is not None and edited.strip():
            group, cluster, edit_errors = self._parse_edited(edited, group)
            if edit_errors:
                errors += edit_errors
                result["yaml"] = edited
                return result
            errors += self._validate(group, cluster)
            guide = self._guide_for(data, values, group) if not errors else guide
        else:
            errors += doc["errors"]
        result.update(group=group, cluster=cluster, guide=guide)
        if group is not None:
            result["yaml"] = edited if edited and edited.strip() else self.to_yaml(group, cluster)
            try:
                result["hcl"] = to_hcl(group, cluster, template=data.id)
            except Exception as e:  # never block a preview on the export
                result["hcl"] = f"# could not build the OpenTofu snippet: {e}\n"
        if group is not None and not errors and host_checks:
            errors += self._host_checks(db, data, group, cluster, warnings)
        if group is not None:
            result["estimate"] = self.estimate(data, group, cluster)
            est = result["estimate"]
            if not est.get("fits", True):
                warnings.append(f"The lab needs about {est['memory_mb'] // 1024} GiB of RAM; the host has "
                                f"{(est.get('host_memory_free_mb') or 0) // 1024} GiB available right now")
        result["ok"] = not errors and group is not None
        return result

    @staticmethod
    def to_yaml(group: Dict[str, Any], cluster: Optional[Dict[str, Any]]) -> str:
        doc: Dict[str, Any] = {"group": group}
        if cluster is not None:
            doc["cluster"] = cluster
        return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False, allow_unicode=True)

    def _parse_edited(self, text: str, rendered_group: Optional[Dict[str, Any]]) -> Tuple[Any, Any, List[str]]:
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError as e:
            return None, None, [f"YAML: {e}"]
        if not isinstance(doc, dict) or not isinstance(doc.get("group"), dict):
            return None, None, ["The document needs a 'group:' mapping (and optionally 'cluster:')"]
        extra = set(doc) - {"group", "cluster"}
        if extra:
            return None, None, [f"Unknown top-level keys: {', '.join(sorted(extra))} (only group and cluster)"]
        cluster = doc.get("cluster")
        if cluster is not None and not isinstance(cluster, dict):
            return None, None, ["'cluster:' must be a mapping"]
        left = placeholders([doc["group"], cluster])
        if left:
            return None, None, [f"Unresolved placeholders: {', '.join(sorted(set(left)))}"]
        return doc["group"], cluster, []

    def _validate(self, group: Dict[str, Any], cluster: Optional[Dict[str, Any]]) -> List[str]:
        errors: List[str] = []
        try:
            GroupSpec.model_validate(group)
        except ValidationError as e:
            errors += _pydantic_errors("group", e)
        if cluster is not None:
            errors += self._check_cluster(cluster)
        return errors

    def _host_checks(self, db: Session, data: TemplateFile, group: Dict[str, Any],
                     cluster: Optional[Dict[str, Any]], warnings: List[str]) -> List[str]:
        """What create would refuse on this host: names taken, subnet overlaps, images, pools"""
        from app.libvirt_client import libvirt_client
        from app.services.group_service import group_service, member_vm_name, network_name, router_vm_name
        errors: List[str] = []
        spec = GroupSpec.model_validate(group)
        existing = db.query(Group).filter(Group.name == spec.name).first()
        if existing is not None and existing.status != "missing":
            errors.append(f"A lab group named '{spec.name}' already exists (change the case number or the name)")
        nets = {n["name"] for n in libvirt_client.list_networks()}
        if network_name(spec.name) in nets:
            errors.append(f"Network '{network_name(spec.name)}' already exists")
        vms = {vm["name"] for vm in libvirt_client.list_vms()}
        for vm in [router_vm_name(spec.name)] + [member_vm_name(spec.name, m.name) for m in spec.members]:
            if len(vm) > 64:
                errors.append(f"VM name '{vm}' is too long (64 characters max)")
            elif vm in vms:
                errors.append(f"A VM named '{vm}' already exists")
        if not errors:
            try:
                group_service.normalize(db, spec)
            except ValueError as e:
                errors.append(str(e))
        if cluster is not None:
            req = self._cluster_request(cluster)
            if db.query(Cluster).filter(Cluster.name == req.name).first():
                errors.append(f"A cluster named '{req.name}' already exists")
            from app.services.cluster_service import cluster_service
            nodes = cluster_service._node_names(req.name, req.ctlplanes, req.workers)
            clash = [n for n in nodes if n in vms]
            if clash:
                errors.append(f"A VM named '{clash[0]}' already exists")
            if cluster.get("image"):
                try:
                    group_service.resolve_image(db, str(cluster["image"]))
                except ValueError as e:
                    errors.append(f"cluster.image: {e}")
            if req.type == "openshift" or "openshift" in data.requires or "pull-secret" in data.requires:
                from app.services.openshift_service import openshift_service
                if not openshift_service.pull_secret_status().get("configured"):
                    errors.append("OpenShift needs your pull secret: add it on the Clusters page first")
            if not spec.uplink:
                errors.append("A cluster needs a group with an uplink (the host reaches its API through the router)")
        for req_name in data.requires:
            if req_name not in KNOWN_REQUIRES:
                warnings.append(f"Requires '{req_name}': not checked by this version of the app")
        return errors

    def estimate(self, data: TemplateFile, group: Dict[str, Any], cluster: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        detail: List[str] = []
        try:
            spec = GroupSpec.model_validate(group)
        except ValidationError:
            return {}
        r = spec.router
        mem, cpu = r.effective_memory(), r.effective_vcpu()
        disk = r.disk_size + (r.registry.disk_gb if r.registry.enabled else 0)
        detail.append(f"router: {mem} MiB, {cpu} vCPU, {disk} GiB")
        for m in spec.members:
            mem += m.memory
            cpu += m.vcpu
            disk += m.disk_size
        if spec.members:
            detail.append(f"{len(spec.members)} member(s): {sum(m.memory for m in spec.members)} MiB")
        if cluster is not None:
            try:
                req = self._cluster_request(cluster)
                if req.type != "openshift":
                    cm = req.ctlplanes * req.ctlplane.memory + req.workers * req.worker.memory
                    mem += cm
                    cpu += req.ctlplanes * req.ctlplane.vcpu + req.workers * req.worker.vcpu
                    disk += req.ctlplanes * req.ctlplane.disk_size + req.workers * req.worker.disk_size
                    detail.append(f"cluster {req.name}: {req.ctlplanes}+{req.workers} node(s), {cm} MiB")
                else:
                    detail.append(f"OpenShift {req.openshift.topology if req.openshift else 'sno'}: "
                                  "see the template's estimate")
            except ValidationError:
                pass
        declared = data.resources
        mem, cpu, disk = max(mem, declared.memory_mb), max(cpu, declared.vcpus), max(disk, declared.disk_gb)
        free = None
        try:
            from app.services.host_service import host_service
            free = int(host_service.get_resources()["memory_free"] // (1024 * 1024))
        except Exception:
            pass
        return {"vcpus": cpu, "memory_mb": mem, "disk_gb": disk, "host_memory_free_mb": free,
                "fits": free is None or mem <= free, "detail": detail}

    # Create

    def create(self, db: Session, template_id: str, raw_params: Dict[str, Any],
               edited: Optional[str] = None) -> Dict[str, Any]:
        from app.services.group_service import group_service
        from app.services.task_service import task_service
        with self._lock:
            r = self.render(db, template_id, raw_params, edited)
            if not r["ok"]:
                raise ValueError("; ".join(r["errors"]) or "The template could not be rendered")
            tpl = self.get(template_id)
            spec = GroupSpec.model_validate(r["group"])
            case = r["params"].get("case")
            spec.template = TemplateRef(
                id=tpl.data.id, title=tpl.data.title, case=_text(case) if case is not None else None,
                params=r["params"], guide=r["guide"] or None,
                created_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat())
            cluster = r["cluster"]
            cluster_id_image = None
            if cluster is not None and cluster.get("image"):
                cluster_id_image = group_service.resolve_image(db, str(cluster["image"])).id
            group, task = group_service.create_group(db, spec)
        out = {"group_id": group.id, "group_name": group.name, "task_id": task.id, "cluster_task_id": None}
        if cluster is not None:
            body = dict(cluster)
            body.pop("image", None)
            if cluster_id_image is not None:
                body["cloud_image_id"] = cluster_id_image
            ctask = task_service.start(db, TaskCreate(
                name=f"Create {body.get('type', 'k3s')} cluster {body['name']} (template {tpl.data.id})",
                type="template_cluster", target_type="group", target_id=group.id, target_name=group.name,
                description=f"Waits for lab group {group.name}, then creates the cluster in it",
            ), self._cluster_task, group.id, body)
            out["cluster_task_id"] = ctask.id
        return out

    def _cluster_task(self, db: Session, task: Task, group_id: int, body: Dict[str, Any]) -> Dict[str, Any]:
        from app.services.cluster_service import cluster_service
        from app.services.task_service import task_service
        deadline = time.monotonic() + GROUP_WAIT_TIMEOUT
        while True:
            if task_service.is_cancelled(task.id):
                raise RuntimeError("Cancelled")
            db.expire_all()
            group = db.query(Group).filter(Group.id == group_id).first()
            if group is None:
                raise RuntimeError("The lab group was deleted")
            if group.status == "ready":
                break
            if group.status in ("error", "missing", "deleting"):
                raise RuntimeError(f"Lab group {group.name} is {group.status}"
                                   + (f": {group.error_message}" if group.error_message else ""))
            if time.monotonic() > deadline:
                raise TimeoutError(f"Lab group {group.name} not ready after {GROUP_WAIT_TIMEOUT // 60} min")
            time.sleep(3)
        task_service.update_progress(db, task.id, 50)
        cluster = cluster_service.create_cluster(db, self._cluster_request(body, group_id))
        return {"cluster_id": cluster.id, "cluster_name": cluster.name}

    # User templates

    def save_from_group(self, db: Session, body: TemplateSave) -> Dict[str, Any]:
        from app.services.group_service import group_service
        templates, _ = self.load()
        if body.id in templates:
            raise ValueError(f"A template with id '{body.id}' already exists")
        group = group_service.get_group(db, body.group_id)
        if group is None:
            raise LookupError("Group not found")
        doc = self.templatize(db, group, body)
        text = yaml.safe_dump(doc, sort_keys=False, default_flow_style=False, allow_unicode=True, width=110)
        self.parse(text)  # what we write must load
        directory = self.user_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{body.id}.yaml"
        tmp = path.with_suffix(".yaml.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
        logger.info(f"Saved group {group.name} as template {body.id} ({path})")
        return self.get(body.id).detail()

    def templatize(self, db: Session, group: Group, body: TemplateSave) -> Dict[str, Any]:
        """A group's spec as a template: assigned fields stripped, addresses of the group network
        turned into {{ip:N}}, the name / subnet into parameters"""
        from app.services.group_service import group_service
        spec_model = GroupSpec.model_validate(group.spec)
        ref = spec_model.template
        # only what differs from the defaults (a readable file)
        spec = GroupSpec.model_validate(group_service.export_yaml(group)["spec"]).model_dump(
            mode="json", exclude_defaults=True)
        net = ipaddress.IPv4Network(spec["cidr"])
        old_name = spec["name"]
        spec.pop("template", None)
        spec.pop("owner", None)
        router = spec.get("router", {})
        for key in ("ip", "lan_mac", "uplink_mac", "uplink_ip", "image"):
            router.pop(key, None)
        wg = router.get("wireguard")
        if isinstance(wg, dict):
            wg.pop("peers", None)
        bgp = router.get("bgp")
        if isinstance(bgp, dict):
            bgp.pop("announce_ranges", None)  # assigned from this host's pool (unique on the host)
        for m in spec.get("members", []):
            m.pop("mac", None)
        if spec.get("domain") == f"{old_name}.lab":
            spec.pop("domain")
        spec.pop("name", None)
        spec["cidr"] = "{{cidr}}"

        def relative(obj: Any) -> Any:
            if isinstance(obj, dict):
                return {k: relative(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [relative(v) for v in obj]
            if isinstance(obj, str) and obj != "{{cidr}}":
                def repl(m: "re.Match") -> str:
                    try:
                        ip = ipaddress.IPv4Address(m.group(1))
                    except ValueError:
                        return m.group(0)
                    if ip in net and ip not in (net.network_address, net.broadcast_address):
                        return "{{ip:%d}}" % (int(ip) - int(net.network_address))
                    return m.group(0)
                return IPV4_IN_TEXT.sub(repl, obj)
            return obj
        spec = relative(spec)

        cluster = None
        clusters = db.query(Cluster).filter(Cluster.group_id == group.id).all()
        if len(clusters) == 1 and clusters[0].type in ("kubeadm", "openshift"):
            cluster = self._cluster_block(clusters[0])
        base = re.sub(r"-+$", "", old_name[:18]) or "lab"
        tags = body.tags or ["custom"]
        guide = body.guide
        if not guide:
            guide = (ref.guide if ref and ref.guide else
                     f"# Case {{{{case}}}}\n\nSaved from lab group **{old_name}**.\n\n"
                     "## Symptom\n\n_Describe what the customer sees._\n\n## Steps\n\n1. …\n\n## Checks\n\n- …\n")
        doc: Dict[str, Any] = {
            "id": body.id, "title": body.title, "summary": body.summary or f"Saved from lab group {old_name}",
            "tags": tags, "requires": [], "resources": {}, "params": [
                {"name": "case", "label": "Case number", "type": "string", "required": True,
                 "pattern": "^[0-9a-z][0-9a-z-]{0,11}$"},
                {"name": "cidr", "label": "Group subnet", "type": "cidr", "default": "auto",
                 "help": "auto = the first free /24"},
            ],
            "name": "c{{case}}-" + base,
            "group": spec,
        }
        if cluster is not None:
            doc["cluster"] = cluster
            doc["requires"] = [cluster["type"]]
        doc["guide"] = guide
        # declared estimate = what the rendered lab computes (the card shows it)
        rendered = self._render_doc(TemplateFile.model_validate(doc),
                                    {"case": "1", "cidr": "auto"}, cidr_for_auto=str(net))
        if rendered["group"] is not None:
            est = self.estimate(TemplateFile.model_validate(doc), rendered["group"], rendered["cluster"])
            if est:
                doc["resources"] = {"vcpus": est["vcpus"], "memory_mb": est["memory_mb"], "disk_gb": est["disk_gb"]}
        return doc

    @staticmethod
    def _cluster_block(cluster: Cluster) -> Dict[str, Any]:
        spec = dict(cluster.spec or {})
        out: Dict[str, Any] = {"name": "c{{case}}-" + re.sub(r"-+$", "", cluster.name[:20]), "type": cluster.type}
        for key in ("version", "ctlplanes", "workers", "ctlplane", "worker", "pod_cidr", "service_cidr",
                    "extra_args", "username", "password", "ssh_keys", "keyboard"):
            if spec.get(key) not in (None, [], ""):
                out[key] = spec[key]
        image = spec.get("image")  # "debian 13"
        if isinstance(image, str) and " " in image:
            out["image"] = image.replace(" ", "-").lower()
        if cluster.type == "openshift" and isinstance(spec.get("openshift"), dict):
            osd = copy.deepcopy(spec["openshift"])
            if isinstance(osd.get("metallb"), dict):
                osd["metallb"].pop("pool", None)
            out["openshift"] = osd
            for key in ("ctlplanes", "workers", "ctlplane", "worker"):
                out.pop(key, None)  # follow the topology
        return out

    def delete(self, template_id: str) -> None:
        tpl = self.get(template_id)
        if not tpl.custom:
            raise ValueError("Built-in templates are read-only")
        tpl.path.unlink()


# OpenTofu export

def _hcl_str(value: Any) -> str:
    return json.dumps(_text(value)).replace("${", "$${").replace("%{", "%%{")


def _hcl_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_hcl_value(v) for v in value) + "]"
    return _hcl_str(value)


def _ident(name: str) -> str:
    ident = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    return ident if ident and not ident[0].isdigit() else f"lab_{ident}"


def to_hcl(group: Dict[str, Any], cluster: Optional[Dict[str, Any]], template: Optional[str] = None) -> str:
    """vmmanager_group (+ vmmanager_cluster) for the rendered spec. What the provider can't express is
    listed as comments (create those parts in the UI / API, or keep the YAML)."""
    spec = GroupSpec.model_validate(group)
    res = _ident(spec.name)
    lines: List[str] = []
    missing: List[str] = []
    if template:
        lines.append(f"# From template {template} (vm-manager). Provider: opentofu_provider (local/vmmanager)")
    lines.append(f'resource "vmmanager_group" "{res}" {{')

    def attr(key: str, value: Any, indent: str = "  ") -> None:
        lines.append(f"{indent}{key} = {_hcl_value(value)}")
    attr("name", spec.name)
    attr("cidr", spec.cidr)
    if spec.domain != f"{spec.name}.lab":
        attr("domain", spec.domain)
    if spec.uplink != "default":
        if spec.uplink is None:
            missing.append("uplink: none (the provider always gives the router an uplink)")
        else:
            attr("uplink", spec.uplink)
    r = spec.router
    if r.image:
        attr("router_image", r.image)
    if r.memory != 512:
        attr("router_memory", r.memory)
    if r.vcpu != 1 or r.disk_size != 10:
        missing.append(f"router.vcpu = {r.vcpu}, router.disk_size = {r.disk_size}")
    if r.dns.forwarders:
        attr("dns_forwarders", r.dns.forwarders)
    if r.wireguard is not None and r.wireguard.enabled:
        attr("wireguard", True)
        if r.wireguard.listen_port != 51820:
            attr("wireguard_listen_port", r.wireguard.listen_port)
    if r.bgp is not None and r.bgp.enabled:
        attr("bgp", True)
        defaults = r.bgp.asn == 64512 and r.bgp.listen and r.bgp.peer_asn == 64513 and r.bgp.maximum_paths == 8
        if not defaults or r.bgp.neighbors or r.bgp.announce_ranges:
            missing.append("router.bgp details (asn, peer_asn, neighbors, announce_ranges): set them on the BGP tab")
    ci = spec.cloud_init
    lines.append("  cloud_init = {")
    if ci.username:
        attr("username", ci.username, "    ")
    if ci.password:
        attr("password", ci.password, "    ")
    if ci.ssh_keys:
        attr("ssh_keys", ci.ssh_keys, "    ")
    if ci.keyboard:
        attr("keyboard", ci.keyboard, "    ")
    lines.append("  }")
    if r.egress.mode != "open" or r.egress.allow:
        lines.append("  egress = {")
        attr("mode", r.egress.mode, "    ")
        if r.egress.allow:
            attr("allow", r.egress.allow, "    ")
        lines.append("  }")
    if r.registry.enabled:
        lines.append("  registry = {")
        attr("enabled", True, "    ")
        for key, default in (("port", 8443), ("disk_gb", 250), ("memory_mb", 8192), ("vcpus", 4)):
            if getattr(r.registry, key) != default:
                attr(key, getattr(r.registry, key), "    ")
        lines.append("  }")
    for m in spec.members:
        lines.append("")
        lines.append("  member {")
        attr("name", m.name, "    ")
        if m.source != "cloud_image":
            attr("source", m.source, "    ")
        if m.source == "cloud_image" and m.image != "debian-13":
            attr("image", m.image, "    ")
        if m.iso:
            attr("iso", m.iso, "    ")
        for key, default in (("memory", 1024), ("vcpu", 1), ("disk_size", 10), ("role", "member")):
            if getattr(m, key) != default:
                attr(key, getattr(m, key), "    ")
        if m.ip:
            attr("ip", m.ip, "    ")
        if m.cloud_init is not None:
            lines.append("    cloud_init = {")
            for key in ("username", "password", "ssh_keys", "keyboard"):
                v = getattr(m.cloud_init, key)
                if v:
                    attr(key, v, "      ")
            lines.append("    }")
        if m.user_data:
            lines.append("    user_data = <<-EOT")
            lines += ["      " + ln.replace("${", "$${").replace("%{", "%%{") for ln in m.user_data.splitlines()]
            lines.append("    EOT")
        lines.append("  }")
    for h in spec.dhcp_hosts:
        lines.append("")
        lines.append("  dhcp_host {")
        attr("mac", h.mac, "    ")
        attr("ip", h.ip, "    ")
        if h.hostname:
            attr("hostname", h.hostname, "    ")
        lines.append("  }")
    for rec in spec.router.dns.records:
        if rec.owner:
            continue
        lines.append("")
        lines.append("  dns_record {")
        attr("name", rec.name, "    ")
        attr("a" if rec.a else "cname", rec.a or rec.cname, "    ")
        lines.append("  }")
    lines.append("}")
    if spec.dhcp is not None:
        missing.append(f"dhcp range {spec.dhcp.start}-{spec.dhcp.end} (the provider uses the default range)")
    if [lb for lb in spec.load_balancers if not lb.owner]:
        missing.append("load_balancers: " + ", ".join(f"{lb.name}:{lb.port}" for lb in spec.load_balancers if not lb.owner))
    if [p for p in spec.address_pools if not p.owner]:
        missing.append("address_pools: " + ", ".join(f"{p.name} {p.start}-{p.end}" for p in spec.address_pools))
    if spec.reservations:
        missing.append("reservations (cluster nodes are created by vmmanager_cluster)")

    if cluster is not None:
        req = ClusterCreate.model_validate({k: v for k, v in cluster.items() if k != "image"})
        cres = _ident(req.name)
        lines += ["", f'resource "vmmanager_cluster" "{cres}" {{']
        attr("name", req.name)
        attr("type", req.type)
        lines.append(f"  group_id = vmmanager_group.{res}.id")
        if req.version:
            attr("version", req.version)
        if req.type != "openshift":
            attr("ctlplanes", req.ctlplanes)
            attr("workers", req.workers)
            for role in ("ctlplane", "worker"):
                nr = getattr(req, role)
                if nr.memory != 2048:
                    attr(f"{role}_memory", nr.memory)
                if nr.vcpu != 2:
                    attr(f"{role}_vcpu", nr.vcpu)
                if nr.disk_size != 20:
                    attr(f"{role}_disk_size", nr.disk_size)
        if cluster.get("image"):
            lines.append(f"  # cloud_image_id = vmmanager_cloud_image.<{cluster['image']}>.id  (default: Debian 13)")
        if req.extra_args:
            attr("extra_args", req.extra_args)
        if req.username:
            attr("username", req.username)
        if req.password:
            attr("password", req.password)
        if req.ssh_keys:
            attr("ssh_keys", req.ssh_keys)
        if req.pod_cidr != "10.244.0.0/16" or req.service_cidr != "10.96.0.0/16":
            missing.append(f"cluster pod_cidr {req.pod_cidr} / service_cidr {req.service_cidr}")
        o = req.openshift
        if req.type == "openshift" and o is not None:
            lines.append("  openshift = {")
            attr("channel", o.channel, "    ")
            attr("topology", o.topology, "    ")
            if o.storage != "none":
                attr("storage", o.storage, "    ")
                attr("storage_disk_size", o.storage_disk_size, "    ")
            if o.storage == "odf":
                attr("odf_profile", o.odf_profile, "    ")
            if o.operators:
                attr("operators", [op.name for op in o.operators], "    ")
            if o.sriov.enabled:
                attr("sriov", True, "    ")
                attr("sriov_nics", o.sriov.nics, "    ")
                attr("sriov_vfs", o.sriov.vfs, "    ")
                attr("sriov_device_type", o.sriov.device_type, "    ")
            if o.metallb.enabled:
                attr("metallb", True, "    ")
                attr("metallb_mode", o.metallb.mode, "    ")
                attr("metallb_addresses", o.metallb.addresses, "    ")
                attr("metallb_demo", o.metallb.demo, "    ")
            attr("disable_updates", o.disable_updates, "    ")
            if o.disconnected:
                attr("disconnected", True, "    ")
            lines.append("  }")
            lines.append("  # needs: resource \"vmmanager_openshift_pull_secret\" (or the pull secret set in the UI)")
        lines.append("}")
    if missing:
        lines += ["", "# Not expressible with the provider (set in the UI / API after apply, or use the YAML):"]
        lines += [f"#   - {m}" for m in missing]
    return "\n".join(lines) + "\n"


template_service = TemplateService()
