"""Cluster type drivers: what to put in a node's cloud-init and how to talk to the cluster

Each driver turns a cluster + node into cloud-init additions, and knows the
in-guest commands (run through the QEMU guest agent) to check readiness and
fetch the admin kubeconfig. k3s nodes set themselves up from cloud-init; kubeadm
is orchestrated by the app (bootstrap()). OpenShift (agent-based installer) slots
in as another driver.
"""
import ipaddress
import json
import re
import shlex
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import yaml

# Cloud images whose package manager is dnf and that ship SELinux enforcing
EL_DISTRIBUTIONS = {"almalinux", "rocky", "centos", "rhel", "fedora", "oraclelinux"}

K3S_BIN = "/usr/local/bin/k3s"


def _el_guest_agent_prep() -> Dict[str, Any]:
    """EL's qemu-guest-agent forbids guest-exec / guest-file-* (FILTER_RPC_ARGS / BLACKLIST_RPC in
    /etc/sysconfig/qemu-ga) and SELinux confines it (virt_qemu_ga_t). The app reads the kubeconfig
    and runs kubectl through guest-exec, so on these lab VMs allow every RPC and make the agent's
    SELinux domain permissive (the rest of the system stays enforcing)."""
    return {
        "write_files": [
            {"path": "/etc/sysconfig/qemu-ga", "permissions": "0644",
             "content": "# vm-manager: allow guest-exec (cluster kubeconfig / kubectl)\n"
                        "FILTER_RPC_ARGS=\"\"\nBLACKLIST_RPC=\"\"\n"},
            {"path": "/etc/vm-manager/qemu-ga-permissive.cil", "permissions": "0644",
             "content": "(typepermissive virt_qemu_ga_t)\n"},
        ],
        "runcmd": [
            "command -v semodule >/dev/null && semodule -i /etc/vm-manager/qemu-ga-permissive.cil || true",
            "systemctl restart qemu-guest-agent || true",
            # k3s documents disabling firewalld; generic cloud images usually don't ship it
            "systemctl disable --now firewalld 2>/dev/null || true",
        ],
    }


