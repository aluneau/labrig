"""Host DNS resolver custom records (dns_service)

dnsmasq's cname= only answers with an address when the target is a name dnsmasq itself knows (host-record /
another cname): checked with dnsmasq 2.93, `cname=ext.test,example.com` answers the CNAME alone (clients get no
address), as does a target only covered by an address=/zone/ wildcard.
"""
import pytest

from app.services.dns_service import dns_service


def test_cname_to_local_a_record(db):
    dns_service.add_record(db, "foo.test", ip="10.1.2.3")
    rec = dns_service.add_record(db, "alias.test", cname="foo.test")
    assert rec["type"] == "CNAME"
    assert "cname=alias.test,foo.test" in dns_service.render(db, "127.0.0.2")


def test_cname_to_external_name_refused(db):
    with pytest.raises(ValueError):
        dns_service.add_record(db, "ext.test", cname="example.com")


def test_cname_to_wildcard_covered_name_refused(db):
    dns_service.add_record(db, "*.apps.test", ip="10.1.2.4")
    with pytest.raises(ValueError):
        dns_service.add_record(db, "w.test", cname="x.apps.test")


def test_cname_to_itself_refused(db):
    with pytest.raises(ValueError):
        dns_service.add_record(db, "loop.test", cname="loop.test")
