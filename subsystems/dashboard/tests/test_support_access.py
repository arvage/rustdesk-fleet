"""Support access (app/support.py, routes/support.py, the console's Open
dashboard): consent, single-use tokens, what a support session can and
can't do, and everything that ends one."""
import re
from datetime import datetime, timedelta, timezone

import pytest

from conftest import (CONSOLE_HOST, HOST, HOST_B, all_routes, db_rows, new_client, provider_client, signed_in_at)
from app import support
from app.console import mail
from app.deps import get_db

FMT = "%Y-%m-%d %H:%M:%S"
PROVIDER = "ops@example.com"


@pytest.fixture
def emails(monkeypatch):
    sent = []
    monkeypatch.setattr(mail, "send", lambda to, subject, html, event="": (sent.append((sorted(to), subject)) or (True, "")))
    return sent


def _sql(sql, *params):
    conn = get_db()
    conn.execute(sql, params)
    conn.commit()
    conn.close()


def _grant(admin, host=HOST, duration="1d", passwords=False, **extra):
    c = signed_in_at(admin, host)
    data = {"duration": duration, "note": "ticket 1234", **({"include_passwords": "1"} if passwords else {}), **extra}
    r = c.post("/admin/support/grant", data=data, follow_redirects=False)
    assert r.status_code == 303
    return c


def _token(tenant_id=1, reason="VPN issue", console=None):
    c = console or provider_client(PROVIDER)
    r = c.post(f"/tenants/{tenant_id}/support/open", data={"reason": reason}, follow_redirects=False)
    m = re.search(r'href="https://([^/"]+)/support/enter\?token=([^"]+)"', r.text)
    return (m.group(1), m.group(2)) if m else (None, r)


def _enter(token, host=HOST):
    c = new_client(host)
    r = c.get(f"/support/enter?token={token}", follow_redirects=False)
    return c, r


def _support_client(two_tenants, passwords=False):
    _grant(two_tenants["a"]["admin"], passwords=passwords)
    host, token = _token()
    assert host == HOST
    c, r = _enter(token)
    assert r.status_code == 303 and r.headers["location"] == "/", r.text[:300]
    return c


# ── Consent and tokens ───────────────────────────────────────────────────────

def test_without_a_grant_there_is_nothing_to_open(two_tenants):
    console = provider_client(PROVIDER)
    assert "Open dashboard" not in console.get("/tenants/1").text
    host, r = _token(console=console)
    assert host is None and r.status_code == 303
    assert db_rows("SELECT COUNT(*) FROM support_tokens")[0][0] == 0
    _, r = _enter("made-up-token")
    assert r.status_code == 403


def test_a_token_works_once_and_signs_support_in_without_a_user_row(two_tenants, emails):
    users_before = db_rows("SELECT COUNT(*) FROM users")[0][0]
    _grant(two_tenants["a"]["admin"])
    assert "Open dashboard" in provider_client(PROVIDER).get("/tenants/1").text
    host, token = _token()
    c, r = _enter(token)
    assert r.status_code == 303 and r.headers["location"] == "/"
    home = c.get("/")
    assert home.status_code == 200 and "Provider support (ops) is signed in" in home.text
    assert db_rows("SELECT COUNT(*) FROM users")[0][0] == users_before
    _, again = _enter(token)
    assert again.status_code == 403
    assert db_rows("SELECT token_hash FROM support_tokens")[0][0] != token        # stored hashed
    assert db_rows("SELECT 1 FROM provisioning_events WHERE tenant_id = 1 AND event = 'support_session_started'"
                   " AND user_email = 'support:ops@example.com'")
    assert db_rows("SELECT 1 FROM provisioning_events WHERE tenant_id = 1 AND event = 'support_session_started'"
                   " AND source = 'provider'")
    assert any("Provider support signed in" in s for _, s in emails)


