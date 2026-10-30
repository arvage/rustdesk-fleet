"""Stripe webhooks and syncing billing state (phase 2; TENANCY.md, "Billing").

Webhooks: POST /stripe/webhook on the public host (app/public_site.py).
receive() checks the signature (STRIPE_WEBHOOK_SECRET, Stripe's 5-minute
tolerance), caps the body, records the event id once in `stripe_events`
(a repeat is acknowledged and ignored) and queues a `stripe_event` job;
Stripe gets its 2xx at once and never waits for a tenant creation. Only
event ids and types are logged, never payloads.

Events aren't trusted for state: for anything about a subscription the job
fetches the subscription from Stripe and syncs from that, so events arriving
out of order (or twice, or not at all: the daily reconciliation) still end in
the right state.

**No card, no tenant.** checkout.session.completed creates a tenant only
after fetching the subscription and customer from Stripe and finding a card
payment method set as the subscription's (or customer's) default. Otherwise
the sign-up fails ("no payment method"), the subscription is cancelled and
the provider admins are alerted. The only callers of the tenant-creation job
are this flow and the console's manual creation (a test enforces it).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from app import settings, signup, stripe_api
from app.console import jobs
from app.deps import get_db, provider_log
from app.tenant_migration import LEGACY_TENANT_ID

log = logging.getLogger("rdf.billing")

MAX_BODY = 256 * 1024
TOLERANCE_S = 300

SUBSCRIPTION_EVENTS = {
    "customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted",
    "customer.subscription.trial_will_end", "customer.subscription.paused", "customer.subscription.resumed",
}
INVOICE_EVENTS = {"invoice.paid", "invoice.payment_failed", "invoice.payment_action_required"}
CUSTOMER_EVENTS = {"customer.updated", "payment_method.detached", "payment_method.attached"}
HANDLED = {"checkout.session.completed"} | SUBSCRIPTION_EVENTS | INVOICE_EVENTS | CUSTOMER_EVENTS


class WebhookRejected(Exception):
    """400: bad signature, stale timestamp, oversized or malformed body."""


def _ts(epoch) -> str | None:
    if epoch in (None, ""):
        return None
    return datetime.fromtimestamp(int(epoch), timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ── Receiving ────────────────────────────────────────────────────────────────

def receive(body: bytes, signature: str) -> str:
    """Verify and record one webhook delivery. Returns "queued" or
    "duplicate"; WebhookRejected for anything that isn't a genuine event."""
    import stripe
    if len(body) > MAX_BODY:
        raise WebhookRejected("body too large")
    secret = settings.stripe_webhook_secret()
    if not secret:
        raise WebhookRejected("webhooks aren't configured")
    try:
        stripe.WebhookSignature.verify_header(body.decode("utf-8"), signature or "", secret, TOLERANCE_S)
        event = json.loads(body)
        event_id, etype = event["id"], event["type"]
        obj = (event.get("data") or {}).get("object") or {}
        # A detached payment method no longer names its customer.
        before = (event.get("data") or {}).get("previous_attributes") or {}
    except Exception:
        raise WebhookRejected("bad signature or body")
    if not isinstance(event_id, str) or not event_id.startswith("evt_") or not isinstance(etype, str):
        raise WebhookRejected("not an event")
    conn = get_db()
    try:
        cur = conn.execute("INSERT OR IGNORE INTO stripe_events (event_id, type, object_id) VALUES (?, ?, ?)",
                           (event_id, etype[:100], str(obj.get("id") or "")[:100]))
        conn.commit()
        if cur.rowcount == 0:
            log.info("stripe event %s (%s): already received", event_id, etype)
            return "duplicate"
    finally:
        conn.close()
    log.info("stripe event %s (%s) received", event_id, etype)
    if etype in HANDLED:
        jobs.enqueue("stripe_event", None, {"event_id": event_id, "type": etype,
                                            "object_id": str(obj.get("id") or ""),
                                            "customer": str(obj.get("customer") or before.get("customer") or "")},
                     "stripe")
    else:
        _done(event_id, "")
    return "queued"


def _done(event_id: str, error: str) -> None:
    conn = get_db()
    conn.execute("UPDATE stripe_events SET processed_at = datetime('now'), error = ? WHERE event_id = ?",
                 (error[:500] or None, event_id))
    conn.commit()
    conn.close()


@jobs.handler("stripe_event")
def _process(job) -> None:
    p = job.params
    try:
        process(p["type"], p.get("object_id", ""), p.get("customer", ""))
    except Exception as e:
        _done(p["event_id"], f"{type(e).__name__}: {e}")
        raise
    _done(p["event_id"], "")


def process(etype: str, object_id: str, customer: str) -> None:
    if etype == "checkout.session.completed":
        checkout_completed(object_id)
    elif etype in SUBSCRIPTION_EVENTS:
        if sync_subscription(object_id) is not None and etype == "customer.subscription.trial_will_end":
            _trial_ending(object_id)
    elif etype in INVOICE_EVENTS:
        invoice = stripe_api.plain(stripe_api.client().v1.invoices.retrieve(object_id))
        sub = _invoice_subscription(invoice)
        if sub:
            sync_subscription(sub)
    elif etype in CUSTOMER_EVENTS:
        for sub in _customer_subscriptions(customer or object_id):
            sync_subscription(sub)


