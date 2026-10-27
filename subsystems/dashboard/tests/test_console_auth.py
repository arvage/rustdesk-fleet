"""Provider console accounts: passkey-only sign-in on the console host, its
own session cookie, per-request checks, the IP allowlist and the CLI."""
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import CONSOLE_HOST, HOST, DATA_DIR, db_rows, login, new_client, signed_in
from app.console import auth as console_auth
from app.deps import get_db


@pytest.fixture
def fake_webauthn(monkeypatch):
    """Accept any ceremony: registration n yields credential id 'cred000n'
    (stored base64url-encoded, which these 8-character ids survive as is)."""
    from webauthn.helpers import base64url_to_bytes
    seq = iter(range(1, 1000))
    monkeypatch.setattr(console_auth, "verify_registration_response", lambda **kw: SimpleNamespace(
        credential_id=base64url_to_bytes(f"cred{next(seq):04d}"), credential_public_key=b"pk", sign_count=0))
    monkeypatch.setattr(console_auth, "verify_authentication_response",
                        lambda **kw: SimpleNamespace(new_sign_count=1))


def provider(email="ops@example.com", name="Ops"):
    conn = get_db()
    uid = console_auth.add_provider_user(conn, email, name)
    link = console_auth.create_enrollment(conn, uid, "test")
    conn.close()
    return uid, link.rsplit("/", 1)[1]


def enroll(token):
    c = new_client(CONSOLE_HOST)
    assert c.get(f"/enroll/{token}").status_code == 200
    assert c.post(f"/enroll/{token}/begin").status_code == 200
    r = c.post(f"/enroll/{token}/complete", json={"id": "x", "response": {}})
    assert r.json() == {"ok": True}, r.text
    return c


def passkey_login(cred_id="cred0001", ip=None):
    c = new_client(CONSOLE_HOST)
    h = {"X-Real-IP": ip} if ip else None
    c.post("/login/webauthn/begin", headers=h)
    return c, c.post("/login/webauthn/complete", json={"id": cred_id}, headers=h)


def test_console_host_needs_a_provider_sign_in():
    c = new_client(CONSOLE_HOST)
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert c.get("/login").status_code == 200
    assert 'type="password"' not in c.get("/login").text


def test_enrollment_link_is_one_time_and_signs_in(fake_webauthn):
    uid, token = provider()
    c = enroll(token)
    assert c.get("/admins").status_code == 200
    assert "rdf_provider_session" in c.cookies
    assert "rdf_session" not in c.cookies
    assert [tuple(r) for r in db_rows("SELECT provider_user_id, credential_id FROM provider_webauthn_credentials")] == [(uid, "cred0001")]
    # Used: the link is dead now.
    assert new_client(CONSOLE_HOST).get(f"/enroll/{token}").status_code == 404
    assert new_client(CONSOLE_HOST).post(f"/enroll/{token}/begin").status_code == 404


def test_enrollment_links_expire_after_an_hour(fake_webauthn):
    _, token = provider()
    conn = get_db()
    conn.execute("UPDATE provider_enrollments SET expires_at = datetime('now', '-1 minute')")
    conn.commit()
    conn.close()
    assert new_client(CONSOLE_HOST).get(f"/enroll/{token}").status_code == 404


