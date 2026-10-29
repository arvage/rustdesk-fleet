"""Tables for provider billing (phase 2; TENANCY.md, "Billing").

tenants.billing_mode:
    stripe    signed up on the public site; Stripe drives the lifecycle
    manual    created in the console (comped or invoiced outside Stripe);
              the local trial timer applies, as before phase 2
    internal  never billed and never suspended for billing (tenant #1)
"""
from __future__ import annotations

from app.tenant_migration import LEGACY_TENANT_ID

BILLING_MODES = ("stripe", "manual", "internal")
UNIT_TYPES = ("flat", "technician", "device")

SCHEMA = """
    -- Plans are configuration: each maps to Stripe Price IDs (plan_prices).
    CREATE TABLE IF NOT EXISTS plans (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        key              TEXT NOT NULL UNIQUE CHECK (key = lower(key) AND key <> ''),
        name             TEXT NOT NULL,
        description      TEXT NOT NULL DEFAULT '',
        is_public        INTEGER NOT NULL DEFAULT 0 CHECK (is_public IN (0, 1)),
        sort_order       INTEGER NOT NULL DEFAULT 0,
        max_technicians  INTEGER,                -- soft limits: warn, never block
        max_devices      INTEGER,
        currency         TEXT,                   -- from the prices (all the same)
        interval         TEXT,                   -- e.g. "month", "3 month"
        archived_at      TEXT,
        created_at       TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS plan_prices (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        plan_id            INTEGER NOT NULL REFERENCES plans(id),
        stripe_price_id    TEXT NOT NULL,
        stripe_product_id  TEXT,
        unit_type          TEXT NOT NULL CHECK (unit_type IN ('flat', 'technician', 'device')),
        included_quantity  INTEGER NOT NULL DEFAULT 0,  -- the free first tier of a graduated price
        billing_scheme     TEXT NOT NULL CHECK (billing_scheme IN ('licensed', 'metered')),
        meter_event_name   TEXT,                        -- metered device prices
        summary            TEXT NOT NULL DEFAULT '',    -- "$5.00 per technician / month"
        UNIQUE (plan_id, unit_type)
    );
    CREATE TABLE IF NOT EXISTS billing_customers (
        tenant_id           INTEGER PRIMARY KEY REFERENCES tenants(id),
        stripe_customer_id  TEXT NOT NULL UNIQUE,
        created_at          TEXT NOT NULL DEFAULT (datetime('now'))
    );
    -- The tenant's subscription as last synced from Stripe (Stripe is the
    -- source of truth; this is a cache plus our own bookkeeping).
    CREATE TABLE IF NOT EXISTS billing_subscriptions (
        tenant_id               INTEGER PRIMARY KEY REFERENCES tenants(id),
        stripe_subscription_id  TEXT NOT NULL UNIQUE,
        status                  TEXT NOT NULL,
        plan_id                 INTEGER REFERENCES plans(id),
        current_period_end      TEXT,
        cancel_at_period_end    INTEGER NOT NULL DEFAULT 0,
        cancel_at               TEXT,
        trial_end               TEXT,
        past_due_since          TEXT,
        has_card                INTEGER,
        card_warning_sent_at    TEXT,
        trial_warning_sent_at   TEXT,
        quantities              TEXT,      -- JSON: what we last pushed to Stripe
        quantities_pushed_at    TEXT,
        last_synced_at          TEXT,
        created_at              TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at              TEXT NOT NULL DEFAULT (datetime('now'))
    );
    -- Every webhook event once: a repeat is acknowledged and ignored.
    CREATE TABLE IF NOT EXISTS stripe_events (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id      TEXT NOT NULL UNIQUE,
        type          TEXT NOT NULL,
        object_id     TEXT,
        received_at   TEXT NOT NULL DEFAULT (datetime('now')),
        processed_at  TEXT,
        error         TEXT
    );
"""


def ensure_tables(conn) -> None:
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tenants)")}
    if "billing_mode" not in cols:
        conn.execute("ALTER TABLE tenants ADD COLUMN billing_mode TEXT NOT NULL DEFAULT 'manual'"
                     " CHECK (billing_mode IN ('stripe', 'manual', 'internal'))")
        # Existing tenants were made by hand: manual. Tenant #1 is ours.
        conn.execute("UPDATE tenants SET billing_mode = 'internal' WHERE id = ?", (LEGACY_TENANT_ID,))
    conn.commit()
