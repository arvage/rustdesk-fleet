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
from datetime import datetime, timezone

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
                                            "customer": str(obj.get("customer") or "")}, "stripe")
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
        sync_subscription(object_id)
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
        return _no_card(row, None, "the checkout finished without a subscription")
    sub = stripe_api.plain(client.v1.subscriptions.retrieve(sub_id, {"expand": ["default_payment_method"]}))
    card = default_card(client, sub)
    if card is None:
        return _no_card(row, sub_id, "no payment method")
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
           [f"The checkout for {row['company']} ({row['email']}) completed without a card on file, so no tenant "
            f"was created and the subscription {sub_id or '(none)'} was cancelled."])


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

def _period_end(sub: dict) -> str | None:
    """Since API 2025-03-31 the billing period lives on the items."""
    ends = [i.get("current_period_end") for i in (sub.get("items") or {}).get("data", []) if i.get("current_period_end")]
    return _ts(max(ends)) if ends else None


def sync_subscription(sub_id: str) -> dict | None:
    """Fetch a subscription from Stripe and record it (and, step 5, apply it
    to the tenant). Returns the subscription, or None if it isn't ours."""
    conn = get_db()
    row = conn.execute("SELECT * FROM billing_subscriptions WHERE stripe_subscription_id = ?", (sub_id,)).fetchone()
    conn.close()
    if row is None or row["tenant_id"] == LEGACY_TENANT_ID:
        return None
    client = stripe_api.client()
    sub = stripe_api.plain(client.v1.subscriptions.retrieve(sub_id, {"expand": ["default_payment_method"]}))
    has_card = default_card(client, sub) is not None
    conn = get_db()
    conn.execute("""UPDATE billing_subscriptions SET status = ?, current_period_end = ?, cancel_at_period_end = ?,
                        cancel_at = ?, trial_end = ?, has_card = ?, last_synced_at = datetime('now'),
                        updated_at = datetime('now') WHERE tenant_id = ?""",
                 (sub["status"], _period_end(sub), 1 if sub.get("cancel_at_period_end") else 0, _ts(sub.get("cancel_at")),
                  _ts(sub.get("trial_end")), 1 if has_card else 0, row["tenant_id"]))
    conn.commit()
    conn.close()
    apply_status(row["tenant_id"], sub)
    return sub


def apply_status(tenant_id: int, sub: dict) -> None:
    """Make the tenant follow its subscription (step 5)."""
