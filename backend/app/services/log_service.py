"""Log viewer + machine diagnostics (docs/logs.md).

- VM logs through the QEMU guest agent (journal, kernel ring buffer, cloud-init, any file): no SSH, no route.
- Pod logs of a cluster through the same kubectl / oc path as the MCP kubectl tool (kube_exec).
- One server-side filter for every caller (UI, API, MCP): level, include / exclude regex, the "known problems"
  ruleset (log_rules.py) with context lines, counts per category and a plain-words hint.
- Diagnostics: OS / firmware / PCI / NIC drivers / disks / SR-IOV / failed units in one guest-exec, plus the agent's
  own info commands (they still work where EL's qemu-ga forbids guest-exec).
"""
import json
import re
import shlex
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import libvirt

from app.libvirt_client import libvirt_client
from app.models import Cluster
from app.services.kube_exec import KubeExecError, run_kubectl
from app.services.log_rules import CATEGORIES, RULES

MAX_LINES = 20000           # lines fetched from a source
DEFAULT_LINES = 1000
MAX_ENTRIES = 5000          # lines returned after filtering
MAX_LINE_CHARS = 4000
MAX_CONTEXT = 20
MAX_PODS = 30
LEVELS = {"error": 2, "warning": 1, "info": 0}
VM_SOURCES = ("journal", "dmesg", "cloud-init", "file")

SAFE_ARG_RE = re.compile(r"^[\w@:+.,/ \-*]{1,120}$")
K8S_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")
DURATION_RE = re.compile(r"^(\d+h)?(\d+m)?(\d+s)?$")
SELECTOR_RE = re.compile(r"^[\w./\-=!, ()]{1,200}$")

# Level heuristics for sources without a syslog priority (files, pod logs)
ERROR_RE = re.compile(r"\b(error|err|fatal|failed|failure|panic|critical|crit|emerg|alert|exception)\b|"
                      r"(^|\s)[EF]\d{4} \d\d:\d\d|level=(error|fatal|panic)|\"level\":\s*\"(error|fatal|panic)\"",
                      re.IGNORECASE)
WARN_RE = re.compile(r"\b(warn|warning|deprecated)\b|(^|\s)W\d{4} \d\d:\d\d|level=warn|\"level\":\s*\"warn",
                     re.IGNORECASE)
# Our own commands, logged by the guest agent (Debian's qemu-ga logs every guest-exec with its arguments): never a
# known problem, or the diagnostics script would flag itself
OWN_COMMANDS_RE = re.compile(r"qemu-ga(\[\d+\])?: info: guest-exec called")
COMPILED = [(r, re.compile(r["pattern"], re.IGNORECASE),
             re.compile(r["ignore"], re.IGNORECASE) if r.get("ignore") else None) for r in RULES]


class LogError(Exception):
    """Plain-words failure (agent down, guest-exec forbidden, bad option): 400 for the API"""


# ----------------------------------------------------------------------------------------------- filtering

def _compile(pattern: Optional[str], what: str) -> Optional["re.Pattern[str]"]:
    if not pattern:
        return None
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        raise LogError(f"{what}: invalid regular expression ({e})")


# an explicit level in the line wins over keywords: klog "E1011 12:00", logfmt level=warning, JSON "level":"info"
KLOG_RE = re.compile(r"(^|\s)([EWIF])\d{4} \d\d:\d\d:\d\d")
EXPLICIT_LEVEL_RE = re.compile(r"\blevel[=:]\s*\"?(\w+)|\"level\":\s*\"(\w+)", re.IGNORECASE)
LEVEL_WORDS = {"error": "error", "err": "error", "fatal": "error", "panic": "error", "critical": "error",
               "crit": "error", "warning": "warning", "warn": "warning", "info": "info", "debug": "info",
               "trace": "info", "notice": "info"}


def guess_level(line: str) -> str:
    m = KLOG_RE.search(line[:120])
    if m:
        return {"E": "error", "F": "error", "W": "warning"}.get(m.group(2), "info")
    m = EXPLICIT_LEVEL_RE.search(line[:300])
    if m and (m.group(1) or m.group(2) or "").lower() in LEVEL_WORDS:
        return LEVEL_WORDS[(m.group(1) or m.group(2)).lower()]
    if ERROR_RE.search(line):
        return "error"
    if WARN_RE.search(line):
        return "warning"
    return "info"


def rules_info() -> Dict[str, Any]:
    return {"categories": CATEGORIES,
            "rules": [{k: r.get(k) for k in ("id", "category", "level", "hint", "pattern", "ignore")} for r in RULES]}


