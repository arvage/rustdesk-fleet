"""The device API checks each report against the hbbs peer table of the
tenant the request's Host belongs to (routes/client_api.py, app/tenancy.py)."""
import base64

from conftest import HOST, HOST_B, db_rows, new_client
from app import tenancy
from app.deps import get_db

LEGACY = {"X-Fleet-Listener": "legacy-api"}


def report(client, dev, path="/api/heartbeat", headers=None, **extra):
    body = {"id": dev["rid"], "uuid": base64.b64encode(dev["uuid"]).decode(), "conns": [], **extra}
    return client.post(path, json=body, headers=headers)


def info_tenants(rid):
    return sorted(r["tenant_id"] for r in db_rows("SELECT tenant_id FROM device_info WHERE rustdesk_id = ?", rid))


def test_each_tenant_accepts_its_own_devices(two_tenants):
    a, b = two_tenants["a"], two_tenants["b"]
    assert report(new_client(HOST), a, "/api/sysinfo", hostname="PC").text == "SYSINFO_UPDATED"
    assert report(new_client(HOST_B), b, "/api/sysinfo", hostname="PC").text == "SYSINFO_UPDATED"
    assert info_tenants(a["rid"]) == [1]
    assert info_tenants(b["rid"]) == [2]


def test_a_report_on_b_for_a_device_in_a_is_rejected(two_tenants):
    a = two_tenants["a"]
    before = db_rows("SELECT * FROM device_info")
    c = new_client(HOST_B)
    assert report(c, a).json() == {}
    assert report(c, a, "/api/sysinfo", hostname="X").text == "REJECTED"
    assert report(c, a, "/api/audit/conn", conn_id=1, action="new").status_code == 400
    assert report(c, a, "/api/audit/file", type=0, info='{"num": 1}').status_code == 400
    assert [tuple(r) for r in db_rows("SELECT * FROM device_info")] == [tuple(r) for r in before]
    assert db_rows("SELECT * FROM device_sessions WHERE tenant_id = 2 AND rustdesk_id = ?", a["rid"]) == []


def test_legacy_listener_maps_an_unknown_host_to_tenant_one(two_tenants):
    a, b = two_tenants["a"], two_tenants["b"]
    ip_host = new_client("203.0.113.9")
    # Without the listener header an IP host is simply unknown.
    assert report(ip_host, a).json() == {}
    # On the legacy listener it is tenant #1...
    assert report(ip_host, a, "/api/sysinfo", headers=LEGACY, hostname="PC").text == "SYSINFO_UPDATED"
    assert info_tenants(a["rid"]) == [1]
    # ...so a tenant B device there is checked against tenant #1's relay, and rejected.
    assert report(ip_host, b, "/api/sysinfo", headers=LEGACY, hostname="PC").text == "REJECTED"
    # And a known host keeps its own tenant, header or not.
    assert report(new_client(HOST_B), b, "/api/sysinfo", headers=LEGACY, hostname="PC").text == "SYSINFO_UPDATED"


def test_legacy_listener_header_does_nothing_outside_the_device_api(two_tenants):
    c = new_client("203.0.113.9")
    for path in ("/", "/login", "/d/" + two_tenants["a"]["token"]):
        assert c.get(path, headers=LEGACY).status_code == 404


def test_a_relay_host_reaches_its_tenants_device_api_only(two_tenants):
    b = two_tenants["b"]
    c = new_client("relay-globex.example.com")
    assert report(c, b, "/api/sysinfo", hostname="PC").text == "SYSINFO_UPDATED"
    assert info_tenants(b["rid"]) == [2]
    assert report(c, two_tenants["a"]).json() == {}
    assert c.get("/login").status_code == 404


def _manage_policy(tenant_id, group_id):
    conn = get_db()
    conn.execute("UPDATE client_groups SET auto_update = 'N', policy_ts = 1234 WHERE tenant_id = ? AND id = ?",
                 (tenant_id, group_id))
    conn.commit()
    conn.close()


def test_suspended_tenant_reports_are_kept_but_get_no_config(two_tenants):
    b = two_tenants["b"]
    _manage_policy(2, b["group_id"])
    c = new_client(HOST_B)
    assert report(c, b).json().get("strategy") == {"config_options": {"allow-auto-update": "N"}}

    conn = get_db()
    conn.execute("UPDATE tenants SET status = 'suspended' WHERE id = 2")
    conn.commit()
    conn.close()
    tenancy.invalidate_cache()
    r = report(c, b, conns=[7])
    assert r.status_code == 200 and "strategy" not in r.json()
    assert db_rows("SELECT 1 FROM device_sessions WHERE tenant_id = 2 AND conn_ref = '7'")
    # Its dashboard and download links are closed meanwhile.
    assert c.get("/login").status_code == 403
    assert c.get("/d/" + b["token"]).status_code == 403
