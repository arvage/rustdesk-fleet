"""Suspend, reactivate, cancel and purge (app/console/lifecycle.py)."""
from datetime import datetime, timedelta, timezone

import pytest

from conftest import DATA_DIR, HOST, HOST_B, db_rows, new_client, provider_client, signed_in_at
from app.console import jobs, lifecycle, mail
from app.console.tenants import OWNED_TABLES
from app.deps import get_db
from app.tenancy import invalidate_cache


@pytest.fixture
def emails(monkeypatch):
    sent = []
    monkeypatch.setattr(mail, "send", lambda to, subject, html, event="": (sent.append((sorted(to), subject)) or (True, "")))
    return sent


def status(tid=2):
    return db_rows("SELECT * FROM tenants WHERE id = ?", tid)[0]


def run(c, path, **form):
    r = c.post(path, data=form, follow_redirects=False)
    jobs.run_pending()
    invalidate_cache()
    return r


def compose_calls(fake, tid=2):
    """The relay actions run for a tenant (through rdf-helper)."""
    return fake.relay_calls(tid)


def test_suspend_keeps_the_relay_and_reactivate_reopens(two_tenants, fake_system):
    c = provider_client()
    run(c, "/tenants/2/suspend")
    assert status()["status"] == "suspended"
    assert new_client(HOST_B).get("/login").status_code == 403
    assert new_client(HOST_B).get("/d/" + two_tenants["b"]["token"]).status_code == 403
    assert compose_calls(fake_system) == []          # relay untouched
    run(c, "/tenants/2/reactivate")
    assert status()["status"] == "active"
    assert new_client(HOST_B).get("/login").status_code == 200


def test_cancel_stops_the_relay_and_leaves_only_the_export_page(two_tenants, fake_system, emails):
    c = provider_client()
    r = run(c, "/tenants/2/cancel", confirm="wrong")
    assert status()["status"] == "active"
    run(c, "/tenants/2/cancel", confirm="globex")
    t = status()
    assert t["status"] == "cancelled" and compose_calls(fake_system) == ["stop"]
    cancelled = datetime.strptime(t["cancelled_at"], "%Y-%m-%d %H:%M:%S")
    assert datetime.strptime(t["purge_after"], "%Y-%m-%d %H:%M:%S") - cancelled == timedelta(days=30)
    assert emails and emails[0][0] == ["admin@b.example.com"]
    # The admin can still sign in, lands on the export page, and nothing else.
    b = signed_in_at(two_tenants["b"]["admin"], HOST_B)
    assert b.get("/", follow_redirects=False).headers["location"] == "/export"
    assert b.get("/export").status_code == 200
    csv = b.get("/export/devices.csv")
    assert csv.status_code == 200 and two_tenants["b"]["rid"] in csv.text
    for name in ("client-groups", "users", "sessions", "file-transfers"):
        assert b.get(f"/export/{name}.csv").status_code == 200
    for path in ("/devices", "/groups", "/users", "/d/" + two_tenants["b"]["token"]):
        assert b.get(path, follow_redirects=False).status_code == 403, path
    # Technicians (no manage_users) can't export.
    tech = signed_in_at(two_tenants["b"]["tech"], HOST_B)
    assert tech.get("/export/devices.csv", follow_redirects=False).status_code == 303
    # The device API still takes reports but pushes nothing.
    assert new_client(HOST_B).post("/api/heartbeat", json={}).status_code == 200


def test_reactivate_within_the_grace_period_starts_the_relay_again(two_tenants, fake_system, emails):
    c = provider_client()
    run(c, "/tenants/2/cancel", confirm="globex")
    run(c, "/tenants/2/reactivate")
    t = status()
    assert t["status"] == "active" and t["cancelled_at"] is None and t["purge_after"] is None
    assert compose_calls(fake_system) == ["stop", "up"]
    # After the grace period it's too late.
    run(c, "/tenants/2/cancel", confirm="globex")
    conn = get_db()
    conn.execute("UPDATE tenants SET purge_after = datetime('now', '-1 hour') WHERE id = 2")
    conn.commit()
    conn.close()
    job_id = jobs.enqueue("reactivate_tenant", 2, {}, "ops@example.com")
    jobs.run_pending()
    assert jobs.get(job_id)["status"] == "failed" and status()["status"] == "cancelled"