def analyze(lines: List[str], levels: Optional[List[Optional[str]]] = None, level: Optional[str] = None,
            include: Optional[str] = None, exclude: Optional[str] = None, problems_only: bool = False,
            category: Optional[str] = None, context: int = 2, max_entries: int = MAX_ENTRIES) -> Dict[str, Any]:
    """Annotate every line (level, known-problem rules), count per category, keep the lines that pass the filters.

    levels: per-line level when the source knows it (journal priority, dmesg -x), else guessed from the text.
    category: comma-separated categories (or rule ids) to keep; implies problems_only.
    Returns {total_lines, matched, returned, truncated, levels, summary[], entries[]}; an entry is
    {n, text, level, rules[], before[], after[]} (context lines only when a filter is active).
    """
    if level and level not in LEVELS:
        raise LogError("level must be error, warning or info")
    inc, exc = _compile(include, "include"), _compile(exclude, "exclude")
    cats = {c.strip() for c in (category or "").split(",") if c.strip()}
    context = max(0, min(int(context or 0), MAX_CONTEXT))
    max_entries = max(1, min(int(max_entries or MAX_ENTRIES), MAX_ENTRIES))
    min_level = LEVELS.get(level or "info", 0)
    filtering = bool(level and level != "info") or bool(inc) or bool(exc) or problems_only or bool(cats)

    summary: Dict[str, Dict[str, Any]] = {}
    level_counts = {"error": 0, "warning": 0, "info": 0}
    selected: List[Tuple[int, str, List[str]]] = []
    matched = 0
    for i, raw in enumerate(lines):
        text = raw if len(raw) <= MAX_LINE_CHARS else raw[:MAX_LINE_CHARS] + " …[cut]"
        if OWN_COMMANDS_RE.search(text):
            lv, hits = "info", []
        else:
            lv = (levels[i] if levels and i < len(levels) else None) or guess_level(text)
            hits = [r for r, rx, ign in COMPILED if rx.search(text) and not (ign and ign.search(text))]
        for r in hits:
            if LEVELS[r["level"]] > LEVELS[lv]:
                lv = r["level"]
            s = summary.setdefault(r["category"], {"category": r["category"], "label": CATEGORIES.get(r["category"],
                                                    r["category"]), "count": 0, "rules": {}})
            s["count"] += 1
            rs = s["rules"].setdefault(r["id"], {"id": r["id"], "count": 0, "hint": r["hint"], "level": r["level"],
                                                 "first_line": i + 1})
            rs["count"] += 1
        level_counts[lv] += 1
        if LEVELS[lv] < min_level:
            continue
        if inc and not inc.search(text):
            continue
        if exc and exc.search(text):
            continue
        if (problems_only or cats) and not hits:
            continue
        if cats and not any(r["category"] in cats or r["id"] in cats for r in hits):
            continue
        matched += 1
        selected.append((i, lv, [r["id"] for r in hits]))

    truncated = len(selected) > max_entries
    if truncated:  # keep the most recent ones
        selected = selected[-max_entries:]
    entries = []
    for i, lv, ids in selected:
        e: Dict[str, Any] = {"n": i + 1, "text": _cut(lines[i]), "level": lv, "rules": ids}
        if filtering and context:
            e["before"] = [_cut(t) for t in lines[max(0, i - context):i]]
            e["after"] = [_cut(t) for t in lines[i + 1:i + 1 + context]]
        entries.append(e)
    order = sorted(summary.values(), key=lambda s: (-max(LEVELS[r["level"]] for r in s["rules"].values()),
                                                    -s["count"]))
    for s in order:
        s["rules"] = sorted(s["rules"].values(), key=lambda r: -r["count"])
        s["hint"] = s["rules"][0]["hint"]
    return {"total_lines": len(lines), "matched": matched, "returned": len(entries), "truncated": truncated,
            "levels": level_counts, "summary": order, "entries": entries}


def _cut(t: str) -> str:
    return t if len(t) <= MAX_LINE_CHARS else t[:MAX_LINE_CHARS] + " …[cut]"


def as_text(result: Dict[str, Any]) -> str:
    return "\n".join(e["text"] for e in result.get("entries", [])) + "\n"


# ----------------------------------------------------------------------------------------------- guest agent

def agent_error(name: str, e: Exception) -> LogError:
    """Plain words for the usual guest agent failures"""
    if isinstance(e, TimeoutError):
        return LogError(f"{name}: the command didn't finish in time ({e})")
    msg = (e.get_error_message() if isinstance(e, libvirt.libvirtError) else "") or str(e)
    code = e.get_error_code() if isinstance(e, libvirt.libvirtError) else None
    low = msg.lower()
    if code == libvirt.VIR_ERR_NO_DOMAIN:
        return LogError(f"{name}: no such VM in libvirt")
    if "disabled" in low or "not allowed" in low or "blocked" in low or "blacklist" in low:
        return LogError(
            f"{name}: the guest agent forbids running commands (guest-exec) in this VM. EL images (RHEL / Alma / "
            "Rocky) block it by default; vm-manager only unlocks it on cluster nodes and lab routers. To allow it, in "
            "the VM: write FILTER_RPC_ARGS=\"\" and BLACKLIST_RPC=\"\" to /etc/sysconfig/qemu-ga, run `semodule -i` "
            "on a CIL file containing (typepermissive virt_qemu_ga_t), restart qemu-guest-agent. Or read the logs "
            f"over SSH. (agent: {msg})")
    if (code == libvirt.VIR_ERR_OPERATION_INVALID and "running" in low) or "not running" in low \
            or "n'est pas en cours" in low:
        return LogError(f"{name}: the VM is not running")
    if code in (getattr(libvirt, "VIR_ERR_AGENT_UNRESPONSIVE", 86), getattr(libvirt, "VIR_ERR_AGENT_UNSYNCED", 87)) \
            or "agent" in low:
        return LogError(f"{name}: the QEMU guest agent doesn't answer: the VM is still booting (the agent starts "
                        f"~30-60 s after power on for a cloud image) or qemu-guest-agent isn't installed / running in "
                        f"it. ({msg})")
    return LogError(f"{name}: guest agent: {msg}")


def _exec(name: str, argv: List[str], timeout: float = 60) -> Dict[str, Any]:
    try:
        return libvirt_client.agent_exec(name, argv[0], argv[1:], timeout=timeout)
    except (libvirt.libvirtError, TimeoutError) as e:
        raise agent_error(name, e)


def _sh(name: str, script: str, timeout: float = 60) -> Dict[str, Any]:
    return _exec(name, ["/bin/sh", "-c", script], timeout)


def _safe(value: Optional[str], what: str, dash: bool = False) -> Optional[str]:
    if value is None or value == "":
        return None
    value = str(value)
    if not SAFE_ARG_RE.match(value) or (value.startswith("-") and not dash):
        raise LogError(f"{what}: unexpected characters in {value!r}")
    return value


def _journal_line(rec: Dict[str, Any]) -> Tuple[str, str]:
    msg = rec.get("MESSAGE", "")
    if isinstance(msg, list):  # non-UTF-8 message: array of bytes
        msg = bytes(b for b in msg if isinstance(b, int)).decode(errors="replace")
    try:
        ts = datetime.fromtimestamp(int(rec.get("__REALTIME_TIMESTAMP", 0)) / 1e6, tz=timezone.utc)
        stamp = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        stamp = "?"
    ident = rec.get("SYSLOG_IDENTIFIER") or rec.get("_COMM") or rec.get("_SYSTEMD_UNIT") or "?"
    pid = rec.get("_PID") or rec.get("SYSLOG_PID")
    try:
        prio = int(rec.get("PRIORITY", 6))
    except (TypeError, ValueError):
        prio = 6
    level = "error" if prio <= 3 else "warning" if prio == 4 else "info"
    if level == "info":  # stdout-captured services (kubelet, crio: klog "E1011 …") all log at priority 6
        level = guess_level(msg)
    return f"{stamp} {ident}{'[' + str(pid) + ']' if pid else ''}: {msg}", level