class ClusterDriver:
    type: str = ""
    #: nodes must live in a lab group (router DNS + API load balancer)
    needs_group: bool = False
    #: bootstrap() does the work after the VMs boot (otherwise cloud-init does everything)
    orchestrated: bool = False

    def bootstrap(self, ops: Any, ctx: Dict[str, Any], nodes: List[Dict[str, Any]], initial: bool) -> None:
        """Set up / join the new nodes once they booted (see KubeadmDriver)"""

    def node_cloud_config(self, ctx: Dict[str, Any], node: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    def ready_nodes_command(self) -> List[str]:
        raise NotImplementedError

    def kubectl_command(self, args: List[str]) -> List[str]:
        raise NotImplementedError

    def kubeconfig_command(self) -> List[str]:
        raise NotImplementedError

    def version_command(self) -> List[str]:
        raise NotImplementedError

    @staticmethod
    def parse_ready(output: str, since: Optional[datetime] = None) -> Dict[str, bool]:
        """`kubectl get nodes -o json` -> {node: Ready?}. With `since` (UTC), a node only counts
        once its kubelet reported Ready after that time: right after a restart the API still
        shows the Ready status recorded before the shutdown."""
        result = {}
        for item in json.loads(output).get("items", []):
            name = item.get("metadata", {}).get("name")
            cond = next((c for c in item.get("status", {}).get("conditions", []) if c.get("type") == "Ready"), {})
            ready = cond.get("status") == "True"
            if ready and since is not None:
                beat = cond.get("lastHeartbeatTime") or ""
                try:
                    ready = datetime.strptime(beat, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) >= since
                except ValueError:
                    ready = False
            if name:
                result[name] = ready
        return result


class K3sDriver(ClusterDriver):  # standalone cluster network (no router)
    """k3s: first control plane runs `k3s server` (with --cluster-init when there are several, i.e.
    embedded etcd), the others join it through https://api.<cluster>.<domain>:6443."""

    type = "k3s"

    def node_cloud_config(self, ctx: Dict[str, Any], node: Dict[str, Any]) -> Dict[str, Any]:
        """ctx: name, zone, api_hostname, api_ip, token, version, ctlplanes, pod_cidr, service_cidr,
        extra_args, el. node: name, role, ip, first (first control plane)."""
        server_url = f"https://{ctx['api_hostname']}:6443"
        config: Dict[str, Any] = {"token": ctx["token"], "node-ip": node["ip"]}
        if node["role"] == "ctlplane":
            service_net = ipaddress.IPv4Network(ctx["service_cidr"], strict=False)
            config.update({
                "tls-san": [ctx["api_hostname"], ctx["api_ip"], f"{node['name']}.{ctx['zone']}", node["ip"]],
                "cluster-cidr": ctx["pod_cidr"],
                "service-cidr": ctx["service_cidr"],
                "cluster-dns": str(service_net.network_address + 10),
                "write-kubeconfig-mode": "0600",
            })
            if node["first"]:
                if ctx["ctlplanes"] > 1:
                    config["cluster-init"] = True  # embedded etcd
            else:
                config["server"] = server_url
            exec_args = "server " + (ctx.get("extra_args") or "")
        else:
            config["server"] = server_url
            exec_args = "agent"

        env = f"INSTALL_K3S_EXEC={shlex.quote(exec_args.strip())}"
        if ctx.get("version"):
            env += f" INSTALL_K3S_VERSION={shlex.quote(ctx['version'])}"
        install = (
            "for i in $(seq 1 30); do curl -sfL https://get.k3s.io -o /root/k3s-install.sh && break; sleep 10; done; "
            f"{env} sh /root/k3s-install.sh"
        )
        extra: Dict[str, Any] = {
            "packages": ["curl"],
            "write_files": [{
                "path": "/etc/rancher/k3s/config.yaml", "permissions": "0600",
                "content": yaml.safe_dump(config, sort_keys=False),
            }],
            "runcmd": [["sh", "-c", install]],
        }
        if ctx.get("el"):
            prep = _el_guest_agent_prep()
            extra["write_files"] = prep["write_files"] + extra["write_files"]
            # k3s' installer adds the rancher repo and installs k3s-selinux + container-selinux itself
            extra["runcmd"] = prep["runcmd"] + extra["runcmd"]
        return extra

    def ready_nodes_command(self) -> List[str]:
        return [K3S_BIN, "kubectl", "get", "nodes", "-o", "json"]

    def kubectl_command(self, args: List[str]) -> List[str]:
        return [K3S_BIN, "kubectl"] + args

    def kubeconfig_command(self) -> List[str]:
        return ["/bin/cat", "/etc/rancher/k3s/k3s.yaml"]

    def version_command(self) -> List[str]:
        return [K3S_BIN, "--version"]

    def token_command(self) -> List[str]:
        """Join token, to recover it for a cluster rebuilt from libvirt metadata"""
        return ["/bin/cat", "/var/lib/rancher/k3s/server/token"]

    @staticmethod
    def parse_version(output: str) -> Optional[str]:
        parts = output.split()  # "k3s version v1.33.5+k3s1 (hash)"
        return parts[2] if len(parts) >= 3 and parts[0] == "k3s" else None


# ---------------------------------------------------------------------------------------- kubeadm

# Kubernetes minor used when the cluster doesn't name one (pkgs.k8s.io repo, kubeadm/kubelet/kubectl).
# Pinned on purpose: bump after checking https://dl.k8s.io/release/stable.txt.
KUBEADM_DEFAULT_VERSION = "v1.37"
# Pod network: Flannel (one manifest, VXLAN, honours the pod CIDR, small footprint)
FLANNEL_VERSION = "v0.28.9"
FLANNEL_MANIFEST = f"https://github.com/flannel-io/flannel/releases/download/{FLANNEL_VERSION}/kube-flannel.yml"
ADMIN_CONF = "/etc/kubernetes/admin.conf"
K8S_STATE = "/var/lib/vmm-k8s"
PREREQS_SCRIPT = "/usr/local/sbin/vmm-k8s-prereqs.sh"
INIT_CONFIG = "/etc/vm-manager/kubeadm-init.yaml"
PREREQS_TIMEOUT = 25 * 60   # package installs + image pulls on a slow mirror
INIT_TIMEOUT = 10 * 60
JOIN_TIMEOUT = 10 * 60

# Node prerequisites, run once by cloud-init. Debian/Ubuntu (apt) and EL (dnf):
# - containerd from Docker's repo (containerd.io 2.x: Kubernetes >= 1.36 needs containerd 2;
#   Debian 13 ships 1.7) with SystemdCgroup = true
# - kubelet / kubeadm / kubectl from pkgs.k8s.io for the cluster's minor (held / excluded)
# - swap off, overlay + br_netfilter, bridge-nf-call-iptables + ip_forward
# - EL: SELinux permissive, as the kubeadm install docs say (lab VMs), firewalld off
# Writes K8S_STATE/prereqs-done, or prereqs-failed with the step that failed (log in
# /var/log/vmm-k8s-prereqs.log); the app polls those through the guest agent.
_PREREQS = r"""#!/bin/sh
# Generated by VM Manager: Kubernetes node prerequisites for kubeadm
exec >>/var/log/vmm-k8s-prereqs.log 2>&1
set -x
STATE=@STATE@
MINOR=@MINOR@
PKGVER=@PKGVER@
ROLE=@ROLE@
mkdir -p "$STATE"
rm -f "$STATE/prereqs-done" "$STATE/prereqs-failed"
fail() { echo "$1 (see /var/log/vmm-k8s-prereqs.log)" > "$STATE/prereqs-failed"; exit 1; }
retry() { n=0; until "$@"; do n=$((n + 1)); [ "$n" -ge 30 ] && return 1; sleep 10; done; }

swapoff -a
sed -i -E '/^[^#].*\sswap\s/ s/^/#/' /etc/fstab
modprobe overlay
modprobe br_netfilter
modprobe nf_conntrack  # EL 10 doesn't load it before kube-proxy needs nf_conntrack_max
sysctl --system >/dev/null

. /etc/os-release
if command -v apt-get >/dev/null; then
    export DEBIAN_FRONTEND=noninteractive
    retry apt-get update -q || fail "apt-get update"
    retry apt-get install -y -q curl ca-certificates conntrack socat ethtool || fail "base packages"
    install -d -m 0755 /etc/apt/keyrings
    retry curl -fsSL "https://download.docker.com/linux/$ID/gpg" -o /etc/apt/keyrings/docker.asc || fail "Docker repo key"
    echo "deb [signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/$ID $VERSION_CODENAME stable" \
        > /etc/apt/sources.list.d/docker.list
    retry curl -fsSL "https://pkgs.k8s.io/core:/stable:/$MINOR/deb/Release.key" -o /etc/apt/keyrings/kubernetes.asc \
        || fail "Kubernetes repo key ($MINOR)"
    echo "deb [signed-by=/etc/apt/keyrings/kubernetes.asc] https://pkgs.k8s.io/core:/stable:/$MINOR/deb/ /" \
        > /etc/apt/sources.list.d/kubernetes.list
    retry apt-get update -q || fail "apt-get update (Docker / Kubernetes repos)"
    V=""
    if [ -n "$PKGVER" ]; then
        V="=$(apt-cache madison kubeadm | awk -v v="$PKGVER" '$3 ~ "^"v"-" {print $3; exit}')"
        [ "$V" = "=" ] && fail "Kubernetes $PKGVER not found in the $MINOR repo"
    fi
    retry apt-get install -y -q containerd.io "kubelet$V" "kubeadm$V" "kubectl$V" || fail "containerd / kubeadm packages"
    apt-mark hold kubelet kubeadm kubectl
else
    setenforce 0
    sed -i 's/^SELINUX=enforcing/SELINUX=permissive/' /etc/selinux/config
    systemctl disable --now firewalld 2>/dev/null
    MAJOR=${VERSION_ID%%.*}
    cat > /etc/yum.repos.d/docker-ce.repo <<EOF
[docker-ce-stable]
name=Docker CE (containerd.io)
baseurl=https://download.docker.com/linux/rhel/$MAJOR/\$basearch/stable
enabled=1
gpgcheck=1
gpgkey=https://download.docker.com/linux/rhel/gpg
EOF
    cat > /etc/yum.repos.d/kubernetes.repo <<EOF
[kubernetes]
name=Kubernetes
baseurl=https://pkgs.k8s.io/core:/stable:/$MINOR/rpm/
enabled=1
gpgcheck=1
gpgkey=https://pkgs.k8s.io/core:/stable:/$MINOR/rpm/repodata/repomd.xml.key
exclude=kubelet kubeadm kubectl cri-tools kubernetes-cni
EOF
    V=""
    [ -n "$PKGVER" ] && V="-$PKGVER"
    retry dnf -y -q install containerd.io "kubelet$V" "kubeadm$V" "kubectl$V" conntrack-tools socat iproute-tc \
        --disableexcludes=kubernetes || fail "containerd / kubeadm packages"
fi

mkdir -p /etc/containerd
containerd config default > /etc/containerd/config.toml || fail "containerd config"
sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' /etc/containerd/config.toml
if ! grep -q 'SystemdCgroup = true' /etc/containerd/config.toml; then
    sed -i "/runtimes\.runc\.options\]/a\            SystemdCgroup = true" /etc/containerd/config.toml
fi
grep -q 'SystemdCgroup = true' /etc/containerd/config.toml || fail "containerd SystemdCgroup"
systemctl enable containerd
systemctl restart containerd || fail "containerd start"
echo "runtime-endpoint: unix:///run/containerd/containerd.sock" > /etc/crictl.yaml
systemctl enable kubelet
if [ "$ROLE" = ctlplane ]; then
    retry kubeadm config images pull --kubernetes-version "$(kubeadm version -o short)" || fail "control plane images"
fi
touch "$STATE/prereqs-done"
"""


def kubeadm_version(version: Optional[str]) -> Dict[str, Optional[str]]:
    """'v1.37' / '1.37' / 'v1.37.1' / None -> {minor: 'v1.37', patch: '1.37.1' or None}"""
    v = (version or KUBEADM_DEFAULT_VERSION).lstrip("v")
    if "+" in v:
        raise ValueError(f"'{version}' is a k3s version, not a Kubernetes one (e.g. v1.37 or v1.37.1)")
    parts = v.split(".")
    if len(parts) not in (2, 3) or not all(p.isdigit() for p in parts):
        raise ValueError(f"Invalid Kubernetes version '{version}' (e.g. v1.37 or v1.37.1)")
    return {"minor": f"v{parts[0]}.{parts[1]}", "patch": v if len(parts) == 3 else None}


def kubeadm_token() -> str:
    """A bootstrap token: [a-z0-9]{6}.[a-z0-9]{16}"""
    import secrets
    import string
    alphabet = string.ascii_lowercase + string.digits
    pick = lambda n: "".join(secrets.choice(alphabet) for _ in range(n))  # noqa: E731
    return f"{pick(6)}.{pick(16)}"


class KubeadmDriver(ClusterDriver):
    """kubeadm: nodes live in a lab group whose router fronts the API (haproxy, api.<cluster>.<domain>
    -> every control plane). cloud-init only installs the prerequisites; the app then runs
    `kubeadm init` on the first control plane, applies Flannel, and joins the other control planes
    (one at a time, etcd) and the workers, all through the guest agent."""

    type = "kubeadm"
    needs_group = True
    orchestrated = True

    def node_cloud_config(self, ctx: Dict[str, Any], node: Dict[str, Any]) -> Dict[str, Any]:
        """ctx: name, zone, api_hostname, api_port, api_sans, token, certificate_key, version,
        pod_cidr, service_cidr, el. node: name, role, ip, first."""
        ver = kubeadm_version(ctx.get("version"))
        script = (_PREREQS.replace("@STATE@", K8S_STATE).replace("@MINOR@", ver["minor"])
                  .replace("@PKGVER@", ver["patch"] or "").replace("@ROLE@", node["role"]))
        files = [
            {"path": "/etc/modules-load.d/k8s.conf", "permissions": "0644", "content": "overlay\nbr_netfilter\nnf_conntrack\n"},
            {"path": "/etc/sysctl.d/99-kubernetes.conf", "permissions": "0644",
             "content": "net.bridge.bridge-nf-call-iptables = 1\nnet.bridge.bridge-nf-call-ip6tables = 1\n"
                        "net.ipv4.ip_forward = 1\n"},
            {"path": PREREQS_SCRIPT, "permissions": "0755", "content": script},
        ]
        if node["role"] == "ctlplane" and node["first"]:
            files.append({"path": INIT_CONFIG, "permissions": "0600", "content": self.init_config(ctx, node)})
        extra: Dict[str, Any] = {"write_files": files, "runcmd": [["sh", PREREQS_SCRIPT]]}
        if ctx.get("el"):
            prep = _el_guest_agent_prep()
            extra["write_files"] = prep["write_files"] + extra["write_files"]
            extra["runcmd"] = prep["runcmd"] + extra["runcmd"]
        return extra

    @staticmethod
    def init_config(ctx: Dict[str, Any], node: Dict[str, Any]) -> str:
        """kubeadm v1beta4 config of the first control plane; @KUBE_VERSION@ is replaced by the
        installed kubeadm's version when init runs"""
        init = {
            "apiVersion": "kubeadm.k8s.io/v1beta4", "kind": "InitConfiguration",
            "bootstrapTokens": [{"token": ctx["token"], "ttl": "24h0m0s",
                                 "groups": ["system:bootstrappers:kubeadm:default-node-token"],
                                 "usages": ["signing", "authentication"]}],
            "certificateKey": ctx["certificate_key"],
            "localAPIEndpoint": {"advertiseAddress": node["ip"], "bindPort": 6443},
            "nodeRegistration": {"name": node["name"], "criSocket": "unix:///run/containerd/containerd.sock"},
        }
        cluster = {
            "apiVersion": "kubeadm.k8s.io/v1beta4", "kind": "ClusterConfiguration",
            "kubernetesVersion": "@KUBE_VERSION@",
            "clusterName": ctx["name"],
            "controlPlaneEndpoint": f"{ctx['api_hostname']}:{ctx['api_port']}",
            "networking": {"podSubnet": ctx["pod_cidr"], "serviceSubnet": ctx["service_cidr"]},
            "apiServer": {"certSANs": ctx["api_sans"]},
        }
        return yaml.safe_dump(init, sort_keys=False) + "---\n" + yaml.safe_dump(cluster, sort_keys=False)

    # ---- orchestration (through the guest agent)

    def bootstrap(self, ops: Any, ctx: Dict[str, Any], nodes: List[Dict[str, Any]], initial: bool) -> None:
        """nodes: the new nodes (name, role, ip, first), control planes first. ops: exec(vm, argv,
        timeout), wait_agent(vm), progress(pct, message), sleep(s), api_node() -> vm name."""
        count = max(len(nodes), 1)
        for i, node in enumerate(nodes):
            ops.progress(35 + 20 * i // count, f"Installing containerd + kubeadm on {node['name']}")
            ops.wait_agent(node["name"])
            self._wait_prereqs(ops, node["name"])

        first = next((n for n in nodes if n.get("first")), None)
        if initial and first is not None:
            ops.progress(56, f"kubeadm init on {first['name']}")
            self._sh(ops, first["name"], (
                f"test -f {ADMIN_CONF} && exit 0; "
                f"sed \"s/@KUBE_VERSION@/$(kubeadm version -o short)/\" {INIT_CONFIG} > {INIT_CONFIG}.rendered && "
                f"kubeadm init --config {INIT_CONFIG}.rendered --upload-certs > /var/log/vmm-kubeadm-init.log 2>&1 "
                "|| { tail -n 40 /var/log/vmm-kubeadm-init.log; exit 1; }"), INIT_TIMEOUT, "kubeadm init")
            ops.progress(62, "Installing the pod network (Flannel)")
            self._sh(ops, first["name"], (
                "n=0; until curl -fsSL -o /root/kube-flannel.yml " + FLANNEL_MANIFEST + "; do "
                "n=$((n+1)); [ $n -ge 20 ] && exit 1; sleep 10; done; "
                f"sed -i 's#10.244.0.0/16#{ctx['pod_cidr']}#' /root/kube-flannel.yml && "
                f"kubectl --kubeconfig {ADMIN_CONF} apply -f /root/kube-flannel.yml"), 600, "Flannel")

        api = ops.api_node()
        joining = [n for n in nodes if not n.get("first")]
        if not joining:
            return
        join = self._join_command(ops, api, ctx["token"])
        ctlplanes = [n for n in joining if n["role"] == "ctlplane"]
        if ctlplanes:
            # the control plane certificates uploaded by init expire after 2 h: upload them again
            self._sh(ops, api, f"kubeadm init phase upload-certs --upload-certs --certificate-key "
                               f"{ctx['certificate_key']} >/dev/null", 120, "upload-certs")
        for i, node in enumerate(joining):
            ops.progress(66 + 14 * i // len(joining), f"Joining {node['name']}")
            # EL hostnames are the FQDN: name the node like the VM
            extra = f" --node-name {node['name']}"
            if node["role"] == "ctlplane":
                extra += (f" --control-plane --certificate-key {ctx['certificate_key']}"
                         f" --apiserver-advertise-address {node['ip']}")
            self._sh(ops, node["name"], (
                "test -f /etc/kubernetes/kubelet.conf && exit 0; "
                f"{join}{extra} > /var/log/vmm-kubeadm-join.log 2>&1 "
                "|| { tail -n 40 /var/log/vmm-kubeadm-join.log; exit 1; }"), JOIN_TIMEOUT, f"kubeadm join on {node['name']}")

    @staticmethod
    def _sh(ops: Any, vm: str, script: str, timeout: float, label: str) -> str:
        out = ops.exec(vm, ["/bin/sh", "-c", script], timeout)
        if out["exitcode"] != 0:
            raise RuntimeError(f"{label} failed on {vm}:\n{(out['stdout'] + out['stderr']).strip()[-2500:]}")
        return out["stdout"]

    def _wait_prereqs(self, ops: Any, vm: str) -> None:
        import time
        script = (f"if test -f {K8S_STATE}/prereqs-done; then echo done; "
                  f"elif test -f {K8S_STATE}/prereqs-failed; then cat {K8S_STATE}/prereqs-failed; "
                  "tail -n 15 /var/log/vmm-k8s-prereqs.log; fi")
        deadline = time.monotonic() + PREREQS_TIMEOUT
        while True:
            try:
                out = ops.exec(vm, ["/bin/sh", "-c", script], 30)["stdout"].strip()
            except Exception:  # agent restarting (EL reconfigures it), guest-exec not allowed yet
                out = ""
            if out == "done":
                return
            if out:
                raise RuntimeError(f"Kubernetes prerequisites failed on {vm}: {out[-2000:]}")
            if time.monotonic() > deadline:
                raise TimeoutError(f"Kubernetes prerequisites not installed on {vm} after "
                                   f"{PREREQS_TIMEOUT // 60} min (see /var/log/vmm-k8s-prereqs.log)")
            ops.sleep(5)

    def _join_command(self, ops: Any, api: str, token: str) -> str:
        """`kubeadm join <endpoint> --token … --discovery-token-ca-cert-hash …` with the cluster's
        token re-created (it may have expired: bootstrap tokens live 24 h)"""
        out = self._sh(ops, api, (f"kubeadm token delete {token} >/dev/null 2>&1; "
                                  f"kubeadm token create {token} --ttl 2h --print-join-command"), 120, "kubeadm token create")
        line = next((l.strip() for l in reversed(out.splitlines()) if l.strip().startswith("kubeadm join ")), "")
        if not re.match(r"^kubeadm join [A-Za-z0-9.:-]+ --token [a-z0-9.]+ --discovery-token-ca-cert-hash sha256:[0-9a-f]{64}$",
                        line):
            raise RuntimeError(f"Unexpected join command from {api}: {out.strip()[-300:]}")
        return line

    # ---- commands

    def ready_nodes_command(self) -> List[str]:
        return self.kubectl_command(["get", "nodes", "-o", "json"])

    def kubectl_command(self, args: List[str]) -> List[str]:
        return ["/usr/bin/kubectl", "--kubeconfig", ADMIN_CONF] + args

    def kubeconfig_command(self) -> List[str]:
        return ["/bin/cat", ADMIN_CONF]

    def version_command(self) -> List[str]:
        return ["/usr/bin/kubectl", "--kubeconfig", ADMIN_CONF, "version", "-o", "json"]

    @staticmethod
    def parse_version(output: str) -> Optional[str]:
        try:
            return json.loads(output)["serverVersion"]["gitVersion"]
        except (ValueError, KeyError, TypeError):
            return None

    def token_command(self) -> List[str]:
        """Clusters rebuilt from libvirt metadata: a new bootstrap token (printed on stdout)"""
        return ["/usr/bin/kubeadm", "token", "create", "--ttl", "24h"]


DRIVERS: Dict[str, ClusterDriver] = {"k3s": K3sDriver(), "kubeadm": KubeadmDriver()}


def get_driver(cluster_type: str) -> ClusterDriver:
    from app.schemas.cluster import CLUSTER_TYPES
    if cluster_type in DRIVERS:
        return DRIVERS[cluster_type]
    if cluster_type in CLUSTER_TYPES:
        raise ValueError(f"Cluster type '{cluster_type}' is not supported yet (available: {', '.join(DRIVERS)})")
    raise ValueError(f"Unknown cluster type '{cluster_type}' (known: {', '.join(CLUSTER_TYPES)})")
