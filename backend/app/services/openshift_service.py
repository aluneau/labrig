"""OpenShift: pull secret, versions, installer / oc binaries, install configs, oc runner

Clusters are installed with the agent-based installer (ABI): the app renders
install-config.yaml + agent-config.yaml, runs `openshift-install agent create image`
on the host and boots the node VMs from the resulting ISO (see openshift_installer).

Everything OpenShift-specific lives under DATA_DIR/openshift (mode 0700):
  pull-secret.json            the user's pull secret (0600, never in the DB, never returned)
  bin/<version>/              openshift-install, oc, kubectl (sha256-checked downloads)
  cache/                      XDG_CACHE_HOME of the installer (base RHCOS ISO, ~1.4 GB per release)
  clusters/<name>/            install dir: copies of the configs, auth/, install state, SSH key

The host has no route to group networks and can't resolve cluster names: `oc` runs with a
kubeconfig whose server is the router's uplink address (haproxy -> API) and whose
tls-server-name is api.<cluster>.<domain>, the name the API certificate is issued for.
"""
import hashlib
import ipaddress
import json
import logging
import os
import re
import shutil
import subprocess
import tarfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml

from app.config import settings
from app.services import sshkey

logger = logging.getLogger(__name__)

MIRROR = "https://mirror.openshift.com/pub/openshift-v4/x86_64/clients"
GRAPH = "https://api.openshift.com/api/upgrades_info/v1/graph"
MIN_MINOR = (4, 16)          # oldest minor offered (agent-based installer + current operators)
# OKD (CentOS Stream CoreOS): versions from the OKD release controller (accepted tags of the <major>-scos-stable
# streams, the same list as the GitHub releases), binaries from the okd-project/okd GitHub releases (fallback:
# `oc adm release extract --command` from the release image)
OKD_STREAMS = "https://amd64.origin.releases.ci.openshift.org/api/v1/releasestreams/accepted"
OKD_TAGS = "https://amd64.origin.releases.ci.openshift.org/api/v1/releasestream/{stream}/tags"
OKD_DOWNLOADS = "https://github.com/okd-project/okd/releases/download"
OKD_RELEASE_IMAGE = "quay.io/okd/scos-release"
# OKD needs no Red Hat pull secret: the documented placeholder (everything comes from public quay.io)
OKD_PULL_SECRET = '{"auths":{"fake":{"auth":"aWQ6cGFzcwo="}}}'
# Add-ons that need Red Hat-only operators / registries: refused on OKD in plain words
OKD_UNSUPPORTED = {
    "odf": "ODF is a Red Hat product (no community build in OKD's catalogs): use LVMS storage",
    "sriov": "the SR-IOV Network Operator is not in OKD's community catalog",
    "acm": "ACM (Red Hat Advanced Cluster Management) is not available on OKD",
    "gitops": "the GitOps add-on is for ACM hubs, which OKD can't be",
    "disconnected": "disconnected installs mirror Red Hat's operator catalogs: OpenShift (ocp) only for now",
}


def is_okd(version: Optional[str]) -> bool:
    return bool(version) and "-okd-" in str(version)


def okd_unsupported(kind: str) -> Optional[str]:
    reason = OKD_UNSUPPORTED.get(kind)
    return f"Not on OKD: {reason}" if reason else None
CACHE_TTL = 3600
HTTP_TIMEOUT = 30

POD_CIDR = "10.128.0.0/14"
SERVICE_CIDR = "172.30.0.0/16"
# Dual stack: OpenShift's documented IPv6 defaults (cluster network /48, one /64 per node; services /112)
POD_CIDR6 = "fd01::/48"
SERVICE_CIDR6 = "fd02::/112"
# Networks OpenShift / OVN-Kubernetes use internally: node networks must not overlap them
RESERVED_CIDRS = [POD_CIDR, SERVICE_CIDR, "100.64.0.0/16", "100.88.0.0/16", "169.254.0.0/17", "10.88.0.0/16"]