def _journal(name: str, lines: int, unit: Optional[List[str]] = None, priority: Optional[str] = None,
             since: Optional[str] = None, until: Optional[str] = None, boot: Optional[str] = None,
             grep: Optional[str] = None, kernel: bool = False) -> Tuple[str, List[str], List[Optional[str]]]:
    argv = ["journalctl", "--no-pager", "-o", "json", "-n", str(lines),
            "--output-fields=PRIORITY,SYSLOG_IDENTIFIER,_COMM,_PID,MESSAGE,_SYSTEMD_UNIT"]
    if kernel:
        argv.append("-k")
    for u in unit or []:
        argv += ["-u", _safe(u, "unit") or ""]
    if priority:
        if priority not in ("emerg", "alert", "crit", "err", "warning", "notice", "info", "debug") \
                and not re.match(r"^[0-7](\.\.[0-7])?$", priority):
            raise LogError("priority: emerg, alert, crit, err, warning, notice, info or debug")
        argv += ["-p", priority]
    if since:
        argv.append("--since=" + _safe(since, "since", dash=True))
    if until:
        argv.append("--until=" + _safe(until, "until", dash=True))
    if boot not in (None, "", "all"):
        if not re.match(r"^-?\d{1,3}$", str(boot)):
            raise LogError("boot: 0 = current boot, -1 = previous one, … or 'all'")
        argv += ["-b", str(boot)]
    if grep:
        if "\n" in grep or len(grep) > 200:
            raise LogError("grep: one line, at most 200 characters")
        argv += ["--grep=" + grep, "--case-sensitive=false"]
    out = _exec(name, ["/usr/bin/env"] + argv, timeout=90)
    texts: List[str] = []
    levels: List[Optional[str]] = []
    for raw in out["stdout"].splitlines():
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        try:
            text, lv = _journal_line(json.loads(raw))
        except ValueError:
            continue
        texts.append(text)
        levels.append(lv)
    if out["exitcode"] != 0 and not texts:
        err = (out["stderr"] or out["stdout"]).strip()
        if "no entries" in err.lower() or not err:
            return " ".join(argv), [], []
        raise LogError(f"{name}: journalctl failed: {err[-500:]}")
    return " ".join(shlex.quote(a) for a in argv), texts, levels


DMESG_LEVEL = {"emerg": "error", "alert": "error", "crit": "error", "err": "error", "warn": "warning"}


def _openshift_cluster(name: str) -> Optional[Cluster]:
    """The OpenShift cluster whose node this VM is, if any"""
    from app.database import SessionLocal
    from app.models import ClusterNode
    with SessionLocal() as db:
        node = db.query(ClusterNode).filter(ClusterNode.name == name).first()
        cluster = db.get(Cluster, node.cluster_id) if node else None
        if cluster is None or cluster.type != "openshift":
            return None
        db.expunge(cluster)
        return cluster


def _node_logs(cluster: Cluster, name: str, source: str, lines: int, unit: Optional[List[str]],
               since: Optional[str], until: Optional[str], boot: Optional[str], grep: Optional[str],
               path: Optional[str]) -> Dict[str, Any]:
    """OpenShift node (RHCOS: qemu-ga forbids guest-exec): `oc adm node-logs` through the API server. No SSH, no
    route to the node, no debug image (works on disconnected clusters)."""
    nodes = _kubectl(cluster, ["get", "nodes", "-o", "name"], timeout=60)
    names = [n.split("/", 1)[-1] for n in nodes["stdout"].split()]
    node = next((n for n in names if n == name or n.startswith(name + ".")), None)
    if node is None:
        raise LogError(f"{name}: not a node of {cluster.name} in the API (nodes: {', '.join(names) or 'none'})")
    args = ["adm", "node-logs", node]
    if source == "cloud-init":
        raise LogError(f"{name}: OpenShift nodes (RHCOS) are provisioned by Ignition, not cloud-init: see the "
                       f"Journal with units ignition-firstboot-complete, machine-config-daemon-firstboot")
    if source == "file":
        if not path or not path.startswith("/var/log/") or ".." in path:
            raise LogError(f"{name}: on an OpenShift node only files under /var/log/ can be read "
                           f"(oc adm node-logs --path), e.g. /var/log/openvswitch/ovs-vswitchd.log")
        args += ["--path=" + _safe(path[len("/var/log/"):], "path")]
        out = _kubectl(cluster, args, timeout=120)
        if out["exitcode"] != 0:
            raise LogError(f"{name}: oc adm node-logs failed: {(out['stderr'] or '').strip()[-300:]}")
        texts = out["stdout"].splitlines()[-lines:]
        return {"source": source, "command": "oc " + " ".join(args), "lines": texts, "levels": []}
    kernel = source == "dmesg"
    # node-logs can't filter on the kernel transport: read more of the journal and keep the kernel's lines
    args += ["-o", "json", f"--tail={min(lines * 10, 50000) if kernel else lines}"]
    for u in ([] if kernel else unit or []):
        args.append("--unit=" + (_safe(u, "unit") or ""))
    if since:
        args.append("--since=" + (_safe(since, "since", dash=True) or ""))
    if until:
        args.append("--until=" + (_safe(until, "until", dash=True) or ""))
    if boot not in (None, "", "all"):
        if not re.match(r"^-?\d{1,3}$", str(boot)):
            raise LogError("boot: 0 = current boot, -1 = previous one, … or 'all'")
        args.append(f"--boot={boot}")
    if grep:
        if "\n" in grep or len(grep) > 200:
            raise LogError("grep: one line, at most 200 characters")
        args += ["--grep=" + grep, "--case-sensitive=false"]
    out = _kubectl(cluster, args, timeout=180)
    texts: List[str] = []
    levels: List[Optional[str]] = []
    for raw in out["stdout"].splitlines():
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if kernel and rec.get("_TRANSPORT") != "kernel" and rec.get("SYSLOG_IDENTIFIER") != "kernel":
            continue
        text, lv = _journal_line(rec)
        texts.append(text)
        levels.append(lv)
    if out["exitcode"] != 0 and not texts:
        err = (out["stderr"] or out["stdout"]).strip()
        if err and "no entries" not in err.lower():
            raise LogError(f"{name}: oc adm node-logs failed: {err[-500:]}")
    return {"source": source, "command": "oc " + " ".join(shlex.quote(a) for a in args),
            "lines": texts[-lines:], "levels": levels[-lines:],
            "notes": [f"{name} is an OpenShift node: read with oc adm node-logs (its guest agent doesn't run "
                      f"commands)"]}