def _invoice_subscription(invoice: dict) -> str | None:
    sub = invoice.get("subscription")
    if not sub:
        details = ((invoice.get("parent") or {}).get("subscription_details") or {})
        sub = details.get("subscription")
    return sub if isinstance(sub, str) else (sub or {}).get("id")


def _customer_subscriptions(customer_id: str) -> list[str]:
    conn = get_db()
    rows = conn.execute("SELECT s.stripe_subscription_id FROM billing_subscriptions s JOIN billing_customers c"
                        " ON c.tenant_id = s.tenant_id WHERE c.stripe_customer_id = ?", (customer_id,)).fetchall()
    conn.close()
    return [r[0] for r in rows]


# ── Checkout completed: the card check, one trial per card, the tenant ──────

def default_card(client, sub: dict) -> dict | None:
    """The subscription's (or else the customer's) default payment method,
    if it is a card attached to the customer; else None."""
    pm = sub.get("default_payment_method")
    if not pm:
        cus = stripe_api.plain(client.v1.customers.retrieve(
            sub["customer"] if isinstance(sub["customer"], str) else sub["customer"]["id"],
            {"expand": ["invoice_settings.default_payment_method"]}))
        pm = (cus.get("invoice_settings") or {}).get("default_payment_method")
    if isinstance(pm, str):
        pm = stripe_api.plain(client.v1.payment_methods.retrieve(pm))
    if not pm or pm.get("type") != "card" or not pm.get("customer"):
        return None
    return pm


def checkout_completed(session_id: str) -> None:
    client = stripe_api.client()
    session = stripe_api.plain(client.v1.checkout.sessions.retrieve(session_id))
    meta = session.get("metadata") or {}
    if meta.get("resubscribe_tenant_id"):
        return resubscribed(int(meta["resubscribe_tenant_id"]), session)
    conn = get_db()
    row = signup.get(conn, int(meta.get("signup_id") or 0)) if (meta.get("signup_id") or "").isdigit() else None
    conn.close()
    if row is None:
        log.warning("checkout %s completed with no sign-up of ours", session_id)
        return
    if row["status"] in ("creating", "created"):
        return          # already handled (a retried delivery)
    sub_id = session.get("subscription")
    sub_id = sub_id if isinstance(sub_id, str) else (sub_id or {}).get("id")
    if not sub_id:
        return _no_card(row, None, signup.NO_CARD)
    sub = stripe_api.plain(client.v1.subscriptions.retrieve(sub_id, {"expand": ["default_payment_method"]}))
    card = default_card(client, sub)
    if card is None:
        return _no_card(row, sub_id, signup.NO_CARD)
    # One trial per card: a fingerprint seen before ends this trial at once.
    fingerprint = (card.get("card") or {}).get("fingerprint") or ""
    conn = get_db()
    seen = conn.execute("SELECT signup_id FROM trial_cards WHERE fingerprint = ?", (fingerprint,)).fetchone()
    trial_denied = bool(seen and seen[0] != row["id"])
    if not seen and fingerprint:
        conn.execute("INSERT INTO trial_cards (fingerprint, signup_id) VALUES (?, ?)", (fingerprint, row["id"]))
        conn.commit()
    conn.close()
    if trial_denied and sub.get("status") == "trialing":
        sub = stripe_api.plain(client.v1.subscriptions.update(sub_id, {"trial_end": "now"}))
    conn = get_db()
    signup._set(conn, row["id"], status="creating", stripe_subscription_id=sub_id, trial_denied=1 if trial_denied else 0,
                failure_reason=None)
    conn.close()
    start_creation(row["id"])


def start_creation(signup_id: int, by: str = "stripe") -> int:
    """Queue the creation job for a paid-for sign-up (and retry it from the
    console with the same sign-up and slug)."""
    conn = get_db()
    row = signup.get(conn, signup_id)
    conn.close()
    return jobs.enqueue("create_tenant", None, {
        "signup_id": row["id"], "slug": row["slug"], "name": row["company"], "admin_email": row["email"],
        "admin_name": row["admin_name"]}, by)


def _no_card(row, sub_id: str | None, reason: str) -> None:
    conn = get_db()
    signup._set(conn, row["id"], status="failed", failure_reason=reason, stripe_subscription_id=sub_id)
    provider_log(conn, "signup_no_payment_method", f"{row['slug']}: {reason}", "stripe")
    conn.close()
    if sub_id:
        try:
            stripe_api.client().v1.subscriptions.cancel(sub_id)
        except Exception as e:
            log.warning("cancelling %s failed: %s", sub_id, e)
    _alert(f"Sign-up {row['slug']} stopped: {reason}",
           [f"The checkout for {row['company']} ({row['email']}) completed without a card on file (for example "
            f"paid with Link or another wallet), so no tenant was created and the subscription {sub_id or '(none)'} "
            "was cancelled. Nothing was charged. They can try again with a card from the progress page; no action "
            "is needed unless they ask for help."])


def _alert(subject: str, lines: list[str]) -> None:
    try:
        from app.console import mail
        mail.send_to_provider_admins(subject, mail.simple_html(subject, lines), event="billing_alert")
    except Exception:
        log.exception("alerting provider admins")


