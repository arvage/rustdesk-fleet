"""The 14-day trial (app/console/lifecycle.py): its end date, the warning,
the suspension at the end, extending it, and what reactivating goes back to."""
from datetime import datetime, timedelta, timezone

import pytest

from conftest import HOST_B, db_rows, new_client, provider_client, signed_in_at
from app.console import jobs, lifecycle, mail
from app.console.tenants import ensure_tables
from app.deps import get_db
from app.tenancy import invalidate_cache

FMT = "%Y-%m-%d %H:%M:%S"


@pytest.fixture
def emails(monkeypatch):
    sent = []
    monkeypatch.setattr(mail, "send", lambda to, subject, html, event="": (sent.append((sorted(to), subject)) or (True, "")))
    return sent


def _trial(ends: datetime, status: str = "trial") -> None:
    conn = get_db()
    conn.execute("UPDATE tenants SET status = ?, trial_ends_at = ?, trial_warning_sent_at = NULL WHERE id = 2",
                 (status, ends.strftime(FMT)))
    conn.commit()
    conn.close()
    invalidate_cache()


def tenant(tid=2):
    return db_rows("SELECT * FROM tenants WHERE id = ?", tid)[0]


def run(c, path, **form):
    r = c.post(path, data=form, follow_redirects=False)
    jobs.run_pending()
    invalidate_cache()
    return r


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def test_existing_trials_get_an_end_date_14_days_after_creation(two_tenants):
    conn = get_db()
    conn.execute("UPDATE tenants SET status = 'trial', trial_ends_at = NULL, created_at = '2026-10-01 08:00:00' WHERE id = 2")
    conn.commit()
    ensure_tables(conn)
    conn.close()
    assert tenant()["trial_ends_at"] == "2026-10-15 08:00:00"
    assert tenant(1)["trial_ends_at"] is None


def test_warning_three_days_before_then_suspended_at_the_end(two_tenants, emails):
    due = _now() + timedelta(days=10)
    _trial(due)
    lifecycle.daily(now=due - timedelta(days=5))
    assert emails == []
    lifecycle.daily(now=due - timedelta(days=2))
    lifecycle.daily(now=due - timedelta(days=1))
    assert len(emails) == 1 and "ends soon" in emails[0][1] and emails[0][0] == ["admin@b.example.com"]
    assert tenant()["status"] == "trial"
    lifecycle.daily(now=due + timedelta(minutes=1))
    invalidate_cache()
    assert tenant()["status"] == "suspended"
    assert "has ended" in emails[-1][1]
    assert new_client(HOST_B).get("/login").status_code == 403
    assert db_rows("SELECT 1 FROM provisioning_events WHERE tenant_id = 2 AND event = 'tenant_trial_expired'")


def test_tenant_one_is_never_suspended_by_the_trial_sweep(two_tenants, emails):
    conn = get_db()
    conn.execute("UPDATE tenants SET status = 'trial', trial_ends_at = '2020-01-01 00:00:00' WHERE id = 1")
    conn.commit()
    conn.close()
    lifecycle.daily()
    assert tenant(1)["status"] == "trial"


def test_suspending_a_trial_and_reactivating_keeps_it_on_trial(two_tenants, fake_system):
    ends = _now() + timedelta(days=9)
    _trial(ends)
    c = provider_client()
    run(c, "/tenants/2/suspend")
    assert tenant()["status"] == "suspended"
    run(c, "/tenants/2/reactivate")
    assert tenant()["status"] == "trial" and tenant()["trial_ends_at"] == ends.strftime(FMT)


def test_reactivating_after_the_trial_ended_makes_it_active(two_tenants, fake_system):
    _trial(_now() - timedelta(days=1), status="suspended")
    run(provider_client(), "/tenants/2/reactivate")
    t = tenant()
    assert t["status"] == "active" and t["trial_ends_at"] is None


def test_extending_an_ended_trial_then_reactivating_puts_it_back_on_trial(two_tenants, fake_system):
    _trial(_now() - timedelta(days=3), status="suspended")
    c = provider_client()
    run(c, "/tenants/2/extend-trial", days="7")
    ends = datetime.strptime(tenant()["trial_ends_at"], FMT).replace(tzinfo=timezone.utc)
    assert timedelta(days=6, hours=23) < ends - _now() <= timedelta(days=7)   # from now, not from the old end
    assert tenant()["status"] == "suspended"
    run(c, "/tenants/2/reactivate")
    assert tenant()["status"] == "trial"


def test_extending_a_running_trial_adds_to_its_end(two_tenants):
    ends = _now() + timedelta(days=2)
    _trial(ends)
    conn = get_db()
    conn.execute("UPDATE tenants SET trial_warning_sent_at = datetime('now') WHERE id = 2")
    conn.commit()
    conn.close()
    run(provider_client(), "/tenants/2/extend-trial", days="14")
    t = tenant()
    assert t["trial_ends_at"] == (ends + timedelta(days=14)).strftime(FMT)
    assert t["trial_warning_sent_at"] is None


def test_activating_clears_the_trial(two_tenants):
    _trial(_now() + timedelta(days=5))
    run(provider_client(), "/tenants/2/activate")
    t = tenant()
    assert t["status"] == "active" and t["trial_ends_at"] is None


def test_an_active_tenant_has_no_trial_to_extend(two_tenants):
    run(provider_client(), "/tenants/2/extend-trial", days="7")
    assert tenant()["trial_ends_at"] is None


def test_the_banner_shows_the_end_date_and_days_left(two_tenants):
    ends = _now() + timedelta(days=5, hours=1)
    _trial(ends)
    c = signed_in_at(two_tenants["b"]["admin"], HOST_B)
    for path in ("/", "/devices"):
        text = c.get(path).text
        assert "6 days left" in text and "is suspended" in text, path


def test_home_device_count_skips_devices_deleted_in_the_dashboard(two_tenants, monkeypatch):
    from app.routes import api
    b = two_tenants["b"]
    monkeypatch.setattr(api, "_container_states", lambda t: {})
    monkeypatch.setattr(api, "_compute_status", lambda t: {b["rid"]: "online", "777": "online"})
    c = signed_in_at(b["admin"], HOST_B)
    assert c.get("/api/server/health").json()["devices"] == {"online": 2, "total": 2}
    conn = get_db()
    conn.execute("UPDATE devices SET hidden = 1 WHERE tenant_id = 2 AND rustdesk_id = ?", (b["rid"],))
    conn.commit()
    conn.close()
    assert c.get("/api/server/health").json()["devices"] == {"online": 1, "total": 1}