# Curated operators offered before the cluster exists (no live catalog yet). managed_by: the
# create options that install and configure them (storage, SR-IOV, MetalLB).
CATALOG: List[Dict[str, Any]] = [
    {"name": "lvms-operator", "display_name": "LVM Storage", "category": "Storage", "managed_by": "storage:lvms",
     "description": "Node-local volumes (TopoLVM) on an extra disk per node: the storage for SNO."},
    {"name": "odf-operator", "display_name": "OpenShift Data Foundation", "category": "Storage",
     "managed_by": "storage:odf", "min_nodes": 3,
     "description": "Ceph block / file / object storage replicated over 3 nodes (needs LSO + a disk per node)."},
    {"name": "local-storage-operator", "display_name": "Local Storage", "category": "Storage",
     "managed_by": "storage:odf", "description": "Exposes node disks as local PVs (used by ODF)."},
    {"name": "sriov-network-operator", "display_name": "SR-IOV Network Operator", "category": "Networking",
     "managed_by": "sriov", "description": "VFs as secondary pod networks (emulated igb NICs in this lab)."},
    {"name": "metallb-operator", "display_name": "MetalLB", "category": "Networking", "managed_by": "metallb",
     "description": "LoadBalancer Services on bare metal: L2 (ARP) or BGP announcements."},
    {"name": "advanced-cluster-management", "display_name": "Advanced Cluster Management", "category": "Multicluster",
     "managed_by": "acm", "description": "ACM hub: manage other clusters (import, policies, applications). Pulls the "
     "multicluster engine; needs 12 GiB and 4 vCPU more than OpenShift alone."},
    {"name": "kubernetes-nmstate-operator", "display_name": "Kubernetes NMState", "category": "Networking",
     "description": "Declarative node network configuration (NodeNetworkConfigurationPolicy)."},
    {"name": "kubevirt-hyperconverged", "display_name": "OpenShift Virtualization", "category": "Virtualization",
     "description": "VMs on OpenShift (nested here: needs RAM). Creates the HyperConverged CR."},
    {"name": "openshift-gitops-operator", "display_name": "Red Hat OpenShift GitOps", "category": "CI/CD",
     "description": "Argo CD for cluster and application GitOps."},
    {"name": "openshift-pipelines-operator-rh", "display_name": "Red Hat OpenShift Pipelines", "category": "CI/CD",
     "description": "Tekton pipelines."},
    {"name": "cluster-logging", "display_name": "Red Hat OpenShift Logging", "category": "Observability",
     "description": "Log collection and forwarding (ClusterLogForwarder)."},
    {"name": "loki-operator", "display_name": "Loki Operator", "category": "Observability",
     "description": "Log storage for OpenShift Logging (needs object storage)."},
    {"name": "cluster-observability-operator", "display_name": "Cluster Observability Operator",
     "category": "Observability", "description": "Monitoring stacks and observability UI plugins."},
    {"name": "web-terminal", "display_name": "Web Terminal", "category": "Developer tools",
     "description": "A terminal with oc in the web console."},
    {"name": "nfd", "display_name": "Node Feature Discovery", "category": "Hardware",
     "description": "Labels nodes with their hardware features."},
    {"name": "servicemeshoperator3", "display_name": "Red Hat OpenShift Service Mesh 3", "category": "Networking",
     "description": "Istio-based service mesh."},
    {"name": "rhbk-operator", "display_name": "Red Hat build of Keycloak", "category": "Security",
     "description": "Identity provider for OAuth / OIDC labs."},
    {"name": "cert-manager-operator", "display_name": "cert-manager Operator", "category": "Security",
     "description": "Certificate management (Issuer, Certificate)."},
    {"name": "compliance-operator", "display_name": "Compliance Operator", "category": "Security",
     "description": "OpenSCAP compliance scans (CIS, PCI-DSS, ...)."},
]


# Disconnected installs: default OperatorHub sources -> their index image (registry.redhat.io/redhat/<x>:v<minor>)
CATALOG_INDEXES = {"redhat-operators": "redhat-operator-index", "certified-operators": "certified-operator-index",
                   "community-operators": "community-operator-index", "redhat-marketplace": "redhat-marketplace-index"}
CATALOG_CACHE_TTL = 7 * 24 * 3600
# Operators the app installs AND configures through an option (CR, disks, pools...): listing them as plain
# operators would install a bare, unconfigured operator (day 2) or be skipped (create). managed_by ->
# (create option, day-2 add-on kind)
# OKD: curated operators of the OKDerators catalog (openshift_addons.OKD_CATALOG_SOURCE, added by the app) and of
# the community catalog. ODF / SR-IOV / ACM are refused on OKD (OKD_UNSUPPORTED).
OKD_CATALOG: List[Dict[str, Any]] = [
    {**next(e for e in CATALOG if e["name"] == name), "source": "okderators"}
    for name in ("lvms-operator", "metallb-operator", "kubernetes-nmstate-operator", "web-terminal", "nfd",
                 "cert-manager-operator", "cluster-observability-operator")
] + [
    {"name": "community-kubevirt-hyperconverged", "display_name": "KubeVirt HyperConverged", "category": "Virtualization",
     "source": "community-operators", "description": "VMs on OKD (community build of OpenShift Virtualization)."},
    {"name": "argocd-operator", "display_name": "Argo CD", "category": "CI/CD", "source": "community-operators",
     "description": "Argo CD for cluster and application GitOps (community operator)."},
    {"name": "grafana-operator", "display_name": "Grafana Operator", "category": "Observability",
     "source": "community-operators", "description": "Grafana instances and dashboards as custom resources."},
    {"name": "keycloak-operator", "display_name": "Keycloak", "category": "Security", "source": "community-operators",
     "description": "Identity provider for OAuth / OIDC labs."},
]

MANAGED_OPTIONS = {
    "storage:lvms": ('"openshift": {"storage": "lvms"}', "lvms"),
    "storage:odf": ('"openshift": {"storage": "odf", "topology": "compact"} (3+ nodes: compact or ha)', "odf"),
    "sriov": ('"openshift": {"sriov": {"enabled": true}}', "sriov"),
    "metallb": ('"openshift": {"metallb": {"enabled": true}}', "metallb"),
    "acm": ('"openshift": {"acm": true}', "acm"),
}


def managed_by(package: str) -> Optional[str]:
    """The option that installs this package (CATALOG managed_by), None for a plain OLM operator"""
    if package == "multicluster-engine":  # pulled in by ACM's MultiClusterHub operator
        return "acm"
    return next((e.get("managed_by") for e in CATALOG if e["name"] == package), None)