def vm_logs(name: str, source: str = "journal", lines: int = DEFAULT_LINES, unit: Optional[List[str]] = None,
            priority: Optional[str] = None, since: Optional[str] = None, until: Optional[str] = None,
            boot: Optional[str] = None, grep: Optional[str] = None, path: Optional[str] = None) -> Dict[str, Any]:
    """Fetch raw lines from a VM: {source, command, lines[], levels[]}. Through the guest agent, or for an
    OpenShift node whose agent forbids commands (RHCOS), `oc adm node-logs`."""
    lines = max(1, min(int(lines or DEFAULT_LINES), MAX_LINES))
    try:
        return _agent_logs(name, source, lines, unit, priority, since, until, boot, grep, path)
    except LogError as e:
        if "forbids running commands" not in str(e):
            raise
        cluster = _openshift_cluster(name)
        if cluster is None:
            raise
        return _node_logs(cluster, name, source, lines, unit, since, until, boot, grep, path)


def _agent_logs(name: str, source: str, lines: int, unit: Optional[List[str]], priority: Optional[str],
                since: Optional[str], until: Optional[str], boot: Optional[str], grep: Optional[str],
                path: Optional[str]) -> Dict[str, Any]:
    if source == "journal":
        cmd, texts, levels = _journal(name, lines, unit, priority, since, until, boot, grep)
    elif source == "dmesg":
        if boot not in (None, "", "0") or since or until:  # older boots / time window: only the journal has them
            cmd, texts, levels = _journal(name, lines, None, priority, since, until, boot or "0", grep, kernel=True)
        else:
            out = _exec(name, ["/usr/bin/env", "dmesg", "-T", "-x"], timeout=30)
            if out["exitcode"] != 0:
                cmd, texts, levels = _journal(name, lines, None, priority, None, None, "0", grep, kernel=True)
            else:
                cmd = "dmesg -T -x"
                texts, levels = [], []
                for raw in out["stdout"].splitlines()[-lines:]:
                    m = re.match(r"^(\w+)\s*:(\w+)\s*:\s?(.*)$", raw)
                    if m:
                        texts.append(f"{m.group(3)}" if m.group(1) == "kern" else f"{m.group(1)}: {m.group(3)}")
                        levels.append(DMESG_LEVEL.get(m.group(2), "info"))
                    else:
                        texts.append(raw)
                        levels.append(None)
                if grep:
                    rx = _compile(grep, "grep")
                    keep = [i for i, t in enumerate(texts) if rx and rx.search(t)]
                    texts, levels = [texts[i] for i in keep], [levels[i] for i in keep]
    elif source == "cloud-init":
        script = (
            "echo '== cloud-init status --long'; cloud-init status --long 2>&1 || true; "
            "for f in /var/log/cloud-init-output.log /var/log/cloud-init.log; do "
            f"if [ -r \"$f\" ]; then echo \"== $f (last {lines} lines)\"; tail -n {lines} \"$f\"; fi; done")
        out = _sh(name, script, timeout=60)
        cmd = "cloud-init status --long; tail /var/log/cloud-init-output.log /var/log/cloud-init.log"
        texts, levels = out["stdout"].splitlines(), []
        if not texts:
            raise LogError(f"{name}: no cloud-init output (not a cloud-init VM?) {out['stderr'][-300:]}")
    elif source == "file":
        if not path or not path.startswith("/") or "\0" in path or "\n" in path or len(path) > 500:
            raise LogError("path: an absolute file path inside the VM, e.g. /var/log/messages")
        out = _exec(name, ["/usr/bin/env", "tail", "-n", str(lines), "--", path], timeout=60)
        if out["exitcode"] != 0:
            raise LogError(f"{name}: can't read {path}: {(out['stderr'] or '').strip()[-300:]}")
        cmd = f"tail -n {lines} -- {shlex.quote(path)}"
        texts, levels = out["stdout"].splitlines(), []
    else:
        raise LogError(f"source must be one of {', '.join(VM_SOURCES)}")
    return {"source": source, "command": cmd, "lines": texts, "levels": levels}


# ----------------------------------------------------------------------------------------------- pods

def _kubectl(cluster: Cluster, args: List[str], timeout: float = 90) -> Dict[str, Any]:
    try:
        return run_kubectl(cluster, args, None, timeout)
    except KubeExecError as e:
        raise LogError(str(e))


def _short(msg: Optional[str]) -> str:
    """Termination messages can be whole logs: keep the last non-empty line (the error), 300 chars"""
    lines = [l for l in (msg or "").strip().splitlines() if l.strip()]
    last = lines[-1].strip() if lines else ""
    return last if len(last) <= 300 else last[:300] + "…"


def _container_problems(st: Dict[str, Any]) -> List[str]:
    out = []
    state, last = st.get("state") or {}, st.get("lastState") or {}
    if "waiting" in state and state["waiting"].get("reason") not in (None, "ContainerCreating", "PodInitializing"):
        w = state["waiting"]
        out.append(f"waiting: {w.get('reason')}: {_short(w.get('message'))}".rstrip(": "))
    if "terminated" in state and state["terminated"].get("exitCode", 0) != 0:
        t = state["terminated"]
        out.append(f"terminated: {t.get('reason')} (exit code {t.get('exitCode')}) {_short(t.get('message'))}".rstrip())
    if "terminated" in last and (last["terminated"].get("exitCode", 0) != 0 or
                                 last["terminated"].get("reason") == "OOMKilled"):
        t = last["terminated"]
        out.append(f"last terminated: {t.get('reason')} (exit code {t.get('exitCode')}) at {t.get('finishedAt')}")
    return out


