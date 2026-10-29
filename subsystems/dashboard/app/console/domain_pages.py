"""Provider console: tenants' custom domains (phase 1d). The tenant page
lists them; a provider admin can remove one for abuse (with a reason, logged,
the tenant's admins emailed) but never adds one; /domains lists every
custom domain with a problem."""
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import domains
from app.console.auth import require_provider
from app.deps import get_db, provider_log
from app.templates_config import templates

router = APIRouter()

EXPIRY_WARN_DAYS = 14


def _flash(request: Request, kind: str, msg: str) -> None:
    request.session["flash"] = {"type": kind, "msg": msg}


def expiring_before() -> str:
    return domains.fmt(domains.now() + timedelta(days=EXPIRY_WARN_DAYS))


@router.post("/tenants/{tenant_id}/domains/{domain_id}/remove")
def remove_domain(request: Request, tenant_id: int, domain_id: int, reason: str = Form(""),
                  user: dict = Depends(require_provider)):
    reason = reason.strip()
    back = RedirectResponse(f"/tenants/{tenant_id}", status_code=303)
    if not reason:
        _flash(request, "error", "Give a reason for removing the domain (the tenant's admins are told).")
        return back
    conn = get_db()
    try:
        host = domains.request_removal(conn, tenant_id, domain_id, f"removed by the provider: {reason[:250]}")
    except domains.DomainError as e:
        conn.close()
        _flash(request, "error", str(e))
        return back
    provider_log(conn, "domain_removed_by_provider", f"{host}; reason: {reason[:300]}", user["email"], tenant_id)
    conn.commit()
    conn.close()
    domains.notify_admins(tenant_id, f"{host} was removed", [
        f"Your provider removed the custom domain {host} from your account. Reason: {reason}",
        "Links on that domain stop working; your dashboard, emails and links use your account's own address."])
    _flash(request, "success", f"Removing {host}. The tenant's admins were emailed.")
    return back


@router.get("/domains", response_class=HTMLResponse)
def domain_problems(request: Request, user: dict = Depends(require_provider)):
    conn = get_db()
    rows = conn.execute(
        """SELECT d.*, t.slug, t.display_name FROM tenant_domains d JOIN tenants t ON t.id = d.tenant_id
           WHERE d.kind = 'custom' AND (d.status IN ('dns_broken', 'failed')
                 OR (d.cert_expires_at IS NOT NULL AND d.cert_expires_at <= ?))
           ORDER BY d.status = 'dns_broken' DESC, d.cert_expires_at, d.hostname""", (expiring_before(),)).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM tenant_domains WHERE kind = 'custom'").fetchone()[0]
    conn.close()
    return templates.TemplateResponse(request, "console/domains.html", {
        "current_user": user, "rows": rows, "total": total, "soon": expiring_before(), "warn_days": EXPIRY_WARN_DAYS})
