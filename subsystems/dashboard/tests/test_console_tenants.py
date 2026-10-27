"""The console's tenant list and detail, the plan, and daily usage."""
from datetime import date

from conftest import db_rows, populate, provider_client
from app.console import maintenance
from app.deps import get_db


def test_list_and_search(two_tenants, fake_system):
    c = provider_client()
    page = c.get("/").text
    for slug in ("tenant1", "globex"):
        assert f"<code>{slug}</code>" in page
    assert "<code>globex</code>" not in c.get("/?q=tenant1").text
    assert "<code>globex</code>" in c.get("/?q=GLOB").text


def test_detail_shows_relay_admins_usage_and_saves_the_plan(two_tenants, fake_system):
    c = provider_client()
    page = c.get("/tenants/2").text
    assert "relay-globex.example.com" in page and "PUBKEY2" in page
    assert "admin@b.example.com" in page and "b.rds.example.com" in page
    r = c.post("/tenants/2/plan", data={"plan": "Pilot, 25 devices"}, follow_redirects=False)
    assert r.headers["location"] == "/tenants/2"
    assert db_rows("SELECT plan FROM tenants WHERE id = 2")[0][0] == "Pilot, 25 devices"
    assert db_rows("SELECT 1 FROM provisioning_events WHERE event = 'tenant_plan_changed' AND source = 'provider'")
    assert c.get("/tenants/99", follow_redirects=False).headers["location"] == "/"


def test_daily_usage_is_recorded_and_outlives_deletions(two_tenants):
    conn = get_db()
    conn.execute("UPDATE device_sessions SET started_at = datetime('now', 'start of day', '+1 hour'),"
                 " ended_at = datetime('now', 'start of day', '+2 hours') WHERE tenant_id = 2")
    conn.commit()
    conn.close()
    assert maintenance.record_usage() == 2
    today = date.today().isoformat()
    [row] = db_rows("SELECT * FROM tenant_usage_daily WHERE tenant_id = 2 AND day = ?", today)
    assert (row["devices"], row["technicians"], row["session_minutes"]) == (1, 2, 60)
    # Refreshing the same day updates the row instead of adding one.
    maintenance.record_usage()
    assert len(db_rows("SELECT * FROM tenant_usage_daily WHERE tenant_id = 2")) == 1
    conn = get_db()
    conn.execute("DELETE FROM device_sessions WHERE tenant_id = 2")
    conn.execute("DELETE FROM devices WHERE tenant_id = 2")
    conn.commit()
    conn.close()
    assert db_rows("SELECT session_minutes FROM tenant_usage_daily WHERE tenant_id = 2")[0][0] == 60
    assert "60" in provider_client().get("/tenants/2").text