def managed_operator_error(package: str, day2: bool) -> Optional[str]:
    """Plain-words refusal for a managed operator requested as a plain operator"""
    owner = managed_by(package)
    if owner is None:
        return None
    option, kind = MANAGED_OPTIONS[owner]
    how = f'the add-on kind "{kind}" instead (POST /clusters/{{id}}/openshift/addons {{"kind": "{kind}"}})' if day2 \
        else f"{option} in the cluster spec instead of listing it in operators"
    return (f"{package} is installed and configured by vm-manager itself (operator, custom resources"
            f"{', storage disks' if owner.startswith('storage') else ''}): use {how}")


CATALOG_TIMEOUT = 20 * 60
# Subscriptions an operator creates itself at run time (not declared in its bundle): mirrored with it
# (ACM's MultiClusterHub operator subscribes to multicluster-engine itself)
RUNTIME_DEPENDENCIES = {"odf-operator": ["odf-dependencies"], "advanced-cluster-management": ["multicluster-engine"]}
# When the catalog can't be read: what redhat-operator-index v4.20 resolves to
FALLBACK_DEPENDENCIES = {"advanced-cluster-management": ["multicluster-engine"], "odf-operator": [
    "odf-dependencies", "ocs-operator", "mcg-operator", "ocs-client-operator", "odf-csi-addons-operator",
    "odf-external-snapshotter-operator", "odf-prometheus-operator", "recipe", "rook-ceph-operator", "cephcsi-operator"]}


def _version_key(version: str) -> Tuple:
    main, _, pre = version.partition("-")
    nums = tuple(int(p) for p in main.split("."))
    # releases sort after their rc / ec builds
    if not pre:
        return nums + (2, 0)
    if pre.startswith("okd-scos."):  # OKD: 5.0.0-okd-scos.3, 5.1.0-okd-scos.ec.0
        rest = pre[len("okd-scos."):]
        ec = rest.startswith("ec.")
        n = rest[3:] if ec else rest
        return nums + (0 if ec else 2, int(n) if n.isdigit() else 0)
    kind, _, n = pre.partition(".")
    return nums + (1 if kind == "rc" else 0, int(n) if n.isdigit() else 0)


def _http_get(url: str, accept: Optional[str] = None) -> bytes:
    req = urllib.request.Request(url, headers={"Accept-Encoding": "identity", "User-Agent": "vm-manager",
                                               **({"Accept": accept} if accept else {})})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read()


