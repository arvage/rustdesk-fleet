"""Quantities (step 6): what counts, pushing licensed quantities (debounced,
forced daily, during the trial), metered device peaks with one meter event
per tenant per day, and soft limits."""
import json
from datetime import datetime, timedelta, timezone

import pytest

import fake_stripe
from app import billing_usage, plans
from app.deps import get_db
from billing_helpers import billing, deliver, sign_up, sql  # noqa: F401
from conftest import create_user


def _item_quantities(fake, sub):
    return sorted((i["price"]["id"], i.get("quantity")) for i in fake.sub(sub)["items"]["data"])


@pytest.fixture
def tenant(billing):
    tid, sub = sign_up(billing)
    return billing, tid, sub


def test_what_counts(tenant):
    b, tid, sub = tenant
    conn = get_db()
    assert billing_usage.counts(conn, tid) == {"technician": 1, "device": 0}
    conn.close()
    create_user("tech@abc.example", "tech", tenant_id=tid)
    sql("INSERT INTO tenant_invites (tenant_id, email, display_name, role, token_hash, expires_at, created_by)"
        " VALUES (?, 'pending@abc.example', 'P', 'tech', 'h', datetime('now', '+1 day'), 'x')", tid)
    for i, hidden in enumerate((0, 0, 1)):
        sql("INSERT INTO devices (tenant_id, rustdesk_id, status, hidden) VALUES (?, ?, 'registered', ?)",
            tid, f"90000000{i}", hidden)
    conn = get_db()
    assert billing_usage.counts(conn, tid) == {"technician": 2, "device": 2}
    conn.close()


def test_creation_pushes_the_real_counts_during_the_trial(tenant):
    b, tid, sub = tenant
    p = b["prices"]
    assert b["fake"].sub(sub)["status"] == "trialing"
    assert dict(_item_quantities(b["fake"], sub)) == {p["flat"]: 1, p["technician"]: 1, p["device"]: 0}
    update = b["fake"].requests("POST", f"/v1/subscriptions/{sub}")
    assert any(u.get("proration_behavior") == "create_prorations" for u in update)


def test_changes_are_pushed_at_most_once_an_hour(tenant, monkeypatch):
    b, tid, sub = tenant
    monkeypatch.setenv("STRIPE_PRORATION", "none")
    now = datetime.now(timezone.utc)
    create_user("tech@abc.example", "tech", tenant_id=tid)
    assert billing_usage.push_quantities(tid, at=now) is None          # pushed at creation, less than an hour ago
    later = now + timedelta(hours=1, minutes=1)
    assert billing_usage.push_quantities(tid, at=later) == {"technician": 2, "device": 0}
    assert dict(_item_quantities(b["fake"], sub))[b["prices"]["technician"]] == 2
    assert b["fake"].requests("POST", f"/v1/subscriptions/{sub}")[-1]["proration_behavior"] == "none"
    n = len(b["fake"].requests("POST", f"/v1/subscriptions/{sub}"))
    assert billing_usage.push_quantities(tid, at=later + timedelta(hours=5)) is None      # unchanged: nothing sent
    assert len(b["fake"].requests("POST", f"/v1/subscriptions/{sub}")) == n
    # The daily job pushes whatever the debounce says.
    create_user("third@abc.example", "tech", tenant_id=tid)
    assert billing_usage.push_quantities(tid, force=True, at=later + timedelta(minutes=5))["technician"] == 3


def test_metered_device_peaks_one_event_per_tenant_per_day(billing):
    fake = billing["fake"]
    meter = fake.add_meter("rdf_devices")
    conn = get_db()
    metered = plans.save(conn, None, {"key": "metered", "name": "Metered", "is_public": "1", "sort_order": "0",
                                      "price_flat": fake.add_price(1000),
                                      "price_device": fake.add_price(3, usage_type="metered", meter=meter)})
    conn.close()
    tid, sub = sign_up(billing, slug="meter", email="m@meter.example", plan=metered)
    day = datetime(2026, 11, 2, 9, tzinfo=timezone.utc)
    for hour, devices in ((0, 3), (5, 7), (9, 5)):
        sql("DELETE FROM devices WHERE tenant_id = ?", tid)
        for i in range(devices):
            sql("INSERT INTO devices (tenant_id, rustdesk_id, status, hidden) VALUES (?, ?, 'registered', 0)",
                tid, f"8000000{i:02d}")
        billing_usage.record_peaks(at=day + timedelta(hours=hour))
    assert billing_usage.report_meters(at=day + timedelta(hours=10)) == 0          # the day isn't over
    assert billing_usage.report_meters(at=day + timedelta(days=1)) == 1
    ev = fake.meter_events[f"rdf-devices-{tid}-2026-11-02"]
    assert ev["event_name"] == "rdf_devices" and ev["payload"]["value"] == "7"
    assert ev["payload"]["stripe_customer_id"] == sql("SELECT stripe_customer_id FROM billing_customers WHERE tenant_id = ?", tid)[0][0]
    assert int(ev["timestamp"]) == int(datetime(2026, 11, 2, 23, 59, 59, tzinfo=timezone.utc).timestamp())
    # Reported once; a lost reply that makes us send again is refused by Stripe and still marked done.
    assert billing_usage.report_meters(at=day + timedelta(days=1, hours=1)) == 0
    sql("UPDATE billing_device_peaks SET reported_at = NULL")
    assert billing_usage.report_meters(at=day + timedelta(days=2)) == 1
    assert len(fake.meter_events) == 1
    # A metered device price has no quantity to push.
    assert "quantity" not in [i for i in fake.sub(sub)["items"]["data"] if i["price"]["recurring"]["usage_type"] == "metered"][0]


def test_soft_limits(tenant):
    b, tid, sub = tenant           # plan: up to 3 technicians, 2 devices
    conn = get_db()
    assert billing_usage.over_limits(conn, tid) == []
    for i in range(3):
        sql("INSERT INTO devices (tenant_id, rustdesk_id, status, hidden) VALUES (?, ?, 'registered', 0)", tid, f"70000000{i}")
    assert billing_usage.over_limits(conn, tid) == ["3 devices (plan: up to 2)"]
    conn.close()


def test_tenant1_quantities_are_never_pushed(tenant):
    assert billing_usage.push_quantities(1, force=True) is None
