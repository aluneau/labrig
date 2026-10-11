"""Run kubectl (oc for OpenShift) against a cluster: one path for the MCP kubectl tool and the log viewer.

kubeadm / k3s: on the first running control plane through the QEMU guest agent; OpenShift: the app's oc on the host.
run_oc: the MCP oc tool (OpenShift support workflows, non-interactive; must-gather / inspect output as a tarball).
"""
import re
import shlex
import shutil
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import libvirt

from app.libvirt_client import libvirt_client
from app.models import Cluster


class KubeExecError(Exception):
    pass


def run_kubectl(cluster: Cluster, args: List[str], stdin: Optional[str] = None,
                timeout: float = 120) -> Dict[str, Any]:
    """{command, node, exitcode, stdout, stderr} (raises KubeExecError when it can't run at all)"""
    from app.services.cluster_drivers import get_driver
    from app.services.cluster_service import cluster_service
    if cluster.type == "openshift":
        from app.services.openshift_service import openshift_service
        try:
            out = openshift_service.oc(cluster.name, cluster.version, args, timeout, stdin=stdin)
        except ValueError as e:
            raise KubeExecError(str(e))
        return {"command": "oc " + shlex.join(args), "node": "(vm-manager host)", **out}
    node = cluster_service._api_node(cluster)
    if node is None:
        raise KubeExecError("The cluster has no control plane")
    argv = get_driver(cluster.type).kubectl_command(args)
    try:
        out = libvirt_client.agent_exec(node.name, argv[0], argv[1:],
                                        stdin.encode() if stdin is not None else None, timeout=timeout)
    except libvirt.libvirtError as e:
        raise KubeExecError(f"{node.name}: guest agent: {e.get_error_message() or e} (is the control plane "
                            f"running? its agent answers ~30-60 s after boot)")
    except TimeoutError as e:
        raise KubeExecError(str(e))
    return {"command": "kubectl " + shlex.join(args), "node": node.name, **out}


# ------------------------------------------------------------------------------------------- oc (OpenShift)
# The MCP `oc` tool: support workflows as at a customer (get / describe / logs / exec / adm inspect / must-gather /
# debug node), non-interactive. Files written by oc (must-gather, inspect, cp, rsync) land in the cluster's install
# dir under oc-output/, downloadable through GET /clusters/{id}/oc-output/{name}.

OUTPUT_DIR = "oc-output"
STDOUT_CAP = 100_000
STDERR_CAP = 20_000
GATHER_STDOUT_CAP = 20_000      # must-gather prints every file it copies: the tail is what matters
GATHER_TIMEOUT = 60 * 60        # must-gather's own --timeout (gathering) defaults to 10 min
GATHERS = ("must-gather", "inspect")
# flags that take a value as the next word (to find positional arguments)
VALUE_FLAGS = {
    "-n", "--namespace", "-c", "--container", "-l", "--selector", "-o", "--output", "--as", "--as-group",
    "--context", "--kubeconfig", "--server", "-s", "--token", "--cluster", "--user", "-f", "--filename",
    "--shell", "--timeout", "--image", "--image-stream", "--dest-dir", "--to-namespace", "--node-name",
    "--run-namespace", "--since", "--since-time", "--tail", "--source-dir", "--node-selector", "--field-selector",
    "--sort-by", "--template", "--request-timeout", "--patch", "--type", "--for", "--volume-percentage",
    "--log-level", "-v", "--loglevel", "--max-log-requests", "--pod-running-timeout",
}
INTERACTIVE_VERBS = {"exec", "rsh", "debug", "run", "attach"}


class OcRefused(KubeExecError):
    """A command the tool won't run (interactive, never-ending): the message says what to run instead"""


