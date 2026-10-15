"""The GitOps source of truth for "deploy a spoke the realistic way": a git repository of the ACM spokes'
provisioning manifests that OpenShift GitOps (Argo CD) on the hub syncs onto the cluster to provision them —
instead of the app applying the CRs with `oc` (docs/openshift.md "GitOps / ZTP").

Three pieces close the loop:
  * this service keeps the repo under DATA_DIR/gitops: `clusters/<hub>/` per hub (each hub's Argo CD syncs
    only its own directory), one `<spoke>/` directory per GitOps spoke with its apply-ready `manifests.yaml`
    (the exact objects acm_spokes applies, minus the Secrets), the ClusterImageSets once per release in
    `imagesets/` (spokes of one release share it), a commit per change. No index file: the Application is a
    recursive directory source (`*/manifests.yaml` + `imagesets/*.yaml`), so deleting a spoke's directory in
    git is all it takes to deprovision it;
  * git_server_service runs an in-lab Gitea the hub's Argo CD can clone over the LAN, and this service
    mirrors the repo into it. Commits pushed to Gitea by a user are merged first (never force-pushed away);
  * it installs the OpenShift GitOps operator on the hub (finish_argocd_install: Argo CD + a ClusterRole
    letting the application-controller create the ACM provisioning CRs) and applies an Argo CD `Application`
    that syncs `clusters/<hub>` (deploy_application).

The app can still apply the CRs directly (the default); GitOps is the alternative `gitops: true` spoke mode, and
only GitOps spokes are in the synced tree (a direct spoke in it would be managed twice, and Argo CD's self-heal
would re-create it while the app deletes it). stdlib only (subprocess git), like the other host-side services.
"""
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.models import Cluster

logger = logging.getLogger(__name__)

ARGOCD_NS = "openshift-gitops"
ARGOCD_OPERATOR = "openshift-gitops-operator"
APP_CONTROLLER_STS = "openshift-gitops-application-controller"           # the StatefulSet
APP_CONTROLLER_SA = "openshift-gitops-argocd-application-controller"    # the ServiceAccount it runs as
SPOKES_CLUSTERROLE = "vmm-gitops-spokes"
# A ClusterImageSet is shared (spokes of one release, direct-apply spokes too): Argo CD never deletes it
NO_PRUNE = {"argocd.argoproj.io/sync-options": "Prune=false"}

_ENV = {**os.environ, "GIT_AUTHOR_NAME": "vm-manager", "GIT_AUTHOR_EMAIL": "vm-manager@localhost",
        "GIT_COMMITTER_NAME": "vm-manager", "GIT_COMMITTER_EMAIL": "vm-manager@localhost"}
_lock = threading.RLock()  # one writer of the repo at a time (two spoke tasks, a delete, a deploy)


