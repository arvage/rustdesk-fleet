"""The tenant's Domains page and the custom-domain worker (step 4): add,
DNS checks, certificate issuance (with Let's Encrypt limits), primary
domain, removal. DNS and rdf-helper are faked."""
import re
from datetime import timedelta

import pytest

from app import domain_dns, domains, tenancy
from app.console import mail
from app.deps import get_db
from conftest import HELPER_MODULE, add_tenant, create_user, get_status, new_client, signed_in_at

SUB = "abc.rds.example.com"
CUSTOM = "rds.abc.example"
BOX = "198.51.100.10"


class Dns:
    """Fake DNS: the custom domain CNAMEs to `target` (None = no record)."""

    def __init__(self):
        self.target = None

    def __call__(self, resolver, name, rtype):
        if name == CUSTOM and self.target and rtype in ("A", "AAAA"):
            return "NOERROR", [(CUSTOM, "CNAME", self.target + "."), (self.target, "A", BOX)] if rtype == "A" else \
                [(CUSTOM, "CNAME", self.target + ".")]
        return ("NOERROR" if name != CUSTOM else "NXDOMAIN"), []


@pytest.fixture
def dns(monkeypatch):
    fake = Dns()
    monkeypatch.setattr(domain_dns, "QUERY", fake)
    return fake


@pytest.fixture
def emails(monkeypatch):
    sent = []
    monkeypatch.setattr(mail, "send", lambda to, subject, html, event="": (sent.append(subject) or (True, "")))
    return sent


@pytest.fixture
def abc(fake_system, dns, emails):
    add_tenant(2, "abc", SUB)
    admin = create_user("admin@abc.example", "admin", tenant_id=2)
    tech = create_user("tech@abc.example", "tech", tenant_id=2)
    return {"admin": admin, "tech": tech, "client": signed_in_at(admin, SUB), "system": fake_system, "dns": dns,
            "emails": emails}


def _row(host=CUSTOM):
    conn = get_db()
    row = conn.execute("SELECT * FROM tenant_domains WHERE hostname = ?", (host,)).fetchone()
    conn.close()
    return row


def _add(c, host=CUSTOM):
    r = c.post("/admin/domains", data={"hostname": host}, follow_redirects=False)
    assert r.status_code == 303
    return _row(host)


def _activate(abc):
    _add(abc["client"])
    abc["dns"].target = SUB
    domains.run_due()
    assert _row()["status"] == "active"


def test_page_lists_the_provider_subdomain_and_shows_records(abc):
    c = abc["client"]
    r = c.get("/admin/domains")
    assert r.status_code == 200 and SUB in r.text and "Part of your account" in r.text
    _add(c)
    r = c.get("/admin/domains")
    assert CUSTOM in r.text and f"<code>{SUB}</code>" in r.text      # the CNAME target
    assert "Checking DNS" in r.text


def test_a_records_method_is_offered_only_with_public_ipv4(abc, monkeypatch):
    c = abc["client"]
    row = _add(c)
    assert row["verify_token"] not in c.get("/admin/domains").text
    monkeypatch.setenv("PUBLIC_IPV4", BOX)
    text = c.get("/admin/domains").text
    assert row["verify_token"] in text and f"_rdf-verify.{CUSTOM}" in text and BOX in text


def test_only_domain_managers(abc):
    c = signed_in_at(abc["tech"], SUB)
    assert get_status(c, "/admin/domains")[0] in (303, 403)
    r = c.post("/admin/domains", data={"hostname": CUSTOM}, follow_redirects=False)
    assert _row() is None, r.status_code


def test_dns_then_certificate_then_active(abc):
    row = _add(abc["client"])
    assert row["status"] == "verifying"
    domains.run_due()
    row = _row()
    assert row["status"] == "pending_dns" and "couldn't find a DNS record" in row["last_check_message"]
    assert not abc["system"].commands("sudo -n /usr/local/sbin/rdf-helper issue-cert")
    abc["dns"].target = SUB
    domains.run_due()                       # not due yet: next check in 5 minutes
    assert _row()["status"] == "pending_dns"
    domains.run_due(at=domains.now() + timedelta(minutes=6))
    row = _row()
    assert row["status"] == "active" and row["verified_at"] and row["check_method"] == "cname"
    helper = [c.split(" ", 3)[3] for c in abc["system"].commands("sudo -n /usr/local/sbin/rdf-helper")]
    assert helper == [f"issue-cert {CUSTOM}", f"write-vhost {CUSTOM}", "reload-nginx"]
    assert any("is live" in s for s in abc["emails"])
    # Now it serves the tenant's dashboard.
    tenancy.invalidate_cache()
    c = signed_in_at(abc["admin"], CUSTOM)
    assert c.get("/devices").status_code == 200


