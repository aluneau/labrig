"""The GitOps source of truth for "deploy a spoke the realistic way": a git repository of the ACM spokes'
provisioning manifests that OpenShift GitOps (Argo CD) on the hub syncs onto the cluster to provision them —
instead of the app applying the CRs with `oc` (docs/openshift.md "GitOps / ZTP").

Three pieces close the loop:
  * this service keeps the repo under DATA_DIR/gitops: one directory per spoke with its apply-ready
    manifests (the exact objects acm_spokes applies, minus the Secrets) + a SiteConfig `ClusterInstance`
    (the ZTP form, for reference) + a kustomization, a commit per change;
  * git_server_service runs an in-lab Gitea the hub's Argo CD can clone over the LAN, and this service
    mirrors the repo into it (sync_spoke pushes on every change);
  * it installs the OpenShift GitOps operator on the hub (finish_argocd_install: Argo CD + a ClusterRole
    letting the application-controller create the ACM provisioning CRs) and applies an Argo CD `Application`
    that syncs `clusters/` (deploy_application).

The app can still apply the CRs directly (the default); GitOps is the alternative `gitops: true` spoke mode.
stdlib only (subprocess git), like the other host-side services.
"""
import logging
import os
import subprocess
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

_ENV = {**os.environ, "GIT_AUTHOR_NAME": "vm-manager", "GIT_AUTHOR_EMAIL": "vm-manager@localhost",
        "GIT_COMMITTER_NAME": "vm-manager", "GIT_COMMITTER_EMAIL": "vm-manager@localhost"}


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
        import shutil
        if not shutil.which("git"):
            return False
        if not (self.repo / ".git").is_dir():
            subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], env=_ENV, timeout=60, check=True)
            readme = self.repo / "README.md"
            readme.write_text("# vm-manager GitOps\n\nACM spoke provisioning manifests, one directory per cluster. "
                              "Point Argo CD (OpenShift GitOps) at `clusters/` or run `oc apply -k clusters/<name>` "
                              "to deploy a spoke.\n")
            self._git("add", "README.md")
            self._git("commit", "-q", "-m", "Initialize GitOps repository")
        return True

    def _spoke_dir(self, name: str) -> Path:
        return self.repo / "clusters" / name

    def _write_root_kustomization(self) -> None:
        """clusters/kustomization.yaml listing every spoke dir, so one Argo CD Application (path clusters/)
        syncs them all and kustomize applies each spoke's manifests.yaml (not its reference SiteConfig)."""
        root = self.repo / "clusters"
        root.mkdir(parents=True, exist_ok=True)
        spokes = sorted(p.name for p in root.glob("*") if (p / "kustomization.yaml").is_file())
        body = "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nresources:\n" + \
               "".join(f"  - {s}\n" for s in spokes)
        (root / "kustomization.yaml").write_text(body)

    # ------------------------------------------------------------------ writes

    def sync_spoke(self, db: Session, cluster: Cluster) -> None:
        """Write a spoke's apply-ready manifests (+ its reference SiteConfig + a kustomization) into the repo,
        commit, and push to the in-lab git server. Best effort: never fail a provision over the git record."""
        import yaml
        from app.services.acm_spokes import spoke_service
        try:
            if not settings.GITOPS_ENABLED or not self._ensure_repo():
                return
            manifests = spoke_service.gitops_manifests(db, cluster)
            d = self._spoke_dir(cluster.name)
            d.mkdir(parents=True, exist_ok=True)
            (d / "manifests.yaml").write_text(
                "# Apply-ready ACM provisioning manifests for spoke "
                f"'{cluster.name}' — Argo CD (OpenShift GitOps) syncs these onto the hub.\n"
                "# The pull-secret and BMC Secret are created by the app, not committed here.\n"
                + "---\n".join(yaml.safe_dump(m, sort_keys=False) for m in manifests))
            try:  # the SiteConfig ClusterInstance (ZTP form), for reference — not applied by the kustomization
                (d / "siteconfig.yaml").write_text(spoke_service.siteconfig_manifest(db, cluster))
            except Exception:
                pass
            (d / "kustomization.yaml").write_text(
                "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nresources:\n"
                "  - manifests.yaml\n")
            self._write_root_kustomization()
            self._git("add", "clusters")
            if self._git("status", "--porcelain").stdout.strip():
                self._git("commit", "-q", "-m", f"Spoke {cluster.name}: provision on {cluster.domain}")
            self._push()
        except Exception as e:  # pragma: no cover - a git hiccup must not break provisioning
            logger.warning(f"GitOps: could not record spoke {cluster.name}: {e}")

    def remove_spoke(self, name: str) -> None:
        try:
            if not settings.GITOPS_ENABLED or not (self.repo / ".git").is_dir():
                return
            d = self._spoke_dir(name)
            if d.is_dir():
                self._git("rm", "-q", "-r", f"clusters/{name}", check=False)
                self._write_root_kustomization()
                self._git("add", "clusters")
                if self._git("status", "--porcelain").stdout.strip():
                    self._git("commit", "-q", "-m", f"Spoke {name}: deleted")
                self._push()
        except Exception as e:  # pragma: no cover
            logger.warning(f"GitOps: could not remove spoke {name}: {e}")

    def _push(self) -> Optional[str]:
        """Mirror the repo into the in-lab git server (best effort); returns the clone URL or None."""
        try:
            from app.services import git_server_service
            if git_server_service.available() is None:
                return git_server_service.push(self.repo)
        except Exception as e:
            logger.warning(f"GitOps: could not push to the in-lab git server: {e}")
        return None

    # ------------------------------------------------------------------ Argo CD on the hub

    def repo_url(self) -> Optional[str]:
        from app.services import git_server_service
        try:
            return git_server_service.clone_url()
        except Exception:
            return None

    def application_manifest(self, hub: Cluster, repo_url: Optional[str] = None) -> str:
        """An Argo CD Application that syncs the spoke manifests onto the hub. repoURL defaults to the in-lab
        git server's clone URL (a placeholder until the git server is up)."""
        import yaml
        url = repo_url or self.repo_url() or "<in-lab git server not started yet>"
        app = {
            "apiVersion": "argoproj.io/v1alpha1", "kind": "Application",
            "metadata": {"name": f"{hub.name}-spokes", "namespace": ARGOCD_NS},
            "spec": {
                "project": "default",
                "source": {"repoURL": url, "targetRevision": "main", "path": "clusters"},
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
        return f"{console}/applications/{ARGOCD_NS}/{hub.name}-spokes" if console else None

    def deploy_application(self, db: Session, hub: Cluster) -> Dict[str, Any]:
        """Push the repo to the in-lab git server and apply the Argo CD Application that syncs the spokes.
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
        self._write_root_kustomization()
        self._git("add", "clusters", check=False)
        if self._git("status", "--porcelain").stdout.strip():
            self._git("commit", "-q", "-m", "Refresh GitOps index")
        url = self._push()
        if not url:
            raise ValueError("Could not push the GitOps repo to the in-lab git server")
        out = runner.oc(["apply", "-f", "-"], 60, stdin=self.application_manifest(hub, url))
        if out["exitcode"] != 0:
            raise RuntimeError(f"Could not apply the Argo CD Application: {(out['stderr'] or '').strip()[-300:]}")
        # Re-applying the (unchanged) Application CR does NOT re-run a failed / retry-exhausted Argo op: force a
        # fresh sync operation so a re-sync always re-attempts against the latest commit.
        import json
        op = {"operation": {"initiatedBy": {"username": "vmm"},
                            "sync": {"revision": "main", "syncStrategy": {"hook": {}}}}}
        patch = runner.oc(["patch", "applications.argoproj.io", f"{hub.name}-spokes", "-n", ARGOCD_NS,
                           "--type", "merge", "-p", json.dumps(op)], 60)
        if patch["exitcode"] != 0:
            raise RuntimeError(f"Could not trigger an Argo CD sync: {(patch['stderr'] or '').strip()[-300:]}")
        return self.status(db, hub)

    def _application_state(self, runner: Any, hub: Cluster,
                           console: Optional[str] = None) -> Optional[Dict[str, Any]]:
        # Fully qualify: ACM/MCE also registers applications.app.k8s.io, which shadows the "application" short name.
        app = runner.oc(["get", "applications.argoproj.io", f"{hub.name}-spokes", "-n", ARGOCD_NS, "-o", "json"], 30)
        if app["exitcode"] != 0:
            return None
        import json
        try:
            status = (json.loads(app["stdout"]).get("status") or {})
        except ValueError:
            return None
        return {"name": f"{hub.name}-spokes",
                "sync": (status.get("sync") or {}).get("status"),
                "health": (status.get("health") or {}).get("status"),
                "revision": ((status.get("sync") or {}).get("revision") or "")[:7],
                "repo_url": self.repo_url(),
                "url": self.application_url(console, hub)}

    # ------------------------------------------------------------------ views

    def log(self, limit: int = 20) -> List[Dict[str, str]]:
        if not (self.repo / ".git").is_dir():
            return []
        out = self._git("log", f"-{limit}", "--pretty=format:%h\x1f%cI\x1f%s", check=False).stdout
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
        import shutil
        from app.services import git_server_service
        enabled = bool(settings.GITOPS_ENABLED) and shutil.which("git") is not None
        exists = (self.repo / ".git").is_dir()
        spokes = sorted(p.name for p in (self.repo / "clusters").glob("*")
                        if (p / "kustomization.yaml").is_file()) if (self.repo / "clusters").is_dir() else []
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
        return {
            "enabled": enabled, "initialized": exists, "repo_path": str(self.repo),
            "clone_url": self.repo_url(), "spokes": spokes, "commits": self.log(10),
            "argocd_installed": argocd_installed,
            "argocd_url": argocd_url,
            "git_server": git_server_service.status(),
            "application": application,
            "application_manifest": self.application_manifest(hub),
            "apply_command": "oc apply -k clusters/<name>   # from a clone of the repo",
        }


gitops_service = GitOpsService()
