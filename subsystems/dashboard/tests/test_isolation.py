"""Tenant B can't see or touch tenant A's data through any route.

test_every_id_route_treats_another_tenants_id_as_missing walks every route
in the app that takes a path parameter. Signed in on tenant B, it requests
each one with tenant A's id and with an id that doesn't exist; the two
answers must be the same (status, redirect, flash message, body), never a
403, never showing A's data, and tenant A's rows must be untouched
afterwards. A new route with a path parameter this file doesn't know about
fails the test until it is added to TENANT_PARAMS or CATALOGUE_PARAMS.
"""
import re

import pytest

from conftest import HOST, HOST_B, all_routes, db_rows, login, new_client, signed_in_at
from app.tenant_migration import TENANT_TABLES

MISSING_INT = 987654


def tenant_param(path: str, name: str):
    """(key in the populate() dict, value that doesn't exist) for a path
    parameter that names tenant data, or None."""
    if name == "key" and path.startswith("/admin/roles/"):
        return "role_key", "no_such_role"
    if name == "group_id" and path.startswith("/dynamic-groups/"):
        return "dynamic_group_id", MISSING_INT
    if name == "filename" and path.startswith("/download/"):
        return "installer_file", "RemoteSupport-nope-1.4.9-linux.sh"
    if name == "token" and path.startswith("/invite/"):
        return "invite_token", "no-such-invite-" + "x" * 20
    return {
        "rustdesk_id": ("rid", "999999999"),
        "rid": ("rid", "999999999"),
        "slug": ("group_slug", "no-such-group"),
        "installer_id": ("installer_id", MISSING_INT),
        "user_id": ("user_id", MISSING_INT),
        "cred_id": ("cred_id", MISSING_INT),
        "link_id": ("link_id", MISSING_INT),
        "token": ("token", "no-such-token-" + "x" * 20),
    }.get(name)


# Path parameters that are not tenant data (fixed catalogues or public
# files), with the value to call them with.
CATALOGUE_PARAMS = {
    ("/notifications/events/{event_type}", "event_type"): "device_offline",   # EVENT_LABELS
    ("/admin/reports/{key}", "key"): "inventory",                             # reports.REPORTS
    ("/admin/reports/{key}/{section}.csv", "key"): "inventory",
    ("/admin/reports/{key}/{section}.csv", "section"): "0",
    ("/admin/emails/{key}/test", "key"): "device_offline",                    # email_templates.TEMPLATES
    ("/admin/emails/{key}", "key"): "device_offline",
    ("/d/{token}/{platform}", "platform"): "linux",
    ("/guides/{filename}", "filename"): "getting-help.pdf",                   # client-docs, public
    ("/export/{name}.csv", "name"): "devices",                                # routes/export.py EXPORTS
}

# Enough form fields for every POST route to get past validation to its
# lookup (a 422 would prove nothing).
FORM = {
    "role": "tech", "email": "someone@example.com", "display_name": "Someone", "new_slug": "renamed-group",
    "name": "Renamed", "description": "", "email_to": "client@example.com", "recipient_name": "",
    "message": "", "label": "x", "group_id": "", "confirm": "yes", "platform": "linux", "enabled": "1",
    "auto_update": "Y", "remote_config": "", "expiry": "7d", "max_uses": "", "note": "",
    "unattended_password": "", "clear_password": "", "brand_app_name": "", "next": "",
}


def _params(path):
    return re.findall(r"{(\w+)}", path)


def _tenant_rows(tenant_id):
    """Every tenant-owned row of one tenant, to prove nothing changed."""
    return {t: sorted(map(tuple, db_rows(f'SELECT * FROM "{t}" WHERE tenant_id = ?', tenant_id)))
            for t in TENANT_TABLES}


def _norm(text: str, values) -> str:
    for v in values:
        text = text.replace(str(v), "<ID>")
    return text


def _flash(client, location):
    """The flash message shown on the page a redirect leads to."""
    if not location or not location.startswith("/"):
        return None
    page = client.get(location, follow_redirects=False).text
    m = re.search(r'<div class="flash (\w+)">(.*?)</div>', page, re.S)
    return m.groups() if m else None


def _call(method, path, user, values):
    c = signed_in_at(user, HOST_B)
    url = path
    for name, value in values.items():
        url = url.replace("{" + name + "}", str(value))
    if method == "GET":
        r = c.get(url, follow_redirects=False)
    else:
        r = c.post(url, data=FORM, follow_redirects=False)
    loc = r.headers.get("location")
    return r, loc, _flash(c, loc)


