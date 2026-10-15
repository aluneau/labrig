"""Login: no password oracle for accounts outside the VM Manager groups, backoff that IP rotation can't skip"""
import types

import pytest

from app.api.v1.endpoints import auth as auth_endpoint
from app.services import auth_service

CSRF = {"X-VMM-Request": "1"}


@pytest.fixture
def pam(monkeypatch):
    """Fake PAM: every account's password is 'right'; records whose password was checked"""
    checked = []

    def check(username, password):
        checked.append(username)
        return password == "right", "" if password == "right" else "Authentication failure"

    monkeypatch.setattr(auth_service, "check_password", check)
    monkeypatch.setattr(auth_endpoint, "FAILURE_FLOOR", 0)
    accounts = {n: types.SimpleNamespace(pw_name=n) for n in ("alice", "victor", "mallory")}

    def getpwnam(name):
        if name not in accounts:
            raise KeyError(name)
        return accounts[name]

    monkeypatch.setattr(auth_endpoint.pwd, "getpwnam", getpwnam)
    # the client IP as uvicorn's proxy headers would set it (it trusts X-Forwarded-For from 127.0.0.1)
    monkeypatch.setattr(auth_endpoint, "client_ip",
                        lambda scope: dict(scope["headers"]).get(b"x-forwarded-for", b"127.0.0.1").decode())
    return checked


def _login(client, user, password, ip=None):
    headers = dict(CSRF, **({"X-Forwarded-For": ip} if ip else {}))
    return client.post("/api/v1/auth/login", json={"username": user, "password": password}, headers=headers)


def test_allowed_user_logs_in(client, pam):
    assert _login(client, "alice", "right").status_code == 200


def test_no_password_oracle_for_accounts_without_role(client, pam):
    """mallory exists but isn't in an admin/viewer group: right and wrong password answer alike,
    and PAM is never asked (no oracle, no faillock lock-out of arbitrary accounts)"""
    right, wrong = _login(client, "mallory", "right"), _login(client, "mallory", "wrong")
    assert right.status_code == wrong.status_code == 401
    assert right.json() == wrong.json()
    assert "mallory" not in pam


def test_backoff_per_user_whatever_the_ip(client, pam):
    statuses = [_login(client, "victor", "wrong", ip=f"203.0.113.{i}").status_code for i in range(1, 16)]
    assert 429 in statuses, statuses


def test_limiter_unit():
    lim = auth_service.LoginLimiter()
    for i in range(lim.USER_FREE_FAILURES):
        lim.failed(f"10.0.0.{i}", "Bob")
    assert lim.retry_after("10.9.9.9", "bob") > 0
    lim.succeeded("10.9.9.9", "bob")
    assert lim.retry_after("10.9.9.9", "bob") == 0
