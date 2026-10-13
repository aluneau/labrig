"""A git repository of the ACM spoke manifests: the GitOps source of truth for "deploy a cluster from git".

The app can apply the spoke CRs to the hub directly (see acm_spokes), but customers drive their fleet from git:
a repo of SiteConfig `ClusterInstance` manifests that Argo CD (OpenShift GitOps) — or `oc apply -k` — syncs
onto the hub. This service keeps that repo under DATA_DIR/gitops: one directory per spoke with its
ClusterInstance (+ a kustomization and a BMC Secret stub, password never committed), a commit per change.

What it does NOT do: it does not install Argo CD, nor create/apply an Argo CD `Application` on the hub (the
OpenShift GitOps operator is a prerequisite that is not assumed here). `application_manifest()` renders the
Application to apply once that operator is installed, and the repo can be served / pushed to any git remote the
GitOps controller can reach. stdlib only (subprocess git), like the other host-side services.
"""
import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.models import Cluster

logger = logging.getLogger(__name__)

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
            readme.write_text("# vm-manager GitOps\n\nSiteConfig `ClusterInstance` manifests for ACM spokes, one "
                              "directory per cluster. Point Argo CD (OpenShift GitOps) at `clusters/` or run "
                              "`oc apply -k clusters/<name>` to deploy.\n")
            self._git("add", "README.md")
            self._git("commit", "-q", "-m", "Initialize GitOps repository")
        return True

    def _spoke_dir(self, name: str) -> Path:
        return self.repo / "clusters" / name

    # ------------------------------------------------------------------ writes

    def sync_spoke(self, db: Session, cluster: Cluster) -> None:
        """Write a spoke's ClusterInstance (+ kustomization + BMC Secret stub) into the repo and commit. Best
        effort: never fails a provision over the git record."""
        from app.services.acm_spokes import spoke_service
        try:
            if not settings.GITOPS_ENABLED or not self._ensure_repo():
                return
            manifest = spoke_service.siteconfig_manifest(db, cluster)
            d = self._spoke_dir(cluster.name)
            d.mkdir(parents=True, exist_ok=True)
            (d / "clusterinstance.yaml").write_text(manifest)
            (d / "kustomization.yaml").write_text(
                "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nresources:\n"
                "  - clusterinstance.yaml\n")
            self._git("add", "clusters")
            if self._git("status", "--porcelain").stdout.strip():
                self._git("commit", "-q", "-m", f"Spoke {cluster.name}: provision on {cluster.domain}")
        except Exception as e:  # pragma: no cover - a git hiccup must not break provisioning
            logger.warning(f"GitOps: could not record spoke {cluster.name}: {e}")

    def remove_spoke(self, name: str) -> None:
        try:
            if not settings.GITOPS_ENABLED or not (self.repo / ".git").is_dir():
                return
            d = self._spoke_dir(name)
            if d.is_dir():
                self._git("rm", "-q", "-r", f"clusters/{name}", check=False)
                if self._git("status", "--porcelain").stdout.strip():
                    self._git("commit", "-q", "-m", f"Spoke {name}: deleted")
        except Exception as e:  # pragma: no cover
            logger.warning(f"GitOps: could not remove spoke {name}: {e}")

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

    def status(self, db: Session, hub: Cluster) -> Dict[str, Any]:
        """The GitOps repo state for the hub's ACM tab: path, recent commits, the spokes it tracks, and the
        Argo CD Application to deploy them once the OpenShift GitOps operator is installed."""
        import shutil
        enabled = bool(settings.GITOPS_ENABLED) and shutil.which("git") is not None
        exists = (self.repo / ".git").is_dir()
        spokes = sorted(p.name for p in (self.repo / "clusters").glob("*")) if (self.repo / "clusters").is_dir() else []
        argocd_installed = False
        try:
            from app.services.acm import acm_service
            if self.svc.to_dict(db, hub)["status"] == "ready":
                runner = acm_service._runner(hub)
                argocd_installed = runner.oc(["get", "crd", "applications.argoproj.io"], 30)["exitcode"] == 0
        except Exception:
            pass
        return {
            "enabled": enabled, "initialized": exists, "repo_path": str(self.repo),
            "clone_url": f"file://{self.repo}", "spokes": spokes, "commits": self.log(10),
            "argocd_installed": argocd_installed,
            "apply_command": "oc apply -k clusters/<name>   # from a clone of the repo",
            "application": self.application_manifest(hub),
        }

    @property
    def svc(self):
        from app.services.cluster_service import cluster_service
        return cluster_service

    def application_manifest(self, hub: Cluster, repo_url: str = "<your-git-repo-url>") -> str:
        """An Argo CD Application that syncs the spoke manifests onto the hub. Apply it after installing the
        OpenShift GitOps (Argo CD) operator and pushing this repo to a reachable git remote."""
        import yaml
        app = {
            "apiVersion": "argoproj.io/v1alpha1", "kind": "Application",
            "metadata": {"name": f"{hub.name}-spokes", "namespace": "openshift-gitops"},
            "spec": {
                "project": "default",
                "source": {"repoURL": repo_url, "targetRevision": "main", "path": "clusters"},
                "destination": {"server": "https://kubernetes.default.svc"},
                "syncPolicy": {"automated": {"prune": True, "selfHeal": True},
                               "syncOptions": ["CreateNamespace=true"]},
            },
        }
        return ("# Deploy spokes from git: install the OpenShift GitOps operator, push this repo to a git remote\n"
                "# the Argo CD controller can reach, set repoURL below, then `oc apply -f` this Application.\n"
                + yaml.safe_dump(app, sort_keys=False))


gitops_service = GitOpsService()