def _split_flags(argv: List[str]) -> Tuple[List[str], List[str], List[str]]:
    """(flags with their values, positionals, words after `--`)"""
    flags: List[str] = []
    pos: List[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--":
            return flags, pos, argv[i + 1:]
        if a.startswith("-") and len(a) > 1:
            flags.append(a)
            if "=" not in a and a in VALUE_FLAGS and i + 1 < len(argv):
                flags.append(argv[i + 1])
                i += 1
        else:
            pos.append(a)
        i += 1
    return flags, pos, []


def _has_flag(flags: List[str], *names: str) -> bool:
    return any(f.split("=", 1)[0] in names for f in flags if f.startswith("-"))


def _flag_values(flags: List[str], name: str) -> List[str]:
    """Values of `--name=v` / `--name v` (flags as returned by _split_flags)"""
    values = []
    for i, f in enumerate(flags):
        key, eq, value = f.partition("=")
        if key == name:
            values.append(value if eq else (flags[i + 1] if i + 1 < len(flags) else ""))
    return values


def oc_gather_kind(argv: List[str]) -> Optional[str]:
    """'must-gather' / 'inspect' for `adm must-gather` / `adm inspect` (they write a directory)"""
    _, pos, _ = _split_flags(argv)
    if len(pos) >= 2 and pos[0] == "adm" and pos[1] in GATHERS:
        return pos[1]
    return None


def check_oc_args(argv: List[str]) -> None:
    """Refuse what needs a terminal or never ends, in plain words with the non-interactive form"""
    if not argv:
        raise OcRefused("No arguments: e.g. 'get clusterversion', 'get co', 'get nodes -o wide'")
    flags, pos, after = _split_flags(argv)
    verb = pos[0] if pos else ""
    if verb in ("login", "logout"):
        raise OcRefused("No login needed: the tool already runs as cluster admin (the installer's kubeconfig). "
                        "'whoami' / 'whoami --show-console' / 'whoami --show-server' show who and where; "
                        "'--as=<user>' runs a command as another user (e.g. 'auth can-i --list --as=developer').")
    if verb == "edit":
        raise OcRefused("'oc edit' opens an editor. Non-interactive forms: 'get <kind> <name> -o yaml', then "
                        "'apply -f -' with the changed YAML in stdin; or 'patch <kind> <name> --type merge -p "
                        "{\"spec\":{...}}'; or 'set env|image|resources ...', 'label', 'annotate', 'scale'.")
    if verb in ("attach", "port-forward", "proxy"):
        raise OcRefused(f"'oc {verb}' keeps a session open. Use 'logs <pod> --tail=200' for output, "
                        "'exec <pod> -- <command>' to run something, 'exec <pod> -- curl -s localhost:<port>/...' "
                        "to reach a port from inside, or a Route / the group's WireGuard to reach a service.")
    if verb in ("logs", "adm") and _has_flag(flags, "-f", "--follow"):
        raise OcRefused("'-f' (follow) never ends here: use 'logs <pod> --tail=200', '--since=10m', "
                        "'-c <container>', '--previous' (the crashed container), 'logs deploy/<name> "
                        "--all-containers', or 'adm node-logs <node> -u kubelet --tail=200'.")
    if _has_flag(flags, "-w", "--watch", "--watch-only"):
        raise OcRefused("'--watch' never ends here: run the 'get' again, or wait for a state with "
                        "'wait --for=condition=Ready pod/<name> --timeout=300s' (or '--for=jsonpath=...').")
    if verb in INTERACTIVE_VERBS:
        for f in flags:
            if f in ("--tty", "--tty=true") or re.fullmatch(r"-[iqT]*t[iqT]*", f):
                raise OcRefused("'-t' asks for a terminal, which the tool doesn't have. Run the command "
                                "directly: 'exec <pod> -- <command>', 'rsh <pod> <command>', "
                                "'debug node/<node> -- chroot /host <command>' (stdin is passed with -i).")
        if verb == "rsh" and len(pos) < 3 and not after:
            raise OcRefused("'rsh <pod>' alone opens a shell. Give the command: 'rsh <pod> cat /etc/os-release', "
                            "or 'exec <pod> -- sh -c \"<commands>\"' (-c <container> for another container).")
        if verb == "exec" and not after and len(pos) < 3:
            raise OcRefused("'exec' needs a command: 'exec <pod> -- <command>' (e.g. 'exec -n openshift-dns "
                            "<pod> -- cat /etc/resolv.conf').")
        if verb == "debug" and not after:
            raise OcRefused("'debug' without a command opens a shell. Give it after '--': "
                            "'debug node/<node> -- chroot /host <command>' (e.g. 'chroot /host journalctl -u "
                            "kubelet --since -10min --no-pager', 'chroot /host crictl ps'), or "
                            "'debug pod/<pod> -- <command>' (a copy of the pod).")
        if verb == "run" and _has_flag(flags, "-i", "--stdin", "--attach", "--rm"):
            raise OcRefused("'run -i / --attach / --rm' attaches to the pod. Create it with "
                            "'run <name> --image=<img> --restart=Never -- <command>', then 'logs <name>'.")
    kind = oc_gather_kind(argv)
    if kind and _has_flag(flags, "--dest-dir"):
        raise OcRefused(f"Leave out --dest-dir: the tool writes 'adm {kind}' output under the cluster's install "
                        "dir and returns a tarball (path + download link).")
    if kind == "inspect" and len(pos) < 3:
        raise OcRefused("'adm inspect' needs what to inspect: e.g. 'adm inspect clusteroperator/kube-apiserver', "
                        "'adm inspect ns/<namespace>', 'adm inspect co' (every cluster operator: minutes).")


def output_dir(cluster: Cluster) -> Path:
    from app.services.openshift_service import openshift_service
    path = openshift_service.cluster_dir(cluster.name) / OUTPUT_DIR
    path.mkdir(exist_ok=True)
    return path


def list_outputs(cluster: Cluster) -> List[Dict[str, Any]]:
    out = []
    for p in sorted(output_dir(cluster).iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        st = p.stat()
        out.append({"name": p.name, "directory": p.is_dir(), "size": st.st_size if p.is_file() else _du(p),
                    "modified": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()})
    return out


def output_path(cluster: Cluster, name: str) -> Path:
    if not name or "/" in name or name.startswith("."):
        raise LookupError("No such file")
    path = output_dir(cluster) / name
    if not path.exists():
        raise LookupError(f"No file {name} in the cluster's oc output")
    return path


def delete_output(cluster: Cluster, name: str) -> None:
    path = output_path(cluster, name)
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def _du(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file() and not f.is_symlink())


def _cap(text: str, limit: int, hint: str) -> str:
    """Keep the start and (mostly) the end of a big output, with a note in the middle"""
    if len(text) <= limit:
        return text
    head = limit // 4
    tail = limit - head
    cut = len(text) - head - tail
    return text[:head] + f"\n\n[... {cut} characters cut out of {len(text)}: {hint} ...]\n\n" + text[-tail:]


def _listing(root: Path, max_lines: int = 50) -> List[str]:
    """The tree's outline: directories 2 levels down, plus each namespace (must-gather: one directory per
    image, then cluster-scoped-resources / namespaces/<ns>), with sizes. Long image directory names are
    shown once and shortened to <image N>."""
    lines: List[str] = []
    base = len(root.parts)
    aliases = {}
    for d in sorted(p for p in root.iterdir() if p.is_dir() and len(p.name) > 40):
        aliases[d.name] = f"<image {len(aliases) + 1}>"
        lines.append(f"{aliases[d.name]} = {d.name}")
    dirs = [p for p in root.rglob("*") if p.is_dir()
            and (len(p.parts) - base <= 2 or (len(p.parts) - base <= 3 and p.parent.name == "namespaces"))]
    for d in sorted(dirs):
        if len(lines) >= max_lines:
            lines.append("... (more directories: see the tarball)")
            break
        rel = d.relative_to(root).parts
        lines.append("/".join((aliases.get(rel[0], rel[0]),) + rel[1:]) + f"/  {_du(d) // 1024} KiB")
    top = [f.name for f in root.iterdir() if f.is_file()]
    if top:
        lines.append("files at the top: " + ", ".join(sorted(top)[:20]))
    return lines


# -------------------------------------------------------------- disconnected clusters: is the image mirrored?

def _images_for(cluster: Cluster, argv: List[str], kind: str) -> List[str]:
    """Images a must-gather / debug node pod will pull: --image values, else the release's image stream"""
    from app.services.openshift_service import openshift_service
    flags, _, _ = _split_flags(argv)
    images = _flag_values(flags, "--image")
    streams = _flag_values(flags, "--image-stream")
    if not images and not streams:
        streams = ["openshift/must-gather" if kind == "must-gather" else "openshift/tools"]
    for stream in streams:
        ns, _, rest = stream.rpartition("/")
        name, _, tag = rest.partition(":")
        try:
            data = openshift_service.oc_json(cluster.name, cluster.version,
                                             ["get", "imagestream", name, "-n", ns or "openshift"], 30)
        except (RuntimeError, ValueError):
            continue
        found = [t["items"][0].get("dockerImageReference") for t in (data.get("status") or {}).get("tags") or []
                 if t.get("tag") == (tag or "latest") and t.get("items")]
        found = found or [(t.get("from") or {}).get("name") for t in (data.get("spec") or {}).get("tags") or []
                          if t.get("name") == (tag or "latest")]
        images += found
    return [i for i in images if i]


def mirror_problem(db: Any, cluster: Cluster, argv: List[str], kind: str) -> Tuple[Optional[str], Optional[str]]:
    """Disconnected OpenShift: (refusal, warning). The pod would sit in ImagePullBackOff if its image isn't in
    the group's mirror registry (only digest references go through the cluster's ImageDigestMirrorSets)."""
    from app.models import Group
    from app.services import registry_images
    from app.services.openshift_installer import openshift_installer
    from app.services.registry_client import RegistryError
    if not openshift_installer.options(cluster).disconnected:
        return None, None
    mirror = openshift_installer.load_mirror(cluster) or {}
    group = db.get(Group, cluster.group_id) if cluster.group_id else None
    registry = mirror.get("registry_host") or "the group's mirror registry"
    own = (mirror.get("registry_host"), mirror.get("uplink_registry_host"))
    what = "must-gather" if kind == "must-gather" else "'oc debug node'"
    copy = f"the group's Registry tab, or POST /api/v1/groups/{cluster.group_id}/registry/images"
    try:
        client = registry_images.client(group, timeout=20) if group is not None else None
    except (ValueError, RuntimeError) as e:
        return None, f"Could not check the mirror registry ({e}): ran anyway"
    for image in _images_for(cluster, argv, kind):
        host = image.split("/", 1)[0]
        if "@sha256:" not in image:
            if host in own:
                continue
            return (f"This cluster is disconnected: it pulls images only by digest, through its mirror registry "
                    f"({registry}). {what} would use {image}, a tag: the pod would never start "
                    f"(ImagePullBackOff). Pass a mirrored image by digest, e.g. "
                    f"--image={registry}/<repo>@sha256:<digest> (copy it in first: {copy})."), None
        repo, digest = image.split("@", 1)
        if host in own:
            repos = [repo.split("/", 1)[1]]
        else:
            repos = [m.split("/", 1)[1] + repo[len(s["source"]):]
                     for s in mirror.get("image_digest_sources") or []
                     if repo == s.get("source") or repo.startswith(s.get("source", "") + "/")
                     for m in (s.get("mirrors") or [])[:1] if "/" in m]
        if not repos:
            return (f"This cluster is disconnected and {what} needs {image}, which isn't mirrored: there is no "
                    f"mirror for {repo} (only the release and the operators mirrored at install / add-on time "
                    f"are). Copy it into {registry} ({copy} with source={image}), then "
                    f"pass --image={registry}/<repo>@{digest}."), None
        if client is None:
            continue
        try:
            client.digest(repos[0], digest)
        except RegistryError as e:
            if re.search(r": (401|403|404) ", str(e)) or "unauthorized" in str(e):
                return (f"This cluster is disconnected and {what} needs {image}, which is not in its mirror "
                        f"registry ({registry}/{repos[0]}): the pod would never start (ImagePullBackOff). "
                        f"Mirror it first (group Registry tab, or POST /api/v1/groups/{cluster.group_id}/registry/"
                        f"images with source={image} and dest_repo={repos[0]}), then run this again."), None
            return None, f"Could not check {image} in the mirror registry ({e}): ran anyway"
        except OSError as e:
            return None, f"Could not reach the mirror registry ({e}): ran anyway"
    return None, None


# -------------------------------------------------------------------------------------------- run

def prepare_oc(db: Any, cluster: Cluster, argv: List[str]) -> Dict[str, Any]:
    """Checks before running (fast, raise KubeExecError / OcRefused): {argv, kind, warnings}"""
    if cluster.type != "openshift":
        raise KubeExecError(f"{cluster.name} is a {cluster.type} cluster, not OpenShift: use the kubectl tool "
                            f"(same arguments, kubectl runs on its control plane)")
    if argv and argv[0] == "oc":
        argv = argv[1:]
    check_oc_args(argv)
    kind = oc_gather_kind(argv)
    _, pos, _ = _split_flags(argv)
    warnings: List[str] = []
    if kind == "must-gather" or (pos[:1] == ["debug"] and len(pos) > 1 and pos[1].startswith(("node/", "nodes/"))):
        refusal, warning = mirror_problem(db, cluster, argv, kind or "debug")
        if refusal:
            raise OcRefused(refusal)
        if warning:
            warnings.append(warning)
    return {"argv": argv, "kind": kind, "warnings": warnings}


def run_oc(db: Any, cluster: Cluster, argv: List[str], stdin: Optional[str] = None, timeout: float = 120,
           cancelled: Optional[Callable[[], bool]] = None, prepared: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """One oc command against an OpenShift cluster, non-interactive, output capped. Runs in the cluster's
    oc-output dir; adm must-gather / inspect write oc-output/<kind>-<time>/, returned as a tarball + listing."""
    from app.services.openshift_service import openshift_service
    prepared = prepared or prepare_oc(db, cluster, argv)
    argv, kind = prepared["argv"], prepared["kind"]
    out_dir = output_dir(cluster)
    run_dir = None
    if kind:
        run_dir = out_dir / f"{kind}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        at = argv.index("--") if "--" in argv else len(argv)  # not into the gather command
        argv = argv[:at] + [f"--dest-dir={run_dir}"] + argv[at:]
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        out = openshift_service.oc(cluster.name, cluster.version, argv, timeout, stdin=stdin, cwd=out_dir,
                                   cancelled=cancelled)
    except ValueError as e:
        raise KubeExecError(str(e))
    result: Dict[str, Any] = {"command": "oc " + shlex.join(argv), "exitcode": out["exitcode"]}
    if kind == "must-gather" and out["exitcode"] == 124:
        result["cleanup"] = _must_gather_cleanup(cluster, argv, out["stdout"] + out["stderr"], started)
    stdout = _drop_copy_listing(out["stdout"]) if kind == "must-gather" else out["stdout"]
    result["stdout"] = _cap(stdout, GATHER_STDOUT_CAP if kind else STDOUT_CAP,
                            "narrow it: -n <namespace>, -l <selector>, -o name / -o jsonpath=..., "
                            "logs --tail=N / --since=10m, describe a single object")
    result["stderr"] = _cap(out["stderr"], STDERR_CAP, "stderr cut")
    if run_dir is not None:
        result.update(_pack(cluster, run_dir))
    if prepared["warnings"]:
        result["warnings"] = prepared["warnings"]
    return result


def _must_gather_cleanup(cluster: Cluster, argv: List[str], output: str, started: str) -> List[str]:
    """oc stopped (timeout / cancel) leaves must-gather's pod, its temporary namespace and cluster role binding.
    Delete them: --run-namespace -> its pods labelled app=must-gather; else the openshift-must-gather-* namespaces
    the log names (or, if it names none yet, those created since `started`, UTC ISO) + their bindings."""
    from app.services.openshift_service import openshift_service
    flags, _, _ = _split_flags(argv)
    run_ns = (_flag_values(flags, "--run-namespace") or [None])[0]
    targets: List[List[str]] = []
    if run_ns:
        targets.append(["-n", run_ns, "pod", "-l", "app=must-gather"])
    else:
        try:
            spaces = openshift_service.oc_json(cluster.name, cluster.version, ["get", "namespaces"], 30)["items"]
            bindings = openshift_service.oc_json(cluster.name, cluster.version,
                                                 ["get", "clusterrolebindings"], 30)["items"]
        except (RuntimeError, ValueError, KeyError) as e:
            return [f"could not list must-gather's namespaces: {e}"]
        named = set(re.findall(r"openshift-must-gather-[a-z0-9]+", output))
        ours = [n["metadata"]["name"] for n in spaces
                if n["metadata"]["name"].startswith("openshift-must-gather-")
                and (n["metadata"]["name"] in named
                     or (not named and n["metadata"].get("creationTimestamp", "") >= started))]
        targets += [["namespace", n] for n in ours]
        targets += [["clusterrolebinding", b["metadata"]["name"]] for b in bindings
                    if b["metadata"]["name"].startswith("must-gather-")
                    and any(sub.get("namespace") in ours for sub in b.get("subjects") or [])]
    done = []
    for target in targets:
        out = openshift_service.oc(cluster.name, cluster.version,
                                   ["delete", "--ignore-not-found", "--wait=false"] + target, 60)
        done.append(f"oc delete {' '.join(target)}: "
                    + ("ok" if out["exitcode"] == 0 else out["stderr"].strip()[-200:]))
    return done


def _drop_copy_listing(text: str) -> str:
    """must-gather prints every file it copies back (rsync): keep the gather log, not that list"""
    out, copying, skipped = [], False, 0
    for line in text.splitlines():
        if copying and re.search(r"\] OUT \S+ (sent [\d,]+ bytes|total size is)", line):
            copying = False
            out.append(f"[... {skipped} lines of copied file names left out: see the listing / tarball ...]")
        if copying:
            skipped += 1
            continue
        out.append(line)
        if re.search(r"\] OUT \S+ receiving (incremental )?file list", line):
            copying, skipped = True, 0
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def _pack(cluster: Cluster, run_dir: Path) -> Dict[str, Any]:
    """Directory written by must-gather / inspect -> <dir>.tar.gz next to it (the directory is removed)"""
    if not run_dir.is_dir() or not any(run_dir.iterdir()):
        if run_dir.is_dir():
            shutil.rmtree(run_dir)
        return {"output": "nothing was written"}
    listing = _listing(run_dir)
    archive = run_dir.with_name(run_dir.name + ".tar.gz")
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(str(run_dir), arcname=run_dir.name)
    shutil.rmtree(run_dir)
    return {"listing": listing, "archive": {
        "name": archive.name, "path": str(archive), "size_kib": archive.stat().st_size // 1024,
        "download": f"/api/v1/clusters/{cluster.id}/oc-output/{archive.name}",
        "delete": f"DELETE /api/v1/clusters/{cluster.id}/oc-output/{archive.name}"}}
