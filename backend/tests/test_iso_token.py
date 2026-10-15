"""Public agent ISO URL (/iso/<token>/agent.iso): only the exact token, compared in constant time"""
import types

from app.services import openshift_installer as oi


def test_iso_token(monkeypatch, tmp_path):
    (tmp_path / "agent.x86_64.iso").write_bytes(b"iso")
    clusters = [types.SimpleNamespace(name="a"), types.SimpleNamespace(name="b")]
    tokens = {"a": None, "b": "s3cr3t-token"}
    inst = oi.openshift_installer
    monkeypatch.setattr(inst, "_is_bmh", lambda c: True)
    monkeypatch.setattr(inst, "_iso_token", lambda c: tokens[c.name])
    monkeypatch.setattr(oi.openshift_service, "cluster_dir", lambda name: tmp_path)
    calls = []
    real = oi.hmac.compare_digest
    monkeypatch.setattr(oi.hmac, "compare_digest", lambda a, b: calls.append(1) or real(a, b))
    db = types.SimpleNamespace(query=lambda model: types.SimpleNamespace(all=lambda: clusters))
    assert inst.iso_for_token(db, "s3cr3t-token") == tmp_path / "agent.x86_64.iso"
    assert calls, "token not compared with hmac.compare_digest"
    for wrong in ("s3cr3t-tokeN", "s3cr3t", "", "é"):
        assert inst.iso_for_token(db, wrong) is None