def test_a_token_expires_after_60_seconds(two_tenants):
    _grant(two_tenants["a"]["admin"])
    _, token = _token()
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).strftime(FMT)
    _sql("UPDATE support_tokens SET expires_at = ?", past)
    _, r = _enter(token)
    assert r.status_code == 403 and "expired" in r.text


def test_a_token_for_tenant_a_does_not_work_on_tenant_b(two_tenants):
    _grant(two_tenants["a"]["admin"])
    _grant(two_tenants["b"]["admin"], HOST_B)
    _, token = _token(1)
    _, r = _enter(token, HOST_B)
    assert r.status_code == 403
    assert db_rows("SELECT COUNT(*) FROM support_sessions")[0][0] == 0


def test_a_reason_is_required_and_a_closed_tenant_cant_be_opened(two_tenants):
    _grant(two_tenants["b"]["admin"], HOST_B)
    host, r = _token(2, reason="  ")
    assert host is None
    _sql("UPDATE tenants SET status = 'suspended' WHERE id = 2")
    from app.tenancy import invalidate_cache
    invalidate_cache()
    host, r = _token(2)
    assert host is None


def test_only_admins_manage_support_access(two_tenants):
    tech = signed_in_at(two_tenants["a"]["tech"], HOST)
    assert tech.get("/admin/support", follow_redirects=False).headers["location"] == "/"
    tech.post("/admin/support/grant", data={"duration": "1d"})
    assert db_rows("SELECT COUNT(*) FROM support_grants")[0][0] == 0


@pytest.mark.parametrize("data,ok", [
    ({"duration": "4h"}, True), ({"duration": "12m"}, True), ({"duration": "13m"}, False), ({"duration": ""}, False),
    ({"duration": "custom", "custom_end": "2020-01-01T10:00"}, False),
    ({"duration": "custom", "custom_end": (datetime.now() + timedelta(days=400)).strftime("%Y-%m-%dT%H:%M")}, False),
    ({"duration": "custom", "custom_end": (datetime.now() + timedelta(days=20)).strftime("%Y-%m-%dT%H:%M")}, True),
])
def test_grant_durations(two_tenants, data, ok):
    signed_in_at(two_tenants["a"]["admin"], HOST).post("/admin/support/grant", data=data)
    assert bool(db_rows("SELECT COUNT(*) FROM support_grants")[0][0]) == ok


def test_granting_again_changes_the_one_active_grant(two_tenants, emails):
    _grant(two_tenants["a"]["admin"], duration="1d")
    _grant(two_tenants["a"]["admin"], duration="7d", passwords=True)
    rows = db_rows("SELECT ends_at, include_passwords FROM support_grants")
    assert len(rows) == 1 and rows[0]["include_passwords"] == 1
    ends = datetime.strptime(rows[0]["ends_at"], FMT).replace(tzinfo=timezone.utc)
    assert timedelta(days=6, hours=23) < ends - datetime.now(timezone.utc) <= timedelta(days=7)
    assert [s for _, s in emails if "Support access" in s] == ["Pacific: Support access granted",
                                                                 "Pacific: Support access changed"] or len(emails) == 2


# ── What ends a support session ─────────────────────────────────────────────

def _ended(c):
    r = c.get("/devices", follow_redirects=False)
    return r.status_code == 303 and r.headers["location"] == "/login"


def test_revoking_ends_the_session_on_the_next_request(two_tenants, emails):
    c = _support_client(two_tenants)
    assert c.get("/devices").status_code == 200
    signed_in_at(two_tenants["a"]["admin"], HOST).post("/admin/support/revoke")
    assert _ended(c)
    assert db_rows("SELECT end_reason FROM support_sessions")[0][0] == "access revoked"
    assert any("revoked" in s for _, s in emails)


def test_grant_expiry_ends_it(two_tenants, emails):
    c = _support_client(two_tenants)
    _sql("UPDATE support_grants SET ends_at = ?", (datetime.now(timezone.utc) - timedelta(minutes=1)).strftime(FMT))
    assert _ended(c)
    support.sweep()
    assert any("Support access ended" in s for _, s in emails)
    assert db_rows("SELECT expiry_notified_at FROM support_grants")[0][0]