def _recent(stamp: Optional[str], minutes: int = 60) -> bool:
    if not stamp:
        return False
    try:
        when = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - when).total_seconds() < minutes * 60


def list_pods(cluster: Cluster, match: Optional[str] = None, namespace: Optional[str] = None,
              selector: Optional[str] = None, problems_only: bool = False) -> Dict[str, Any]:
    """Pods with their state; match = case-insensitive substring of 'namespace/name' (e.g. 'sriov')"""
    args = ["get", "pods", "-o", "json"]
    if namespace:
        if not K8S_NAME_RE.match(namespace):
            raise LogError("namespace: invalid name")
        args += ["-n", namespace]
    else:
        args.append("-A")
    if selector:
        if not SELECTOR_RE.match(selector):
            raise LogError("selector: invalid label selector")
        args += ["-l", selector]
    out = _kubectl(cluster, args, timeout=120)
    if out["exitcode"] != 0:
        raise LogError(f"{out['command']}: {(out['stderr'] or out['stdout']).strip()[-500:]}")
    try:
        items = json.loads(out["stdout"]).get("items", [])
    except ValueError:
        raise LogError("kubectl get pods returned no JSON")
    pods = []
    needle = (match or "").lower()
    for p in items:
        meta, spec, status = p.get("metadata", {}), p.get("spec", {}), p.get("status", {})
        full = f"{meta.get('namespace')}/{meta.get('name')}"
        if needle and needle not in full.lower():
            continue
        statuses = {s["name"]: s for s in (status.get("initContainerStatuses") or []) +
                    (status.get("containerStatuses") or [])}
        containers, problems = [], []
        for kind, lst in (("init", spec.get("initContainers") or []), ("main", spec.get("containers") or [])):
            for c in lst:
                st = statuses.get(c["name"], {})
                cp = _container_problems(st)
                problems += [f"{c['name']}: {x}" for x in cp]
                containers.append({"name": c["name"], "init": kind == "init", "ready": bool(st.get("ready")),
                                   "restarts": st.get("restartCount", 0), "image": c.get("image"),
                                   "problems": cp})
        main = [c for c in containers if not c["init"]]
        if status.get("phase") == "Pending" and not problems:
            for cond in status.get("conditions") or []:
                if cond.get("status") == "False" and cond.get("reason"):
                    problems.append(f"{cond.get('type')}: {cond.get('reason')}: {_short(cond.get('message'))}")
        if status.get("reason"):
            problems.append(f"pod: {status.get('reason')}: {_short(status.get('message'))}")
        unhealthy = status.get("phase") not in ("Running", "Succeeded") or any(
            c["problems"] and not c["problems"][0].startswith("last ") for c in containers) or any(
            not c["ready"] for c in main if status.get("phase") == "Running") or any(
            _recent(st.get("lastState", {}).get("terminated", {}).get("finishedAt")) for st in statuses.values())
        if problems_only and not unhealthy:
            continue
        pods.append({"namespace": meta.get("namespace"), "name": meta.get("name"), "phase": status.get("phase"),
                     "node": spec.get("nodeName"), "ready": f"{sum(c['ready'] for c in main)}/{len(main)}",
                     "restarts": sum(c["restarts"] for c in containers), "containers": containers,
                     "problems": problems, "unhealthy": unhealthy,
                     "created": meta.get("creationTimestamp")})
    pods.sort(key=lambda p: (p["namespace"], p["name"]))
    return {"command": out["command"], "count": len(pods), "pods": pods[:500], "truncated": len(pods) > 500}


def pod_logs(cluster: Cluster, namespace: Optional[str] = None, pod: Optional[str] = None,
             selector: Optional[str] = None, match: Optional[str] = None, container: Optional[str] = None,
             previous: bool = False, tail: int = 500, since: Optional[str] = None,
             events: bool = True) -> Dict[str, Any]:
    """Logs of one pod, or of every pod matching a selector / name substring (up to MAX_PODS), one block per
    container prefixed with [namespace/pod/container]; plus status lines (waiting reasons, OOMKilled) and the pods'
    events, since ImagePullBackOff / FailedScheduling never appear in a container log."""
    tail = max(1, min(int(tail or 500), MAX_LINES))
    if since and not (DURATION_RE.match(since) and since):
        raise LogError("since: a duration like 30s, 15m, 2h or 1h30m")
    if container and not K8S_NAME_RE.match(container):
        raise LogError("container: invalid name")
    if pod:
        if not namespace:
            raise LogError("pod needs its namespace")
        if not K8S_NAME_RE.match(pod):
            raise LogError("pod: invalid name")
        found = list_pods(cluster, namespace=namespace)["pods"]
        pods = [p for p in found if p["name"] == pod]
        if not pods:
            raise LogError(f"No pod {namespace}/{pod}")
    elif selector or match or namespace:
        pods = list_pods(cluster, match=match, namespace=namespace, selector=selector)["pods"]
        if not pods:
            raise LogError("No pod matches" + (f" '{match}'" if match else "") +
                           (f" in namespace {namespace}" if namespace else "") +
                           (f" with selector {selector}" if selector else ""))
    else:
        raise LogError("Give a pod (+ namespace), a label selector, a namespace or a name substring (match)")
    skipped = max(0, len(pods) - MAX_PODS)
    pods = pods[:MAX_PODS]

    jobs: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    for p in pods:
        for c in p["containers"]:
            if container and c["name"] != container:
                continue
            if previous and not c["restarts"] and "last terminated" not in " ".join(c["problems"]):
                continue
            jobs.append((p, c))

    def fetch(job: Tuple[Dict[str, Any], Dict[str, Any]]) -> Tuple[str, Dict[str, Any]]:
        p, c = job
        args = ["logs", "-n", p["namespace"], p["name"], "-c", c["name"], "--timestamps", f"--tail={tail}"]
        if previous:
            args.append("--previous")
        if since:
            args.append(f"--since={since}")
        return f"{p['namespace']}/{p['name']}/{c['name']}", _kubectl(cluster, args, timeout=90)

    lines: List[str] = []
    notes: List[str] = []
    is_openshift = cluster.type == "openshift"
    with ThreadPoolExecutor(max_workers=6 if is_openshift else 3) as pool:
        results = list(pool.map(fetch, jobs))
    by_key = dict(results)
    if events:
        lines += _events(cluster, pods)
    for p in pods:
        for c in p["containers"]:
            key = f"{p['namespace']}/{p['name']}/{c['name']}"
            for prob in c["problems"]:
                lines.append(f"[{key}] status {prob}")
            out = by_key.get(key)
            if out is None:
                continue
            if out["exitcode"] != 0:
                err = (out["stderr"] or out["stdout"]).strip().splitlines()
                notes.append(f"{key}: {err[-1] if err else 'kubectl logs failed'}")
                continue
            lines += [f"[{key}] {t}" for t in out["stdout"].splitlines()]
    if skipped:
        notes.append(f"{skipped} more pods matched: narrow the match / namespace (at most {MAX_PODS} pods)")
    tool = "oc" if is_openshift else "kubectl"
    return {"source": "pods", "command": f"{tool} logs --timestamps --tail={tail}" + (" --previous" if previous else "")
            + (f" --since={since}" if since else "") + f" ({len(jobs)} containers)",
            "pods": [{k: p[k] for k in ("namespace", "name", "phase", "ready", "restarts", "node", "problems")}
                     for p in pods],
            "lines": lines, "levels": [], "notes": notes}