def creation_finished(signup_id: int, tenant_id: int) -> None:
    """Called by the creation job once the tenant exists: link it to its
    Stripe customer and subscription, and sync."""
    conn = get_db()
    row = signup.get(conn, signup_id)
    conn.execute("INSERT OR REPLACE INTO billing_customers (tenant_id, stripe_customer_id) VALUES (?, ?)",
                 (tenant_id, row["stripe_customer_id"]))
    conn.execute("INSERT OR IGNORE INTO billing_subscriptions (tenant_id, stripe_subscription_id, status, plan_id)"
                 " VALUES (?, ?, 'incomplete', ?)", (tenant_id, row["stripe_subscription_id"], row["plan_id"]))
    conn.commit()
    signup._set(conn, signup_id, status="created", tenant_id=tenant_id, failure_reason=None)
    conn.close()
    try:
        stripe_api.client().v1.subscriptions.update(row["stripe_subscription_id"],
                                                    {"metadata": {"tenant_id": str(tenant_id)}})
    except Exception as e:
        log.warning("tagging %s: %s", row["stripe_subscription_id"], e)
    sync_subscription(row["stripe_subscription_id"])
    try:
        from app import billing_usage
        billing_usage.push_quantities(tenant_id, force=True)      # the real counts from day one
    except Exception as e:
        log.warning("quantities for tenant %s: %s", tenant_id, e)


def creation_failed(signup_id: int, error: str) -> None:
    """The creation job failed after the card was taken: keep the Stripe
    subscription, mark the sign-up failed (retryable from the console) and
    alert the provider admins."""
    conn = get_db()
    row = signup.get(conn, signup_id)
    signup._set(conn, signup_id, status="failed", failure_reason=error[:500], tenant_id=None)
    provider_log(conn, "signup_creation_failed", f"{row['slug']}: {error[:300]}", "stripe")
    conn.close()
    _alert(f"Sign-up {row['slug']}: tenant creation failed",
           [f"Creating the tenant for {row['company']} ({row['email']}) failed after they entered their card: {error}",
            "Their Stripe subscription is kept. Retry from the console's Sign-ups page; it reuses the same slug."])


def resubscribed(tenant_id: int, session: dict) -> None:
    """A Resubscribe checkout (from the export page) completed."""
    if tenant_id == LEGACY_TENANT_ID:
        return
    sub_id = session.get("subscription")
    sub_id = sub_id if isinstance(sub_id, str) else (sub_id or {}).get("id")
    if not sub_id:
        return
    client = stripe_api.client()
    sub = stripe_api.plain(client.v1.subscriptions.retrieve(sub_id, {"expand": ["default_payment_method"]}))
    if default_card(client, sub) is None:
        client.v1.subscriptions.cancel(sub_id)
        _alert(f"Tenant {tenant_id}: resubscribe without a card", ["The subscription was cancelled."])
        return
    conn = get_db()
    prev = conn.execute("SELECT stripe_subscription_id, status FROM billing_subscriptions WHERE tenant_id = ?",
                        (tenant_id,)).fetchone()
    if prev and prev["stripe_subscription_id"] != sub_id and prev["status"] in ("trialing", "active", "past_due"):
        # Two Resubscribe checkouts both paid: keep the first, the provider refunds the second.
        conn.close()
        _alert(f"Tenant {tenant_id}: paid a second resubscription",
               [f"Keeping {prev['stripe_subscription_id']}; cancel and refund {sub_id} in Stripe."])
        return
    plan = (session.get("metadata") or {}).get("plan_id")
    conn.execute("""INSERT INTO billing_subscriptions (tenant_id, stripe_subscription_id, status, plan_id)
                    VALUES (?, ?, 'incomplete', ?)
                    ON CONFLICT(tenant_id) DO UPDATE SET stripe_subscription_id = excluded.stripe_subscription_id,
                        status = 'incomplete', plan_id = COALESCE(excluded.plan_id, plan_id), past_due_since = NULL,
                        cancel_at_period_end = 0, cancel_at = NULL, updated_at = datetime('now')""",
                 (tenant_id, sub_id, int(plan) if plan and plan.isdigit() else None))
    conn.commit()
    conn.close()
    sync_subscription(sub_id)


# ── Syncing from Stripe ──────────────────────────────────────────────────────

_MONTHS = {"day": 12 / 365, "week": 12 / 52, "month": 1, "year": 12}


def _item_amount(price: dict, qty: int) -> int:
    """One period's amount for a licensed item (graduated or volume tiers too)."""
    if price.get("billing_scheme") != "tiered":
        return int(price.get("unit_amount") or 0) * qty
    tiers = price.get("tiers") or []
    if price.get("tiers_mode") == "volume":
        tier = next((t for t in tiers if t.get("up_to") is None or qty <= int(t["up_to"])), tiers[-1] if tiers else {})
        return int(tier.get("unit_amount") or 0) * qty + int(tier.get("flat_amount") or 0)
    total, below = 0, 0
    for t in tiers:
        top = qty if t.get("up_to") is None else min(qty, int(t["up_to"]))
        n = max(0, top - below)
        if n:
            total += int(t.get("unit_amount") or 0) * n + int(t.get("flat_amount") or 0)
        below = top if t.get("up_to") is not None else qty
        if below >= qty:
            break
    return total


