"""Every request is matched to a tenant by its Host (app/tenancy.py)."""
import base64

from conftest import HOST, add_peer, db_rows, new_client
from app import tenancy
from app.deps import get_db


def set_status(tenant_id: int, status: str) -> None:
    conn = get_db()
    conn.execute("UPDATE tenants SET status = ? WHERE id = ?", (status, tenant_id))
    conn.commit()
    conn.close()
    tenancy.invalidate_cache()


def heartbeat(client, rid="123456789", uuid=b"uuid-1", headers=None):
    return client.post("/api/heartbeat", headers=headers,
                       json={"id": rid, "uuid": base64.b64encode(uuid).decode(), "conns": []})


def test_tenant_one_is_the_existing_host():
    t = tenancy.resolve_host(HOST.upper() + ":443")
    assert t is not None and t.id == 1 and t.slug == "tenant1" and t.hostname == HOST
    assert t.relay is not None and t.relay.pubkey == "PUBKEY1"
    assert t.relay.ports == {21115, 21116, 21117, 21118, 21119}


def test_known_host_serves_the_landing_page_and_login(admin):
    c = new_client()
    assert c.get("/").status_code == 200
    assert c.get("/login").status_code == 200


def test_unknown_host_is_404_everywhere():
    c = new_client("unknown.example.net")
    for path in ("/", "/login", "/setup", "/account", "/static/style.css", "/d/sometoken",
                 "/guides/getting-help.pdf", "/devices", "/api/devices"):
        r = c.get(path, follow_redirects=False)
        assert r.status_code == 404, path
        assert "Not found" in r.text
        assert "set-cookie" not in r.headers
    assert c.post("/login", data={"email": "a@example.com", "password": "x"}).status_code == 404


def test_device_api_on_an_unknown_host_is_rejected_like_an_unknown_device():
    add_peer("123456789", b"uuid-1")
    r = heartbeat(new_client("203.0.113.9"))
    assert r.status_code == 200 and r.json() == {}
    assert db_rows("SELECT * FROM device_info") == []
    c = new_client("203.0.113.9")
    assert c.post("/api/sysinfo", json={"id": "123456789"}).text == "REJECTED"
    assert c.post("/api/audit/conn", json={}).status_code == 400


def test_suspended_tenant_gets_the_inactive_page(admin):
    set_status(1, "suspended")
    c = new_client()
    for path in ("/", "/login", "/account", "/d/sometoken"):
        r = c.get(path, follow_redirects=False)
        assert r.status_code == 403, path
        assert "not active" in r.text
    set_status(1, "cancelled")
    assert new_client().get("/login").status_code == 403


def test_suspended_tenant_still_accepts_device_reports():
    add_peer("123456789", b"uuid-1")
    set_status(1, "suspended")
    assert heartbeat(new_client()).status_code == 200
    assert [r["rustdesk_id"] for r in db_rows("SELECT rustdesk_id FROM device_info")] == ["123456789"]
