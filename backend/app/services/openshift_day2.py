"""OpenShift day 2 (docs/openshift.md "Day 2"): add / remove workers, cluster upgrades

Workers: `oc adm node-image create` (OpenShift 4.17+) runs a pod in the cluster that builds a node ISO
from a nodes-config.yaml (hostname, MAC, rootDeviceHints /dev/vda) -> the ISO goes to the default pool ->
new VMs exactly like the install's (reserved MAC + DNS name in the group, empty disk then CD) -> the node
writes RHCOS and reboots, the app approves its CSRs (platform none has no machine approver) until it is
Ready -> eject + delete the ISO. The router's ingress load balancers (80 / 443) follow the nodes that run
router pods (`ingress_ips`). Removing a worker = cordon + drain, delete the Node, delete the VM, drop its
reservation / records.

Upgrades: the ClusterVersion is the source of truth (channel, availableUpdates, conditionalUpdates with
their risks, history). Starting one = `oc adm upgrade --to=<version>` (a not-recommended conditional
update only with accept_risks), then a task follows the ClusterVersion until the new version is Completed.
`spec.openshift.upgrade` records the run so a server restart re-attaches a new follow task
(`recover`): the upgrade itself goes on in the cluster.
"""
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import yaml
from sqlalchemy.orm import Session

from app.config import settings
from app.libvirt_client import libvirt_client
from app.models import Cluster, ClusterNode, Task
from app.services.cluster_network import GroupClusterNetwork
from app.services.openshift_service import _version_key, openshift_service
from app.services.task_service import task_service

logger = logging.getLogger(__name__)

NODE_IMAGE_TIMEOUT = 30 * 60   # oc adm node-image create: pulls the node-joiner image, builds the ISO
JOIN_TIMEOUT = 60 * 60         # node ISO boot -> RHCOS on disk -> reboot -> kubelet Ready
UPGRADE_TIMEOUT = 5 * 3600     # z-stream on SNO ~1 h; minor upgrades / nested rigs take longer
RETRIEVE_TIMEOUT = 2 * 60      # new channel -> CVO fetched its updates
SETTLE_TIMEOUT = 15 * 60       # oc adm upgrade refused while an operator is degraded (transient after node changes)
CHANNEL_RE = re.compile(r"^[a-z]+-\d+(\.\d+)?$")  # stable-4.18, eus-4.18, OKD stable-4
PERCENT_RE = re.compile(r"(\d+)% complete")


def ingress_ips(topology: str, nodes: List[ClusterNode]) -> List[str]:
    """Backends of the router's 80 / 443 load balancers = the nodes that may run the ingress router pods.
    SNO / compact masters are schedulable and keep their router pod when workers join (haproxy health
    checks drop the nodes without one); HA masters never run it: workers only."""
    masters = [n.ip for n in nodes if n.role == "ctlplane"]
    workers = [n.ip for n in nodes if n.role == "worker"]
    if topology == "ha":
        return workers or masters
    return masters + workers


