"""GitOps repo layout: a hub with no spoke still has its clusters/<hub> path in git (Argo CD: "app path does not exist")"""
import shutil
import subprocess

import pytest

from app.services.gitops_service import GitOpsService


@pytest.mark.skipif(not shutil.which("git"), reason="git not installed")
def test_empty_hub_dir_is_tracked():
    svc = GitOpsService()
    shutil.rmtree(svc.repo, ignore_errors=True)
    assert svc._ensure_repo()
    svc._keep_hub_dir("hub1")
    svc._git("add", "-A", "clusters")
    assert svc._commit("Hub hub1: refresh the GitOps index")
    tracked = subprocess.run(["git", "-C", str(svc.repo), "ls-files", "clusters/hub1"],
                             capture_output=True, text=True, check=True).stdout.split()
    assert tracked == ["clusters/hub1/README.md"]
    assert svc._spoke_dirs("hub1") == []          # the README is not a spoke
    svc._keep_hub_dir("hub1")                     # idempotent: nothing new to commit
    assert not svc._commit("again")