def test_the_four_hour_cap_ends_it(two_tenants):
    c = _support_client(two_tenants)
    _sql("UPDATE support_sessions SET started_at = ?", (datetime.now(timezone.utc) - timedelta(hours=4, minutes=1)).strftime(FMT))
    assert _ended(c)
    assert db_rows("SELECT end_reason FROM support_sessions")[0][0] == "4-hour limit"


def test_disabling_or_signing_out_the_provider_user_ends_it(two_tenants):
    c = _support_client(two_tenants)
    _sql("UPDATE provider_users SET disabled_at = datetime('now') WHERE email = ?", PROVIDER)
    assert _ended(c)
    _sql("UPDATE provider_users SET disabled_at = NULL WHERE email = ?", PROVIDER)
    c = _support_client(two_tenants)
    _sql("UPDATE provider_users SET session_version = session_version + 1 WHERE email = ?", PROVIDER)
    assert _ended(c)


def test_suspending_the_tenant_ends_it(two_tenants):
    _grant(two_tenants["b"]["admin"], HOST_B)
    _, token = _token(2)
    c, _ = _enter(token, HOST_B)
    assert c.get("/devices").status_code == 200
    provider_client(PROVIDER).post("/tenants/2/suspend")
    assert db_rows("SELECT end_reason FROM support_sessions")[0][0] == "tenant suspended"
    assert c.get("/devices", follow_redirects=False).status_code == 403


def test_end_session_in_the_banner(two_tenants):
    c = _support_client(two_tenants)
    r = c.post("/support/end", follow_redirects=False)
    assert r.headers["location"] == "/login"
    assert db_rows("SELECT end_reason FROM support_sessions")[0][0] == "ended by support"
    assert _ended(c)


def test_a_support_session_for_a_cannot_reach_b(two_tenants):
    c = _support_client(two_tenants)
    other = new_client(HOST_B)
    other.cookies.set("__Host-rdf_session", c.cookies.get("__Host-rdf_session"))
    assert other.get("/devices", follow_redirects=False).headers["location"] == "/login"
    assert two_tenants["b"]["secret"] not in c.get("/devices").text


# ── What a support session can't do ─────────────────────────────────────────

FORM = {"role": "admin", "email": "x@example.com", "display_name": "X", "name": "X", "permissions": "manage_users",
        "password": "Correct-horse-1", "current_password": "x", "auth_method": "password", "duration": "12m",
        "group_ids": "1"}


def test_support_cant_reach_users_roles_support_access_account_or_export(two_tenants):
    a = two_tenants["a"]
    c = _support_client(two_tenants)
    before = {t: db_rows(f"SELECT * FROM {t}") for t in ("users", "roles", "support_grants", "webauthn_credentials",
                                                         "user_group_access")}
    checked = []
    for method, path in all_routes():
        if not support.forbidden_path(path):
            continue
        url = (path.replace("{user_id}", str(a["admin"]["id"])).replace("{cred_id}", str(a["cred_id"]))
               .replace("{key}", a["role_key"]).replace("{name}", "devices"))
        r = c.get(url, follow_redirects=False) if method == "GET" else c.post(url, data=FORM, follow_redirects=False)
        assert r.status_code in (303, 403), (method, url, r.status_code)
        if r.status_code == 303:
            assert r.headers["location"] in ("/", "/login"), (method, url, r.headers["location"])
        checked.append(f"{method} {url}")
    assert len(checked) >= 25, checked
    after = {t: db_rows(f"SELECT * FROM {t}") for t in before}
    assert {t: [tuple(r) for r in v] for t, v in after.items()} == {t: [tuple(r) for r in v] for t, v in before.items()}
    # Still signed in: refusals don't end the session.
    assert c.get("/devices").status_code == 200


