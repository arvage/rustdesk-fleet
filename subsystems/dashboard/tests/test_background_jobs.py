"""Background jobs run per tenant, with each tenant's own relay, rows and
notification recipients (routes/api.py, routes/home.py, notifications.py)."""
from conftest import add_peer_to, db_rows
from app import tenancy
from app.deps import get_db
from app.routes import api


def _fired(monkeypatch):
    calls = []
    monkeypatch.setattr(api, "fire_notification", lambda tid, event, ctx: calls.append((tid, event, ctx)))
    return calls


def test_new_devices_are_announced_to_their_own_tenant(two_tenants, monkeypatch):
    calls = _fired(monkeypatch)
    add_peer_to(two_tenants["b"]["hbbs"], "200000099", b"uuid-new")
    for t in tenancy.active_tenants():
        api.watch_tenant_devices(t)
    assert [tuple(r) for r in db_rows("SELECT tenant_id FROM devices WHERE rustdesk_id = '200000099'")] == [(2,)]
    assert [tuple(r) for r in db_rows(
        "SELECT tenant_id FROM provisioning_events WHERE event = 'device_registered' AND detail = '200000099'")] == [(2,)]
    assert [(tid, ev) for tid, ev, ctx in calls if ctx.get("rustdesk_id") == "200000099"] == [(2, "device_registered")]


def test_status_baselines_and_events_are_per_tenant(two_tenants, monkeypatch):
    calls = _fired(monkeypatch)
    api._prev_status.clear()
    a, b = two_tenants["a"], two_tenants["b"]
    tenants = {t.id: t for t in tenancy.active_tenants()}
    for t in tenants.values():
        api.watch_tenant_devices(t)          # first pass: baseline only
    assert api._prev_status[1] == {a["rid"]: "online"} and api._prev_status[2] == {b["rid"]: "online"}
    # B's device goes quiet (no heartbeat, and it registered long ago, so the
    # fallback signal doesn't count it online either): only B hears about it.
    import sqlite3
    hb = sqlite3.connect(b["hbbs"])
    hb.execute("UPDATE peer SET created_at = datetime('now', '-2 hours')")
    hb.commit()
    hb.close()
    conn = get_db()
    conn.execute("UPDATE device_info SET heartbeat_at = datetime('now', '-1 hour') WHERE tenant_id = 2")
    conn.execute("UPDATE device_info SET heartbeat_at = datetime('now') WHERE tenant_id = 1")
    conn.commit()
    conn.close()
    for t in tenants.values():
        api.watch_tenant_devices(t)
    assert [(tid, ev, ctx["rustdesk_id"]) for tid, ev, ctx in calls] == [(2, "device_offline", b["rid"])]
    assert [tuple(r) for r in db_rows("SELECT tenant_id, detail FROM provisioning_events WHERE event = 'device_offline'")] \
        == [(2, b["rid"])]


def test_the_session_sweeper_only_touches_its_tenant(two_tenants):
    conn = get_db()
    conn.execute("UPDATE device_sessions SET last_seen_at = datetime('now', '-1 hour'), ended_at = NULL")
    conn.commit()
    conn.close()
    from app.routes.client_api import sweep_stale_sessions
    assert sweep_stale_sessions(tenancy.load_tenant(2)) == 1
    open_by_tenant = {r[0]: r[1] for r in db_rows(
        "SELECT tenant_id, COUNT(*) FROM device_sessions WHERE ended_at IS NULL GROUP BY tenant_id")}
    assert open_by_tenant == {1: 1}


def test_notifications_use_each_tenants_own_settings(two_tenants, monkeypatch):
    from app import notifications
    sent = []
    monkeypatch.setattr(notifications, "_send_sync", lambda settings, to, subject, html, reply_to="":
                        sent.append((settings["tenant_id"], tuple(to), subject)))

    class _Now:  # run the worker inline
        def __init__(self, target, daemon):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(notifications.threading, "Thread", _Now)
    conn = get_db()
    conn.execute("UPDATE notification_settings SET enabled = 1, from_addr = 'fleet@example.com'")
    conn.commit()
    conn.close()
    notifications.fire_notification(2, "device_offline", {"rustdesk_id": "200000002"})
    notifications.fire_notification(1, "device_offline", {"rustdesk_id": "100000001"})
    assert [(t, to) for t, to, _ in sent] == [(2, ("ops@b.example.com",)), (1, ("ops@a.example.com",))]
    # Each tenant's own template override is used for its own email.
    assert "Subject-B-secret" in sent[0][2] and "Subject-A-secret" in sent[1][2]
    assert [tuple(r) for r in db_rows("SELECT tenant_id, recipients FROM notification_log ORDER BY id")] == [
        (2, "ops@b.example.com"), (1, "ops@a.example.com")]
    # A suspended tenant gets no email at all.
    conn = get_db()
    conn.execute("UPDATE tenants SET status = 'suspended' WHERE id = 2")
    conn.commit()
    conn.close()
    notifications.fire_notification(2, "device_offline", {"rustdesk_id": "200000002"})
    assert len(sent) == 2
