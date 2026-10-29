"""Cross-origin state changes are refused (app/security.py), and no GET
route changes the database."""
import hashlib
import logging
import re
import sqlite3

import pytest

from conftest import (CONSOLE_HOST, DATA_DIR, HOST, HOST_B, all_routes, create_user, login, new_client,
                      provider_client, signed_in_at)
from test_isolation import CATALOGUE_PARAMS, tenant_param

SIBLING = "https://acme.rds.example.com"


def _post(host, headers, path="/login"):
    c = new_client(host)
    c.headers.pop("Sec-Fetch-Site", None)
    return c.post(path, data={"email": "x@example.com", "password": "x"}, headers=headers, follow_redirects=False)


@pytest.mark.parametrize("host", [HOST, CONSOLE_HOST])
@pytest.mark.parametrize("headers", [
    {"Sec-Fetch-Site": "same-site", "Origin": SIBLING},
    {"Sec-Fetch-Site": "cross-site", "Origin": "https://evil.example.net"},
    {"Sec-Fetch-Site": "none"},
    {"Origin": SIBLING},
    {"Origin": "null"},
    {},
])
def test_cross_origin_posts_are_refused(host, headers, caplog):
    with caplog.at_level(logging.WARNING, logger="rdf.security"):
        r = _post(host, headers)
    assert r.status_code == 403 and "Request refused" in r.text
    assert "Refused cross-origin POST /login" in caplog.text


@pytest.mark.parametrize("host", [HOST, CONSOLE_HOST])
@pytest.mark.parametrize("headers", [
    {"Sec-Fetch-Site": "same-origin"},
    {"Sec-Fetch-Site": "same-origin", "Origin": SIBLING},     # Sec-Fetch-Site wins
])
def test_same_origin_posts_pass(host, headers):
    assert _post(host, headers).status_code != 403


def test_a_matching_origin_alone_passes():
    assert _post(HOST, {"Origin": f"https://{HOST}"}).status_code != 403
    assert _post(CONSOLE_HOST, {"Origin": f"https://{CONSOLE_HOST}"}).status_code != 403
    assert _post(HOST, {"Origin": f"https://{CONSOLE_HOST}"}).status_code == 403
    assert _post(HOST, {"Origin": f"http://{HOST}"}).status_code == 403


def test_refused_requests_are_not_written_to_the_audit_log():
    from conftest import db_rows
    before = db_rows("SELECT COUNT(*) FROM provisioning_events")[0][0]
    for _ in range(5):
        _post(HOST, {"Sec-Fetch-Site": "cross-site"})
    assert db_rows("SELECT COUNT(*) FROM provisioning_events")[0][0] == before


def test_device_api_is_unaffected(two_tenants):
    for path in ("/api/heartbeat", "/api/sysinfo", "/api/sysinfo_ver", "/api/audit/conn", "/api/audit/file"):
        r = _post(HOST, {}, path)
        assert r.status_code != 403, path


def test_a_cross_origin_post_with_a_valid_session_changes_nothing(two_tenants):
    from conftest import db_rows
    a = two_tenants["a"]
    c = signed_in_at(a["admin"], HOST)
    r = c.post(f"/groups/{a['group_slug']}/delete", headers={"Sec-Fetch-Site": "same-site"}, follow_redirects=False)
    assert r.status_code == 403
    assert db_rows("SELECT 1 FROM client_groups WHERE id = ?", a["group_id"])


# ── No GET route writes ──────────────────────────────────────────────────────

# GET routes that change state on purpose, with the reason.
GET_WRITES_ALLOWED = {
    "/d/{token}/{platform}": "a client downloading an installer: counts the link use and logs the download",
    "/support/enter": "provider support's way in: spends a single-use 60-second token from the console and starts"
                      " a support session (a cross-origin POST from the console would be refused)",
}

CONSOLE_PARAMS = {"tenant_id": 2, "job_id": 1, "token": "no-such-token"}


def _db_digest() -> str:
    conn = sqlite3.connect(DATA_DIR / "fleet.sqlite3")
    h = hashlib.sha256()
    for line in conn.iterdump():
        h.update(line.encode())
    conn.close()
    return h.hexdigest()


def _console_get_routes():
    from fastapi.routing import APIRoute, iter_route_contexts
    from app.console.main import console_app
    return sorted({ctx.path for ctx in iter_route_contexts(console_app.routes)
                   if isinstance(ctx.route, APIRoute) and "GET" in (ctx.methods or ())})


def _fill(path, values):
    for name in re.findall(r"{(\w+)}", path):
        path = path.replace("{" + name + "}", str(values(name)))
    return path


def test_no_get_route_writes_to_the_database(two_tenants, fake_system):
    a = two_tenants["a"]
    tenant = signed_in_at(a["admin"], HOST)
    console = provider_client()
    changed = []
    for method, path in all_routes():
        if method != "GET" or path in GET_WRITES_ALLOWED:
            continue
        url = _fill(path, lambda n: CATALOGUE_PARAMS.get((path, n)) or a[tenant_param(path, n)[0]])
        before = _db_digest()
        tenant.get(url, follow_redirects=False)
        if _db_digest() != before:
            changed.append(f"tenant GET {url}")
    for path in _console_get_routes():
        url = _fill(path, CONSOLE_PARAMS.get)
        before = _db_digest()
        console.get(url, follow_redirects=False)
        if _db_digest() != before:
            changed.append(f"console GET {url}")
    assert not changed, "GET routes changed the database (make them POSTs, or allowlist with a reason): " + repr(changed)
