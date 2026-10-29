"""Console: billing (phase 2). Per tenant: billing mode, the Stripe customer
and subscription, quantities, coupon, resync. The Billing overview (MRR,
trials ending, past due, failed sign-ups, webhook errors, soft limits) and
the Sign-ups list with retry."""
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import billing, billing_usage, signup, stripe_api
from app.console.auth import require_provider
from app.deps import get_db, provider_log
from app.templates_config import templates

router = APIRouter()


def _flash(request: Request, kind: str, msg: str) -> None:
    request.session["flash"] = {"type": kind, "msg": msg}


def _back(tenant_id: int) -> RedirectResponse:
    return RedirectResponse(f"/tenants/{tenant_id}", status_code=303)


def tenant_billing(tenant_id: int, mode: str) -> dict:
    """The billing part of the console's tenant page."""
    conn = get_db()
    out = {"mode": mode, "sub": None, "customer": None, "counts": billing_usage.counts(conn, tenant_id),
           "over": billing_usage.over_limits(conn, tenant_id), "plan": None, "stripe_mode": stripe_api.mode()}
    bs = conn.execute("SELECT * FROM billing_subscriptions WHERE tenant_id = ?", (tenant_id,)).fetchone()
    cus = conn.execute("SELECT stripe_customer_id FROM billing_customers WHERE tenant_id = ?", (tenant_id,)).fetchone()
    if bs:
        out["sub"] = dict(bs)
        out["sub_url"] = stripe_api.dashboard_url("subscriptions", bs["stripe_subscription_id"])
        p = conn.execute("SELECT * FROM plans WHERE id = ?", (bs["plan_id"],)).fetchone() if bs["plan_id"] else None
        out["plan"] = dict(p) if p else None
    if cus:
        out["customer"] = cus[0]
        out["customer_url"] = stripe_api.dashboard_url("customers", cus[0])
    conn.close()
    if mode == "stripe" and bs and stripe_api.enabled():
        ov = billing.overview(tenant_id)
        out["invoices"], out["upcoming"], out["error"] = ov["invoices"], ov["upcoming"], ov["error"]
    return out


@router.post("/tenants/{tenant_id}/billing-mode")
def billing_mode(request: Request, tenant_id: int, mode: str = Form(""), user: dict = Depends(require_provider)):
    try:
        billing.set_billing_mode(tenant_id, mode, user["email"])
        _flash(request, "success", f"Billing mode: {mode}.")
    except ValueError as e:
        _flash(request, "error", str(e))
    return _back(tenant_id)


@router.post("/tenants/{tenant_id}/billing-sync")
def billing_sync(request: Request, tenant_id: int, user: dict = Depends(require_provider)):
    row = billing.subscription_of(tenant_id)
    if row is None:
        _flash(request, "error", "No Stripe subscription.")
        return _back(tenant_id)
    try:
        sub = billing.sync_subscription(row["stripe_subscription_id"])
        billing_usage.push_quantities(tenant_id, force=True)
        changed = getattr(sub, "_changed", None) if sub is not None else None
        conn = get_db()
        provider_log(conn, "billing_resynced", changed or "no change", user["email"], tenant_id)
        conn.close()
        _flash(request, "success", f"Synced from Stripe{': ' + changed if changed else '; nothing to change'}.")
    except Exception as e:
        _flash(request, "error", f"Stripe: {e}")
    return _back(tenant_id)


@router.post("/tenants/{tenant_id}/coupon")
def billing_coupon(request: Request, tenant_id: int, coupon: str = Form(""), user: dict = Depends(require_provider)):
    try:
        billing.apply_coupon(tenant_id, coupon, user["email"])
        _flash(request, "success", f"Coupon {coupon.strip()} applied.")
    except ValueError as e:
        _flash(request, "error", str(e))
    except Exception as e:
        _flash(request, "error", f"Stripe: {getattr(e, 'user_message', None) or e}")
    return _back(tenant_id)


@router.get("/billing", response_class=HTMLResponse)
def overview(request: Request, user: dict = Depends(require_provider)):
    conn = get_db()
    live = conn.execute("""SELECT s.*, t.slug, t.display_name, t.status AS tenant_status, p.name AS plan_name
                           FROM billing_subscriptions s JOIN tenants t ON t.id = s.tenant_id
                           LEFT JOIN plans p ON p.id = s.plan_id
                           WHERE t.billing_mode = 'stripe' AND t.status <> 'purged'
                           ORDER BY t.slug""").fetchall()
    mrr: dict[tuple, int] = {}
    for r in live:
        if r["status"] in ("active", "past_due") and r["mrr"]:
            key = (r["plan_name"] or "—", (r["currency"] or "").upper())
            mrr[key] = mrr.get(key, 0) + r["mrr"]
    week = signup.fmt(signup.now() + timedelta(days=7))
    now = signup.fmt(signup.now())
    ctx = {
        "current_user": user, "billing_missing": stripe_api.missing(), "stripe_mode": stripe_api.mode(),
        "mrr": sorted(((k[0], k[1], v) for k, v in mrr.items())),
        "trials": [r for r in live if r["status"] == "trialing" and r["trial_end"] and now <= r["trial_end"] <= week],
        "unpaid": [r for r in live if r["status"] in ("past_due", "unpaid")],
        "no_card": [r for r in live if r["has_card"] == 0 and r["status"] in ("trialing", "active", "past_due")],
        "over": [(r, billing_usage.over_limits(conn, r["tenant_id"])) for r in live],
        "failed": conn.execute("SELECT * FROM signups WHERE status = 'failed' ORDER BY updated_at DESC").fetchall(),
        "errors": conn.execute("SELECT * FROM stripe_events WHERE error IS NOT NULL ORDER BY id DESC LIMIT 50").fetchall(),
    }
    ctx["over"] = [(r, o) for r, o in ctx["over"] if o]
    conn.close()
    return templates.TemplateResponse(request, "console/billing.html", ctx)


@router.get("/signups", response_class=HTMLResponse)
def signups(request: Request, user: dict = Depends(require_provider)):
    conn = get_db()
    rows = conn.execute("""SELECT s.*, p.name AS plan_name FROM signups s LEFT JOIN plans p ON p.id = s.plan_id
                           WHERE s.status <> 'created' OR s.updated_at >= datetime('now', '-30 days')
                           ORDER BY s.id DESC LIMIT 200""").fetchall()
    conn.close()
    return templates.TemplateResponse(request, "console/signups.html", {"current_user": user, "rows": rows})


@router.post("/signups/{signup_id}/retry")
def signup_retry(request: Request, signup_id: int, user: dict = Depends(require_provider)):
    """Create the tenant again for a paid-for sign-up whose creation failed:
    the same sign-up, slug and Stripe subscription."""
    conn = get_db()
    row = signup.get(conn, signup_id)
    if row is None or row["status"] != "failed" or not row["stripe_subscription_id"] \
            or row["failure_reason"] == "no payment method":
        conn.close()
        _flash(request, "error", "Only a paid-for sign-up whose tenant creation failed can be retried.")
        return RedirectResponse("/signups", status_code=303)
    signup._set(conn, signup_id, status="creating", failure_reason=None)
    provider_log(conn, "signup_retried", row["slug"], user["email"])
    conn.close()
    job_id = billing.start_creation(signup_id, user["email"])
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)