class OpenShiftDay2:

    @property
    def svc(self):
        from app.services.cluster_service import cluster_service
        return cluster_service

    @property
    def inst(self):
        from app.services.openshift_installer import openshift_installer
        return openshift_installer

    # ------------------------------------------------------------- checks

    def _check_vm_cluster(self, db: Session, cluster: Cluster, what: str) -> None:
        from app.services.acm_spokes import provisioner
        spec = cluster.spec or {}
        if spec.get("bare_metal") or provisioner(cluster) is not None:
            raise ValueError(f"{what} is only offered for OpenShift clusters of VMs in a lab group (this one runs "
                             "on bare-metal hosts or was provisioned by an ACM hub)")
        if not cluster.group_id:
            raise ValueError(f"{what}: the cluster has no lab group")
        if self.svc.to_dict(db, cluster)["status"] != "ready":
            raise ValueError("Start the cluster first (it must be ready)")
        if not cluster.kubeconfig:
            raise ValueError("The cluster has no kubeconfig")

    @staticmethod
    def _disconnected(cluster: Cluster) -> bool:
        return bool(((cluster.spec or {}).get("openshift") or {}).get("disconnected"))

    # ------------------------------------------------------------- workers

    def add_workers(self, db: Session, cluster: Cluster, count: int, worker: Optional[Dict[str, int]]) -> Task:
        from app.services.openshift_installer import MINIMUMS
        self._check_vm_cluster(db, cluster, "Adding workers")
        if self._disconnected(cluster):
            raise ValueError("Adding workers to a disconnected cluster is not supported yet: the node image is "
                             "built from release images that would have to be mirrored first")
        if _version_key(cluster.version or "0") < _version_key("4.17"):
            raise ValueError(f"Adding workers needs OpenShift 4.17 or later (`oc adm node-image create`); "
                             f"this cluster runs {cluster.version}: upgrade it first")
        spec = cluster.spec or {}
        res = dict(worker or spec.get("worker") or {})
        low = [f"{k} {res.get(k)} (minimum {v})" for k, v in MINIMUMS["worker"].items() if (res.get(k) or 0) < v]
        if low:
            raise ValueError("Worker too small for OpenShift: " + ", ".join(low)
                             + " (memory MiB, vcpu, disk_size GiB)")
        opts = self.inst.options(cluster)
        self.inst._check_budget(db, {"ctlplane": res, "worker": res}, {"masters": 0, "workers": count}, opts)
        return self.svc._run_task(db, cluster, "scale", "Add workers to", self._add_run, count, res)

    def _add_run(self, db: Session, task: Task, cluster_id: int, count: int, res: Dict[str, int]) -> Dict[str, Any]:
        svc = self.svc
        from app.services.cluster_service import _node_index, _random_mac

        def body(cluster: Cluster) -> Dict[str, Any]:
            inst = self.inst
            opts = inst.options(cluster)
            last = max((_node_index(n.name) for n in cluster.nodes if n.role == "worker"), default=-1)
            names = svc._node_names(cluster.name, 0, count, last + 1)
            svc._set_status(db, cluster, "provisioning", f"Adding {', '.join(names)}")
            cluster.spec = {**cluster.spec, "worker": res}
            db.commit()
            network = GroupClusterNetwork(cluster.group_id, cluster.name, owned=bool(cluster.group_owned))
            network.ensure()

            svc._check(task, cluster, db, 3, "Reserving addresses and DNS names, moving the ingress load balancers")
            zone = inst.zone(cluster)
            taken = [n.ip for n in cluster.nodes if n.ip]
            added: List[ClusterNode] = []
            for name in names:
                ip = network.allocate_ip(taken)
                taken.append(ip)
                node = ClusterNode(name=name, role="worker", ip=ip, mac=_random_mac())
                cluster.nodes.append(node)
                added.append(node)
            db.commit()
            for node in added:
                network.reserve(node.name, node.mac, node.ip)
                network.publish(node.ip, [f"{node.name}.{zone}", node.name])
            self._publish_ingress(network, cluster, opts.topology)
            network.commit()
            try:
                return finish(cluster, network, opts, added, names)
            except Exception:
                db.rollback()
                self._drop_unbuilt(db, cluster, network, opts.topology, added)
                raise

        def finish(cluster: Cluster, network: GroupClusterNetwork, opts, added: List[ClusterNode],
                   names: List[str]) -> Dict[str, Any]:
            inst = self.inst

            def bin_progress(message: str, pct: int) -> None:
                svc._check(task, cluster, db, 5 + pct * 5 // 100, message)
            openshift_service.ensure_binaries(cluster.version, bin_progress, lambda: task_service.is_cancelled(task.id))

            svc._check(task, cluster, db, 10, "Building the node ISO (oc adm node-image create: a pod in the cluster)")
            iso = self._node_image(db, task, cluster, added)
            inst._update_spec(db, cluster, node_iso_path=iso)

            svc._check(task, cluster, db, 30, "Creating the worker VMs")
            inst._create_vms(db, cluster, added, opts, iso)
            for node in added:
                libvirt_client.start_vm(node.name)
            self._wait_join(db, task, cluster, added)

            svc._check(task, cluster, db, 97, "Ejecting the node ISO")
            for node in added:
                try:
                    libvirt_client.set_cdrom(node.name, None)
                except Exception as e:  # noqa: BLE001 - the worker is in; a mounted ISO is harmless
                    logger.warning(f"Could not eject the node ISO from {node.name}: {e}")
            libvirt_client.delete_volume_by_path(iso)
            spec = {k: v for k, v in cluster.spec.items() if k != "node_iso_path"}
            cluster.spec = {**spec, "workers": sum(1 for n in cluster.nodes if n.role == "worker")}
            db.commit()
            inst.save_spec(cluster)
            svc._set_status(db, cluster, "ready", None)
            return {"added": names}

        # a failed add leaves a working cluster: back to ready, with the error as its message
        return svc._guarded(db, task, cluster_id, body, failed_status="ready")

    def _drop_unbuilt(self, db: Session, cluster: Cluster, network: GroupClusterNetwork, topology: str,
                      added: List[ClusterNode]) -> None:
        """A failed add: forget the new nodes whose VM was never created (their reservation, names, LB backends);
        nodes with a VM stay listed, to be removed like any worker"""
        try:
            db.refresh(cluster)
            gone = [n for n in cluster.nodes if n.name in {a.name for a in added}
                    and libvirt_client.get_vm(n.name) is None]
            if not gone:
                return
            for node in gone:
                network.release(node.mac)
                network.unpublish(node.ip)
                cluster.nodes.remove(node)
            self._publish_ingress(network, cluster, topology)
            network.commit()
            db.commit()
        except Exception:  # noqa: BLE001 - never hide the original error
            logger.exception(f"Could not clean up the new workers of {cluster.name}")

    def _publish_ingress(self, network: GroupClusterNetwork, cluster: Cluster, topology: str) -> None:
        from app.services.openshift_installer import LB_PORTS
        if ((cluster.spec or {}).get("openshift") or {}).get("platform") == "baremetal":
            return  # the router's 80 / 443 point at the ingress VIP (keepalived moves it to a router pod's node)
        ips = ingress_ips(topology, list(cluster.nodes))
        network.publish_lb(f"{cluster.name}-http", LB_PORTS["http"], [f"{ip}:80" for ip in ips])
        network.publish_lb(f"{cluster.name}-https", LB_PORTS["https"], [f"{ip}:443" for ip in ips])

    def _node_image(self, db: Session, task: Task, cluster: Cluster, nodes: List[ClusterNode]) -> str:
        """nodes-config.yaml -> `oc adm node-image create` -> ISO uploaded to the default pool (its path)"""
        workdir = openshift_service.cluster_dir(cluster.name)
        ssh_key = openshift_service.ssh_keypair(workdir)
        outdir = workdir / "node-image"
        if outdir.exists():
            for entry in outdir.iterdir():
                if entry.is_file():
                    entry.unlink()
        outdir.mkdir(mode=0o700, exist_ok=True)
        config = {"hosts": [{"hostname": n.name, "rootDeviceHints": {"deviceName": "/dev/vda"},
                             "interfaces": [{"name": "enp1s0", "macAddress": n.mac}]} for n in nodes],
                  "sshKey": ssh_key}
        (outdir / "nodes-config.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
        out = openshift_service.oc(
            cluster.name, cluster.version,
            ["adm", "node-image", "create", "--dir", str(outdir), "--report",
             "-a", str(openshift_service._pull_secret_path)],
            NODE_IMAGE_TIMEOUT, cwd=outdir, cancelled=lambda: task_service.is_cancelled(task.id))
        isos = sorted(outdir.glob("*.iso"))
        if out["exitcode"] != 0 or not isos:
            detail = (out["stderr"] or out["stdout"]).strip()[-800:]
            report = outdir / "report.json"
            if report.exists():
                detail += "\nreport.json: " + report.read_text()[-800:]
            if task_service.is_cancelled(task.id):
                from app.services.cluster_service import Cancelled
                raise Cancelled()
            raise RuntimeError(f"oc adm node-image create failed: {detail}")
        iso_file = isos[0]
        svc = self.svc
        svc._check(task, cluster, db, 25, "Uploading the node ISO to the default pool")
        pool_name = libvirt_client.ensure_pool(settings.DEFAULT_POOL_NAME, settings.DEFAULT_POOL_PATH).name()
        name = f"{cluster.name}-node-{int(time.time())}.iso"
        with open(iso_file, "rb") as f:
            path = libvirt_client.upload_volume(pool_name, name, iso_file.stat().st_size, f)
        iso_file.unlink()
        return path

    def _wait_join(self, db: Session, task: Task, cluster: Cluster, nodes: List[ClusterNode]) -> None:
        from app.services.cluster_drivers import ClusterDriver
        svc = self.svc
        expected = [n.name for n in nodes]
        deadline = time.monotonic() + JOIN_TIMEOUT
        last_csr = 0.0
        while True:
            if time.monotonic() - last_csr > 20:
                openshift_service.approve_csrs(cluster.name, cluster.version)
                last_csr = time.monotonic()
            out = openshift_service.oc(cluster.name, cluster.version, ["get", "nodes", "-o", "json"], 30)
            ready: Dict[str, bool] = {}
            if out["exitcode"] == 0:
                ready = ClusterDriver.parse_ready(out["stdout"], None)
            joined = [n for n in expected if n in ready]
            count = sum(1 for n in expected if ready.get(n))
            if count == len(expected):
                return
            if joined:
                message = f"Workers joined, waiting for Ready: {count}/{len(expected)} (CSRs approved by the app)"
            else:
                message = ("Booting the node ISO: writing RHCOS to disk, rebooting, joining the cluster "
                           f"(0/{len(expected)} joined)")
            svc._check(task, cluster, db, 35 + 60 * (len(joined) + count) // (2 * len(expected)), message)
            if time.monotonic() > deadline:
                raise TimeoutError(f"{', '.join(n for n in expected if not ready.get(n))} not Ready after "
                                   f"{JOIN_TIMEOUT // 60} min: check the VM console (node ISO boot / install)")
            svc._sleep(task, 15)

    def remove_worker(self, db: Session, cluster: Cluster, node_name: str) -> Task:
        node = next((n for n in cluster.nodes if n.name == node_name), None)
        if node is None:
            raise LookupError(f"Node '{node_name}' is not part of cluster {cluster.name}")
        if node.role != "worker":
            raise ValueError("Only workers can be removed")
        from app.services.acm_spokes import provisioner
        if (cluster.spec or {}).get("bare_metal") or provisioner(cluster) is not None:
            raise ValueError("Removing nodes is only offered for OpenShift clusters of VMs in a lab group")
        return self.svc._run_task(db, cluster, "scale", f"Remove {node_name} from", self._remove_run, node_name)

    def _remove_run(self, db: Session, task: Task, cluster_id: int, node_name: str) -> Dict[str, Any]:
        svc = self.svc

        def body(cluster: Cluster) -> Dict[str, Any]:
            node = next(n for n in cluster.nodes if n.name == node_name)
            previous = svc.to_dict(db, cluster)["status"]
            svc._set_status(db, cluster, "provisioning", f"Removing {node_name}")
            deleted = False
            if previous == "ready":  # a stopped cluster: nothing to drain, the Node object stays (delete it later)
                svc._check(task, cluster, db, 10, f"Draining {node_name}")
                drain = openshift_service.oc(cluster.name, cluster.version, [
                    "adm", "drain", node_name, "--ignore-daemonsets", "--delete-emptydir-data", "--force",
                    "--timeout=300s"], 330, cancelled=lambda: task_service.is_cancelled(task.id))
                if drain["exitcode"] != 0:
                    logger.warning(f"drain {node_name}: {(drain['stderr'] or drain['stdout']).strip()[-300:]}")
                svc._check(task, cluster, db, 60, f"Deleting the Node {node_name}")
                out = openshift_service.oc(cluster.name, cluster.version, ["delete", "node", node_name,
                                                                          "--ignore-not-found"], 60)
                deleted = out["exitcode"] == 0
            svc._check(task, cluster, db, 70, f"Deleting the VM {node_name}, its reservation and DNS names")
            network = GroupClusterNetwork(cluster.group_id, cluster.name, owned=bool(cluster.group_owned))
            if node.mac:
                network.release(node.mac)
            if node.ip:
                network.unpublish(node.ip)
            cluster.nodes.remove(node)
            self._publish_ingress(network, cluster, self.inst.options(cluster).topology)
            network.commit()
            libvirt_client.delete_vm(node_name, delete_disks=True)
            cluster.spec = {**cluster.spec, "workers": sum(1 for n in cluster.nodes if n.role == "worker")}
            db.commit()
            self.inst.save_spec(cluster)
            from app.services.vm_service import vm_service
            vm_service.sync_vms(db)
            svc._set_status(db, cluster, "ready" if previous == "ready" else "stopped" if previous == "stopped"
                            else "ready", None)
            return {"removed": node_name, "deleted_from_openshift": deleted}

        return svc._guarded(db, task, cluster_id, body, failed_status="ready")

    # ------------------------------------------------------------- upgrades

    def _cv(self, cluster: Cluster) -> Dict[str, Any]:
        return openshift_service.oc_json(cluster.name, cluster.version, ["get", "clusterversion", "version"], 30)

    @staticmethod
    def _summary(cv: Dict[str, Any]) -> Dict[str, Any]:
        spec = cv.get("spec") or {}
        status = cv.get("status") or {}
        conds = {c.get("type"): c for c in status.get("conditions") or []}

        def cond(name: str) -> Dict[str, Any]:
            c = conds.get(name) or {}
            return {"status": c.get("status"), "reason": c.get("reason"), "message": c.get("message")}
        history = status.get("history") or []
        current = next((h.get("version") for h in history if h.get("state") == "Completed"), None)
        desired = (status.get("desired") or {}).get("version")
        progressing = conds.get("Progressing") or {}
        updating = progressing.get("status") == "True" and bool(history) and history[0].get("state") != "Completed"
        pct = None
        m = PERCENT_RE.search(progressing.get("message") or "")
        if m:
            pct = int(m.group(1))
        available = sorted({u.get("version") for u in status.get("availableUpdates") or [] if u.get("version")},
                           key=_version_key, reverse=True)
        conditional = []
        for cu in status.get("conditionalUpdates") or []:
            release = cu.get("release") or {}
            rec = next((c for c in cu.get("conditions") or [] if c.get("type") == "Recommended"), {})
            conditional.append({
                "version": release.get("version"),
                # True: the risks don't apply to this cluster (CVO evaluated them); False: they do;
                # None: not evaluated yet
                "recommended": {"True": True, "False": False}.get(rec.get("status")),
                "reason": rec.get("reason"), "message": rec.get("message"),
                "risks": [{"name": r.get("name"), "message": r.get("message"), "url": r.get("url")}
                          for r in cu.get("risks") or []],
            })
        conditional.sort(key=lambda c: _version_key(c["version"] or "0"), reverse=True)
        retrieved = cond("RetrievedUpdates")
        upgradeable = cond("Upgradeable")
        failing = cond("Failing")
        return {
            "current": current, "desired": desired, "channel": spec.get("channel") or "",
            "channels": sorted((status.get("desired") or {}).get("channels") or [], key=lambda c: (
                c.split("-")[0], _version_key(c.split("-")[-1]))),
            "updating": updating, "progress": pct, "message": progressing.get("message"),
            "failing": failing["message"] if failing["status"] == "True" else None,
            # False = minor upgrades blocked (z-streams still allowed), with the operator's reason
            "upgradeable": upgradeable["status"] != "False",
            "upgradeable_message": upgradeable["message"] if upgradeable["status"] == "False" else None,
            "retrieved": retrieved["status"] == "True",
            "retrieved_message": (retrieved["message"] if retrieved["status"] != "True" else None),
            "available": available,
            "conditional": [c for c in conditional if c["version"] and c["version"] not in available],
            "history": [{"version": h.get("version"), "state": h.get("state"), "started": h.get("startedTime"),
                         "completed": h.get("completionTime")} for h in history[:8]],
        }

    def upgrades(self, db: Session, cluster: Cluster) -> Dict[str, Any]:
        """GET /clusters/{id}/upgrades: the ClusterVersion in plain words"""
        if cluster.type != "openshift":
            raise ValueError(f"{cluster.name} is a {cluster.type} cluster: upgrades are for OpenShift")
        base = {"cluster_id": cluster.id, "version": cluster.version,
                "disconnected": self._disconnected(cluster),
                "upgrade": ((cluster.spec or {}).get("openshift") or {}).get("upgrade"),
                "task_running": task_service.is_running(cluster.task_id)}
        if self.svc.to_dict(db, cluster)["status"] not in ("ready", "provisioning") or not cluster.kubeconfig:
            return {**base, "reachable": False, "error": "Start the cluster to see its updates"}
        try:
            cv = self._cv(cluster)
        except (RuntimeError, ValueError) as e:
            return {**base, "reachable": False, "error": f"Can't read the ClusterVersion: {e}"}
        summary = self._summary(cv)
        ops = self.inst.cluster_operators(cluster) or []
        target = summary["desired"]
        summary["operators_total"] = len(ops)
        summary["operators_updated"] = sum(1 for o in ops if o.get("version") == target)
        summary["operators_pending"] = [o["name"] for o in ops if o.get("version") != target][:20] \
            if summary["updating"] else []
        return {**base, "reachable": True, **summary}

    def set_upgrade(self, db: Session, cluster: Cluster, channel: Optional[str], version: Optional[str],
                    accept_risks: bool) -> Dict[str, Any]:
        """POST /clusters/{id}/upgrades: set the channel, and/or start an upgrade to `version` (a task).
        Returns the new upgrades view (+ task_id when an upgrade started)."""
        if cluster.type != "openshift":
            raise ValueError(f"{cluster.name} is a {cluster.type} cluster: upgrades are for OpenShift")
        if not channel and not version:
            raise ValueError("Give a channel, a version, or both")
        if self._disconnected(cluster):
            raise ValueError("Upgrading a disconnected cluster is not supported yet: the new release has to be "
                             "mirrored into the router's registry first (oc-mirror) and the upgrade started by "
                             "image digest. Upgrade a connected cluster, or re-create this one on the new version.")
        self._check_vm_cluster(db, cluster, "Upgrading")
        if channel is not None and channel and not CHANNEL_RE.match(channel):
            raise ValueError(f"Not a channel: {channel} (e.g. stable-4.18, fast-4.19, eus-4.18; OKD: stable-4)")
        self.svc._ensure_idle(cluster)
        if channel is not None and channel != (self._summary(self._cv(cluster))["channel"]):
            self._patch_channel(cluster, channel)
        if not version:
            return self.upgrades(db, cluster)
        if not re.match(r"^\d+\.\d+\.\d+(-[a-z0-9.]+)?$", version):
            raise ValueError(f"Not a version: {version}")
        info = self._summary(self._cv(cluster))
        if info["updating"]:
            raise ValueError(f"An upgrade to {info['desired']} is already running")
        if version == info["current"]:
            raise ValueError(f"The cluster already runs {version}")
        if not channel:  # the version must be offered now; with a new channel the task waits for it
            self._offered(info, version, accept_risks)
        task = self.svc._run_task(db, cluster, "upgrade", "Upgrade", self._upgrade_run, version,
                                  accept_risks)
        return {**self.upgrades(db, cluster), "task_id": task.id}

    def _patch_channel(self, cluster: Cluster, channel: str) -> None:
        out = openshift_service.oc(cluster.name, cluster.version, [
            "patch", "clusterversion", "version", "--type", "merge", "-p",
            '{"spec":{"channel":"%s"}}' % channel], 60)
        if out["exitcode"] != 0:
            raise RuntimeError(f"Could not set the channel: {(out['stderr'] or out['stdout']).strip()[-300:]}")

    @staticmethod
    def _offered(info: Dict[str, Any], version: str, accept_risks: bool) -> None:
        if version in info["available"]:
            return
        cond = next((c for c in info["conditional"] if c["version"] == version), None)
        if cond is None:
            hint = (f"; offered: {', '.join(info['available'][:6])}" if info["available"]
                    else f"; nothing is offered on channel '{info['channel'] or '(none)'}'"
                    + (f": {info['retrieved_message']}" if info.get("retrieved_message") else ""))
            raise ValueError(f"{version} is not an update of {info['current']} in channel "
                             f"'{info['channel'] or '(none)'}'{hint}")
        if cond["recommended"] is not True and not accept_risks:
            risks = "; ".join(f"{r['name']}: {r['message']}" for r in cond["risks"]) or cond.get("message") or ""
            raise ValueError(f"{version} is a conditional update with known risks for this cluster ({risks}). "
                             "Set accept_risks to upgrade anyway.")

    def _upgrade_run(self, db: Session, task: Task, cluster_id: int, version: str, accept_risks: bool) -> Dict[str, Any]:
        svc = self.svc

        def body(cluster: Cluster) -> Dict[str, Any]:
            deadline = time.monotonic() + RETRIEVE_TIMEOUT
            while True:  # after a channel change the CVO needs a moment to fetch the graph
                info = self._summary(self._cv(cluster))
                try:
                    self._offered(info, version, accept_risks)
                    break
                except ValueError:
                    if time.monotonic() > deadline:
                        raise
                svc._check(task, cluster, db, 1, f"Waiting for the update service to offer {version}")
                svc._sleep(task, 10)
            args = ["adm", "upgrade", f"--to={version}"]
            if version not in info["available"]:
                args.append("--allow-not-recommended")
            deadline = time.monotonic() + SETTLE_TIMEOUT
            while True:
                out = openshift_service.oc(cluster.name, cluster.version, args, 120)
                text = (out["stderr"] or out["stdout"]).strip()
                if out["exitcode"] == 0:
                    break
                # a cluster operator degraded for a moment (e.g. a node just removed): wait for it to settle
                if "--allow-upgrade-with-warnings" not in text or time.monotonic() > deadline:
                    raise RuntimeError(f"oc adm upgrade refused: {text[-500:]}")
                svc._check(task, cluster, db, 1, "Waiting for the cluster to settle before upgrading: "
                           + " ".join(text.split())[:200])
                svc._sleep(task, 30)
            started = datetime.now(timezone.utc).isoformat()
            self._set_upgrade(db, cluster, {"target": version, "from": info["current"], "state": "running",
                                            "started": started, "message": None})
            return self._follow(db, task, cluster, version)

        return self._guarded_upgrade(db, task, cluster_id, body)

    def _guarded_upgrade(self, db: Session, task: Task, cluster_id: int, body) -> Dict[str, Any]:
        """Failures of the follow-up leave the cluster as it is (ready, the CVO keeps going on its own)"""
        svc = self.svc
        try:
            return svc._guarded(db, task, cluster_id, body, failed_status="ready")
        except Exception as e:
            cluster = svc.get_cluster(db, cluster_id)
            upgrade = ((cluster.spec or {}).get("openshift") or {}).get("upgrade") if cluster else None
            if cluster is not None and upgrade and upgrade.get("state") == "running":
                from app.services.cluster_service import Cancelled
                self._set_upgrade(db, cluster, {**upgrade, "state": "unfollowed" if isinstance(e, Cancelled)
                                                else "failed", "message": "Cancelled: the upgrade goes on in the "
                                                "cluster, the app stopped following it" if isinstance(e, Cancelled)
                                                else str(e)[:500]})
            raise

    def _set_upgrade(self, db: Session, cluster: Cluster, upgrade: Dict[str, Any]) -> None:
        os_spec = dict((cluster.spec or {}).get("openshift") or {})
        os_spec["upgrade"] = upgrade
        cluster.spec = {**cluster.spec, "openshift": os_spec}
        db.commit()
        self.inst.save_spec(cluster)

    def _follow(self, db: Session, task: Task, cluster: Cluster, version: str) -> Dict[str, Any]:
        """Until the ClusterVersion history shows `version` Completed (API outages = node reboots: tolerated)"""
        svc = self.svc
        deadline = time.monotonic() + UPGRADE_TIMEOUT
        last_ok = time.monotonic()
        while True:
            try:
                info = self._summary(self._cv(cluster))
                last_ok = time.monotonic()
            except (RuntimeError, ValueError) as e:
                info = None
                svc._check(task, cluster, db, None, f"Upgrading to {version}: API unreachable (a node is "
                           f"rebooting?) — {str(e)[:120]}")
            if info is not None:
                if info["current"] == version and not info["updating"]:
                    break
                ops = self.inst.cluster_operators(cluster) or []
                done = sum(1 for o in ops if o.get("version") == version)
                pct = info["progress"] if info["progress"] is not None else (100 * done // max(len(ops), 1))
                text = f"Upgrading to {version}: {pct} %, cluster operators {done}/{len(ops)} updated"
                if info["failing"]:
                    text += f" — {info['failing'][:200]}"
                elif info["message"]:
                    text += f" — {info['message'][:200]}"
                svc._check(task, cluster, db, max(2, min(98, pct)), text)
                openshift_service.approve_csrs(cluster.name, cluster.version)  # rotated kubelet certs after reboots
            if time.monotonic() > deadline:
                raise TimeoutError(f"The upgrade to {version} did not complete in {UPGRADE_TIMEOUT // 3600} h "
                                   "(it may still be going on: see the cluster operators)")
            if time.monotonic() - last_ok > 45 * 60:
                raise TimeoutError("The API has been unreachable for 45 min during the upgrade")
            svc._sleep(task, 30)
        old = cluster.version
        cluster.version = version
        upgrade = ((cluster.spec or {}).get("openshift") or {}).get("upgrade") or {}
        os_spec = {**(cluster.spec.get("openshift") or {}), "version": version,
                   "upgrade": {**upgrade, "state": "completed",
                               "completed": datetime.now(timezone.utc).isoformat()}}
        cluster.spec = {**cluster.spec, "openshift": os_spec, "image": f"OpenShift {version}"}
        db.commit()
        self.inst.save_spec(cluster)
        svc._set_status(db, cluster, "ready", None)
        return {"from": old, "version": version}

    def recover(self, db: Session) -> None:
        """At startup: re-attach a follow task to each upgrade a restart interrupted (it went on in the cluster)"""
        for cluster in db.query(Cluster).filter(Cluster.type == "openshift").all():
            upgrade = ((cluster.spec or {}).get("openshift") or {}).get("upgrade") or {}
            if upgrade.get("state") != "running" or task_service.is_running(cluster.task_id):
                continue
            target = upgrade.get("target")
            logger.info(f"Following the interrupted upgrade of {cluster.name} to {target} again")

            def run(db: Session, task: Task, cluster_id: int, version: str) -> Dict[str, Any]:
                return self._guarded_upgrade(db, task, cluster_id,
                                             lambda c: self._follow(db, task, c, version))
            if cluster.status == "provisioning":
                self.svc._set_status(db, cluster, "ready", None)
            self.svc._run_task(db, cluster, "upgrade", "Follow the upgrade of", run, target)


openshift_day2 = OpenShiftDay2()
