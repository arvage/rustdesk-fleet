"""Passkeys on custom domains (step 7): the RP ID is always the tenant's own
host, /.well-known/webauthn lists its served custom domains, and the sign-in
handoff for browsers without Related Origin Requests. Tenant #1 unchanged."""
import json
import logging
import re
from datetime import timedelta

import pytest

from app import handoff
from app.deps import get_db
from conftest import HOST, HOST_B, PASSWORD, add_tenant, create_user, new_client, signed_in_at

SUB = "abc.rds.example.com"
CUSTOM = "rds.abc.example"
CUSTOM2 = "help.abc.example"


def _sql(sql, *params):
    conn = get_db()
    cur = conn.execute(sql, params)
    conn.commit()
    rows = cur.fetchall()
    conn.close()
    return rows


def _custom(tenant_id, host, status="active"):
    _sql("INSERT INTO tenant_domains (tenant_id, hostname, kind, status, verified_at, verify_token)"
         " VALUES (?, ?, 'custom', ?, datetime('now'), 'rdf-verify-xxxxxxxxxxxxxxxxxxxx')", tenant_id, host, status)
    from app import tenancy
    tenancy.invalidate_cache()


@pytest.fixture
def abc():
    add_tenant(2, "abc", SUB)
    _custom(2, CUSTOM)
    admin = create_user("admin@abc.example", "admin", tenant_id=2)
    return {"admin": admin}


def _pending_passkey(user, host):
    """A client on host with the user's password step done (auth_method both)."""
    _sql("UPDATE users SET auth_method = 'both' WHERE id = ?", user["id"])
    _sql("INSERT INTO webauthn_credentials (tenant_id, user_id, credential_id, public_key, nickname, rp_id)"
         " VALUES (?, ?, ?, x'00', 'k', ?)", user["tenant_id"], user["id"], f"Y3JlZA{user['id']}", SUB)
    c = new_client(host)
    r = c.post("/login", data={"email": user["email"], "password": PASSWORD}, follow_redirects=False)
    assert r.headers["location"] == "/login/webauthn"
    return c


def test_rp_id_is_the_provider_subdomain_on_both_hosts(abc):
    for host in (SUB, CUSTOM):
        c = _pending_passkey(abc["admin"], host) if host == SUB else new_client(host)
        if host == CUSTOM:
            r = c.post("/login", data={"email": abc["admin"]["email"], "password": PASSWORD}, follow_redirects=False)
            assert r.headers["location"] == "/login/webauthn"
        opts = c.post("/login/webauthn/begin").json()
        assert opts["rpId"] == SUB, host
    # Registration too.
    for host in (SUB, CUSTOM):
        c = signed_in_at(create_user(f"u-{host}@abc.example", "admin", tenant_id=2), host)
        assert c.post("/account/passkeys/register/begin").json()["rp"]["id"] == SUB


def test_verification_expects_the_subdomain_rp_and_the_custom_origin(abc, monkeypatch):
    from app.routes import webauthn as routes
    seen = {}

    class Verified:
        new_sign_count = 1

    def fake_verify(**kw):
        seen.update(kw)
        return Verified()
    monkeypatch.setattr(routes, "verify_authentication_response", fake_verify)
    c = _pending_passkey(abc["admin"], CUSTOM)
    c.post("/login/webauthn/begin")
    r = c.post("/login/webauthn/complete", json={"id": f"Y3JlZA{abc['admin']['id']}", "type": "public-key",
                                                 "response": {}})
    assert r.json()["ok"] is True
    assert seen["expected_rp_id"] == SUB and seen["expected_origin"] == f"https://{CUSTOM}"
    assert c.get("/devices", follow_redirects=False).status_code == 200


def test_well_known_lists_exactly_the_served_custom_domains(abc):
    _custom(2, CUSTOM2, status="pending_dns")
    _custom(2, "old.abc.example", status="failed")
    _custom(2, "broken.abc.example", status="dns_broken")
    r = new_client(SUB).get("/.well-known/webauthn")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    assert sorted(r.json()["origins"]) == sorted([f"https://{CUSTOM}", "https://broken.abc.example"])
    # Only on the RP ID's own host.
    assert new_client(CUSTOM).get("/.well-known/webauthn").status_code == 404
    # A domain being removed drops out at once.
    _sql("UPDATE tenant_domains SET remove_reason = 'x' WHERE hostname = 'broken.abc.example'")
    assert new_client(SUB).get("/.well-known/webauthn").json()["origins"] == [f"https://{CUSTOM}"]


def test_another_tenants_document_lists_only_its_own(two_tenants):
    assert new_client(HOST_B).get("/.well-known/webauthn").json()["origins"] == ["https://rds.b.example"]
    assert new_client(HOST).get("/.well-known/webauthn").json()["origins"] == ["https://rds.a.example"]


def _handoff_to(host_client, to=CUSTOM):
    r = host_client.get(f"/login/handoff/start?to={to}", follow_redirects=False)
    return r


