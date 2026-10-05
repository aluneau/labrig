"""Cluster type drivers: what to put in a node's cloud-init and how to talk to the cluster

Each driver turns a cluster + node into cloud-init additions, and knows the
in-guest commands (run through the QEMU guest agent) to check readiness and
fetch the admin kubeconfig. Only k3s exists for now; kubeadm and OpenShift
(agent-based installer) slot in as other drivers.
"""
import ipaddress
import shlex
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
    def parse_ready(output: str) -> Dict[str, bool]:
        """`kubectl get nodes --no-headers` -> {node: Ready?}"""
        result = {}
        for line in output.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                result[parts[0]] = "Ready" in parts[1].split(",")
        return result


class K3sDriver(ClusterDriver):
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
        return [K3S_BIN, "kubectl", "get", "nodes", "--no-headers"]

    def kubectl_command(self, args: List[str]) -> List[str]:
        return [K3S_BIN, "kubectl"] + args

    def kubeconfig_command(self) -> List[str]:
        return ["/bin/cat", "/etc/rancher/k3s/k3s.yaml"]

    def version_command(self) -> List[str]:
        return [K3S_BIN, "--version"]

    @staticmethod
    def parse_version(output: str) -> Optional[str]:
        parts = output.split()  # "k3s version v1.33.5+k3s1 (hash)"
        return parts[2] if len(parts) >= 3 and parts[0] == "k3s" else None


DRIVERS: Dict[str, ClusterDriver] = {"k3s": K3sDriver()}


def get_driver(cluster_type: str) -> ClusterDriver:
    from app.schemas.cluster import CLUSTER_TYPES
    if cluster_type in DRIVERS:
        return DRIVERS[cluster_type]
    if cluster_type in CLUSTER_TYPES:
        raise ValueError(f"Cluster type '{cluster_type}' is not supported yet (available: {', '.join(DRIVERS)})")
    raise ValueError(f"Unknown cluster type '{cluster_type}' (known: {', '.join(CLUSTER_TYPES)})")
