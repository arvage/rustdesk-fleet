"""Console: billing plans (phase 2, app/plans.py)."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import plans, stripe_api
from app.console.auth import require_provider
from app.deps import get_db, provider_log
from app.templates_config import templates

router = APIRouter()


def _flash(request: Request, kind: str, msg: str) -> None:
    request.session["flash"] = {"type": kind, "msg": msg}


def _page(request: Request, user: dict, plan=None, prices=None, form=None, error: str = "", status: int = 200):
    return templates.TemplateResponse(request, "console/plan_edit.html", {
        "current_user": user, "plan": plan, "prices": {p["unit_type"]: p for p in (prices or [])},
        "form": form or {}, "error": error, "billing_missing": stripe_api.missing(),
        "units": plans.UNIT_LABELS}, status_code=status)


@router.get("/plans", response_class=HTMLResponse)
def plan_list(request: Request, user: dict = Depends(require_provider)):
    conn = get_db()
    rows = [{**dict(p), "prices": plans.prices_of(conn, p["id"]),
             "subscribers": conn.execute("SELECT COUNT(*) FROM billing_subscriptions WHERE plan_id = ?"
                                         " AND status NOT IN ('canceled', 'incomplete_expired')", (p["id"],)).fetchone()[0]}
            for p in plans.all_plans(conn)]
    conn.close()
    return templates.TemplateResponse(request, "console/plans.html", {
        "current_user": user, "plans": rows, "billing_missing": stripe_api.missing(), "mode": stripe_api.mode()})


@router.get("/plans/new", response_class=HTMLResponse)
def plan_new(request: Request, user: dict = Depends(require_provider)):
    return _page(request, user)


@router.get("/plans/{plan_id}", response_class=HTMLResponse)
def plan_edit(request: Request, plan_id: int, user: dict = Depends(require_provider)):
    conn = get_db()
    plan, prices = plans.get(conn, plan_id), plans.prices_of(conn, plan_id)
    conn.close()
    if plan is None:
        _flash(request, "error", "Plan not found.")
        return RedirectResponse("/plans", status_code=303)
    return _page(request, user, plan, prices)


async def _save(request: Request, plan_id: int | None, user: dict):
    form = dict(await request.form())
    conn = get_db()
    try:
        new_id = plans.save(conn, plan_id, form)
    except (plans.PlanError, stripe_api.BillingUnavailable) as e:
        plan = plans.get(conn, plan_id) if plan_id else None
        conn.close()
        return _page(request, user, plan, None, form, str(e), 400)
    provider_log(conn, "plan_saved", f"{form.get('key')}: " + ", ".join(
        f"{p['unit_type']}={p['stripe_price_id']}" for p in plans.prices_of(conn, new_id)), user["email"])
    conn.commit()
    conn.close()
    _flash(request, "success", "Plan saved. Prices checked with Stripe.")
    return RedirectResponse(f"/plans/{new_id}", status_code=303)


@router.post("/plans")
async def plan_create(request: Request, user: dict = Depends(require_provider)):
    return await _save(request, None, user)


@router.post("/plans/{plan_id}")
async def plan_update(request: Request, plan_id: int, user: dict = Depends(require_provider)):
    conn = get_db()
    exists = plans.get(conn, plan_id) is not None
    conn.close()
    if not exists:
        return RedirectResponse("/plans", status_code=303)
    return await _save(request, plan_id, user)


@router.post("/plans/{plan_id}/archive")
def plan_archive(request: Request, plan_id: int, user: dict = Depends(require_provider)):
    return _archive(request, plan_id, True, user)


@router.post("/plans/{plan_id}/unarchive")
def plan_unarchive(request: Request, plan_id: int, user: dict = Depends(require_provider)):
    return _archive(request, plan_id, False, user)


def _archive(request: Request, plan_id: int, archived: bool, user: dict):
    conn = get_db()
    plan = plans.get(conn, plan_id)
    if plan is not None:
        plans.set_archived(conn, plan_id, archived)
        provider_log(conn, "plan_archived" if archived else "plan_unarchived", plan["key"], user["email"])
        conn.commit()
        _flash(request, "success", f"{plan['name']} {'archived: hidden from sign-up; subscribers stay on it' if archived else 'restored'}.")
    conn.close()
    return RedirectResponse("/plans", status_code=303)