def mrr(sub: dict) -> tuple[int, str]:
    """Monthly recurring amount from the subscription's licensed items
    (metered usage and discounts excluded), and its currency."""
    total, currency = 0.0, ""
    for item in (sub.get("items") or {}).get("data", []):
        price = item.get("price") or {}
        rec = price.get("recurring") or {}
        if not isinstance(price, dict) or rec.get("usage_type") == "metered":
            continue
        currency = price.get("currency") or currency
        months = _MONTHS.get(rec.get("interval"), 1) * int(rec.get("interval_count") or 1)
        total += _item_amount(price, int(item.get("quantity") or 0)) / months
    return int(round(total)), currency


def _period_end(sub: dict) -> str | None:
    """Since API 2025-03-31 the billing period lives on the items."""
    ends = [i.get("current_period_end") for i in (sub.get("items") or {}).get("data", []) if i.get("current_period_end")]
    return _ts(max(ends)) if ends else None


def _clock_now(client, clock) -> datetime | None:
    """A test-clock customer (STRIPE_TEST_CLOCK) lives at the clock's time, so
    the past-due timer advances with the clock (apply_status). None (real
    time) if the clock can't be read."""
    clock_id = clock.get("id") if isinstance(clock, dict) else clock
    try:
        frozen = stripe_api.plain(client.v1.test_helpers.test_clocks.retrieve(clock_id))["frozen_time"]
    except Exception as e:  # noqa: BLE001
        log.warning("test clock %s unreadable, using real time: %s", clock_id, type(e).__name__)
        return None
    return datetime.fromtimestamp(int(frozen), timezone.utc)


def _cancel_scheduled(sub: dict) -> bool:
    """The portal schedules a cancellation with cancel_at (a date) rather than
    cancel_at_period_end; either way the subscription is ending. Stored as
    cancel_at_period_end, which the banners and pages read."""
    return bool(sub.get("cancel_at_period_end") or (sub.get("cancel_at") and sub.get("status") != "canceled"))


class _Synced(dict):
    _changed: str | None = None


def sync_subscription(sub_id: str, at: datetime | None = None, quiet: bool = False) -> dict | None:
    """Fetch a subscription from Stripe and record it (and, step 5, apply it
    to the tenant). Returns the subscription, or None if it isn't ours."""
    conn = get_db()
    row = conn.execute("SELECT * FROM billing_subscriptions WHERE stripe_subscription_id = ?", (sub_id,)).fetchone()
    conn.close()
    if row is None or row["tenant_id"] == LEGACY_TENANT_ID:
        return None
    client = stripe_api.client()
    # Tiers (for the monthly amount) only come back when asked for.
    sub = stripe_api.plain(client.v1.subscriptions.retrieve(
        sub_id, {"expand": ["default_payment_method", "items.data.price.tiers"]}))
    has_card = default_card(client, sub) is not None
    clock = _clock_now(client, sub["test_clock"]) if at is None and sub.get("test_clock") else None
    conn = get_db()
    amount, currency = mrr(sub)
    discount = ", ".join(str((d.get("coupon") if isinstance(d, dict) else d) or "") for d in (sub.get("discounts") or []))
    conn.execute("""UPDATE billing_subscriptions SET status = ?, current_period_end = ?, cancel_at_period_end = ?,
                        cancel_at = ?, trial_end = ?, has_card = ?, mrr = ?, currency = ?, discount = ?,
                        last_synced_at = datetime('now'), updated_at = datetime('now') WHERE tenant_id = ?""",
                 (sub["status"], _period_end(sub), 1 if _cancel_scheduled(sub) else 0, _ts(sub.get("cancel_at")),
                  _ts(sub.get("trial_end")), 1 if has_card else 0, amount, currency, discount or None, row["tenant_id"]))
    conn.commit()
    conn.close()
    out = _Synced(sub)
    out._changed = apply_status(row["tenant_id"], sub, at, clock=clock)
    return out


# ── Stripe status drives the tenant (billing_mode = stripe only) ───────────
#
#   trialing            trial (ends at Stripe's trial_end; no local timer)
#   active              active
#   past_due            stays open with a banner; suspended after
#                       PAST_DUE_SUSPEND_DAYS
#   unpaid, paused      suspended
#   incomplete          sign-up not finished: nothing to do
#   incomplete_expired  the creation is undone
#   canceled            cancelled: relay stopped, 30-day grace, export, purge
#   cancel_at_period_end  unchanged, with a banner saying when it ends
#
# A suspension billing made (tenants.billing_suspended = 1) is lifted when
# the subscription is good again; one a provider admin made is not.