class GitOpsService:
    @property
    def repo(self) -> Path:
        path = Path(settings.DATA_DIR) / "gitops"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, text=True,
                              env=_ENV, timeout=60, check=check)

    def _ensure_repo(self) -> bool:
        """git init the repo on first use. False (disabled) if git isn't installed."""
        if not shutil.which("git"):
            return False
        if not (self.repo / ".git").is_dir():
            subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], env=_ENV, timeout=60, check=True)
            readme = self.repo / "README.md"
            readme.write_text("# vm-manager GitOps\n\nACM spoke provisioning manifests: `clusters/<hub>/<spoke>/`. "
                              "Each hub's Argo CD (OpenShift GitOps) syncs `clusters/<hub>`; deleting a spoke's "
                              "directory deprovisions it. Secrets (pull secret, BMC passwords) are never here.\n")
            self._git("add", "README.md")
            self._git("commit", "-q", "-m", "Initialize GitOps repository")
        return True

    def _hub_dir(self, hub_name: str) -> Path:
        return self.repo / "clusters" / hub_name

    def _spoke_dirs(self, hub_name: str) -> List[str]:
        d = self._hub_dir(hub_name)
        return sorted(p.name for p in d.glob("*") if (p / "manifests.yaml").is_file()) if d.is_dir() else []

    def _migrate_layout(self, db: Session) -> None:
        """Older layout: clusters/<spoke>/ for every hub + clusters/kustomization.yaml (synced by every hub's
        Application, so a second hub would have provisioned the first hub's spokes). Move each spoke under its
        hub's directory; drop the kustomization indexes (a root one makes Argo CD use kustomize)."""
        from app.services.acm_spokes import provisioner
        root = self.repo / "clusters"
        if not root.is_dir():
            return
        moved = False
        for d in sorted(root.glob("*")):
            if not (d / "manifests.yaml").is_file():
                continue  # a hub directory
            cluster = db.query(Cluster).filter(Cluster.name == d.name).first()
            hub_name = (provisioner(cluster) or {}).get("hub_name") if cluster is not None else None
            if not hub_name:
                continue
            self._hub_dir(hub_name).mkdir(parents=True, exist_ok=True)
            self._git("mv", f"clusters/{d.name}", f"clusters/{hub_name}/{d.name}")
            moved = True
        for index in [root / "kustomization.yaml"] + list(root.glob("*/kustomization.yaml")):
            index.unlink()
            moved = True
        if moved:
            self._git("add", "-A", "clusters")
            self._commit("Move spokes under their hub's directory (one Argo CD Application per hub)")

    def _commit(self, message: str) -> bool:
        if self._git("status", "--porcelain").stdout.strip():
            self._git("commit", "-q", "-m", message)
            return True
        return False

    # ------------------------------------------------------------------ writes

    def sync_spoke(self, db: Session, cluster: Cluster, strict: bool = False) -> None:
        """Write a GitOps spoke's apply-ready manifests (+ its reference SiteConfig, real hosts) into the
        repo, commit, and push to the in-lab git server. strict (the provisioning task) raises when the commit or
        the push fails: Argo CD would never see the spoke. Else best effort (a record only)."""
        import yaml
        from app.services.acm_spokes import provisioner, spoke_service
        try:
            if not settings.GITOPS_ENABLED or not self._ensure_repo():
                if strict:
                    raise RuntimeError("GitOps is disabled on this host (GITOPS_ENABLED=false or git missing)")
                return
            hub_name = (provisioner(cluster) or {}).get("hub_name")
            if not hub_name:
                raise ValueError(f"{cluster.name} is not an ACM spoke")
            manifests = spoke_service.gitops_manifests(db, cluster)
            with _lock:
                self._pull()
                self._migrate_layout(db)
                hub_dir = self._hub_dir(hub_name)
                d = hub_dir / cluster.name
                d.mkdir(parents=True, exist_ok=True)
                imagesets = [m for m in manifests if m.get("kind") == "ClusterImageSet"]
                rest = [m for m in manifests if m.get("kind") != "ClusterImageSet"]
                for m in imagesets:
                    m = {**m, "metadata": {**m["metadata"], "annotations": dict(NO_PRUNE)}}
                    (hub_dir / "imagesets").mkdir(parents=True, exist_ok=True)
                    (hub_dir / "imagesets" / f"{m['metadata']['name']}.yaml").write_text(
                        yaml.safe_dump(m, sort_keys=False))
                (d / "manifests.yaml").write_text(
                    "# Apply-ready ACM provisioning manifests for spoke "
                    f"'{cluster.name}' — Argo CD (OpenShift GitOps) on hub '{hub_name}' syncs these.\n"
                    "# The pull-secret and BMC Secret are created by the app, not committed here.\n"
                    "# Delete this directory to deprovision the spoke (Argo CD prunes its objects on the hub).\n"
                    f"# By hand: oc apply -f clusters/{hub_name}/{cluster.name}/manifests.yaml\n"
                    + "---\n".join(yaml.safe_dump(m, sort_keys=False) for m in rest))
                try:  # the SiteConfig ClusterInstance (ZTP form), for reference — not synced (Application include)
                    (d / "siteconfig.yaml").write_text(spoke_service.siteconfig_manifest(db, cluster))
                except Exception:
                    pass
                self._git("add", "-A", "clusters")
                self._commit(f"Spoke {cluster.name}: provision from hub {hub_name} on {cluster.domain}")
                url = self._push(strict=strict)
            if strict and not url:
                raise RuntimeError("Could not push the GitOps repo to the in-lab git server")
        except Exception as e:
            if strict:
                raise
            logger.warning(f"GitOps: could not record spoke {cluster.name}: {e}")

    def remove_spoke(self, name: str, hub_name: Optional[str]) -> bool:
        """Delete a spoke's directory (one commit) and push. True when it was in git (Argo CD will prune it)."""
        try:
            if not settings.GITOPS_ENABLED or not hub_name or not (self.repo / ".git").is_dir():
                return False
            with _lock:
                self._pull()
                d = self._hub_dir(hub_name) / name
                legacy = self.repo / "clusters" / name
                if not d.is_dir() and not (legacy / "manifests.yaml").is_file():
                    return False
                self._git("rm", "-q", "-r", "--ignore-unmatch",
                          f"clusters/{hub_name}/{name}" if d.is_dir() else f"clusters/{name}", check=False)
                shutil.rmtree(d if d.is_dir() else legacy, ignore_errors=True)
                self._git("add", "-A", "clusters")
                self._commit(f"Spoke {name}: deleted")
                self._push()
            return True
        except Exception as e:  # pragma: no cover
            logger.warning(f"GitOps: could not remove spoke {name}: {e}")
            return False

    def _pull(self) -> None:
        """Merge commits pushed to the in-lab git server (a user editing / deleting a spoke there) before the app
        commits: the app's push must never throw them away. App files win a conflict; an unmergeable history is
        left alone (the push then keeps the server's tip as a backup branch)."""
        from app.services import git_server_service
        if git_server_service.available() is not None or not git_server_service.running():
            return
        try:
            url = git_server_service.push_url()
        except Exception as e:
            logger.warning(f"GitOps: in-lab git server not ready: {e}")
            return
        out = self._git("fetch", "-q", url, "main", check=False)
        if out.returncode != 0:
            return  # empty repository on the server (never pushed)
        if self._git("merge-base", "--is-ancestor", "FETCH_HEAD", "HEAD", check=False).returncode == 0:
            return  # nothing new on the server
        merge = self._git("merge", "-q", "--no-edit", "-X", "ours", "-m",
                          "Merge changes pushed to the in-lab git server", "FETCH_HEAD", check=False)
        if merge.returncode != 0:
            self._git("merge", "--abort", check=False)
            logger.warning("GitOps: could not merge the commits pushed to the in-lab git server: "
                           f"{git_server_service.redact((merge.stderr or merge.stdout).strip()[-300:])}")

    def _push(self, strict: bool = False) -> Optional[str]:
        """Mirror the repo into the in-lab git server; returns the clone URL or None (best effort unless strict)."""
        try:
            from app.services import git_server_service
            if git_server_service.available() is None:
                return git_server_service.push(self.repo)
        except Exception as e:
            if strict:
                raise
            logger.warning(f"GitOps: could not push to the in-lab git server: {e}")
        return None

    def server_spokes(self, hub_name: str) -> Optional[List[str]]:
        """The spokes of a hub as the in-lab git server has them (what Argo CD syncs: a user may have added or
        deleted one there). None when the server isn't running / has nothing yet."""
        from app.services import git_server_service
        if not (self.repo / ".git").is_dir() or git_server_service.available() is not None \
                or not git_server_service.running():
            return None
        ref = "refs/remotes/gitea/main"
        with _lock:
            try:
                fetched = self._git("fetch", "-q", git_server_service.push_url(), f"+main:{ref}", check=False)
            except Exception:
                return None
            if fetched.returncode != 0:
                return None
            out = self._git("ls-tree", "-r", "--name-only", ref, f"clusters/{hub_name}/", check=False)
        prefix = f"clusters/{hub_name}/"
        return sorted(p[len(prefix):-len("/manifests.yaml")] for p in out.stdout.splitlines()
                      if p.startswith(prefix) and p.endswith("/manifests.yaml") and p.count("/") == 3)

    def head(self) -> Optional[str]:
        out = self._git("rev-parse", "HEAD", check=False)
        return out.stdout.strip() if out.returncode == 0 else None

    # ------------------------------------------------------------------ Argo CD on the hub

    def repo_url(self) -> Optional[str]:
        from app.services import git_server_service
        try:
            return git_server_service.clone_url()
        except Exception:
            return None

    @staticmethod
    def app_name(hub: Cluster) -> str:
        return f"{hub.name}-spokes"

    def application_manifest(self, hub: Cluster, repo_url: Optional[str] = None) -> str:
        """An Argo CD Application that syncs this hub's spoke manifests (clusters/<hub>) onto it. repoURL
        defaults to the in-lab git server's clone URL (a placeholder until the git server is up)."""
        import yaml
        url = repo_url or self.repo_url() or "<in-lab git server not started yet>"
        app = {
            "apiVersion": "argoproj.io/v1alpha1", "kind": "Application",
            "metadata": {"name": self.app_name(hub), "namespace": ARGOCD_NS},
            "spec": {
                "project": "default",
                # a directory source, no index to keep in step: deleting a spoke's directory deprovisions it
                "source": {"repoURL": url, "targetRevision": "main", "path": f"clusters/{hub.name}",
                           "directory": {"recurse": True, "include": "{imagesets/*.yaml,*/manifests.yaml}"}},
                "destination": {"server": "https://kubernetes.default.svc"},
                "syncPolicy": {"automated": {"prune": True, "selfHeal": True},
                               "syncOptions": ["CreateNamespace=true"]},
                # ACM owns the ManagedCluster labels (cloud / vendor / feature.* are auto-detected): don't let
                # Argo CD fight the controller over them.
                "ignoreDifferences": [{"group": "cluster.open-cluster-management.io", "kind": "ManagedCluster",
                                       "jsonPointers": ["/metadata/labels"]}],
            },
        }
        return ("# Argo CD Application: deploy the ACM spokes from git. The app applies this once the OpenShift\n"
                "# GitOps operator is installed and the repo is pushed to the in-lab git server.\n"
                + yaml.safe_dump(app, sort_keys=False))

    def _spoke_clusterrole(self) -> List[Dict[str, Any]]:
        """A ClusterRole + binding letting Argo CD's application-controller create the ACM provisioning CRs."""
        groups = ["hive.openshift.io", "extensions.hive.openshift.io", "agent-install.openshift.io",
                  "metal3.io", "cluster.open-cluster-management.io", "agent.open-cluster-management.io",
                  "siteconfig.open-cluster-management.io"]
        rules = [{"apiGroups": [""], "resources": ["namespaces", "secrets", "configmaps", "serviceaccounts"],
                  "verbs": ["*"]}]
        rules += [{"apiGroups": [g], "resources": ["*"], "verbs": ["*"]} for g in groups]
        # ACM's admission webhook requires `update` on the managedclusters/accept subresource to set
        # HubAcceptsClient; without it Argo CD's sync of the ManagedCluster is forbidden -> OutOfSync.
        rules.append({"apiGroups": ["register.open-cluster-management.io"],
                      "resources": ["managedclusters/accept"], "verbs": ["update"]})
        return [
            {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole",
             "metadata": {"name": SPOKES_CLUSTERROLE}, "rules": rules},
            {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding",
             "metadata": {"name": SPOKES_CLUSTERROLE},
             "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": SPOKES_CLUSTERROLE},
             "subjects": [{"kind": "ServiceAccount", "name": APP_CONTROLLER_SA, "namespace": ARGOCD_NS}]},
        ]

    def finish_argocd_install(self, runner: Any) -> None:
        """After install_operator("openshift-gitops-operator"): wait for the default Argo CD instance to come
        up and grant its application-controller rights over the ACM provisioning CRs. Called from the `gitops`
        day-2 add-on (openshift_installer.run_addon)."""
        deadline = time.monotonic() + 15 * 60
        while runner.oc(["get", "crd", "applications.argoproj.io"], 30)["exitcode"] != 0:
            if time.monotonic() > deadline:
                raise TimeoutError("The Argo CD CRDs did not appear after the operator install")
            runner.sleep(10)
        runner.log("Argo CD: waiting for the application-controller")
        while True:
            ready = runner.oc(["get", "statefulset", APP_CONTROLLER_STS, "-n", ARGOCD_NS,
                               "-o", "jsonpath={.status.readyReplicas}"], 30)
            if ready["exitcode"] == 0 and (ready["stdout"].strip() or "0").isdigit() \
                    and int(ready["stdout"].strip() or 0) >= 1:
                break
            if time.monotonic() > deadline:
                raise TimeoutError("The Argo CD application-controller did not become ready")
            runner.sleep(10)
        runner.log("Argo CD: granting the application-controller rights over the ACM provisioning CRs")
        runner.apply_retry(self._spoke_clusterrole(), "Argo CD RBAC for spokes")

    def argocd_installed(self, runner: Any) -> bool:
        return runner.oc(["get", "crd", "applications.argoproj.io"], 30)["exitcode"] == 0

    def argocd_url(self, runner: Any) -> Optional[str]:
        """The Argo CD web console URL = the openshift-gitops-server Route on the hub (apps.<hub>, which the host
        DNS resolver now serves). None until the Route exists."""
        out = runner.oc(["get", "route", "openshift-gitops-server", "-n", ARGOCD_NS,
                         "-o", "jsonpath={.spec.host}"], 30)
        host = (out.get("stdout") or "").strip() if out["exitcode"] == 0 else ""
        return f"https://{host}" if host else None

    def application_url(self, console: Optional[str], hub: Cluster) -> Optional[str]:
        """Deep-link into the Argo CD console for this hub's spokes Application."""
        return f"{console}/applications/{ARGOCD_NS}/{self.app_name(hub)}" if console else None

    def _force_sync(self, runner: Any, hub: Cluster) -> None:
        """Start a sync operation against the branch tip now (Argo CD polls git every 3 min, and re-applying an
        unchanged Application does NOT re-run a failed / retry-exhausted operation)."""
        op = {"operation": {"initiatedBy": {"username": "vmm"},
                            "sync": {"revision": "main", "prune": True, "syncStrategy": {"hook": {}}}}}
        patch = runner.oc(["patch", "applications.argoproj.io", self.app_name(hub), "-n", ARGOCD_NS,
                           "--type", "merge", "-p", json.dumps(op)], 60)
        if patch["exitcode"] != 0:
            raise RuntimeError(f"Could not trigger an Argo CD sync: {(patch['stderr'] or '').strip()[-300:]}")

    def deploy_application(self, db: Session, hub: Cluster) -> Dict[str, Any]:
        """Push the repo to the in-lab git server and apply the Argo CD Application that syncs the hub's spokes.
        Returns the GitOps status (incl. the Application sync / health)."""
        from app.services.acm import acm_service
        from app.services import git_server_service
        if self.svc.to_dict(db, hub)["status"] != "ready":
            raise ValueError(f"Hub {hub.name} is not ready")
        runner = acm_service._runner(hub)
        if not self.argocd_installed(runner):
            raise ValueError("Install OpenShift GitOps (Argo CD) on the hub first")
        problem = git_server_service.available()
        if problem:
            raise ValueError(problem)
        if not self._ensure_repo():
            raise ValueError("git is not installed on the host")
        git_server_service.ensure_ready()
        with _lock:
            self._pull()
            self._migrate_layout(db)
            self._git("add", "-A", "clusters", check=False)
            self._commit(f"Hub {hub.name}: refresh the GitOps index")
            url = self._push()
        if not url:
            raise ValueError("Could not push the GitOps repo to the in-lab git server")
        out = runner.oc(["apply", "-f", "-"], 60, stdin=self.application_manifest(hub, url))
        if out["exitcode"] != 0:
            raise RuntimeError(f"Could not apply the Argo CD Application: {(out['stderr'] or '').strip()[-300:]}")
        self._force_sync(runner, hub)
        return self.status(db, hub)

    def wait_pruned(self, runner: Any, hub: Cluster, name: str, timeout: int = 5 * 60) -> bool:
        """After remove_spoke: make Argo CD sync the new commit now and wait until a sync of that commit has run
        (it pruned the spoke: its objects are no longer desired), so deleting what is left with oc can't be undone
        by self-heal. False on timeout (logged): the caller deletes anyway."""
        if self._application_state(runner, hub) is None:
            return True  # no Application: nothing syncs this hub from git
        want = self.head()
        deadline = time.monotonic() + timeout
        kicked = 0.0
        while time.monotonic() < deadline:
            if time.monotonic() - kicked > 60:
                try:
                    self._force_sync(runner, hub)
                except RuntimeError as e:
                    logger.warning(f"GitOps: {e}")
                kicked = time.monotonic()
            status: Dict[str, Any] = {}
            app = runner.oc(["get", "applications.argoproj.io", self.app_name(hub), "-n", ARGOCD_NS,
                             "-o", "json"], 30)
            if app["exitcode"] == 0:
                try:
                    status = json.loads(app["stdout"]).get("status") or {}
                except ValueError:
                    pass
            op = status.get("operationState") or {}
            if ((op.get("syncResult") or {}).get("revision") == want
                    and op.get("phase") in ("Succeeded", "Failed", "Error")):
                return True
            runner.sleep(5)
        logger.warning(f"GitOps: Argo CD did not sync the removal of spoke {name} within {timeout // 60} min")
        return False

    def _application_state(self, runner: Any, hub: Cluster,
                           console: Optional[str] = None) -> Optional[Dict[str, Any]]:
        # Fully qualify: ACM/MCE also registers applications.app.k8s.io, which shadows the "application" short name.
        app = runner.oc(["get", "applications.argoproj.io", self.app_name(hub), "-n", ARGOCD_NS, "-o", "json"], 30)
        if app["exitcode"] != 0:
            return None
        try:
            body = json.loads(app["stdout"])
        except ValueError:
            return None
        status = body.get("status") or {}
        op = status.get("operationState") or {}
        return {"name": self.app_name(hub),
                "sync": (status.get("sync") or {}).get("status"),
                "health": (status.get("health") or {}).get("status"),
                "revision": ((status.get("sync") or {}).get("revision") or "")[:7],
                "path": ((body.get("spec") or {}).get("source") or {}).get("path"),
                "operation": op.get("phase"),
                "message": (op.get("message") or "")[:300] or None,
                "repo_url": self.repo_url(),
                "url": self.application_url(console, hub),
                "reconciled_at": status.get("reconciledAt"),
                "operation": self._operation_view(status.get("operationState") or {}),
                "spokes": self._spoke_resources(status.get("resources") or [])}

    @staticmethod
    def _operation_view(op: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """The last Argo CD sync operation, for the UI: phase (Running | Succeeded | Failed | Error) + its message."""
        if not op:
            return None
        return {"phase": op.get("phase"), "message": (op.get("message") or "")[:600],
                "started_at": op.get("startedAt"), "finished_at": op.get("finishedAt")}

    def _spoke_resources(self, resources: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """Per-spoke rollup of the Application's resources (namespaced ones by namespace, cluster-scoped ones such
        as the ManagedCluster by name): worst sync / health state + resource count."""
        spokes = set(p.name for p in (self.repo / "clusters").glob("*")) if (self.repo / "clusters").is_dir() else set()
        bad_health = ["Missing", "Degraded", "Progressing", "Suspended", "Unknown"]
        out: Dict[str, Dict[str, Any]] = {}
        for r in resources:
            name = r.get("namespace") if r.get("namespace") in spokes else r.get("name") if r.get("name") in spokes else None
            if not name:
                continue
            e = out.setdefault(name, {"sync": "Synced", "health": "Healthy", "resources": 0, "out_of_sync": 0})
            e["resources"] += 1
            if r.get("status") and r.get("status") != "Synced":
                e["out_of_sync"] += 1
                e["sync"] = r.get("status")
            h = (r.get("health") or {}).get("status")
            if h in bad_health and (e["health"] not in bad_health or bad_health.index(h) < bad_health.index(e["health"])):
                e["health"] = h
        return out

    # ------------------------------------------------------------------ views

    def log(self, limit: int = 20, ref: str = "HEAD") -> List[Dict[str, str]]:
        if not (self.repo / ".git").is_dir():
            return []
        out = self._git("log", f"-{limit}", "--pretty=format:%h\x1f%cI\x1f%s", ref, "--", check=False).stdout
        rows = []
        for line in out.splitlines():
            parts = line.split("\x1f")
            if len(parts) == 3:
                rows.append({"commit": parts[0], "date": parts[1], "message": parts[2]})
        return rows

    @property
    def svc(self):
        from app.services.cluster_service import cluster_service
        return cluster_service

    def status(self, db: Session, hub: Cluster) -> Dict[str, Any]:
        """The GitOps repo + in-lab git server + Argo CD state for the hub's ACM tab."""
        from app.services import git_server_service
        enabled = bool(settings.GITOPS_ENABLED) and shutil.which("git") is not None
        exists = (self.repo / ".git").is_dir()
        argocd_installed = False
        argocd_url: Optional[str] = None
        application: Optional[Dict[str, Any]] = None
        try:
            from app.services.acm import acm_service
            if self.svc.to_dict(db, hub)["status"] == "ready":
                runner = acm_service._runner(hub)
                argocd_installed = self.argocd_installed(runner)
                if argocd_installed:
                    argocd_url = self.argocd_url(runner)
                    application = self._application_state(runner, hub, argocd_url)
        except Exception:
            pass
        on_server = self.server_spokes(hub.name)
        spokes = on_server if on_server is not None else self._spoke_dirs(hub.name)
        # GitOps spokes of this hub whose directory is gone from git (deleted there by a user): Argo CD pruned
        # them from the hub; the app's cluster row stays until it is deleted (which then just frees the machine)
        from app.services.acm_spokes import provisioner
        missing = sorted(c.name for c in db.query(Cluster).all()
                         if (provisioner(c) or {}).get("hub_id") == hub.id and (c.spec or {}).get("gitops")
                         and c.status not in ("provisioning", "deleting") and c.name not in spokes)
        return {
            "enabled": enabled, "initialized": exists, "repo_path": str(self.repo),
            "path": f"clusters/{hub.name}",
            "clone_url": self.repo_url(), "spokes": spokes, "missing_from_git": missing,
            "commits": self.log(10, "refs/remotes/gitea/main" if on_server is not None else "HEAD"),
            "head": (self.head() or "")[:7] if exists else None,
            "argocd_installed": argocd_installed,
            "argocd_url": argocd_url,
            "git_server": git_server_service.status(),
            "application": application,
            "application_manifest": self.application_manifest(hub),
            "apply_command": f"oc apply -f clusters/{hub.name}/<spoke>/manifests.yaml   # from a clone of the repo",
        }


gitops_service = GitOpsService()