def _variants(path, a, b):
    """(values with A's ids, values with missing ids) pairs to try. With two
    tenant parameters, also B's own parent with A's child (the sneaky case)."""
    names = _params(path)
    tenant_names = [n for n in names if tenant_param(path, n)]
    base = {n: CATALOGUE_PARAMS[(path, n)] for n in names if (path, n) in CATALOGUE_PARAMS}
    a_vals = {n: a[tenant_param(path, n)[0]] for n in tenant_names}
    missing = {n: tenant_param(path, n)[1] for n in tenant_names}
    out = [({**base, **a_vals}, {**base, **missing})]
    if len(tenant_names) == 2:
        parent, child = tenant_names
        own = {parent: b[tenant_param(path, parent)[0]]}
        out.append(({**base, **own, child: a_vals[child]}, {**base, **own, child: missing[child]}))
    return out


def test_every_route_parameter_is_classified():
    unknown = [(path, n) for _, path in all_routes() for n in _params(path)
               if not tenant_param(path, n) and (path, n) not in CATALOGUE_PARAMS]
    assert not unknown, ("Classify these path parameters in test_isolation.py "
                         "(TENANT_PARAMS or CATALOGUE_PARAMS): " + repr(unknown))


def test_every_id_route_treats_another_tenants_id_as_missing(two_tenants):
    a, b = two_tenants["a"], two_tenants["b"]
    before = _tenant_rows(1)
    routes = [(m, p) for m, p in all_routes() if _params(p) and any(tenant_param(p, n) for n in _params(p))]
    assert len(routes) >= 30, routes
    checked = []
    for method, path in routes:
        for a_vals, missing_vals in _variants(path, a, b):
            r_a, loc_a, flash_a = _call(method, path, b["admin"], a_vals)
            r_m, loc_m, flash_m = _call(method, path, b["admin"], missing_vals)
            where = f"{method} {path} {a_vals}"
            ids = [v for v in a_vals.values()] + [v for v in missing_vals.values()]
            assert r_a.status_code != 403, where
            assert r_a.status_code != 422, f"{where}: add form fields so the route reaches its lookup"
            assert r_a.status_code == r_m.status_code, (where, r_a.status_code, r_m.status_code, r_a.text[:300])
            assert _norm(loc_a or "", ids) == _norm(loc_m or "", ids), (where, loc_a, loc_m)
            assert _norm(str(flash_a), ids) == _norm(str(flash_m), ids), (where, flash_a, flash_m)
            assert _norm(r_a.text, ids) == _norm(r_m.text, ids), (where, r_a.text[:500], r_m.text[:500])
            assert a["secret"] not in r_a.text, where
            checked.append(where)
    assert _tenant_rows(1) == before, "a request on tenant B changed tenant A's data"


def test_form_ids_from_another_tenant_are_ignored(two_tenants):
    a, b = two_tenants["a"], two_tenants["b"]
    c = signed_in_at(b["admin"], HOST_B)
    # Put B's device in A's group: refused like a missing group.
    r = c.post(f"/devices/{b['rid']}/assign-group", data={"group_id": a["group_id"]}, follow_redirects=False)
    assert r.headers["location"] == "/devices"
    assert db_rows("SELECT group_id FROM devices WHERE tenant_id = 2 AND rustdesk_id = ?", b["rid"])[0][0] == b["group_id"]
    # Edit with A's group: the group is dropped, not linked across tenants.
    c.post(f"/devices/{b['rid']}/edit", data={"label": "x", "group_id": a["group_id"]})
    assert db_rows("SELECT group_id FROM devices WHERE tenant_id = 2 AND rustdesk_id = ?", b["rid"])[0][0] is None
    # Give a B user access to A's group: ignored.
    c.post(f"/users/{b['user_id']}/access", data={"group_ids": [a["group_id"], b["group_id"]]})
    assert [r[0] for r in db_rows("SELECT group_id FROM user_group_access WHERE user_id = ?", b["user_id"])] == [b["group_id"]]
    # Save over A's dynamic group: not found, A's unchanged.
    r = c.post("/dynamic-groups/save", data={"group_id": a["dynamic_group_id"], "name": "Mine", "match": "all",
                                              "rules": '[{"field": "hostname", "op": "contains", "value": "x"}]'},
               follow_redirects=False)
    assert r.headers["location"] == "/dynamic-groups"
    assert db_rows("SELECT name FROM dynamic_groups WHERE id = ?", a["dynamic_group_id"])[0][0] == "Dyn-A-secret"


def test_lists_show_only_the_tenants_own_rows(two_tenants):
    a, b = two_tenants["a"], two_tenants["b"]
    c = signed_in_at(b["admin"], HOST_B)
    for path in ("/devices", "/groups", "/users", "/audit", "/audit?cat=sessions", "/timeline",
                 "/dynamic-groups", "/admin/roles", "/admin/emails", "/notifications", "/api/devices",
                 "/api/devices/status", "/admin/export/devices.csv", f"/groups/{b['group_slug']}"):
        text = c.get(path).text
        assert a["secret"] not in text and a["rid"] not in text, path
    assert b["secret"] in c.get("/devices").text