def _events(cluster: Cluster, pods: List[Dict[str, Any]]) -> List[str]:
    namespaces = sorted({p["namespace"] for p in pods})
    if len(namespaces) > 5:
        return []
    names = {(p["namespace"], p["name"]) for p in pods}
    lines = []
    for ns in namespaces:
        out = _kubectl(cluster, ["get", "events", "-n", ns, "-o", "json"], timeout=60)
        if out["exitcode"] != 0:
            continue
        try:
            items = json.loads(out["stdout"]).get("items", [])
        except ValueError:
            continue
        for ev in items:
            obj = ev.get("involvedObject") or {}
            if (ns, obj.get("name")) not in names:
                continue
            when = ev.get("lastTimestamp") or ev.get("eventTime") or (ev.get("metadata") or {}).get("creationTimestamp")
            count = ev.get("count") or 1
            lines.append(f"[{ns}/{obj.get('name')}] {when} event {ev.get('type')} {ev.get('reason')}: "
                         f"{(ev.get('message') or '').strip()}" + (f" (x{count})" if count > 1 else ""))
    lines.sort(key=lambda t: t.split("] ", 1)[1] if "] " in t else t)
    return lines


# ----------------------------------------------------------------------------------------------- diagnostics

DIAG_SCRIPT = r"""
export LC_ALL=C PATH=/usr/sbin:/usr/bin:/sbin:/bin
s() { echo "@@@VMM $1"; }
run() { "$@" 2>&1; echo "@@@RC $?"; }
s os-release; cat /etc/os-release 2>/dev/null
s uname; uname -srvmo
s uptime; cat /proc/uptime
s cmdline; cat /proc/cmdline
s cpu; grep -m1 'model name' /proc/cpuinfo; nproc; grep -m1 -o -w -E 'vmx|svm' /proc/cpuinfo
s meminfo; grep -E '^(MemTotal|MemAvailable|SwapTotal|SwapFree):' /proc/meminfo
s dmi-bios; run dmidecode -t bios
s dmi-system; run dmidecode -t system
s dmi-sysfs; for f in bios_vendor bios_version bios_date bios_release sys_vendor product_name product_version product_family board_vendor board_name; do [ -r /sys/class/dmi/id/$f ] && echo "$f: $(cat /sys/class/dmi/id/$f)"; done
s lspci; run lspci -nnk
s ip; run ip -j addr
s iplink; run ip -j -d link
s ethtool; for d in /sys/class/net/*; do n=${d##*/}; [ "$n" = lo ] && continue; [ -e "$d/device" ] || continue; echo "### $n"; ethtool -i "$n" 2>&1; done; command -v ethtool >/dev/null || echo "@@@RC 127"
s lsblk; run lsblk -J -o NAME,SIZE,TYPE,MOUNTPOINT,FSTYPE,MODEL,SERIAL,ROTA
s df; run df -hPT -x tmpfs -x devtmpfs -x overlay
s sriov; for f in /sys/class/net/*/device/sriov_totalvfs; do [ -r "$f" ] || continue; d=${f%/sriov_totalvfs}; n=${d%/device}; n=${n##*/}; echo "$n $(cat $f) $(cat $d/sriov_numvfs) $(readlink -f $d | sed 's#.*/##') $(basename $(readlink -f $d/driver) 2>/dev/null)"; done
s iommu; ls /sys/kernel/iommu_groups 2>/dev/null | wc -l
s vfio; for d in /sys/bus/pci/drivers/vfio-pci/0*; do [ -e "$d" ] && echo "${d##*/}"; done
s failed-units; run systemctl --failed --no-legend --plain
s selinux; getenforce 2>/dev/null || echo n/a
s journal-errors; journalctl -p err -b --no-pager -n 30 -o short-iso 2>&1
"""

SECTION_RE = re.compile(r"^@@@VMM (\S+)$")


def _sections(text: str) -> Dict[str, Tuple[str, Optional[int]]]:
    out: Dict[str, Tuple[str, Optional[int]]] = {}
    cur, buf, rc = None, [], None  # type: Optional[str], List[str], Optional[int]
    for line in text.splitlines():
        m = SECTION_RE.match(line)
        if m:
            if cur:
                out[cur] = ("\n".join(buf), rc)
            cur, buf, rc = m.group(1), [], None
            continue
        if line.startswith("@@@RC "):
            try:
                rc = int(line[6:])
            except ValueError:
                pass
            continue
        buf.append(line)
    if cur:
        out[cur] = ("\n".join(buf), rc)
    return out


def _kv(text: str) -> Dict[str, str]:
    d = {}
    for line in text.splitlines():
        if ":" in line and line.startswith("\t") and not line.startswith("\t\t"):
            k, v = line.strip().split(":", 1)
            if v.strip():
                d[k.strip()] = v.strip()
    return d


