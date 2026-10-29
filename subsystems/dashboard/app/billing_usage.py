"""What a Stripe tenant is billed for (phase 2; TENANCY.md, "Billing").

    billable technicians  the tenant's users (pending invitations aren't users
                          yet; provider support sessions never are)
    billable devices      non-hidden rows in the tenant's devices table

Licensed quantities (a plan's technician price, and its device price unless
metered) are pushed to the subscription's items when they change, at most
once an hour per tenant (the hourly maintenance pass), and by the daily
reconciliation whatever the debounce says. Trials included, so the first
invoice is right. Proration follows STRIPE_PRORATION.

Metered devices (a device price backed by a Billing Meter): the hourly pass
keeps each day's peak device count in billing_device_peaks, and once the
day is over it is reported as one meter event, timestamped the last second
of that day (UTC), with the identifier rdf-devices-<tenant>-<day>, so a
retry can never count a day twice. How those daily peaks become a bill is the
meter's aggregation, set in Stripe: `sum` bills device-days (price it per
device per day), `last` bills the last day's peak of the period.

Included quantities are the prices' free first tiers (app/plans.py): we
always report the real counts.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone

from app import settings, stripe_api
from app.deps import get_db
from app.tenant_migration import LEGACY_TENANT_ID

log = logging.getLogger("rdf.billing")

DEBOUNCE = timedelta(hours=1)
LIVE = ("trialing", "active", "past_due")

SCHEMA = """
    CREATE TABLE IF NOT EXISTS billing_device_peaks (
        tenant_id    INTEGER NOT NULL,
        day          TEXT NOT NULL,          -- YYYY-MM-DD (UTC)
        peak         INTEGER NOT NULL DEFAULT 0,
        reported_at  TEXT,
        PRIMARY KEY (tenant_id, day)
    );
"""


def ensure_table(conn) -> None:
    conn.executescript(SCHEMA)


def counts(conn, tenant_id: int) -> dict[str, int]:
    return {
        "technician": conn.execute("SELECT COUNT(*) FROM users WHERE tenant_id = ?", (tenant_id,)).fetchone()[0],
        "device": conn.execute("SELECT COUNT(*) FROM devices WHERE tenant_id = ? AND hidden = 0",
                               (tenant_id,)).fetchone()[0],
    }


def over_limits(conn, tenant_id: int) -> list[str]:
    """The plan's soft limits this tenant is over, in words (warn only)."""
    row = conn.execute("SELECT p.max_technicians, p.max_devices FROM billing_subscriptions s JOIN plans p"
                       " ON p.id = s.plan_id WHERE s.tenant_id = ?", (tenant_id,)).fetchone()
    if row is None:
        return []
    n = counts(conn, tenant_id)
    out = []
    if row["max_technicians"] is not None and n["technician"] > row["max_technicians"]:
        out.append(f"{n['technician']} technicians (plan: up to {row['max_technicians']})")
    if row["max_devices"] is not None and n["device"] > row["max_devices"]:
        out.append(f"{n['device']} devices (plan: up to {row['max_devices']})")
    return out


def _stripe_tenants(conn):
    return conn.execute("SELECT s.* FROM billing_subscriptions s JOIN tenants t ON t.id = s.tenant_id"
                        " WHERE t.billing_mode = 'stripe' AND t.id <> ? AND s.status IN (?, ?, ?)",
                        (LEGACY_TENANT_ID, *LIVE)).fetchall()


