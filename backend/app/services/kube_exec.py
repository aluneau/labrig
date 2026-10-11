"""Run kubectl (oc for OpenShift) against a cluster: one path for the MCP kubectl tool and the log viewer.

kubeadm / k3s: on the first running control plane through the QEMU guest agent; OpenShift: the app's oc on the host.
"""
import shlex
from typing import Any, Dict, List, Optional

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
