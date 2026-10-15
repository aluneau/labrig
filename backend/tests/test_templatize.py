"""Save a group as a template, render it on another subnet: addresses follow the new subnet"""
from app.models import Group
from app.schemas.template import TemplateFile, TemplateSave
from app.services.template_service import template_service

CIDR = "10.77.5.0/24"


def _group(db):
    spec = {
        "name": "e2e-bug-tpl", "cidr": CIDR,
        "router": {"ip": "10.77.5.1",
                   "egress": {"mode": "blocked", "exempt": [{"source": "10.77.5.40/32"},
                                                            {"source": "10.77.5.64/28"}]}},
        "members": [{"name": "web", "ip": "10.77.5.21", "image": "debian-13",
                     "user_data": "#cloud-config\nruncmd:\n  - ip route add 10.77.5.0/24 dev eth0\n"}],
    }
    db.add(Group(name="e2e-bug-tpl", cidr=CIDR, domain="e2e-bug-tpl.lab", spec=spec, status="ready"))
    db.commit()
    return db.query(Group).filter(Group.name == "e2e-bug-tpl").one()


def _render(db):
    group = _group(db)
    doc = template_service.templatize(db, group, TemplateSave(group_id=group.id, id="e2e-bug", title="t"))
    out = template_service._render_doc(TemplateFile.model_validate(doc), {"case": "7", "cidr": "10.88.1.0/24"})
    assert out["errors"] == [], out["errors"]
    return out["group"]


def test_member_ip_follows_subnet(db):
    assert _render(db)["members"][0]["ip"] == "10.88.1.21"


def test_exempt_cidrs_follow_subnet(db):
    """`source` CIDRs of the group network (an address followed by /N) must move with the subnet too"""
    sources = [e["source"] for e in _render(db)["router"]["egress"]["exempt"]]
    assert sources == ["10.88.1.40/32", "10.88.1.64/28"], sources


def test_group_network_in_text_follows_subnet(db):
    """the group's own network written in a member's user_data (a route) moves with the subnet"""
    assert "10.88.1.0/24" in _render(db)["members"][0]["user_data"]
