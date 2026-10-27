"""Console pages for tenants: create, list, detail, lifecycle."""
from __future__ import annotations

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