def test_support_has_no_user_or_role_capabilities(two_tenants):
    c = _support_client(two_tenants)
    html = c.get("/admin").text
    assert 'href="/users"' not in c.get("/devices").text and 'href="/account"' not in c.get("/devices").text
    assert "/admin/support" not in c.get("/devices").text
    assert html


def test_unattended_passwords_stay_hidden_and_unchanged(two_tenants):
    a = two_tenants["a"]
    _sql("UPDATE client_groups SET unattended_password = 'Very-Secret-Pw-9' WHERE id = ?", a["group_id"])
    c = _support_client(two_tenants)
    page = c.get(f"/groups/{a['group_slug']}").text
    assert "Very-Secret-Pw-9" not in page and "Password set" in page
    c.post(f"/groups/{a['group_slug']}/edit", data={"new_slug": a["group_slug"], "display_name": "Renamed",
                                                     "unattended_password": "Changed-Pw-1", "clear_password": "1"})
    row = db_rows("SELECT display_name, unattended_password FROM client_groups WHERE id = ?", a["group_id"])[0]
    assert tuple(row) == ("Renamed", "Very-Secret-Pw-9")
    c.post("/groups", data={"slug": "fresh", "display_name": "Fresh", "unattended_password": "New-Pw-1"})
    assert db_rows("SELECT unattended_password FROM client_groups WHERE slug = 'fresh'")[0][0] is None
    for path in ("/groups", "/devices", "/api/devices", f"/groups/{a['group_slug']}"):
        assert "Very-Secret-Pw-9" not in c.get(path).text, path


def test_with_the_grant_flag_support_sees_passwords(two_tenants):
    a = two_tenants["a"]
    _sql("UPDATE client_groups SET unattended_password = 'Very-Secret-Pw-9' WHERE id = ?", a["group_id"])
    c = _support_client(two_tenants, passwords=True)
    assert "Very-Secret-Pw-9" in c.get(f"/groups/{a['group_slug']}").text


def test_audit_entries_in_a_support_session_are_attributed(two_tenants):
    c = _support_client(two_tenants)
    c.post("/groups", data={"slug": "by-support", "display_name": "By support"})
    assert db_rows("SELECT user_email FROM provisioning_events WHERE event = 'group_created' AND detail = 'by-support'")[0][0] \
        == "support:ops@example.com"
    c.post(f"/api/devices/{two_tenants['a']['rid']}/connect")
    assert db_rows("SELECT user_email FROM connect_clicks ORDER BY id DESC LIMIT 1")[0][0] == "support:ops@example.com"
    logs = signed_in_at(two_tenants["a"]["admin"], HOST).get("/audit").text
    assert "support:ops@example.com" in logs


def test_the_tenant_sees_the_history(two_tenants):
    _support_client(two_tenants)
    page = signed_in_at(two_tenants["a"]["admin"], HOST).get("/admin/support").text
    assert "Support can sign in until" in page and "VPN issue" in page and "ops@example.com" in page


def test_purge_removes_grants_tokens_and_sessions(two_tenants, fake_system):
    from app.console import lifecycle
    _grant(two_tenants["b"]["admin"], HOST_B)
    _token(2)
    _sql("UPDATE tenants SET status = 'cancelled', cancelled_at = datetime('now'), purge_after = datetime('now') WHERE id = 2")
    lifecycle.purge_now(2)
    for t in support.SUPPORT_TABLES:
        assert db_rows(f"SELECT COUNT(*) FROM {t} WHERE tenant_id = 2")[0][0] == 0, t


def test_the_access_log_never_shows_the_token():
    import logging
    from app.main import _QuietClientReports
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                            ("1.2.3.4:5", "GET", "/support/enter?token=SECRET", "1.1", 303), None)
    assert _QuietClientReports().filter(rec) is True
    assert "SECRET" not in rec.getMessage() and "/support/enter" in rec.getMessage()
