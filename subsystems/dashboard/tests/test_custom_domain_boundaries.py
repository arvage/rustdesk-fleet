"""Custom domains at the edges (step 9): installers never name one, links
in emails use the primary domain, the phase 1c protections hold on a custom
domain, and tenant A's custom domain never reaches tenant B."""
import re

import pytest

from app import domains
from app.deps import get_db
from conftest import HOST, HOST_B, PASSWORD, new_client, signed_in_at
from installer_render import PLATFORMS, render_tenant_installers
from test_device_api_tenancy import report
from test_installers_tenancy import _group


def _sql(sql, *params):
    conn = get_db()
    cur = conn.execute(sql, params)
    conn.commit()
    rows = cur.fetchall()
    conn.close()
    return rows


def _make_primary(tenant_id, host):
    conn = get_db()
    did = conn.execute("SELECT id FROM tenant_domains WHERE hostname = ?", (host,)).fetchone()[0]
    domains.make_primary(conn, tenant_id, did)
    conn.close()


@pytest.mark.parametrize("tenant_id", [1, 2])
def test_installers_never_contain_a_custom_domain(two_tenants, tmp_path, tenant_id):
    t = two_tenants["a" if tenant_id == 1 else "b"]
    _make_primary(tenant_id, t["custom_host"])
    _group(tenant_id, "acme")
    out = render_tenant_installers(tenant_id, "acme", tmp_path)
    for platform in PLATFORMS:
        assert t["custom_host"] not in out[platform], platform
    if tenant_id == 2:
        assert 'api-server = "https://b.rds.example.com"' in out["windows-x64"]


def test_download_link_email_uses_the_primary_domain(two_tenants, monkeypatch):
    b = two_tenants["b"]
    _make_primary(2, b["custom_host"])
    seen = {}
    from app.routes import links

    def fake_send(tid, **kw):
        seen.update(kw)
        return True, ""
    monkeypatch.setattr(links, "send_download_link_email", fake_send)
    c = signed_in_at(b["admin"], HOST_B)
    c.post(f"/groups/{b['group_slug']}/links/{b['link_id']}/email", data={"email_to": "client@example.com"})
    assert seen["url"] == f"https://{b['custom_host']}/d/{b['token']}"
    # Back to the provider subdomain when the custom domain stops being primary.
    _make_primary(2, HOST_B)
    c.post(f"/groups/{b['group_slug']}/links/{b['link_id']}/email", data={"email_to": "client@example.com"})
    assert seen["url"] == f"https://{HOST_B}/d/{b['token']}"


# ── Phase 1c protections on a custom domain ─────────────────────────────────

def test_headers_and_csp_on_a_custom_domain(two_tenants):
    host = two_tenants["b"]["custom_host"]
    c = new_client(host)
    r1, r2 = c.get("/login"), c.get("/login")
    for h in ("strict-transport-security", "x-content-type-options", "referrer-policy", "x-frame-options",
              "permissions-policy", "cross-origin-opener-policy"):
        assert r1.headers.get(h), h
    csp = r1.headers.get("content-security-policy-report-only") or r1.headers.get("content-security-policy")
    nonce = re.search(r"'nonce-([^']+)'", csp).group(1)
    other = (r2.headers.get("content-security-policy-report-only") or r2.headers.get("content-security-policy"))
    assert nonce not in other and f'nonce="{nonce}"' in r1.text


def test_host_only_cookie_on_a_custom_domain(two_tenants):
    b = two_tenants["b"]
    c = new_client(b["custom_host"])
    r = c.post("/login", data={"email": b["admin"]["email"], "password": PASSWORD}, follow_redirects=False)
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("__Host-rdf_session=") and "path=/;" in cookie.lower() and "secure" in cookie.lower()
    assert "domain=" not in cookie.lower()


@pytest.mark.parametrize("headers,ok", [
    ({"Sec-Fetch-Site": "same-origin"}, True),
    ({"Sec-Fetch-Site": "same-site"}, False),
    ({"Sec-Fetch-Site": "cross-site"}, False),
    ({"Sec-Fetch-Site": "", "Origin": "https://rds.b.example"}, True),
    ({"Sec-Fetch-Site": "", "Origin": f"https://{HOST_B}"}, False),       # its own provider subdomain is another origin
    ({"Sec-Fetch-Site": ""}, False),
])
def test_origin_checks_on_a_custom_domain(two_tenants, headers, ok):
    b = two_tenants["b"]
    c = signed_in_at(b["admin"], b["custom_host"])
    hdrs = {k: v for k, v in headers.items() if v}
    if "Sec-Fetch-Site" not in hdrs:
        c.headers.pop("Sec-Fetch-Site", None)
    r = c.post("/account/profile", data={"display_name": "Changed", "email": b["admin"]["email"]}, headers=hdrs,
               follow_redirects=False)
    assert (r.status_code != 403) == ok, (headers, r.status_code)


# ── Isolation ────────────────────────────────────────────────────────────────

def test_tenant_a_custom_domain_never_reaches_tenant_b(two_tenants):
    a, b = two_tenants["a"], two_tenants["b"]
    # B's admin can't sign in on A's custom domain...
    c = new_client(a["custom_host"])
    r = c.post("/login", data={"email": b["admin"]["email"], "password": PASSWORD}, follow_redirects=False)
    assert r.status_code == 401
    # ...nor carry B's session there.
    b_client = signed_in_at(b["admin"], b["custom_host"])
    on_a = new_client(a["custom_host"])
    on_a.cookies.set("__Host-rdf_session", b_client.cookies.get("__Host-rdf_session"))
    assert on_a.get("/devices", follow_redirects=False).status_code == 303
    # A signed in on A's custom domain sees only A.
    a_client = signed_in_at(a["admin"], a["custom_host"])
    for page in ("/devices", "/groups", "/audit", "/admin/domains"):
        text = a_client.get(page).text
        assert b["secret"] not in text and b["custom_host"] not in text, page
    # B's ids on A's custom domain: not found, never B's data.
    r = a_client.get(f"/groups/{b['group_slug']}", follow_redirects=False)
    assert r.status_code in (303, 404) and b["secret"] not in r.text
    # A device of B reporting on A's custom domain is rejected like an unknown one.
    assert report(new_client(a["custom_host"]), b).json() == {}


def test_tenant1_is_unchanged_by_its_own_custom_domain(two_tenants):
    a = two_tenants["a"]
    # Its legacy host still answers, is still primary, and still the RP ID.
    conn = get_db()
    assert domains.primary_host(conn, 1) == HOST and domains.home_host(conn, 1) == HOST
    conn.close()
    assert signed_in_at(a["admin"], HOST).get("/devices").status_code == 200
