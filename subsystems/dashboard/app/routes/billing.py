"""The tenant's Billing page (Admin → Billing; manage_billing, never provider
support): the subscription, counts, invoices, plan changes and the Stripe
Customer Portal. app/billing.py has the Stripe side."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app import billing
from app.auth import require_auth
from app.deps import get_db
from app.permissions import require_perm
from app.tenancy import Tenant, get_tenant
from app.templates_config import templates

router = APIRouter()


def _allowed(user: dict) -> bool:
    if user.get("is_support"):
        return False
    require_perm(user, "manage_billing")
    return True


def _flash(request: Request, kind: str, msg: str) -> None:
    request.session["flash"] = {"type": kind, "msg": msg}


@router.get("/admin/billing", response_class=HTMLResponse)
def billing_page(request: Request, current_user: dict = Depends(require_auth), tenant: Tenant = Depends(get_tenant)):
    if not _allowed(current_user):
        return Response("Not found", status_code=404)
    ctx = {"current_user": current_user, "tenant": tenant, "mode": tenant.billing_mode}
    if tenant.billing_mode == "stripe":
        ctx.update(billing.overview(tenant.id))
    else:
        conn = get_db()
        ctx["t"] = conn.execute("SELECT status, plan, trial_ends_at FROM tenants WHERE id = ?", (tenant.id,)).fetchone()
        conn.close()
    return templates.TemplateResponse(request, "billing.html", ctx)


def _home(tenant: Tenant) -> str:
    from app import domains
    conn = get_db()
    try:
        return domains.home_host(conn, tenant.id)
    finally:
        conn.close()


@router.post("/admin/billing/portal")
def billing_portal(request: Request, current_user: dict = Depends(require_auth), tenant: Tenant = Depends(get_tenant)):
    if not _allowed(current_user) or tenant.billing_mode != "stripe":
        return Response("Not found", status_code=404)
    try:
        url = billing.portal_url(tenant.id, f"https://{tenant.hostname or _home(tenant)}/admin/billing")
    except Exception:
        _flash(request, "error", "We couldn't open the billing portal right now; try again shortly.")
        return RedirectResponse("/admin/billing", status_code=303)
    return RedirectResponse(url, status_code=303)


@router.post("/admin/billing/plan")
def billing_plan(request: Request, plan: int = Form(...), current_user: dict = Depends(require_auth),
                 tenant: Tenant = Depends(get_tenant)):
    if not _allowed(current_user) or tenant.billing_mode != "stripe":
        return Response("Not found", status_code=404)
    try:
        billing.change_plan(tenant.id, plan, current_user["email"])
    except ValueError as e:
        _flash(request, "error", str(e))
        return RedirectResponse("/admin/billing", status_code=303)
    except Exception:
        _flash(request, "error", "We couldn't change the plan right now; try again shortly.")
        return RedirectResponse("/admin/billing", status_code=303)
    _flash(request, "success", "Plan changed. Any difference is prorated on your next invoice.")
    return RedirectResponse("/admin/billing", status_code=303)