class OpenShiftService:
    def __init__(self) -> None:
        self._cache: Dict[str, Tuple[float, Any]] = {}
        self._lock = threading.Lock()
        self._bin_locks: Dict[str, threading.Lock] = {}

    # ------------------------------------------------------------------ paths

    @property
    def root(self) -> Path:
        path = Path(settings.DATA_DIR) / "openshift"
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700)
        return path

    def cluster_dir(self, name: str) -> Path:
        path = self.root / "clusters" / name
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700)
        return path

    def bin_dir(self, version: str) -> Path:
        return self.root / "bin" / version

    # ------------------------------------------------------------- pull secret

    @property
    def _pull_secret_path(self) -> Path:
        return self.root / "pull-secret.json"

    @staticmethod
    def _validate_pull_secret(text: str) -> Dict[str, Any]:
        try:
            data = json.loads(text)
        except ValueError:
            raise ValueError("The pull secret is not valid JSON (copy it from "
                             "console.redhat.com/openshift/install/pull-secret)")
        if not isinstance(data, dict) or not isinstance(data.get("auths"), dict) or not data["auths"]:
            raise ValueError("The pull secret has no 'auths' entries")
        for registry, entry in data["auths"].items():
            if not isinstance(entry, dict) or not entry.get("auth"):
                raise ValueError(f"The pull secret entry for {registry} has no 'auth'")
        return data

    def pull_secret_status(self) -> Dict[str, Any]:
        path = self._pull_secret_path
        if not path.exists():
            return {"configured": False, "registries": [], "source": None}
        try:
            data = self._validate_pull_secret(path.read_text())
        except ValueError:
            return {"configured": False, "registries": [], "source": None}
        source = None
        meta = self.root / "pull-secret.source"
        if meta.exists():
            source = meta.read_text().strip() or None
        return {"configured": True, "registries": sorted(data["auths"]), "source": source}

    def set_pull_secret(self, content: Optional[str], path: Optional[str]) -> Dict[str, Any]:
        source = "pasted"
        if not content:
            if not path:
                raise ValueError("Give the pull secret content or a path on the host")
            file = Path(os.path.expanduser(path.strip()))
            if not file.is_file():
                raise ValueError(f"{file} does not exist on the host (or is not readable by the app)")
            if file.stat().st_size > 64 * 1024:
                raise ValueError(f"{file} is too large for a pull secret")
            content = file.read_text()
            source = str(file)
        data = self._validate_pull_secret(content)
        target = self._pull_secret_path
        tmp = target.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, separators=(",", ":"))
        os.replace(tmp, target)
        (self.root / "pull-secret.source").write_text(source)
        return self.pull_secret_status()

    def delete_pull_secret(self) -> Dict[str, Any]:
        for name in ("pull-secret.json", "pull-secret.source"):
            (self.root / name).unlink(missing_ok=True)
        return self.pull_secret_status()

    def pull_secret_for(self, version: Optional[str]) -> str:
        """install-config pullSecret: OKD's placeholder for an OKD release, else the user's"""
        return OKD_PULL_SECRET if is_okd(version) else self.pull_secret()

    def pull_secret(self) -> str:
        """Compact JSON for install-config.yaml"""
        path = self._pull_secret_path
        if not path.exists():
            raise ValueError("No pull secret: add it first (OpenShift settings in the Create cluster dialog)")
        return json.dumps(self._validate_pull_secret(path.read_text()), separators=(",", ":"))

    # ---------------------------------------------------------------- versions

    def _cached(self, key: str, fetch: Callable[[], Any]) -> Any:
        with self._lock:
            hit = self._cache.get(key)
            if hit and time.monotonic() - hit[0] < CACHE_TTL:
                return hit[1]
        value = fetch()
        with self._lock:
            self._cache[key] = (time.monotonic(), value)
        return value

    def _graph(self, channel: str) -> List[Dict[str, Any]]:
        def fetch():
            raw = _http_get(f"{GRAPH}?channel={channel}&arch=amd64", accept="application/json")
            return json.loads(raw).get("nodes") or []
        return self._cached(f"graph:{channel}", fetch)

    def _okd_tags(self) -> List[Dict[str, Any]]:
        """Accepted OKD (SCOS) releases of every <major>-scos-stable stream: [{version, payload}]"""
        def fetch():
            streams = json.loads(_http_get(OKD_STREAMS, accept="application/json"))
            result = []
            for stream in sorted(k for k in streams if re.match(r"^\d+-scos-stable$", k)):
                data = json.loads(_http_get(OKD_TAGS.format(stream=stream), accept="application/json"))
                result += [{"version": t["name"], "payload": t.get("pullSpec")} for t in data.get("tags") or []
                           if t.get("phase") == "Accepted" and re.match(r"^\d+\.\d+\.\d+-okd-scos\.\d+$", t["name"])]
            return result
        try:
            return self._cached("okd:tags", fetch)
        except Exception as e:
            raise RuntimeError(f"Cannot reach the OKD release controller: {e}")

    def versions(self, channel: str, distribution: str = "ocp") -> List[Dict[str, Any]]:
        if not re.match(r"^(stable|fast|candidate|eus)-\d+\.\d+$", channel):
            raise ValueError(f"Invalid channel '{channel}' (e.g. stable-4.20)")
        minor = channel.split("-", 1)[1]
        if distribution == "okd":
            result = [dict(t) for t in self._okd_tags() if t["version"].startswith(minor + ".")]
            result.sort(key=lambda v: _version_key(v["version"]), reverse=True)
            for v in result:
                v["cached"] = self.binaries_cached(v["version"])
            return result
        try:
            nodes = self._graph(channel)
        except Exception as e:
            raise RuntimeError(f"Cannot reach the OpenShift update service: {e}")
        result = [{"version": n["version"], "payload": n.get("payload")} for n in nodes
                  if str(n.get("version", "")).startswith(minor + ".")]
        result.sort(key=lambda v: _version_key(v["version"]), reverse=True)
        for v in result:
            v["cached"] = self.binaries_cached(v["version"])
        return result

    def channels(self, distribution: str = "ocp") -> List[Dict[str, Any]]:
        if distribution == "okd":  # one "stable-<minor>" per minor with accepted OKD releases
            by_minor: Dict[Tuple[int, int], List[str]] = {}
            for t in self._okd_tags():
                major, minor = (int(x) for x in t["version"].split(".")[:2])
                if (major, minor) >= MIN_MINOR:
                    by_minor.setdefault((major, minor), []).append(t["version"])
            return [{"name": f"stable-{ma}.{mi}", "minor": f"{ma}.{mi}",
                     "latest": max(by_minor[(ma, mi)], key=_version_key)} for ma, mi in sorted(by_minor, reverse=True)]

        def fetch():
            html = _http_get(f"{MIRROR}/ocp/").decode("utf-8", "replace")
            minors = {tuple(int(x) for x in m.split(".")) for m in re.findall(r'href="stable-(\d+\.\d+)/', html)}
            return sorted((m for m in minors if m >= MIN_MINOR), reverse=True)
        try:
            minors = self._cached("minors", fetch)
        except Exception as e:
            raise RuntimeError(f"Cannot reach mirror.openshift.com: {e}")
        result = []
        for major, minor in minors:
            name = f"stable-{major}.{minor}"
            latest = None
            try:
                versions = self.versions(name)
                latest = versions[0]["version"] if versions else None
            except RuntimeError:
                pass
            if latest:  # a minor in the mirror before it reaches stable: offer its candidate channel
                result.append({"name": name, "minor": f"{major}.{minor}", "latest": latest})
            else:
                cand = f"candidate-{major}.{minor}"
                try:
                    versions = self.versions(cand)
                except RuntimeError:
                    versions = []
                if versions:
                    result.append({"name": cand, "minor": f"{major}.{minor}", "latest": versions[0]["version"]})
        return result

    def resolve_version(self, channel: str, version: Optional[str], distribution: str = "ocp") -> str:
        if version:
            return version
        versions = self.versions(channel, distribution)
        if not versions:
            raise ValueError(f"No {'OKD ' if distribution == 'okd' else ''}release in channel {channel}")
        return versions[0]["version"]

    # ---------------------------------------------------------------- binaries

    def binaries_cached(self, version: str) -> bool:
        d = self.bin_dir(version)
        return (d / "openshift-install").is_file() and (d / "oc").is_file()

    def _mirror_dir(self, version: str) -> str:
        return f"{MIRROR}/{'ocp-dev-preview' if '-ec.' in version else 'ocp'}/{version}"

    def ensure_binaries(self, version: str, progress: Callable[[str, int], None],
                        cancelled: Callable[[], bool]) -> Path:
        """Download + verify openshift-install and oc for `version` (cached). progress(message, pct 0-100)"""
        with self._lock:
            lock = self._bin_locks.setdefault(version, threading.Lock())
        with lock:
            target = self.bin_dir(version)
            if self.binaries_cached(version):
                return target
            if is_okd(version):
                base = f"{OKD_DOWNLOADS}/{version}"
                files = [(f"openshift-install-linux-{version}.tar.gz", ["openshift-install"]),
                         (f"openshift-client-linux-{version}.tar.gz", ["oc", "kubectl"])]
            else:
                base = self._mirror_dir(version)
                files = [("openshift-install-linux.tar.gz", ["openshift-install"]),
                         ("openshift-client-linux.tar.gz", ["oc", "kubectl"])]
            sums = {}
            try:
                for line in _http_get(f"{base}/sha256sum.txt").decode().splitlines():
                    parts = line.split()
                    if len(parts) == 2:
                        sums[parts[1].lstrip("*")] = parts[0]
            except Exception as e:
                if not is_okd(version):
                    raise
                logger.warning(f"OKD {version}: no GitHub release files ({e}): extracting from the release image")
                return self._okd_extract(version, progress)
            tmp = self.root / "bin" / f".{version}.tmp"
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir(parents=True)
            try:
                for i, (archive, members) in enumerate(files):
                    if archive not in sums:
                        raise RuntimeError(f"{archive} is not published for {version}")
                    path = tmp / archive
                    self._download(f"{base}/{archive}", path, sums[archive],
                                   lambda pct, i=i, archive=archive: progress(
                                       f"Downloading {archive} ({version})", (i * 100 + pct) // len(files)),
                                   cancelled)
                    with tarfile.open(path) as tar:
                        for member in tar.getmembers():
                            if member.name in members and member.isfile():
                                src = tar.extractfile(member)
                                with open(tmp / member.name, "wb") as out:
                                    shutil.copyfileobj(src, out)
                                os.chmod(tmp / member.name, 0o755)
                    path.unlink()
                if not (tmp / "openshift-install").is_file() or not (tmp / "oc").is_file():
                    raise RuntimeError("openshift-install / oc missing from the downloaded archives")
                shutil.rmtree(target, ignore_errors=True)
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(tmp, target)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            return target

    def _okd_extract(self, version: str, progress: Callable[[str, int], None]) -> Path:
        """openshift-install + oc from the OKD release image itself (an oc of any release does it)"""
        target = self.bin_dir(version)
        tmp = self.root / "bin" / f".{version}.tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.root)}
        try:
            for i, command in enumerate(("openshift-install", "oc")):
                progress(f"Extracting {command} from {OKD_RELEASE_IMAGE}:{version}", i * 50)
                proc = subprocess.run([str(self._any_oc()), "adm", "release", "extract", f"--command={command}",
                                       f"--to={tmp}", f"{OKD_RELEASE_IMAGE}:{version}"],
                                      capture_output=True, text=True, timeout=30 * 60, env=env)
                if proc.returncode != 0:
                    raise RuntimeError(f"Cannot extract {command} of OKD {version}: "
                                       f"{(proc.stderr or proc.stdout).strip()[-300:]}")
            shutil.rmtree(target, ignore_errors=True)
            os.replace(tmp, target)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return target

    @staticmethod
    def _download(url: str, path: Path, sha256: str, progress: Callable[[int], None],
                  cancelled: Callable[[], bool]) -> None:
        req = urllib.request.Request(url, headers={"Accept-Encoding": "identity", "User-Agent": "vm-manager"})
        digest = hashlib.sha256()
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp, open(path, "wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            done, last = 0, -1
            while True:
                if cancelled():
                    raise InterruptedError("Cancelled")
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                pct = done * 100 // total if total else 0
                if pct != last:
                    last = pct
                    progress(pct)
        if digest.hexdigest() != sha256:
            raise RuntimeError(f"Checksum mismatch for {url.rsplit('/', 1)[-1]}")

    # ----------------------------------------------------------------- catalog

    @staticmethod
    def catalog_index(source: str, minor: str) -> Optional[str]:
        """Index image of a default OperatorHub source ("redhat-operators" -> redhat-operator-index:v4.20)"""
        name = CATALOG_INDEXES.get(source)
        return f"registry.redhat.io/redhat/{name}:v{minor}" if name else None

    def _catalog_configs(self, minor: str) -> Path:
        """The FBC (/configs) of redhat-operator-index:v<minor>, extracted on the host (~200 MB) and kept a
        week: package defaults and dependencies, to know what to mirror. Uses the pull secret file
        (never printed)."""
        base = self.root / "catalogs" / f"v{minor}"
        configs = base / "configs"
        stamp = base / ".complete"
        if stamp.exists() and time.time() - stamp.stat().st_mtime < CATALOG_CACHE_TTL:
            return configs
        with self._lock:
            lock = self._bin_locks.setdefault(f"catalog:{minor}", threading.Lock())
        with lock:
            if stamp.exists() and time.time() - stamp.stat().st_mtime < CATALOG_CACHE_TTL:
                return configs
            tmp = base / ".tmp"
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir(parents=True)
            image = self.catalog_index("redhat-operators", minor)
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.root)}
            proc = subprocess.run([str(self._any_oc()), "image", "extract", image, "--filter-by-os=linux/amd64",
                                   f"--registry-config={self._pull_secret_path}", "--path", f"/configs/:{tmp}",
                                   "--confirm"], capture_output=True, text=True, timeout=CATALOG_TIMEOUT, env=env)
            if proc.returncode != 0:
                shutil.rmtree(tmp, ignore_errors=True)
                raise RuntimeError(f"Cannot read {image}: {(proc.stderr or proc.stdout).strip()[-300:]}")
            shutil.rmtree(configs, ignore_errors=True)
            os.replace(tmp, configs)
            stamp.touch()
        return configs

    @staticmethod
    def _fbc_package(configs: Path, name: str) -> Dict[str, Any]:
        """{default_channel, head bundle properties} of a package of a file-based catalog"""
        docs: List[Dict[str, Any]] = []
        pkg_dir = configs / name
        files = [pkg_dir] if pkg_dir.is_file() else sorted(p for p in pkg_dir.rglob("*") if p.is_file()) \
            if pkg_dir.is_dir() else []
        for f in files:
            text = f.read_text(errors="replace").strip()
            if f.suffix == ".json":
                decoder, i = json.JSONDecoder(), 0
                while i < len(text):
                    obj, i = decoder.raw_decode(text, i)
                    docs.append(obj)
                    while i < len(text) and text[i].isspace():
                        i += 1
            elif f.suffix in (".yaml", ".yml"):
                docs += [d for d in yaml.safe_load_all(text) if isinstance(d, dict)]
        package = next((d for d in docs if d.get("schema") == "olm.package"), None)
        if package is None:
            return {}
        default = package.get("defaultChannel")
        channel = next((d for d in docs if d.get("schema") == "olm.channel" and d.get("name") == default), None)
        bundles = {d.get("name"): d for d in docs if d.get("schema") == "olm.bundle"}
        head = None
        if channel:
            entries = channel.get("entries") or []
            replaced = {e.get("replaces") for e in entries} | {s for e in entries for s in e.get("skips") or []}
            heads = [e["name"] for e in entries if e.get("name") not in replaced]

            def version(bundle: str) -> Tuple:
                props = (bundles.get(bundle) or {}).get("properties") or []
                v = next((p["value"].get("version", "") for p in props if p.get("type") == "olm.package"), "")
                return tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[.+-]", v))
            head = max(heads, key=version) if heads else None
        return {"default_channel": default, "properties": (bundles.get(head) or {}).get("properties") or []}

    def operator_closure(self, minor: str, names: List[str]) -> List[str]:
        """`names` + the packages their default-channel heads require (olm.package.required, and the
        subscriptions some operators create themselves, e.g. ODF's odf-dependencies): oc-mirror only
        copies the packages it is given. Falls back to known lists when the catalog can't be read."""
        try:
            configs = self._catalog_configs(minor)
        except (RuntimeError, OSError, subprocess.TimeoutExpired, ValueError) as e:
            logger.warning(f"Operator dependencies from the catalog v{minor}: {e}; using the built-in lists")
            result = list(names)
            for name in names:
                result += [d for d in FALLBACK_DEPENDENCIES.get(name, []) if d not in result]
            return result
        result: List[str] = []
        queue = list(names)
        while queue:
            name = queue.pop(0)
            if name in result:
                continue
            result.append(name)
            info = self._fbc_package(configs, name)
            for prop in info.get("properties") or []:
                if prop.get("type") == "olm.package.required":
                    queue.append(prop["value"]["packageName"])
            for dep in RUNTIME_DEPENDENCIES.get(name, []):
                if (configs / dep).exists():
                    queue.append(dep)
        return result

    @staticmethod
    def catalog(distribution: str = "ocp") -> List[Dict[str, Any]]:
        if distribution == "okd":
            return [{"source": "community-operators", "managed_by": None, "min_nodes": 1, **entry}
                    for entry in OKD_CATALOG]
        return [{"source": "redhat-operators", "managed_by": None, "min_nodes": 1, **entry} for entry in CATALOG]

    # ----------------------------------------------------------- install files

    @staticmethod
    def ssh_keypair(directory: Path) -> str:
        """Per-cluster ed25519 key (sshKey in install-config: `core` on the nodes). Returns the public key."""
        key = directory / "id_ed25519"
        if not key.exists():
            # pure Python: ssh-keygen runs SELinux-confined on EL and can't write in /var/lib/vm-manager
            sshkey.write_keypair(key, f"vm-manager@{directory.name}")
        return (directory / "id_ed25519.pub").read_text().strip()

    @staticmethod
    def install_config(name: str, base_domain: str, machine_cidr: str, masters: int, workers: int,
                       pull_secret: str, ssh_key: str, mirror: Optional[Dict[str, Any]] = None,
                       vips: Optional[Dict[str, str]] = None,
                       machine_cidr6: Optional[str] = None) -> Dict[str, Any]:
        """mirror (disconnected): {image_digest_sources: [{source, mirrors}], ca_pem}: the release comes
        from the group's mirror registry, whose CA every node trusts (additionalTrustBundlePolicy Always:
        also the cluster-wide trust bundle, for OLM catalogs / operators pulling from it).
        vips {api, ingress (+ api6, ingress6)}: platform baremetal (in-cluster keepalived + haproxy on those
        addresses)"""
        config = {
            "apiVersion": "v1",
            "baseDomain": base_domain,
            "metadata": {"name": name},
            "compute": [{"name": "worker", "replicas": workers, "architecture": "amd64"}],
            "controlPlane": {"name": "master", "replicas": masters, "architecture": "amd64"},
            "networking": {
                "networkType": "OVNKubernetes",
                "machineNetwork": [{"cidr": machine_cidr}],
                "clusterNetwork": [{"cidr": POD_CIDR, "hostPrefix": 23}],
                "serviceNetwork": [SERVICE_CIDR],
            },
            # dual stack: both families, IPv4 first (primary)
            "platform": {"baremetal": {"apiVIPs": [vips["api"]] + ([vips["api6"]] if vips.get("api6") else []),
                                       "ingressVIPs": [vips["ingress"]]
                                       + ([vips["ingress6"]] if vips.get("ingress6") else [])}}
            if vips else {"none": {}},
            "pullSecret": pull_secret,
            "sshKey": ssh_key,
        }
        if machine_cidr6:
            # dual stack, IPv4 first (primary family): the nodes' IPv6 addresses come from DHCPv6 (group /64)
            net = config["networking"]
            net["machineNetwork"].append({"cidr": machine_cidr6})
            net["clusterNetwork"].append({"cidr": POD_CIDR6, "hostPrefix": 64})
            net["serviceNetwork"].append(SERVICE_CIDR6)
        if mirror:
            config["imageDigestSources"] = mirror["image_digest_sources"]
            config["additionalTrustBundle"] = mirror["ca_pem"].strip() + "\n"
            config["additionalTrustBundlePolicy"] = "Always"
        return config

    @staticmethod
    def agent_config(name: str, rendezvous_ip: str, ntp: Optional[str],
                     hosts: List[Dict[str, Any]]) -> Dict[str, Any]:
        """hosts: [{name, role: master|worker, mac}] (addresses come from the router's DHCP reservations)"""
        config: Dict[str, Any] = {
            "apiVersion": "v1beta1", "kind": "AgentConfig", "metadata": {"name": name},
            "rendezvousIP": rendezvous_ip,
            "hosts": [{
                "hostname": h["name"], "role": h["role"],
                "rootDeviceHints": {"deviceName": "/dev/vda"},
                "interfaces": [{"name": "enp1s0", "macAddress": h["mac"]}],
            } for h in hosts],
        }
        if ntp:
            config["additionalNTPSources"] = [ntp]
        return config

    BMH_IFACE = "bmhnet0"

    @classmethod
    def nmstate_static(cls, mac: str, ip: str, prefix: int, gateway: str,
                       dns: List[str], search: Optional[List[str]] = None) -> Dict[str, Any]:
        """A static-IP nmstate config for one NIC matched by MAC (identifier: mac-address, so the kernel's
        name for it doesn't matter). Shared by the bare-metal agent-config and the ACM spoke's
        NMStateConfig CR (field shapes mirror a customer's SiteConfig ClusterInstance nodeNetwork)."""
        iface = cls.BMH_IFACE
        dns_config: Dict[str, Any] = {"server": list(dns)}
        if search:
            dns_config["search"] = list(search)
        return {
            "interfaces": [{
                "name": iface, "type": "ethernet", "state": "up",
                "identifier": "mac-address", "mac-address": mac,
                "ipv4": {"enabled": True, "dhcp": False,
                         "address": [{"ip": ip, "prefix-length": int(prefix)}]},
                "ipv6": {"enabled": False},
            }],
            "dns-resolver": {"config": dns_config},
            "routes": {"config": [{"destination": "0.0.0.0/0", "next-hop-address": gateway,
                                   "next-hop-interface": iface, "table-id": 254}]},
        }

    @classmethod
    def agent_config_bmh(cls, name: str, rendezvous_ip: str, hosts: List[Dict[str, Any]],
                         ntp: Optional[List[str]] = None) -> Dict[str, Any]:
        """Bare-metal agent-config: static networking (nmstate) per host, no router DHCP. The NIC is
        matched by MAC (identifier: mac-address), so the kernel's name for it doesn't matter.
        hosts: [{name, role, mac, ip, prefix, gateway, dns: [...], root_device?}]"""
        def entry(h: Dict[str, Any]) -> Dict[str, Any]:
            host: Dict[str, Any] = {
                "hostname": h["name"], "role": h["role"],
                "interfaces": [{"name": cls.BMH_IFACE, "macAddress": h["mac"]}],
                "networkConfig": cls.nmstate_static(h["mac"], h["ip"], h["prefix"], h["gateway"], h["dns"]),
            }
            if h.get("root_device"):
                host["rootDeviceHints"] = {"deviceName": h["root_device"]}
            return host
        config: Dict[str, Any] = {
            "apiVersion": "v1beta1", "kind": "AgentConfig", "metadata": {"name": name},
            "rendezvousIP": rendezvous_ip, "hosts": [entry(h) for h in hosts],
        }
        if ntp:
            config["additionalNTPSources"] = list(ntp)
        return config

    @staticmethod
    def check_cidr(cidr: str) -> None:
        net = ipaddress.IPv4Network(cidr, strict=False)
        for reserved in RESERVED_CIDRS:
            if net.overlaps(ipaddress.IPv4Network(reserved)):
                raise ValueError(f"The node network {net} overlaps {reserved}, used inside OpenShift")

    # --------------------------------------------------------------------- oc

    def kubeconfig_for_host(self, kubeconfig: str, cluster_name: str, server: str, api_name: str) -> str:
        """The installer's kubeconfig pointed at an address the host / a VPN client can reach. The API
        certificate covers api.<cluster>.<domain>, not that address: tls-server-name keeps TLS checks."""
        config = yaml.safe_load(kubeconfig)
        for entry in config.get("clusters") or []:
            entry["name"] = cluster_name
            entry["cluster"]["server"] = server
            entry["cluster"]["tls-server-name"] = api_name
        for entry in config.get("users") or []:
            entry["name"] = f"{cluster_name}-admin"
        for entry in config.get("contexts") or []:
            entry["name"] = cluster_name
            entry["context"]["cluster"] = cluster_name
            entry["context"]["user"] = f"{cluster_name}-admin"
        config["current-context"] = cluster_name
        return yaml.safe_dump(config, sort_keys=False)

    def oc(self, cluster_name: str, version: str, args: List[str], timeout: float = 60,
           stdin: Optional[str] = None, cwd: Optional[Path] = None,
           cancelled: Optional[Callable[[], bool]] = None) -> Dict[str, Any]:
        """Run oc against a cluster (kubeconfig = install dir host-kubeconfig). cwd: where commands that write
        files (adm must-gather / inspect, cp, rsync) put them; cancelled: polled, stops oc (SIGINT, so
        must-gather / debug delete their temporary namespace / pod)"""
        oc_bin = self.bin_dir(version) / "oc"
        if not oc_bin.is_file():
            oc_bin = self._any_oc()
        kubeconfig = self.cluster_dir(cluster_name) / "host-kubeconfig"
        if not kubeconfig.exists():
            raise ValueError("The cluster has no kubeconfig yet")
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.root),
               "KUBECONFIG": str(kubeconfig)}
        proc = subprocess.Popen([str(oc_bin)] + args,
                                stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
                                cwd=str(cwd) if cwd else None)
        deadline = time.monotonic() + timeout
        stop = ""
        pending_input = stdin
        while True:
            try:
                # communicate() may be called again after a timeout without losing output
                out, err = proc.communicate(pending_input, timeout=max(0.1, min(2.0, deadline - time.monotonic())))
                break
            except subprocess.TimeoutExpired:
                pending_input = None
                if time.monotonic() >= deadline:
                    stop = f"oc {' '.join(args[:3])}: timed out after {timeout:.0f} s"
                elif cancelled is not None and cancelled():
                    stop = "cancelled"
                else:
                    continue
                proc.send_signal(2)  # SIGINT: oc cleans up like after Ctrl-C
                try:
                    out, err = proc.communicate(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    out, err = proc.communicate()
                break
        if stop:
            return {"exitcode": 124, "stdout": out, "stderr": (err + "\n" if err else "") + stop}
        return {"exitcode": proc.returncode, "stdout": out, "stderr": err}

    def _any_oc(self) -> Path:
        bins = sorted((self.root / "bin").glob("*/oc"), key=lambda p: _version_key(p.parent.name), reverse=True) \
            if (self.root / "bin").exists() else []
        if not bins:
            raise ValueError("oc is not downloaded")
        return bins[0]

    def oc_json(self, cluster_name: str, version: str, args: List[str], timeout: float = 60) -> Any:
        out = self.oc(cluster_name, version, args + ["-o", "json"], timeout)
        if out["exitcode"] != 0:
            raise RuntimeError((out["stderr"] or out["stdout"]).strip()[-500:] or "oc failed")
        return json.loads(out["stdout"])

    def apply(self, cluster_name: str, version: str, manifests: List[Dict[str, Any]], timeout: float = 120) -> None:
        text = "\n---\n".join(yaml.safe_dump(m, sort_keys=False) for m in manifests)
        out = self.oc(cluster_name, version, ["apply", "-f", "-"], timeout, stdin=text)
        if out["exitcode"] != 0:
            raise RuntimeError(f"oc apply failed: {(out['stderr'] or out['stdout']).strip()[-800:]}")

    def approve_csrs(self, cluster_name: str, version: str) -> int:
        """Approve pending node CSRs (client + serving): platform none has no machine approver for
        new / restarted nodes. Returns how many were approved."""
        try:
            data = self.oc_json(cluster_name, version, ["get", "csr"], 30)
        except (RuntimeError, ValueError):
            return 0
        pending = [item["metadata"]["name"] for item in data.get("items", [])
                   if not (item.get("status") or {}).get("conditions")
                   and item.get("spec", {}).get("signerName") in (
                       "kubernetes.io/kube-apiserver-client-kubelet", "kubernetes.io/kubelet-serving")]
        if pending:
            self.oc(cluster_name, version, ["adm", "certificate", "approve"] + pending, 60)
        return len(pending)


openshift_service = OpenShiftService()