def test_passkey_sign_in_and_disabled_users_are_signed_out(fake_webauthn):
    uid, token = provider()
    enroll(token)
    c, r = passkey_login()
    assert r.json() == {"ok": True}
    assert c.get("/admins").status_code == 200
    conn = get_db()
    conn.execute("UPDATE provider_users SET disabled_at = datetime('now') WHERE id = ?", (uid,))
    conn.commit()
    conn.close()
    r = c.get("/admins", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    # And a disabled user can't sign in again.
    _, r = passkey_login()
    assert r.status_code == 400


def test_unknown_passkeys_fail_and_lock_the_ip(fake_webauthn):
    from app import ratelimit
    codes = [passkey_login("nope", ip="203.0.113.5")[1].status_code for _ in range(ratelimit.MAX_IP_FAILURES + 1)]
    assert codes[:ratelimit.MAX_IP_FAILURES] == [400] * ratelimit.MAX_IP_FAILURES and codes[-1] == 429


def test_tenant_and_provider_sessions_never_mix(fake_webauthn, admin):
    _, token = provider()
    console = enroll(token)
    tenant = signed_in(admin)
    # A tenant cookie gives nothing on the console...
    c = new_client(CONSOLE_HOST)
    c.cookies.set("rdf_session", tenant.cookies.get("rdf_session"))
    c.cookies.set("rdf_provider_session", tenant.cookies.get("rdf_session"))
    assert c.get("/admins", follow_redirects=False).headers["location"] == "/login"
    # ...and a provider cookie gives nothing on a tenant host.
    t = new_client(HOST)
    t.cookies.set("rdf_session", console.cookies.get("rdf_provider_session"))
    assert t.get("/devices", follow_redirects=False).headers["location"] == "/login"


def test_console_and_tenant_routes_dont_cross(fake_webauthn, admin):
    _, token = provider()
    console = enroll(token)
    for path in ("/devices", "/groups", "/users", "/api/devices", "/setup"):
        assert console.get(path, follow_redirects=False).status_code == 404, path
    assert console.post("/api/heartbeat", json={}).status_code == 404
    tenant = signed_in(admin)
    for path in ("/admins", "/audit?tenant=x", "/enroll/abc"):
        r = tenant.get(path, follow_redirects=False)
        assert r.status_code in (404, 200) and "Provider admins" not in r.text, path
    assert tenant.get("/admins", follow_redirects=False).status_code == 404


def test_allowed_ips(monkeypatch, fake_webauthn):
    monkeypatch.setenv("PROVIDER_ALLOWED_IPS", "198.51.100.0/24, 203.0.113.7")
    c = new_client(CONSOLE_HOST)
    assert c.get("/login", headers={"X-Real-IP": "192.0.2.1"}).status_code == 403
    assert c.get("/login", headers={"X-Real-IP": "198.51.100.20"}).status_code == 200
    assert c.get("/login", headers={"X-Real-IP": "203.0.113.7"}).status_code == 200
    # The allowlist is the console's: tenant hosts don't care.
    assert new_client(HOST).get("/login", headers={"X-Real-IP": "192.0.2.1"}).status_code == 200


def test_console_actions_are_in_the_provider_log_not_the_tenants(fake_webauthn, admin):
    _, token = provider()
    console = enroll(token)
    console.post("/admins", data={"email": "second@example.com", "display_name": "Second"})
    rows = db_rows("SELECT event, source, user_email FROM provisioning_events WHERE event = 'provider_admin_invited'")
    assert [tuple(r) for r in rows] == [("provider_admin_invited", "provider", "ops@example.com")]
    assert "provider_admin_invited" in console.get("/audit").text
    assert "provider_admin_invited" not in signed_in(admin).get("/audit").text


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "provider_admin.py"


def cli(*args):
    env = {**os.environ, "FLEET_DATA_DIR": str(DATA_DIR)}
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=60)


def test_cli_add_list_disable_enroll_link():
    r = cli("add", "Boss@Example.com", "The Boss")
    assert r.returncode == 0, r.stderr
    assert f"https://{CONSOLE_HOST}/enroll/" in r.stdout
    assert "boss@example.com" in cli("list").stdout
    assert cli("add", "boss@example.com", "Again").returncode == 1
    r = cli("enroll-link", "boss@example.com")
    assert r.returncode == 0 and "/enroll/" in r.stdout
    assert len(db_rows("SELECT 1 FROM provider_enrollments WHERE used_at IS NULL")) == 1   # the old link was retired
    assert cli("disable", "boss@example.com").returncode == 0
    assert "disabled" in cli("list").stdout
    assert cli("enroll-link", "boss@example.com").returncode == 1
    assert cli("disable", "nobody@example.com").returncode == 1
