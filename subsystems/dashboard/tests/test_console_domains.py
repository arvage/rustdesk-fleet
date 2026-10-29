"""Custom domains in the provider console (step 8): the tenant page lists
them, a provider admin can remove one (reason, logged, tenant emailed) but
not add one, and /domains lists the ones with problems."""
from datetime import timedelta

import pytest

from app import domains
from app.console import mail
from app.deps import get_db
from conftest import add_tenant, create_user, provider_client

CONSOLE = "admin.rds.example.com"


def _sql(sql, *params):
    conn = get_db()
    cur = conn.execute(sql, params)
    conn.commit()
    rows = cur.fetchall()
    conn.close()
    return rows


@pytest.fixture
def setup(monkeypatch, fake_system):
    sent = []
    monkeypatch.setattr(mail, "send", lambda to, subject, html, event="": (sent.append((to, subject, html)) or (True, "")))
    add_tenant(2, "abc", "abc.rds.example.com")
    create_user("admin@abc.example", "admin", tenant_id=2)
    soon = domains.fmt(domains.now() + timedelta(days=5))
    for host, status, extra in (("rds.abc.example", "active", {"cert_expires_at": "2027-01-20 12:00:00",
                                                              "check_method": "cname", "last_check_ok": 1}),
                                ("broken.abc.example", "dns_broken", {"broken_since": "2026-10-28 00:00:00",
                                                                      "last_check_message": "points elsewhere"}),
                                ("old.abc.example", "failed", {}),
                                ("soon.abc.example", "active", {"cert_expires_at": soon})):
        cols = ", ".join(["tenant_id", "hostname", "kind", "status", "last_checked_at", *extra])
        _sql(f"INSERT INTO tenant_domains ({cols}) VALUES (2, ?, 'custom', ?, datetime('now'){', ?' * len(extra)})",
             host, status, *extra.values())
    return {"emails": sent, "system": fake_system, "c": provider_client()}


def test_tenant_page_lists_domains_with_status_method_check_and_expiry(setup):
    text = setup["c"].get("/tenants/2").text
    for s in ("rds.abc.example", "broken.abc.example", "dns_broken", "CNAME", "points here", "points elsewhere",
              "2027-01-20", "soon"):
        assert s in text, s
    assert 'action="/tenants/2/domains/' in text


def test_provider_removes_a_domain_with_a_reason(setup):
    c = setup["c"]
    did = _sql("SELECT id FROM tenant_domains WHERE hostname = 'rds.abc.example'")[0][0]
    r = c.post(f"/tenants/2/domains/{did}/remove", data={"reason": ""}, follow_redirects=False)
    assert r.status_code == 303 and _sql("SELECT remove_reason FROM tenant_domains WHERE id = ?", did)[0][0] is None
    c.post(f"/tenants/2/domains/{did}/remove", data={"reason": "phishing reports"})
    assert "phishing reports" in _sql("SELECT remove_reason FROM tenant_domains WHERE id = ?", did)[0][0]
    assert _sql("SELECT 1 FROM provisioning_events WHERE event = 'domain_removed_by_provider' AND source = 'provider'"
                " AND tenant_id = 2 AND detail LIKE '%phishing reports%'")
    assert any(to == ["admin@abc.example"] and "rds.abc.example was removed" in subject and "phishing reports" in html
               for to, subject, html in setup["emails"])
    domains.run_due()
    assert not _sql("SELECT 1 FROM tenant_domains WHERE id = ?", did)
    assert setup["system"].commands("sudo -n /usr/local/sbin/rdf-helper release-domain rds.abc.example")


def test_provider_cant_remove_a_provider_subdomain_or_add_domains(setup):
    c = setup["c"]
    sub = _sql("SELECT id FROM tenant_domains WHERE hostname = 'abc.rds.example.com'")[0][0]
    c.post(f"/tenants/2/domains/{sub}/remove", data={"reason": "x"})
    assert _sql("SELECT remove_reason FROM tenant_domains WHERE id = ?", sub)[0][0] is None
    from fastapi.routing import APIRoute, iter_route_contexts
    from app.console.main import console_app
    posts = [ctx.path for ctx in iter_route_contexts(console_app.routes)
             if isinstance(ctx.route, APIRoute) and "POST" in (ctx.methods or ()) and "domain" in ctx.path]
    assert posts == ["/tenants/{tenant_id}/domains/{domain_id}/remove"]


def test_problem_list(setup):
    text = setup["c"].get("/domains").text
    assert "broken.abc.example" in text and "old.abc.example" in text and "soon.abc.example" in text
    assert "rds.abc.example</td>" not in text          # healthy: not listed