def test_warning_a_week_before_and_purge_after_the_grace_period(two_tenants, fake_system, emails):
    c = provider_client()
    run(c, "/tenants/2/cancel", confirm="globex")
    due = datetime.strptime(status()["purge_after"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    lifecycle.daily(now=due - timedelta(days=10))
    assert len(emails) == 1                       # just the cancellation email
    lifecycle.daily(now=due - timedelta(days=6))
    lifecycle.daily(now=due - timedelta(days=5))
    assert len(emails) == 2 and "deleted soon" in emails[1][1]
    assert db_rows("SELECT * FROM provider_jobs WHERE kind = 'purge_tenant'") == []

    conn = get_db()
    conn.execute("INSERT INTO tenant_usage_daily (day, tenant_id, devices) VALUES ('2026-10-01', 2, 7)")
    conn.commit()
    conn.close()
    (DATA_DIR / "tenants" / "2" / "relay").mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "tenants" / "2" / "relay" / "docker-compose.yml").write_text("services: {}\n")
    lifecycle.daily(now=due + timedelta(minutes=1))
    jobs.run_pending()
    invalidate_cache()
    t = status()
    assert t["status"] == "purged" and t["slug"] == "globex" and t["purged_at"]
    # Every tenant-owned row is gone...
    for table in OWNED_TABLES:
        if table == "provisioning_events":
            assert db_rows("SELECT * FROM provisioning_events WHERE tenant_id = 2 AND source IS NOT 'provider'") == []
        else:
            assert db_rows(f'SELECT * FROM "{table}" WHERE tenant_id = 2') == [], table
    assert db_rows("SELECT * FROM relays WHERE tenant_id = 2") == []
    assert db_rows("SELECT * FROM tenant_domains WHERE tenant_id = 2") == []
    assert not (DATA_DIR / "tenants" / "2").exists()
    # ...the relay, the vhost and the certificate were removed...
    assert "down" in compose_calls(fake_system)
    helper = [" ".join(c[3:]) for c in fake_system.calls if c[:2] == ["sudo", "-n"] and not c[3].startswith("relay-")]
    # The provider subdomain is unpublished; the custom domain released (DNS no longer matters).
    assert helper == ["purge-relay-data 2", f"remove-vhost {HOST_B}", "reload-nginx", f"revoke-cert {HOST_B}",
                      "release-domain rds.b.example"]
    # ...and what we keep is kept: usage, the provider log, the slug, the host is gone.
    assert db_rows("SELECT devices FROM tenant_usage_daily WHERE tenant_id = 2")[0][0] == 7
    assert db_rows("SELECT 1 FROM provisioning_events WHERE tenant_id = 2 AND event = 'tenant_cancelled' AND source = 'provider'")
    assert db_rows("SELECT 1 FROM provisioning_events WHERE tenant_id = 2 AND event = 'tenant_purged'")
    assert new_client(HOST_B).get("/login").status_code == 404
    # Tenant A is untouched.
    assert db_rows("SELECT COUNT(*) FROM devices WHERE tenant_id = 1")[0][0] == 1


def test_purged_port_blocks_are_never_reused(two_tenants, fake_system, emails, monkeypatch):
    from app.console import relays
    conn = get_db()
    relays.allocate_block(conn, 2, "globex")
    conn.commit()
    conn.close()
    c = provider_client()
    run(c, "/tenants/2/cancel", confirm="globex")
    run(c, "/tenants/2/purge", confirm="globex")
    assert status()["status"] == "purged"
    conn = get_db()
    assert relays.allocate_block(conn, 3, "initech").base == 21210
    conn.close()


def test_purge_now_needs_the_slug_and_a_cancelled_tenant(two_tenants, fake_system, emails):
    c = provider_client()
    run(c, "/tenants/2/purge", confirm="globex")
    assert status()["status"] == "active"          # not cancelled yet
    run(c, "/tenants/2/cancel", confirm="globex")
    run(c, "/tenants/2/purge", confirm="nope")
    assert status()["status"] == "cancelled"
    run(c, "/tenants/2/extend-grace", days="10")
    assert db_rows("SELECT 1 FROM provisioning_events WHERE event = 'tenant_grace_extended'")


def test_tenant_one_is_guarded(two_tenants, fake_system, emails):
    c = provider_client()
    run(c, "/tenants/1/cancel", confirm="tenant1")
    run(c, "/tenants/1/purge", confirm="tenant1")
    assert status(1)["status"] == "active"
    assert db_rows("SELECT * FROM provider_jobs") == []
    with pytest.raises(lifecycle.LifecycleError):
        lifecycle.purge_now(1)
    job_id = jobs.enqueue("cancel_tenant", 1, {}, "x")
    jobs.run_pending()
    assert jobs.get(job_id)["status"] == "failed" and status(1)["status"] == "active"
    assert new_client(HOST).get("/login").status_code == 200
