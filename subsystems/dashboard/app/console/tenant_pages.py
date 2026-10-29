"""Console pages for tenants: create, list, detail, lifecycle."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import settings
from app.console import jobs, tenants
from app.console.auth import require_provider
from app.deps import get_db, provider_log
from app.templates_config import templates

router = APIRouter()


def _flash(request: Request, kind: str, msg: str) -> None:
    request.session["flash"] = {"type": kind, "msg": msg}


@router.get("/tenants/new", response_class=HTMLResponse)
async def new_tenant(request: Request, user: dict = Depends(require_provider)):
    return templates.TemplateResponse(request, "console/tenant_new.html", {
        "current_user": user, "errors": [], "f": {}, "domain": settings.provider_domain()})


@router.post("/tenants")
async def create_tenant(request: Request, name: str = Form(""), slug: str = Form(""), admin_email: str = Form(""),
                        admin_name: str = Form(""), user: dict = Depends(require_provider)):
    slug = slug.strip().lower() or settings.suggest_slug(name)
    f = {"name": name, "slug": slug, "admin_email": admin_email, "admin_name": admin_name}
    conn = get_db()
    errors = tenants.validate(conn, name, slug, admin_email.strip())
    busy = conn.execute("SELECT id FROM provider_jobs WHERE kind = 'create_tenant' AND status IN ('queued','running')"
                        " AND params LIKE ?", (f'%"slug": "{slug}"%',)).fetchone()
    conn.close()
    if busy:
        errors.append(f"“{slug}” is already being created (job {busy['id']}).")
    if errors:
        return templates.TemplateResponse(request, "console/tenant_new.html", {
            "current_user": user, "errors": errors, "f": f, "domain": settings.provider_domain()}, status_code=400)
    job_id = jobs.enqueue("create_tenant", None, {"name": name.strip(), "slug": slug,
                                                  "admin_email": admin_email.strip().lower(),
                                                  "admin_name": admin_name.strip()}, user["email"])
    conn = get_db()
    provider_log(conn, "tenant_create_queued", f"{slug} ({name.strip()})", user["email"])
    conn.close()
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@router.post("/tenants/{tenant_id}/invites/{invite_id}/resend")
async def resend_invite(request: Request, tenant_id: int, invite_id: int, user: dict = Depends(require_provider)):
    conn = get_db()
    row = conn.execute(
        """SELECT i.*, t.slug, t.display_name AS tenant_name, t.status AS tenant_status FROM tenant_invites i
           JOIN tenants t ON t.id = i.tenant_id WHERE i.id = ? AND i.tenant_id = ?""", (invite_id, tenant_id)).fetchone()
    if row is None or row["tenant_status"] in ("cancelled", "purged"):
        conn.close()
        _flash(request, "error", "Invitation not found.")
        return RedirectResponse(f"/tenants/{tenant_id}", status_code=303)
    if conn.execute("SELECT 1 FROM users WHERE tenant_id = ? AND email = ?", (tenant_id, row["email"])).fetchone():
        conn.close()
        _flash(request, "error", f"{row['email']} has already set up their account.")
        return RedirectResponse(f"/tenants/{tenant_id}", status_code=303)
    link = tenants.create_invite(conn, tenant_id, row["slug"], row["email"], row["display_name"], user["email"], row["role"])
    provider_log(conn, "tenant_invite_resent", row["email"], user["email"], tenant_id)
    conn.close()
    ok, err = tenants.email_invite(row["email"], row["tenant_name"], link)
    request.session["new_invite_link"] = {"email": row["email"], "link": link, "emailed": ok}
    _flash(request, "success" if ok else "error",
           f"New invitation emailed to {row['email']}." if ok else f"New invitation made, but not emailed: {err}")
    return RedirectResponse(f"/tenants/{tenant_id}", status_code=303)


# ── List and detail ──────────────────────────────────────────────────────────

def _month_bounds(offset: int = 0) -> tuple[str, str]:
    from datetime import date
    today = date.today()
    y, m = today.year, today.month + offset
    while m <= 0:
        y, m = y - 1, m + 12
    start = date(y, m, 1)
    end = date(y + (m == 12), m % 12 + 1, 1)
    return f"{start} 00:00:00", f"{end} 00:00:00"


def tenant_summaries(conn, tenant_id: int | None = None) -> list[dict]:
    """One row per tenant (not purged ones unless asked by id) with the
    numbers the list shows."""
    from app.console import relays
    from app.console.system import SYSTEM
    from app.tenancy import load_tenant
    where = "WHERE t.id = ?" if tenant_id else "WHERE t.status <> 'purged'"
    rows = conn.execute(
        f"""SELECT t.*,
               (SELECT COUNT(*) FROM devices d WHERE d.tenant_id = t.id AND d.hidden = 0) AS devices,
               (SELECT COUNT(*) FROM users u WHERE u.tenant_id = t.id) AS technicians,
               (SELECT MAX(heartbeat_at) FROM device_info di WHERE di.tenant_id = t.id) AS last_heartbeat,
               (SELECT MAX(last_sign_in_at) FROM users u WHERE u.tenant_id = t.id) AS last_sign_in,
               (SELECT hostname FROM tenant_domains td WHERE td.tenant_id = t.id AND td.is_primary = 1) AS web_domain,
               (SELECT cert_expires_at FROM tenant_domains td WHERE td.tenant_id = t.id AND td.is_primary = 1) AS cert_expires_at,
               r.status AS relay_status, r.host AS relay_host
            FROM tenants t LEFT JOIN relays r ON r.tenant_id = t.id {where} ORDER BY t.id""",
        (tenant_id,) if tenant_id else (),
    ).fetchall()
    running = SYSTEM.running_containers()
    out = []
    for r in rows:
        d = dict(r)
        t = load_tenant(r["id"])
        d["relay_version"] = relays.running_version(t) if t and t.relay else None
        if t and t.relay and running is not None:
            h = relays.health(t, running)
            d["relay_containers"] = f"{sum(1 for c in h.values() if c['running'])}/2 running"
        else:
            d["relay_containers"] = "—"
        d["last_activity"] = max(x for x in (r["last_heartbeat"], r["last_sign_in"], "") if x is not None) or None
        out.append(d)
    return out


@router.get("/", response_class=HTMLResponse)
def tenant_list(request: Request, q: str = "", user: dict = Depends(require_provider)):
    conn = get_db()
    rows = tenant_summaries(conn)
    conn.close()
    if q:
        ql = q.lower()
        rows = [r for r in rows if ql in r["slug"] or ql in r["display_name"].lower() or ql in (r["web_domain"] or "")]
    return templates.TemplateResponse(request, "console/tenants.html", {"current_user": user, "tenants": rows, "q": q})


def usage(conn, tenant_id: int) -> dict:
    from app.console.maintenance import session_minutes
    this, last = _month_bounds(0), _month_bounds(-1)
    history = [dict(r) for r in conn.execute(
        "SELECT * FROM tenant_usage_daily WHERE tenant_id = ? ORDER BY day DESC LIMIT 31", (tenant_id,))]
    return {"session_hours_this_month": round(session_minutes(conn, tenant_id, *this) / 60, 1),
            "session_hours_last_month": round(session_minutes(conn, tenant_id, *last) / 60, 1),
            "history": history}


@router.get("/tenants/{tenant_id}", response_class=HTMLResponse)
def tenant_detail(request: Request, tenant_id: int, user: dict = Depends(require_provider)):
    from app.console import domain_pages, relays
    from app.tenancy import load_tenant
    conn = get_db()
    rows = tenant_summaries(conn, tenant_id)
    if not rows:
        conn.close()
        _flash(request, "error", "Tenant not found.")
        return RedirectResponse("/", status_code=303)
    t = rows[0]
    domains = conn.execute("SELECT * FROM tenant_domains WHERE tenant_id = ? ORDER BY is_primary DESC, id",
                           (tenant_id,)).fetchall()
    admins = conn.execute("SELECT email, display_name, created_at, last_sign_in_at FROM users"
                          " WHERE tenant_id = ? AND role = 'admin' ORDER BY created_at", (tenant_id,)).fetchall()
    invites = conn.execute(
        """SELECT i.* FROM tenant_invites i WHERE i.tenant_id = ? AND i.used_at IS NULL
             AND NOT EXISTS (SELECT 1 FROM users u WHERE u.tenant_id = i.tenant_id AND u.email = i.email)
           ORDER BY i.id DESC""", (tenant_id,)).fetchall()
    use = usage(conn, tenant_id)
    from app import support
    grant = support.active_grant(conn, tenant_id)
    support_sessions = conn.execute("SELECT * FROM support_sessions WHERE tenant_id = ? ORDER BY id DESC LIMIT 5",
                                    (tenant_id,)).fetchall()
    job_rows = conn.execute("SELECT id, kind, status, created_at FROM provider_jobs WHERE tenant_id = ?"
                            " ORDER BY id DESC LIMIT 10", (tenant_id,)).fetchall()
    conn.close()
    tenant = load_tenant(tenant_id)
    relay = tenant.relay if tenant else None
    return templates.TemplateResponse(request, "console/tenant.html", {
        "current_user": user, "t": t, "relay": relay, "block": relays.block_of(relay) if relay else None,
        "containers": relays.health(tenant) if relay else {}, "domains": domains, "admins": admins,
        "invites": invites, "usage": use, "jobs": job_rows, "now": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "new_invite": request.session.pop("new_invite_link", None),
        "grant": grant, "support_sessions": support_sessions,
        "expiring_before": domain_pages.expiring_before(),
    })


@router.post("/tenants/{tenant_id}/support/open")
async def tenant_support_open(request: Request, tenant_id: int, reason: str = Form(""),
                              user: dict = Depends(require_provider)):
    """Sign this provider admin in to the tenant's dashboard: only while the
    tenant has granted support access, with a reason, through a single-use
    token valid for 60 seconds (app/support.py)."""
    from app import support
    reason = reason.strip()
    conn = get_db()
    try:
        t = conn.execute("SELECT id, slug, status FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
        if t is None or t["status"] not in ("trial", "active"):
            _flash(request, "error", "That tenant isn't open, so support can't sign in.")
            return RedirectResponse(f"/tenants/{tenant_id}" if t else "/", status_code=303)
        grant = support.active_grant(conn, tenant_id)
        if grant is None:
            _flash(request, "error", "The tenant hasn't granted support access.")
            return RedirectResponse(f"/tenants/{tenant_id}", status_code=303)
        if not reason:
            _flash(request, "error", "Give a reason for opening the tenant's dashboard.")
            return RedirectResponse(f"/tenants/{tenant_id}", status_code=303)
        # The tenant's own host (never a custom domain): it always works.
        domain = conn.execute("SELECT hostname FROM tenant_domains WHERE tenant_id = ? AND kind <> 'custom'"
                              " ORDER BY is_primary DESC, id"
                              " LIMIT 1", (tenant_id,)).fetchone()
        if domain is None:
            _flash(request, "error", "The tenant has no web domain.")
            return RedirectResponse(f"/tenants/{tenant_id}", status_code=303)
        token = support.issue_token(conn, tenant_id, grant["id"], user["id"], reason)
        provider_log(conn, "support_token_issued", f"{t['slug']}; reason: {reason[:300]}", user["email"], tenant_id)
    finally:
        conn.close()
    # A page that moves on with script rather than a redirect: the console's
    # CSP (form-action 'self') would block a form post redirecting elsewhere.
    url = f"https://{domain['hostname']}/support/enter?token={token}"
    return templates.TemplateResponse(request, "console/support_open.html",
                                      {"current_user": user, "url": url, "slug": t["slug"]},
                                      headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@router.post("/tenants/{tenant_id}/plan")
async def tenant_plan(request: Request, tenant_id: int, plan: str = Form(""), user: dict = Depends(require_provider)):
    conn = get_db()
    row = conn.execute("SELECT slug FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
    if row is None:
        conn.close()
        _flash(request, "error", "Tenant not found.")
        return RedirectResponse("/", status_code=303)
    conn.execute("UPDATE tenants SET plan = ?, updated_at = datetime('now') WHERE id = ?", (plan.strip()[:200], tenant_id))
    conn.commit()
    provider_log(conn, "tenant_plan_changed", f"{row['slug']}: {plan.strip()[:200]}", user["email"], tenant_id)
    conn.close()
    _flash(request, "success", "Plan saved.")
    return RedirectResponse(f"/tenants/{tenant_id}", status_code=303)


# ── Lifecycle ────────────────────────────────────────────────────────────────

def _lifecycle_target(request: Request, tenant_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
    conn.close()
    if row is None or row["status"] == "purged":
        _flash(request, "error", "Tenant not found.")
        return None
    return row


def _queue(request: Request, kind: str, row, user: dict, params: dict | None = None):
    if jobs.active_for(row["id"]):
        _flash(request, "error", f"{row['slug']} already has a job running; wait for it to finish.")
        return RedirectResponse(f"/tenants/{row['id']}", status_code=303)
    job_id = jobs.enqueue(kind, row["id"], params or {}, user["email"])
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@router.post("/tenants/{tenant_id}/suspend")
async def tenant_suspend(request: Request, tenant_id: int, user: dict = Depends(require_provider)):
    from app.console import lifecycle
    row = _lifecycle_target(request, tenant_id)
    if row is None:
        return RedirectResponse("/", status_code=303)
    try:
        lifecycle.suspend(tenant_id, user["email"])
        _flash(request, "success", f"{row['slug']} is suspended. Its relay keeps running.")
    except lifecycle.LifecycleError as e:
        _flash(request, "error", str(e))
    return RedirectResponse(f"/tenants/{tenant_id}", status_code=303)


@router.post("/tenants/{tenant_id}/activate")
async def tenant_activate(request: Request, tenant_id: int, user: dict = Depends(require_provider)):
    from app.console import lifecycle
    row = _lifecycle_target(request, tenant_id)
    if row is None:
        return RedirectResponse("/", status_code=303)
    try:
        lifecycle.activate(tenant_id, user["email"])
        _flash(request, "success", f"{row['slug']} is active. Its trial notice is gone.")
    except lifecycle.LifecycleError as e:
        _flash(request, "error", str(e))
    return RedirectResponse(f"/tenants/{tenant_id}", status_code=303)


@router.post("/tenants/{tenant_id}/reactivate")
async def tenant_reactivate(request: Request, tenant_id: int, user: dict = Depends(require_provider)):
    row = _lifecycle_target(request, tenant_id)
    if row is None:
        return RedirectResponse("/", status_code=303)
    if row["status"] not in ("suspended", "cancelled"):
        _flash(request, "error", "Only a suspended or cancelled tenant can be reactivated.")
        return RedirectResponse(f"/tenants/{tenant_id}", status_code=303)
    return _queue(request, "reactivate_tenant", row, user)


@router.post("/tenants/{tenant_id}/cancel")
async def tenant_cancel(request: Request, tenant_id: int, confirm: str = Form(""),
                        user: dict = Depends(require_provider)):
    from app.console import lifecycle
    row = _lifecycle_target(request, tenant_id)
    if row is None:
        return RedirectResponse("/", status_code=303)
    try:
        lifecycle.guard(tenant_id, "cancel")
    except lifecycle.LifecycleError as e:
        _flash(request, "error", str(e))
        return RedirectResponse(f"/tenants/{tenant_id}", status_code=303)
    if confirm != row["slug"]:
        _flash(request, "error", "Type the tenant's slug to confirm.")
        return RedirectResponse(f"/tenants/{tenant_id}", status_code=303)
    return _queue(request, "cancel_tenant", row, user)


@router.post("/tenants/{tenant_id}/extend-trial")
async def tenant_extend_trial(request: Request, tenant_id: int, days: str = Form("14"),
                              user: dict = Depends(require_provider)):
    from app.console import lifecycle
    row = _lifecycle_target(request, tenant_id)
    if row is None:
        return RedirectResponse("/", status_code=303)
    try:
        new = lifecycle.extend_trial(tenant_id, int(days) if days.strip().isdigit() else 14, user["email"])
        msg = f"Trial extended: it ends {new} UTC."
        if row["status"] == "suspended":
            msg += " Reactivate the tenant to put it back on trial."
        _flash(request, "success", msg)
    except lifecycle.LifecycleError as e:
        _flash(request, "error", str(e))
    return RedirectResponse(f"/tenants/{tenant_id}", status_code=303)


@router.post("/tenants/{tenant_id}/extend-grace")
async def tenant_extend(request: Request, tenant_id: int, days: str = Form("30"),
                        user: dict = Depends(require_provider)):
    from app.console import lifecycle
    row = _lifecycle_target(request, tenant_id)
    if row is None:
        return RedirectResponse("/", status_code=303)
    try:
        new = lifecycle.extend_grace(tenant_id, int(days) if days.strip().isdigit() else 30, user["email"])
        _flash(request, "success", f"Grace period extended: purge after {new} UTC.")
    except lifecycle.LifecycleError as e:
        _flash(request, "error", str(e))
    return RedirectResponse(f"/tenants/{tenant_id}", status_code=303)


@router.post("/tenants/{tenant_id}/purge")
async def tenant_purge(request: Request, tenant_id: int, confirm: str = Form(""),
                       user: dict = Depends(require_provider)):
    from app.console import lifecycle
    row = _lifecycle_target(request, tenant_id)
    if row is None:
        return RedirectResponse("/", status_code=303)
    try:
        lifecycle.guard(tenant_id, "purge")
    except lifecycle.LifecycleError as e:
        _flash(request, "error", str(e))
        return RedirectResponse(f"/tenants/{tenant_id}", status_code=303)
    if row["status"] != "cancelled":
        _flash(request, "error", "Cancel the tenant first; only cancelled tenants can be purged.")
        return RedirectResponse(f"/tenants/{tenant_id}", status_code=303)
    if confirm != row["slug"]:
        _flash(request, "error", "Type the tenant's slug to confirm the purge.")
        return RedirectResponse(f"/tenants/{tenant_id}", status_code=303)
    return _queue(request, "purge_tenant", row, user, {"reason": "purge now"})