def _lspci(text: str) -> List[Dict[str, Any]]:
    devices: List[Dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        if not line.startswith(("\t", " ")):
            m = re.match(r"^(\S+) (.+?) \[([0-9a-f]{4})\]: (.*)$", line)
            if m:
                devices.append({"slot": m.group(1), "class": m.group(2), "device": m.group(4)})
            else:
                devices.append({"slot": line.split(" ", 1)[0], "device": line})
        elif devices:
            k, _, v = line.strip().partition(":")
            if k == "Kernel driver in use":
                devices[-1]["driver"] = v.strip()
            elif k == "Kernel modules":
                devices[-1]["modules"] = v.strip()
            elif k == "Subsystem":
                devices[-1]["subsystem"] = v.strip()
    return devices


def _json(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None


def _agent_info(name: str) -> Dict[str, Any]:
    """The agent's own info commands: allowed even where guest-exec is forbidden (EL defaults)"""
    info: Dict[str, Any] = {}
    for key, cmd in (("osinfo", "guest-get-osinfo"), ("hostname", "guest-get-host-name"),
                     ("timezone", "guest-get-timezone"), ("interfaces", "guest-network-get-interfaces"),
                     ("fsinfo", "guest-get-fsinfo"), ("disks", "guest-get-disks"), ("users", "guest-get-users")):
        try:
            info[key] = libvirt_client.agent_command(name, cmd, timeout=10)
        except libvirt.libvirtError as e:
            if key == "osinfo":
                raise agent_error(name, e)
            info.setdefault("unavailable", []).append(cmd)
    return info


def _openshift_node_info(cluster: Cluster, name: str) -> Optional[Dict[str, Any]]:
    """nodeInfo / capacity / conditions of an OpenShift node from the API (RHCOS: no guest-exec)"""
    try:
        out = _kubectl(cluster, ["get", "nodes", "-o", "json"], timeout=60)
        items = json.loads(out["stdout"]).get("items", [])
    except (LogError, ValueError):
        return None
    item = next((i for i in items if i["metadata"]["name"] in (name,) or i["metadata"]["name"].startswith(name + ".")),
                None)
    if item is None:
        return None
    st = item.get("status", {})
    info = st.get("nodeInfo", {})
    findings = []
    for c in st.get("conditions", []):
        bad = c.get("status") != ("True" if c.get("type") == "Ready" else "False")
        if bad:
            findings.append({"level": "error" if c.get("type") == "Ready" else "warning",
                             "text": f"node condition {c.get('type')}={c.get('status')}: {c.get('message', '')}"})
    return {
        "name": item["metadata"]["name"],
        "roles": sorted(k.split("/", 1)[1] for k in item["metadata"].get("labels", {})
                        if k.startswith("node-role.kubernetes.io/")),
        "os_image": info.get("osImage"), "kernel": info.get("kernelVersion"),
        "kubelet": info.get("kubeletVersion"), "container_runtime": info.get("containerRuntimeVersion"),
        "architecture": info.get("architecture"),
        "capacity": st.get("capacity", {}), "allocatable": st.get("allocatable", {}),
        "conditions": [{"type": c.get("type"), "status": c.get("status"), "reason": c.get("reason")}
                       for c in st.get("conditions", [])],
        "findings": findings,
    }


def diagnostics(name: str) -> Dict[str, Any]:
    agent = _agent_info(name)
    result: Dict[str, Any] = {"vm": name, "agent": agent, "exec": True, "missing_tools": [], "findings": []}
    findings: List[Dict[str, str]] = result["findings"]
    try:
        out = _sh(name, DIAG_SCRIPT, timeout=120)
    except LogError as e:
        result["exec"] = False
        result["exec_error"] = str(e)
        osi = agent.get("osinfo") or {}
        result["os"] = {"pretty_name": osi.get("pretty-name"), "kernel": osi.get("kernel-release"),
                        "kernel_version": osi.get("kernel-version"), "machine": osi.get("machine")}
        result["nics"] = [{"name": i.get("name"), "mac": i.get("hardware-address"), "addresses": [
            f"{a.get('ip-address')}/{a.get('prefix')}" for a in i.get("ip-addresses") or []]}
            for i in agent.get("interfaces") or [] if i.get("name") != "lo"]
        result["filesystems"] = [f"{f.get('mountpoint')} {f.get('type')} "
                                 f"{(f.get('used-bytes') or 0) // 2**20} MiB used of {(f.get('total-bytes') or 0) // 2**20} MiB"
                                 for f in agent.get("fsinfo") or []]
        cluster = _openshift_cluster(name) if "forbids running commands" in str(e) else None
        node = _openshift_node_info(cluster, name) if cluster else None
        if node:
            result["node"] = node
            findings.extend(node.pop("findings"))
            findings.append({"level": "info", "text": "OpenShift node (RHCOS): its guest agent doesn't run commands; "
                                                      "node details come from the API, logs from oc adm node-logs."})
        else:
            findings.append({"level": "warning", "text": "Only the guest agent's own information is available: "
                                                         "guest-exec is forbidden or failed (see exec_error)."})
        return result
    sec = _sections(out["stdout"])
    # the structured sections below cover interfaces / file systems / disks: keep the agent's identity only
    result["agent"] = {k: agent[k] for k in ("osinfo", "hostname", "timezone", "users") if k in agent}

    def body(key: str) -> str:
        return sec.get(key, ("", None))[0]

    for key, tool in (("dmi-bios", "dmidecode"), ("lspci", "lspci"), ("ip", "ip"), ("lsblk", "lsblk"),
                      ("ethtool", "ethtool"), ("failed-units", "systemctl")):
        if sec.get(key, ("", None))[1] == 127:
            result["missing_tools"].append(tool)
    osr = dict(re.findall(r'^(\w+)="?([^"\n]*)"?$', body("os-release"), re.M))
    result["os"] = {"pretty_name": osr.get("PRETTY_NAME"), "id": osr.get("ID"), "version": osr.get("VERSION_ID"),
                    "kernel": body("uname").strip(), "cmdline": body("cmdline").strip(),
                    "selinux": body("selinux").strip() or None}
    try:
        result["os"]["uptime_s"] = int(float(body("uptime").split()[0]))
    except (IndexError, ValueError):
        pass
    cpu = body("cpu").splitlines()
    mem = dict(re.findall(r"^(\w+):\s+(\d+) kB", body("meminfo"), re.M))
    result["hardware"] = {
        "cpu_model": cpu[0].split(":", 1)[1].strip() if cpu and ":" in cpu[0] else None,
        "cpus": int(cpu[1]) if len(cpu) > 1 and cpu[1].isdigit() else None,
        "virtualization": (cpu[2] if len(cpu) > 2 else None),
        "memory_mib": int(mem["MemTotal"]) // 1024 if "MemTotal" in mem else None,
        "memory_available_mib": int(mem["MemAvailable"]) // 1024 if "MemAvailable" in mem else None,
        "swap_mib": int(mem["SwapTotal"]) // 1024 if "SwapTotal" in mem else None,
    }
    result["bios"] = _kv(body("dmi-bios")) if sec.get("dmi-bios", ("", 1))[1] == 0 else {}
    result["system"] = _kv(body("dmi-system")) if sec.get("dmi-system", ("", 1))[1] == 0 else {}
    if not result["bios"] or not result["system"]:  # no dmidecode: the kernel's copy of the DMI tables
        dmi = dict(l.split(": ", 1) for l in body("dmi-sysfs").splitlines() if ": " in l)
        result["bios"] = result["bios"] or {k: v for k, v in (
            ("Vendor", dmi.get("bios_vendor")), ("Version", dmi.get("bios_version")),
            ("Release Date", dmi.get("bios_date")), ("BIOS Revision", dmi.get("bios_release"))) if v}
        result["system"] = result["system"] or {k: v for k, v in (
            ("Manufacturer", dmi.get("sys_vendor")), ("Product Name", dmi.get("product_name")),
            ("Version", dmi.get("product_version")), ("Family", dmi.get("product_family")),
            ("Board", " ".join(x for x in (dmi.get("board_vendor"), dmi.get("board_name")) if x))) if v}
    result["pci"] = _lspci(body("lspci")) if sec.get("lspci", ("", 1))[1] == 0 else []

    # NICs: ip addr + driver / firmware from ethtool -i + VFs from ip -d link
    ethtool: Dict[str, Dict[str, str]] = {}
    cur = None
    for line in body("ethtool").splitlines():
        if line.startswith("### "):
            cur = line[4:].strip()
            ethtool[cur] = {}
        elif cur and ":" in line:
            k, v = line.split(":", 1)
            ethtool[cur][k.strip()] = v.strip()
    links = {l.get("ifname"): l for l in (_json(body("iplink")) or [])}
    nics = []
    for a in _json(body("ip")) or []:
        n = a.get("ifname")
        if n == "lo":
            continue
        et = ethtool.get(n, {})
        link = links.get(n, {})
        nic = {"name": n, "mac": a.get("address"), "state": a.get("operstate"), "mtu": a.get("mtu"),
               "addresses": [f"{i.get('local')}/{i.get('prefixlen')}" for i in a.get("addr_info", [])
                             if i.get("scope") != "link"],
               "driver": et.get("driver"), "driver_version": et.get("version"),
               "firmware_version": et.get("firmware-version"), "bus_info": et.get("bus-info"),
               "kind": (link.get("linkinfo") or {}).get("info_kind")}
        vfs = link.get("vfinfo_list")
        if vfs:
            nic["vfs"] = [{"vf": v.get("vf"), "mac": v.get("address"), "vlan": (v.get("vlan_list") or [{}])[0].get("vlan"),
                           "trust": v.get("trust"), "spoofchk": v.get("spoofchk"), "link_state": v.get("link_state")}
                          for v in vfs]
        if a.get("operstate") == "DOWN" and "UP" in (a.get("flags") or []):
            findings.append({"level": "warning", "text": f"{n} is administratively up but its link is down"})
        nics.append(nic)
    result["nics"] = nics
    blk = _json(body("lsblk")) or {}
    result["disks"] = blk.get("blockdevices", [])
    result["filesystems"] = body("df").splitlines()
    for line in result["filesystems"][1:]:
        parts = line.split()
        if len(parts) >= 7 and parts[5].rstrip("%").isdigit() and int(parts[5].rstrip("%")) >= 90:
            findings.append({"level": "error" if int(parts[5].rstrip("%")) >= 98 else "warning",
                             "text": f"{parts[6]} is {parts[5]} full"})
    sriov = []
    for line in body("sriov").splitlines():
        p = line.split()
        if len(p) >= 4:
            sriov.append({"pf": p[0], "total_vfs": int(p[1]) if p[1].isdigit() else None,
                          "num_vfs": int(p[2]) if p[2].isdigit() else None, "pci": p[3],
                          "driver": p[4] if len(p) > 4 else None})
    result["sriov"] = {"pfs": sriov, "iommu_groups": int(body("iommu").strip() or 0)
                       if body("iommu").strip().isdigit() else 0,
                       "vfio_devices": body("vfio").split()}
    if sriov and not result["sriov"]["iommu_groups"]:
        findings.append({"level": "warning", "text": "SR-IOV capable NICs but no IOMMU groups: VFs can't be passed "
                                                     "through (intel_iommu=on, vIOMMU on the VM)"})
    failed = [l.split()[0] for l in body("failed-units").splitlines() if l.strip()]
    result["failed_units"] = failed
    for u in failed:
        findings.append({"level": "error", "text": f"systemd unit {u} failed: journal source with unit={u}"})
    errs = [l for l in body("journal-errors").splitlines() if l.strip() and not l.startswith("-- ")]
    result["recent_errors"] = errs
    if errs:
        summary = analyze(errs)["summary"]
        for s in summary:
            findings.append({"level": "warning", "text": f"{s['label']}: {s['count']} recent error line(s). "
                                                         f"{s['hint']}"})
    if result["missing_tools"]:
        findings.append({"level": "info", "text": "Not installed in the guest: " + ", ".join(result["missing_tools"])})
    return result