def test_a_cname_to_another_account_never_issues(abc):
    add_tenant(3, "globex", "globex.rds.example.com")
    _add(abc["client"])
    abc["dns"].target = "globex.rds.example.com"
    domains.run_due()
    row = _row()
    assert row["status"] == "pending_dns" and "different account" in row["last_check_message"]
    assert not abc["system"].commands("sudo -n /usr/local/sbin/rdf-helper issue-cert")


def test_gives_up_after_7_days_and_releases(abc):
    _add(abc["client"])
    domains.run_due(at=domains.now() + timedelta(days=7, minutes=1))
    row = _row()
    assert row["status"] == "failed" and domains.is_released(row)
    assert any("wasn't set up" in s for s in abc["emails"])
    # Check now starts over.
    r = abc["client"].post(f"/admin/domains/{row['id']}/check", follow_redirects=False)
    assert r.status_code == 303 and _row()["status"] == "verifying"


def test_issuance_is_limited_to_once_an_hour_and_five_failures(abc):
    abc["system"].fail["issue-cert"] = "too many certificates"
    _add(abc["client"])
    abc["dns"].target = SUB
    t = domains.now()
    domains.run_due(at=t)
    issue = lambda: len(abc["system"].commands("sudo -n /usr/local/sbin/rdf-helper issue-cert"))
    assert issue() == 1 and _row()["status"] == "issuing_certificate" and _row()["issue_failures"] == 1
    domains.run_due(at=t + timedelta(minutes=30))
    assert issue() == 1                                  # not within the hour
    for n in range(2, 6):
        domains.run_due(at=t + timedelta(hours=n - 1, minutes=1))
        assert issue() == n
    row = _row()
    assert row["status"] == "failed" and row["issue_failures"] == 5 and "press Check now" in row["last_check_message"]
    domains.run_due(at=t + timedelta(hours=10))
    assert issue() == 5                                  # stopped
    # Check now resets the count (the hourly limit still applies).
    abc["client"].post(f"/admin/domains/{row['id']}/check")
    domains.run_due(at=t + timedelta(hours=10, minutes=1))
    assert issue() == 6


def test_primary_domain_is_used_for_links_and_invites(abc):
    c = abc["client"]
    pending = _add(c)
    r = c.post(f"/admin/domains/{pending['id']}/primary", follow_redirects=False)
    assert r.status_code == 303 and not _row()["is_primary"]          # only an active domain
    abc["dns"].target = SUB
    domains.run_due()
    c.post(f"/admin/domains/{_row()['id']}/primary")
    assert _row()["is_primary"] == 1 and _row(SUB)["is_primary"] == 0
    conn = get_db()
    assert domains.primary_host(conn, 2) == CUSTOM
    gid = conn.execute("INSERT INTO client_groups (tenant_id, slug, display_name) VALUES (2, 'g', 'G')").lastrowid
    conn.execute("INSERT INTO download_links (tenant_id, token, group_id) VALUES (2, ?, ?)", ("tok-" + "x" * 20, gid))
    conn.commit()
    from app.console.tenants import create_invite
    invite = create_invite(conn, 2, "abc", "new@abc.example", "New", "test")
    conn.close()
    assert invite.startswith(f"https://{CUSTOM}/invite/")
    assert f"https://{CUSTOM}/d/tok-" in c.get("/groups/g").text


def test_removing_the_primary_falls_back_and_releases(abc):
    _activate(abc)
    c = abc["client"]
    row = _row()
    c.post(f"/admin/domains/{row['id']}/primary")
    r = c.post(f"/admin/domains/{row['id']}/remove", follow_redirects=False)
    assert r.status_code == 303
    assert _row(SUB)["is_primary"] == 1                # at once, before the worker runs
    tenancy.invalidate_cache()
    assert tenancy.resolve_host(CUSTOM) is None        # no longer reaches the tenant
    domains.run_due()
    assert _row() is None
    assert abc["system"].commands(f"sudo -n /usr/local/sbin/rdf-helper release-domain {CUSTOM}")


def test_the_provider_subdomain_cant_be_removed(abc):
    r = abc["client"].post(f"/admin/domains/{_row(SUB)['id']}/remove", follow_redirects=False)
    assert r.status_code == 303 and _row(SUB) is not None


def test_another_tenants_domain_ids_are_not_reachable(abc):
    _activate(abc)
    add_tenant(3, "globex", "globex.rds.example.com")
    other = signed_in_at(create_user("admin@globex.example", "admin", tenant_id=3), "globex.rds.example.com")
    rid = _row()["id"]
    for action in ("check", "primary", "remove"):
        other.post(f"/admin/domains/{rid}/{action}")
    row = _row()
    assert row["status"] == "active" and row["remove_reason"] is None and not row["is_primary"]


def test_support_sessions_can_look_but_not_change(two_tenants, fake_system, dns, emails):
    from test_support_access import _support_client
    c = _support_client(two_tenants)
    assert c.get("/admin/domains").status_code == 200
    c.post("/admin/domains", data={"hostname": CUSTOM})
    assert _row() is None