def push_quantities(tenant_id: int, force: bool = False, at: datetime | None = None) -> dict | None:
    """Push licensed quantities that changed. Returns what was pushed, or
    None (nothing changed, debounced, or nothing licensed by count)."""
    now = at or datetime.now(timezone.utc)
    conn = get_db()
    try:
        if tenant_id == LEGACY_TENANT_ID:
            return None
        bs = conn.execute("SELECT * FROM billing_subscriptions WHERE tenant_id = ?", (tenant_id,)).fetchone()
        if bs is None or bs["status"] not in LIVE or bs["plan_id"] is None:
            return None
        prices = {p["stripe_price_id"]: p["unit_type"] for p in conn.execute(
            "SELECT * FROM plan_prices WHERE plan_id = ? AND billing_scheme = 'licensed'"
            " AND unit_type IN ('technician', 'device')", (bs["plan_id"],))}
        if not prices:
            return None
        n = counts(conn, tenant_id)
        want = {u: n[u] for u in set(prices.values())}
        last = json.loads(bs["quantities"] or "{}")
        pushed_at = bs["quantities_pushed_at"]
        if not force:
            if last == want:
                return None
            if pushed_at and now - datetime.strptime(pushed_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) < DEBOUNCE:
                return None
    finally:
        conn.close()
    client = stripe_api.client()
    sub = stripe_api.plain(client.v1.subscriptions.retrieve(bs["stripe_subscription_id"]))
    items = []
    for item in (sub.get("items") or {}).get("data", []):
        price = item.get("price") or {}
        unit = prices.get(price.get("id") if isinstance(price, dict) else price)
        if unit and item.get("quantity") != want[unit]:
            items.append({"id": item["id"], "quantity": want[unit]})
    if items:
        client.v1.subscriptions.update(bs["stripe_subscription_id"],
                                       {"items": items, "proration_behavior": settings.stripe_proration()})
    conn = get_db()
    conn.execute("UPDATE billing_subscriptions SET quantities = ?, quantities_pushed_at = ? WHERE tenant_id = ?",
                 (json.dumps(want, sort_keys=True), now.strftime("%Y-%m-%d %H:%M:%S"), tenant_id))
    conn.commit()
    conn.close()
    if items:
        log.info("tenant %s: quantities %s", tenant_id, want)
    return want


def record_peaks(at: datetime | None = None) -> None:
    """Hourly: today's peak device count for tenants on a metered plan."""
    now = at or datetime.now(timezone.utc)
    day = now.date().isoformat()
    conn = get_db()
    for bs in _stripe_tenants(conn):
        if not _meter_event(conn, bs["plan_id"]):
            continue
        n = counts(conn, bs["tenant_id"])["device"]
        conn.execute("""INSERT INTO billing_device_peaks (tenant_id, day, peak) VALUES (?, ?, ?)
                        ON CONFLICT(tenant_id, day) DO UPDATE SET peak = MAX(peak, excluded.peak)""",
                     (bs["tenant_id"], day, n))
    conn.commit()
    conn.close()


def _meter_event(conn, plan_id) -> str | None:
    row = conn.execute("SELECT meter_event_name FROM plan_prices WHERE plan_id = ? AND unit_type = 'device'"
                       " AND billing_scheme = 'metered'", (plan_id,)).fetchone()
    return row[0] if row else None


def report_meters(at: datetime | None = None) -> int:
    """Report every finished day's peak not yet reported (idempotent)."""
    today = (at or datetime.now(timezone.utc)).date().isoformat()
    conn = get_db()
    rows = conn.execute("""SELECT p.*, c.stripe_customer_id, s.plan_id FROM billing_device_peaks p
                           JOIN billing_customers c ON c.tenant_id = p.tenant_id
                           JOIN billing_subscriptions s ON s.tenant_id = p.tenant_id
                           WHERE p.reported_at IS NULL AND p.day < ? AND p.tenant_id <> ?""",
                        (today, LEGACY_TENANT_ID)).fetchall()
    conn.close()
    sent = 0
    client = stripe_api.client() if rows else None
    for r in rows:
        conn = get_db()
        event_name = _meter_event(conn, r["plan_id"])
        conn.close()
        if not event_name:
            continue
        end = datetime.combine(date.fromisoformat(r["day"]), datetime.max.time()).replace(microsecond=0,
                                                                                           tzinfo=timezone.utc)
        try:
            client.v1.billing.meter_events.create({
                "event_name": event_name, "identifier": f"rdf-devices-{r['tenant_id']}-{r['day']}",
                "timestamp": int(end.timestamp()),
                "payload": {"stripe_customer_id": r["stripe_customer_id"], "value": str(r["peak"])}})
        except Exception as e:
            if "already exists" not in str(e):
                log.warning("meter event for tenant %s %s: %s", r["tenant_id"], r["day"], e)
                continue
        conn = get_db()
        conn.execute("UPDATE billing_device_peaks SET reported_at = datetime('now') WHERE tenant_id = ? AND day = ?",
                     (r["tenant_id"], r["day"]))
        conn.commit()
        conn.close()
        sent += 1
    return sent


def hourly(at: datetime | None = None) -> None:
    """The maintenance pass: quantities (debounced), peaks, meter reports."""
    if not stripe_api.enabled():
        return
    conn = get_db()
    tids = [r["tenant_id"] for r in _stripe_tenants(conn)]
    conn.close()
    for tid in tids:
        try:
            push_quantities(tid, at=at)
        except Exception as e:
            log.warning("quantities for tenant %s: %s", tid, e)
    record_peaks(at)
    report_meters(at)
