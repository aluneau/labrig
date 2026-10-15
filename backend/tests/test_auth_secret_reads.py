"""Viewers must never get secret reads, whatever spelling of the id reaches the endpoint"""
import pytest

from app.database import SessionLocal
from app.models.cluster import Cluster

KUBECONFIG = "apiVersion: v1\nusers: [{name: admin, user: {client-key-data: SECRETKEYDATA}}]\n"


@pytest.fixture(scope="module")
def cluster_id():
    with SessionLocal() as db:
        row = Cluster(name="sec-demo", type="k3s", network="vmm-k-sec-demo", domain="lab.test",
                      kubeconfig=KUBECONFIG, status="ready")
        db.add(row)
        db.commit()
        return row.id


# FastAPI parses these as the int id: the middleware must not only match \d+
SPELLINGS = ["{id}", "+{id}", "{id}.0", "%20{id}", "0{id}"]


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_viewer_kubeconfig_refused(client, viewer_headers, cluster_id, spelling):
    r = client.get(f"/api/v1/clusters/{spelling.format(id=cluster_id)}/kubeconfig", headers=viewer_headers)
    assert r.status_code == 403, r.text
    assert "SECRETKEYDATA" not in r.text


def test_admin_kubeconfig_with_odd_id(client, admin_headers, cluster_id):
    """The odd spelling really reaches the endpoint (so the refusal above is the middleware's)"""
    r = client.get(f"/api/v1/clusters/+{cluster_id}/kubeconfig", headers=admin_headers)
    assert r.status_code == 200 and "SECRETKEYDATA" in r.text


@pytest.mark.parametrize("path", [
    "/api/v1/vms/+1/logs", "/api/v1/clusters/1/kubeconfig/", "/api/v1/vms/1.0/logs",
    "/api/v1/groups/+1/registry/credentials", "/api/v1/groups/1.0/bmc/credentials",
    "/api/v1/groups/+1/wireguard/peers/laptop/config", "/api/v1/clusters/1.0/oc-output/x.tar.gz",
    "/api/v1/clusters/+1/acm/gitops/git-credentials", "/api/v1/clusters/+1/openshift/credentials",
    # passwords inside text the JSON redaction can't see (YAML export, rendered router files)
    "/api/v1/groups/1/export", "/api/v1/groups/+1/export", "/api/v1/groups/1/router/config",
])
def test_viewer_secret_reads_refused(client, viewer_headers, path):
    assert client.get(path, headers=viewer_headers).status_code == 403