# ── Sign-in ──────────────────────────────────────────────────────────────────

def test_a_user_of_a_cannot_sign_in_on_bs_host(two_tenants):
    a = two_tenants["a"]
    r = login(new_client(HOST_B), a["admin"])
    assert r.status_code == 401
    assert login(new_client(HOST), a["admin"]).status_code == 303


def test_the_same_email_can_be_a_user_of_both_tenants(two_tenants):
    from conftest import create_user
    create_user("shared@example.com", "admin", password="password-for-a", tenant_id=1)
    create_user("shared@example.com", "viewer", password="password-for-b", tenant_id=2)
    a_user = {"email": "shared@example.com", "password": "password-for-a"}
    b_user = {"email": "shared@example.com", "password": "password-for-b"}
    assert login(new_client(HOST), a_user).status_code == 303
    assert login(new_client(HOST_B), b_user).status_code == 303
    assert login(new_client(HOST), b_user).status_code == 401
    assert login(new_client(HOST_B), a_user).status_code == 401
    # And each lands in its own tenant with its own role.
    assert "Administrator" in signed_in_at(a_user, HOST).get("/account").text
    assert "Viewer" in signed_in_at(b_user, HOST_B).get("/account").text


def test_a_session_cookie_from_a_is_rejected_on_bs_host(two_tenants):
    a_client = signed_in_at(two_tenants["a"]["admin"], HOST)
    cookie = a_client.cookies.get("__Host-rdf_session")
    b_client = new_client(HOST_B)
    b_client.cookies.set("__Host-rdf_session", cookie)
    r = b_client.get("/devices", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    # The session was cleared, not just refused once.
    assert b_client.get("/devices", follow_redirects=False).headers["location"] == "/login"
    # Still fine at home.
    assert a_client.get("/devices").status_code == 200


def test_email_lockouts_are_per_tenant(two_tenants):
    from conftest import create_user
    create_user("shared@example.com", "admin", password="password-for-a", tenant_id=1)
    create_user("shared@example.com", "admin", password="password-for-b", tenant_id=2)
    c = new_client(HOST_B)
    for _ in range(6):
        c.post("/login", data={"email": "shared@example.com", "password": "wrong"}, headers={"X-Real-IP": "203.0.113.1"})
    assert login(new_client(HOST_B), {"email": "shared@example.com", "password": "password-for-b"}).status_code == 429
    assert login(new_client(HOST), {"email": "shared@example.com", "password": "password-for-a"}).status_code == 303


def test_a_pending_passkey_sign_in_doesnt_carry_to_another_tenant(two_tenants):
    from conftest import create_user
    from app.deps import get_db
    u = create_user("pk@example.com", "admin", tenant_id=1)
    conn = get_db()
    conn.execute("UPDATE users SET auth_method = 'passkey' WHERE id = ?", (u["id"],))
    conn.execute("INSERT INTO webauthn_credentials (tenant_id, user_id, credential_id, public_key) VALUES (1, ?, 'cGs', x'00')",
                 (u["id"],))
    conn.commit()
    conn.close()
    c = new_client(HOST)
    assert c.post("/login", data={"email": u["email"], "password": ""}, follow_redirects=False).headers["location"] == "/login/webauthn"
    cookie = c.cookies.get("__Host-rdf_session")
    other = new_client(HOST_B)
    other.cookies.set("__Host-rdf_session", cookie)
    assert other.post("/login/webauthn/begin").status_code == 400
    assert other.get("/login/webauthn", follow_redirects=False).headers["location"] == "/login"
    # At home the passkey step goes ahead, with the home domain as the RP ID.
    r = c.post("/login/webauthn/begin")
    assert r.status_code == 200 and r.json()["rpId"] == HOST


def test_setup_is_only_for_tenant_one(admin):
    """Other tenants get their first admin from an invite link (provider
    console), never from /setup and SETUP_TOKEN."""
    from conftest import SETUP_TOKEN, add_tenant
    add_tenant(3, "initech", "c.rds.example.com")
    c = new_client("c.rds.example.com")
    assert c.get("/", follow_redirects=False).headers["location"] == "/login"
    assert c.get("/setup").status_code == 404
    r = c.post("/setup", data={"email": "boss@initech.example.com", "display_name": "Boss",
                               "password": "long-password-1", "password2": "long-password-1",
                               "setup_token": SETUP_TOKEN}, follow_redirects=False)
    assert r.status_code == 404
    assert db_rows("SELECT * FROM users WHERE tenant_id = 3") == []
    assert new_client(HOST).get("/setup", follow_redirects=False).headers["location"] == "/"