def _tenant(conn, tenant_id: int):
    return conn.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def apply_status(tenant_id: int, sub: dict, at: datetime | None = None,
                 clock: datetime | None = None) -> str | None:
    """Make a Stripe tenant follow its subscription. Returns what changed
    (for the reconciliation log), or None. 'clock' is a test clock's time: it
    drives the past-due timer only (Stripe's dates); the grace period is ours
    and stays on real time."""
    from app.console import lifecycle
    from app.tenancy import invalidate_cache
    now = at or _now()
    due_now = clock or now
    conn = get_db()
    t = _tenant(conn, tenant_id)
    if t is None or tenant_id == LEGACY_TENANT_ID or t["billing_mode"] != "stripe" or t["status"] == "purged":
        conn.close()
        return None
    status, current = sub["status"], t["status"]
    bs = conn.execute("SELECT * FROM billing_subscriptions WHERE tenant_id = ?", (tenant_id,)).fetchone()
    changed = None
    good = status in ("trialing", "active")
    # past_due bookkeeping
    if status == "past_due" and not bs["past_due_since"]:
        conn.execute("UPDATE billing_subscriptions SET past_due_since = ? WHERE tenant_id = ?", (_fmt(due_now), tenant_id))
        conn.commit()
        _email(tenant_id, "payment_failed")
    elif status != "past_due" and bs["past_due_since"]:
        conn.execute("UPDATE billing_subscriptions SET past_due_since = NULL WHERE tenant_id = ?", (tenant_id,))
        conn.commit()
    trial_end = _ts(sub.get("trial_end")) if status == "trialing" else None
    if good and current in ("trial", "active"):
        want = "trial" if status == "trialing" else "active"
        if current != want or t["trial_ends_at"] != trial_end:
            lifecycle._set(conn, tenant_id, status=want, trial_ends_at=trial_end)
            changed = f"{current} → {want}" if current != want else f"trial ends {trial_end}"
    elif good and current == "suspended" and t["billing_suspended"]:
        want = "trial" if status == "trialing" else "active"
        lifecycle._set(conn, tenant_id, status=want, trial_ends_at=trial_end, billing_suspended=0)
        provider_log(conn, "tenant_reactivated", f"{t['slug']}: paid ({status})", "stripe", tenant_id)
        changed = f"suspended → {want} (paid)"
        _email(tenant_id, "reactivated")
    elif good and current == "cancelled" and t["purge_after"] and t["purge_after"] > _fmt(now):
        # Resubscribed during the grace period: same relay, keys and devices.
        if not jobs.active_for(tenant_id):
            jobs.enqueue("reactivate_tenant", tenant_id, {"reason": "resubscribed"}, "stripe")
            changed = "cancelled → reactivating (resubscribed)"
            _email(tenant_id, "reactivated")
    elif status == "past_due" and current in ("trial", "active"):
        since = datetime.strptime((bs["past_due_since"] or _fmt(due_now))[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        if due_now - since >= timedelta(days=settings.past_due_suspend_days()):
            changed = _billing_suspend(conn, t, "past due for " + str(settings.past_due_suspend_days()) + " days")
    elif status in ("unpaid", "paused") and current in ("trial", "active"):
        changed = _billing_suspend(conn, t, status)
    elif status == "canceled" and current in ("trial", "active", "suspended"):
        if not jobs.active_for(tenant_id):
            jobs.enqueue("cancel_tenant", tenant_id, {"reason": "subscription canceled in Stripe"}, "stripe")
            changed = f"{current} → cancelling (subscription canceled)"
    elif status == "incomplete_expired" and current in ("trial", "active"):
        # The sign-up lets go of the tenant first (it references it).
        conn.execute("UPDATE signups SET status = 'failed', failure_reason = 'payment never completed', tenant_id = NULL,"
                     " updated_at = datetime('now') WHERE tenant_id = ?", (tenant_id,))
        conn.commit()
        conn.close()
        from app.console.tenants import undo_creation
        undo_creation(tenant_id, t["slug"])
        conn = get_db()
        changed = "creation undone (payment never completed)"
    conn.close()
    _card_check(tenant_id, sub)
    if changed:
        invalidate_cache()
        log.info("tenant %s: %s", tenant_id, changed)
    return changed


def _billing_suspend(conn, t, why: str) -> str:
    from app.console import lifecycle
    lifecycle._set(conn, t["id"], status="suspended", billing_suspended=1)
    provider_log(conn, "tenant_suspended", f"{t['slug']}: {why} (billing)", "stripe", t["id"])
    _email(t["id"], "suspended")
    return f"{t['status']} → suspended ({why})"


def _card_check(tenant_id: int, sub: dict) -> None:
    """A trial or active subscription with no default card: the admins see a
    banner (has_card = 0) and get one email. At the trial's end Stripe
    cancels it (missing_payment_method = cancel); at a renewal the invoice
    fails and past_due takes over."""
    conn = get_db()
    bs = conn.execute("SELECT has_card, card_warning_sent_at FROM billing_subscriptions WHERE tenant_id = ?",
                      (tenant_id,)).fetchone()
    if bs is None:
        conn.close()
        return
    if sub["status"] in ("trialing", "active") and not bs["has_card"] and not bs["card_warning_sent_at"]:
        conn.execute("UPDATE billing_subscriptions SET card_warning_sent_at = datetime('now') WHERE tenant_id = ?",
                     (tenant_id,))
        conn.commit()
        conn.close()
        _email(tenant_id, "card_missing")
        return
    if bs["has_card"] and bs["card_warning_sent_at"]:
        conn.execute("UPDATE billing_subscriptions SET card_warning_sent_at = NULL WHERE tenant_id = ?", (tenant_id,))
        conn.commit()
    conn.close()


def _trial_ending(sub_id: str) -> None:
    """Stripe's trial_will_end (3 days before): one email per trial."""
    conn = get_db()
    row = conn.execute("SELECT tenant_id, trial_warning_sent_at FROM billing_subscriptions WHERE stripe_subscription_id = ?",
                       (sub_id,)).fetchone()
    if row and not row["trial_warning_sent_at"]:
        conn.execute("UPDATE billing_subscriptions SET trial_warning_sent_at = datetime('now') WHERE tenant_id = ?",
                     (row["tenant_id"],))
        conn.commit()
        conn.close()
        _email(row["tenant_id"], "trial_ending")
        return
    conn.close()


def _email(tenant_id: int, kind: str) -> None:
    try:
        from app import billing_emails
        billing_emails.lifecycle(tenant_id, kind)
    except Exception:
        log.exception("billing email %s for tenant %s", kind, tenant_id)


def reconcile(at: datetime | None = None) -> list[str]:
    """Daily: fetch every Stripe tenant's subscription and fix any drift
    (missed webhooks), logging what changed. Also applies the past-due
    suspension once its days are up."""
    conn = get_db()
    rows = conn.execute("SELECT s.tenant_id, s.stripe_subscription_id, s.status, t.status AS tenant_status, t.slug"
                        " FROM billing_subscriptions s JOIN tenants t ON t.id = s.tenant_id"
                        " WHERE t.billing_mode = 'stripe' AND t.status <> 'purged' AND t.id <> ?",
                        (LEGACY_TENANT_ID,)).fetchall()
    conn.close()
    changes = []
    for r in rows:
        try:
            sub = sync_subscription(r["stripe_subscription_id"], at=at, quiet=True)
        except Exception as e:
            changes.append(f"{r['slug']}: sync failed ({e})")
            continue
        try:
            from app import billing_usage
            billing_usage.push_quantities(r["tenant_id"], force=True, at=at)
        except Exception as e:
            log.warning("quantities for %s: %s", r["slug"], e)
        conn = get_db()
        after = conn.execute("SELECT s.status, t.status FROM billing_subscriptions s JOIN tenants t ON t.id = s.tenant_id"
                             " WHERE s.tenant_id = ?", (r["tenant_id"],)).fetchone()
        what = []
        if after[0] != r["status"]:
            what.append(f"subscription {r['status']} → {after[0]}")
        if sub is not None and getattr(sub, "_changed", None):
            what.append(sub._changed)
        if what:
            provider_log(conn, "billing_reconciled", f"{r['slug']}: " + "; ".join(what), "scheduled", r["tenant_id"])
            changes.append(f"{r['slug']}: " + "; ".join(what))
        conn.close()
    return changes


# ── Provider actions that act on Stripe ──────────────────────────────────────

def subscription_of(tenant_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM billing_subscriptions WHERE tenant_id = ?", (tenant_id,)).fetchone()
    conn.close()
    return row


def cancel_subscription(tenant_id: int, at_period_end: bool, by: str) -> None:
    """The console's Cancel for a Stripe tenant: cancel the subscription now
    or at the end of the period, then sync."""
    if tenant_id == LEGACY_TENANT_ID:
        raise ValueError("Tenant #1 is never billed.")
    row = subscription_of(tenant_id)
    if row is None:
        return
    client = stripe_api.client()
    if at_period_end:
        client.v1.subscriptions.update(row["stripe_subscription_id"], {"cancel_at_period_end": True})
    elif row["status"] not in ("canceled", "incomplete_expired"):
        client.v1.subscriptions.cancel(row["stripe_subscription_id"])
    conn = get_db()
    provider_log(conn, "subscription_cancelled", f"{row['stripe_subscription_id']}"
                 + (" at period end" if at_period_end else " now"), by, tenant_id)
    conn.close()
    sync_subscription(row["stripe_subscription_id"])


def resubscribe_url(tenant_id: int, home_host: str) -> str:
    """A Checkout Session to subscribe again during the cancellation grace
    period: the same Stripe customer and plan, no trial, card required
    (signup.checkout_params, like sign-up)."""
    conn = get_db()
    cus = conn.execute("SELECT stripe_customer_id FROM billing_customers WHERE tenant_id = ?", (tenant_id,)).fetchone()
    sub = conn.execute("SELECT plan_id FROM billing_subscriptions WHERE tenant_id = ?", (tenant_id,)).fetchone()
    plan_id = sub["plan_id"] if sub and sub["plan_id"] else None
    from app import plans
    if plan_id is None or (plans.get(conn, plan_id) or {"archived_at": 1})["archived_at"]:
        offered = plans.public_plans(conn)
        plan_id = offered[0]["id"] if offered else None
    if cus is None or plan_id is None:
        conn.close()
        raise signup.SignupError("Resubscribing isn't available for this account; contact your provider.")
    live = conn.execute("SELECT status FROM billing_subscriptions WHERE tenant_id = ?", (tenant_id,)).fetchone()
    if live and live["status"] in ("trialing", "active", "past_due"):
        conn.close()
        raise signup.SignupError("You've already resubscribed; your account is being reopened.")
    params = signup.checkout_params(cus[0], plan_id, trial=False, conn=conn,
                                    metadata={"resubscribe_tenant_id": str(tenant_id), "plan_id": str(plan_id)},
                                    success_url=f"https://{home_host}/export?resubscribed=1",
                                    cancel_url=f"https://{home_host}/export")
    conn.close()
    return stripe_api.client().v1.checkout.sessions.create(params).url


# ── The tenant's Billing page: portal, plan change, what it shows ───────────

PORTAL_CONFIG_KEY = "stripe_portal_configuration"


def portal_features(conn) -> dict:
    """What the Customer Portal allows, from the console's plans: change the
    card (never leave without one: Stripe won't remove the default card of a
    customer with a subscription, and our sync re-checks), see invoices,
    update billing details, cancel at period end. Switching plans in the
    portal only works between single-price subscriptions, so it is turned on
    only when every public plan is one licensed price; otherwise plans are
    switched on our Billing page (change_plan)."""
    from app import plans
    public = plans.public_plans(conn)
    single = public and all(len(p["prices"]) == 1 and p["prices"][0]["billing_scheme"] == "licensed" for p in public)
    features = {
        "customer_update": {"enabled": True, "allowed_updates": ["address", "email", "name", "tax_id"]},
        "invoice_history": {"enabled": True},
        "payment_method_update": {"enabled": True},
        "subscription_cancel": {"enabled": True, "mode": "at_period_end",
                                "cancellation_reason": {"enabled": True, "options": [
                                    "too_expensive", "missing_features", "switched_service", "unused",
                                    "customer_service", "too_complex", "low_quality", "other"]}},
        "subscription_update": {"enabled": False},
    }
    if single:
        by_product: dict[str, list[str]] = {}
        for p in public:
            pr = p["prices"][0]
            by_product.setdefault(pr["stripe_product_id"], []).append(pr["stripe_price_id"])
        features["subscription_update"] = {
            "enabled": True, "default_allowed_updates": ["price"], "proration_behavior": settings.stripe_proration(),
            "products": [{"product": k, "prices": v} for k, v in list(by_product.items())[:10]]}
    return features


def sync_portal_configuration() -> str:
    """Create or update our portal configuration; returns its id."""
    from app import provider_config
    conn = get_db()
    params = {"name": "RustDesk Fleet", "features": portal_features(conn),
              "business_profile": {k: v for k, v in (("terms_of_service_url", settings.terms_url()),
                                                     ("privacy_policy_url", settings.privacy_url())) if v}}
    conn.close()
    client = stripe_api.client()
    current = provider_config.get(PORTAL_CONFIG_KEY)
    if current:
        try:
            client.v1.billing_portal.configurations.update(current, params)
            return current
        except Exception as e:
            log.warning("updating portal configuration %s: %s", current, e)
    cfg = client.v1.billing_portal.configurations.create(params)
    provider_config.set_many({PORTAL_CONFIG_KEY: cfg.id})
    return cfg.id


def portal_url(tenant_id: int, return_url: str) -> str:
    from app import provider_config
    conn = get_db()
    cus = conn.execute("SELECT stripe_customer_id FROM billing_customers WHERE tenant_id = ?", (tenant_id,)).fetchone()
    conn.close()
    if cus is None or tenant_id == LEGACY_TENANT_ID:
        raise ValueError("This account isn't billed through Stripe.")
    config = provider_config.get(PORTAL_CONFIG_KEY) or sync_portal_configuration()
    session = stripe_api.client().v1.billing_portal.sessions.create(
        {"customer": cus[0], "return_url": return_url, "configuration": config})
    return session.url


def change_plan(tenant_id: int, plan_id: int, by: str) -> None:
    """Switch a Stripe tenant to another public plan: replace the
    subscription's items with the plan's prices (current quantities),
    prorated per STRIPE_PRORATION, then sync."""
    from app import billing_usage, plans
    conn = get_db()
    plan = plans.offered(conn, plan_id)
    bs = conn.execute("SELECT * FROM billing_subscriptions WHERE tenant_id = ?", (tenant_id,)).fetchone()
    if plan is None or bs is None or tenant_id == LEGACY_TENANT_ID:
        conn.close()
        raise ValueError("That plan isn't available.")
    if bs["plan_id"] == plan["id"]:
        conn.close()
        return
    n = billing_usage.counts(conn, tenant_id)
    conn.close()
    client = stripe_api.client()
    sub = stripe_api.plain(client.v1.subscriptions.retrieve(bs["stripe_subscription_id"]))
    items = [{"id": i["id"], "deleted": True} for i in sub["items"]["data"]]
    for pr in plan["prices"]:
        item = {"price": pr["stripe_price_id"]}
        if pr["billing_scheme"] == "licensed":
            item["quantity"] = 1 if pr["unit_type"] == "flat" else n[pr["unit_type"]]
        items.append(item)
    client.v1.subscriptions.update(bs["stripe_subscription_id"],
                                   {"items": items, "proration_behavior": billing_usage.proration_for(sub),
                                    "metadata": {"plan_id": str(plan["id"])}})
    conn = get_db()
    conn.execute("UPDATE billing_subscriptions SET plan_id = ?, quantities = NULL WHERE tenant_id = ?",
                 (plan["id"], tenant_id))
    conn.commit()
    from app.deps import log_event
    log_event(conn, "plan_changed", f"{plan['name']}", by, tenant_id=tenant_id)
    conn.commit()
    conn.close()
    sync_subscription(bs["stripe_subscription_id"])


def overview(tenant_id: int) -> dict:
    """What the Billing page shows, fetched from Stripe (short timeout).
    'error' says if Stripe couldn't be reached; the rest still shows."""
    from app import billing_usage, plans
    conn = get_db()
    bs = conn.execute("SELECT * FROM billing_subscriptions WHERE tenant_id = ?", (tenant_id,)).fetchone()
    cus = conn.execute("SELECT stripe_customer_id FROM billing_customers WHERE tenant_id = ?", (tenant_id,)).fetchone()
    out = {"sub": dict(bs) if bs else None, "counts": billing_usage.counts(conn, tenant_id),
           "over": billing_usage.over_limits(conn, tenant_id), "plan": None, "prices": [],
           "plans": plans.public_plans(conn), "invoices": [], "upcoming": None, "error": ""}
    if bs and bs["plan_id"]:
        p = plans.get(conn, bs["plan_id"])
        out["plan"] = dict(p) if p else None
        out["prices"] = [dict(r) for r in plans.prices_of(conn, bs["plan_id"])]
    conn.close()
    if not (bs and cus):
        return out
    import stripe
    try:
        client = stripe_api.client()
        if bs["status"] in ("trialing", "active", "past_due") and not bs["cancel_at_period_end"]:
            try:
                prev = stripe_api.plain(client.v1.invoices.create_preview(
                    {"customer": cus[0], "subscription": bs["stripe_subscription_id"]}))
                out["upcoming"] = {"amount": prev.get("amount_due"), "currency": prev.get("currency"),
                                   "date": _ts(prev.get("next_payment_attempt") or prev.get("period_end"))}
            except stripe.InvalidRequestError:
                pass        # no next invoice (e.g. a cancellation not synced yet): nothing to preview
        inv = stripe_api.plain(client.v1.invoices.list({"customer": cus[0], "limit": 12}))
        out["invoices"] = [{"number": i.get("number") or i["id"], "created": _ts(i.get("created")),
                            "amount": i.get("amount_paid") or i.get("amount_due"), "currency": i.get("currency"),
                            "status": i.get("status"), "url": i.get("hosted_invoice_url")}
                           for i in inv.get("data", [])]
    except Exception as e:
        out["error"] = f"Stripe couldn't be reached ({type(e).__name__}); try again shortly."
    return out


# ── Console actions ──────────────────────────────────────────────────────────

def set_billing_mode(tenant_id: int, mode: str, by: str) -> None:
    from app.billing_schema import BILLING_MODES
    if tenant_id == LEGACY_TENANT_ID:
        raise ValueError("Tenant #1 is always internal: never billed.")
    if mode not in BILLING_MODES:
        raise ValueError("Unknown billing mode.")
    conn = get_db()
    try:
        if mode == "stripe" and not conn.execute("SELECT 1 FROM billing_subscriptions WHERE tenant_id = ?",
                                                 (tenant_id,)).fetchone():
            raise ValueError("This tenant has no Stripe subscription; it can't be put on Stripe billing here.")
        conn.execute("UPDATE tenants SET billing_mode = ?, updated_at = datetime('now') WHERE id = ?", (mode, tenant_id))
        conn.commit()
        provider_log(conn, "billing_mode_changed", mode, by, tenant_id)
    finally:
        conn.close()
    from app.tenancy import invalidate_cache
    invalidate_cache()


def extend_trial(tenant_id: int, days: int, by: str) -> str:
    """A Stripe tenant's trial lives in Stripe: move its trial_end."""
    row = subscription_of(tenant_id)
    if row is None or tenant_id == LEGACY_TENANT_ID:
        raise ValueError("No Stripe subscription.")
    if row["status"] != "trialing" or not row["trial_end"]:
        raise ValueError("Only a subscription still on trial has a trial to extend.")
    days = max(1, min(90, int(days)))
    base = max(_now(), datetime.strptime(row["trial_end"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc))
    new = base + timedelta(days=days)
    stripe_api.client().v1.subscriptions.update(row["stripe_subscription_id"],
                                                {"trial_end": int(new.timestamp()), "proration_behavior": "none"})
    conn = get_db()
    provider_log(conn, "tenant_trial_extended", f"+{days}d → {_fmt(new)} (Stripe)", by, tenant_id)
    conn.close()
    sync_subscription(row["stripe_subscription_id"])
    return _fmt(new)


def apply_coupon(tenant_id: int, coupon: str, by: str) -> None:
    import re
    coupon = coupon.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", coupon):
        raise ValueError("Enter a Stripe coupon id.")
    row = subscription_of(tenant_id)
    if row is None or tenant_id == LEGACY_TENANT_ID:
        raise ValueError("No Stripe subscription.")
    stripe_api.client().v1.subscriptions.update(row["stripe_subscription_id"], {"discounts": [{"coupon": coupon}]})
    conn = get_db()
    provider_log(conn, "coupon_applied", coupon, by, tenant_id)
    conn.close()
    sync_subscription(row["stripe_subscription_id"])