def test_handoff_end_to_end(abc):
    home = new_client(SUB)
    r = _handoff_to(home)
    assert r.headers["location"] == "/login"
    r = home.post("/login", data={"email": abc["admin"]["email"], "password": PASSWORD}, follow_redirects=False)
    assert r.headers["location"] == "/login/handoff/go"
    r = home.get("/login/handoff/go", follow_redirects=False)
    m = re.match(rf"https://{re.escape(CUSTOM)}/login/handoff\?token=([\w-]+)$", r.headers["location"])
    assert m, r.headers.get("location")
    custom = new_client(CUSTOM)
    r = custom.get(f"/login/handoff?token={m.group(1)}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/" and r.headers["cache-control"] == "no-store"
    assert custom.get("/devices", follow_redirects=False).status_code == 200
    # Single use.
    again = new_client(CUSTOM)
    assert again.get(f"/login/handoff?token={m.group(1)}", follow_redirects=False).status_code == 403
    assert again.get("/devices", follow_redirects=False).status_code == 303


def test_already_signed_in_on_the_home_host_goes_straight_back(abc):
    home = signed_in_at(abc["admin"], SUB)
    assert _handoff_to(home).headers["location"] == "/login/handoff/go"


def test_handoff_only_goes_to_the_tenants_served_custom_domains(abc):
    _custom(2, CUSTOM2, status="pending_dns")
    add_tenant(3, "globex", "globex.rds.example.com")
    _custom(3, "rds.globex.example")
    home = signed_in_at(abc["admin"], SUB)
    for to in (CUSTOM2, "rds.globex.example", "evil.example", SUB):
        r = _handoff_to(home, to)
        assert r.headers["location"] == "/login", to
        assert home.get("/login/handoff/go", follow_redirects=False).headers["location"] == "/", to
    # Started on the custom domain itself: refused (only the own host hands off).
    assert _handoff_to(new_client(CUSTOM)).headers["location"] == "/login"


def test_token_scope_and_expiry(abc):
    _custom(2, CUSTOM2)
    add_tenant(3, "globex", "globex.rds.example.com")
    _custom(3, "rds.globex.example")
    uid = abc["admin"]["id"]
    # Another host of the same tenant: refused.
    t = handoff.issue(2, uid, CUSTOM)
    assert new_client(CUSTOM2).get(f"/login/handoff?token={t}", follow_redirects=False).status_code == 403
    # Another tenant's host: refused.
    t = handoff.issue(2, uid, CUSTOM)
    assert new_client("rds.globex.example").get(f"/login/handoff?token={t}", follow_redirects=False).status_code == 403
    # Expired after 60 seconds.
    t = handoff.issue(2, uid, CUSTOM)
    _sql("UPDATE login_handoffs SET expires_at = datetime('now', '-1 second') WHERE used_at IS NULL")
    assert new_client(CUSTOM).get(f"/login/handoff?token={t}", follow_redirects=False).status_code == 403
    # The token signs in exactly its own user.
    other = create_user("tech@abc.example", "tech", tenant_id=2)
    t = handoff.issue(2, other["id"], CUSTOM)
    c = new_client(CUSTOM)
    c.get(f"/login/handoff?token={t}")
    with c.stream("GET", "/account") as r:
        assert "tech@abc.example" in r.read().decode()
    # Stored hashed, valid 60 seconds.
    row = _sql("SELECT * FROM login_handoffs ORDER BY id DESC LIMIT 1")[0]
    assert t not in json.dumps(dict(row)) and row["target_host"] == CUSTOM
    from datetime import datetime
    created = datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S")
    assert datetime.strptime(row["expires_at"], "%Y-%m-%d %H:%M:%S") - created <= timedelta(seconds=61)


def test_handoff_token_is_kept_out_of_the_app_log():
    from app.main import _QuietClientReports
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                            ("1.2.3.4:1", "GET", "/login/handoff?token=SECRET", "1.1", 303), None)
    assert _QuietClientReports().filter(rec) is True and "SECRET" not in rec.getMessage()


def test_tenant1_keeps_its_rp_id_and_credentials(two_tenants):
    a = two_tenants["a"]
    # Credentials from before phase 1d (no rp_id) are backfilled on upgrade
    # with the host they were made on.
    from app.webauthn_config import ensure_rp_column
    conn = get_db()
    ensure_rp_column(conn)
    conn.close()
    assert _sql("SELECT rp_id FROM webauthn_credentials WHERE tenant_id = 1")[0][0] == HOST
    assert _sql("SELECT rp_id FROM webauthn_credentials WHERE tenant_id = 2")[0][0] == HOST_B
    c = signed_in_at(a["admin"], HOST)
    assert c.post("/account/passkeys/register/begin").json()["rp"]["id"] == HOST
    # Even signed in on tenant #1's custom domain, its passkeys stay on HOST.
    c = signed_in_at(a["admin"], a["custom_host"])
    assert c.post("/account/passkeys/register/begin").json()["rp"]["id"] == HOST


def test_login_page_on_a_custom_domain_knows_where_to_hand_off(abc):
    c = _pending_passkey(abc["admin"], CUSTOM)
    text = c.get("/login/webauthn").text
    assert f'const HANDOFF = "https://{SUB}/login/handoff/start?to={CUSTOM}"' in text
    c = _pending_passkey(create_user("x@abc.example", "admin", tenant_id=2), SUB)
    assert 'const HANDOFF = "";' in c.get("/login/webauthn").text
