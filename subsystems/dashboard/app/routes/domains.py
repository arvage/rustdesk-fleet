"""The tenant's Domains page (Admin → Domains): its provider subdomain and
custom domains, with the DNS records to create. The rules and the slow work
(DNS checks, certificates, removal) are in app/domains.py."""
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import domains, settings
from app.auth import require_auth
from app.deps import get_db, log_event
from app.permissions import require_perm, user_can
from app.tenancy import Tenant, get_tenant
from app.templates_config import templates

router = APIRouter()

STATUS_LABELS = {
    "verifying": ("Checking DNS…", "badge-pending"),
    "pending_dns": ("Waiting for DNS", "badge-pending"),
    "issuing_certificate": ("Getting a certificate…", "badge-pending"),
    "active": ("Active", "badge-active"),
    "dns_broken": ("DNS broken", "badge-failed"),
    "failed": ("Failed", "badge-failed"),
}
EXPIRY_WARN_DAYS = 14


def _flash(request: Request, type_: str, msg: str) -> None:
    request.session["flash"] = {"type": type_, "msg": msg}


def _back() -> RedirectResponse:
    return RedirectResponse("/admin/domains", status_code=303)


@router.get("/admin/domains", response_class=HTMLResponse)
def domains_page(request: Request, current_user: dict = Depends(require_auth), tenant: Tenant = Depends(get_tenant)):
    # Provider support may look (to help) but not change anything.
    if not current_user.get("is_support"):
        require_perm(current_user, "manage_domains")
    conn = get_db()
    rows = domains.list_for(conn, tenant.id)
    home = domains.home_host(conn, tenant.id)
    conn.close()
    soon = domains.fmt(domains.now() + timedelta(days=EXPIRY_WARN_DAYS))
    items = []
    for r in rows:
        status = "removing" if r["remove_reason"] else r["status"]
        label, badge = STATUS_LABELS.get(status, ("Removing…", "badge-offline"))
        items.append({**dict(r), "status": status, "label": label, "badge": badge,
                      "expiring": bool(r["cert_expires_at"] and r["cert_expires_at"] <= soon)})
    return templates.TemplateResponse(request, "domains.html", {
        "current_user": current_user, "items": items, "home": home,
        "can_manage": user_can(current_user, "manage_domains") and not current_user.get("is_support"),
        "ipv4": settings.public_ipv4(), "ipv6": settings.public_ipv6(),
        "limit": settings.max_custom_domains(),
        "custom_count": sum(1 for r in rows if r["kind"] == "custom"),
        "busy": any(i["status"] in ("verifying", "issuing_certificate", "removing") for i in items),
        "pending_days": domains.PENDING_DAYS, "open": tenant.is_open,
    })


def _manage(current_user: dict) -> None:
    require_perm(current_user, "manage_domains")


@router.post("/admin/domains")
def domain_add(request: Request, hostname: str = Form(""), current_user: dict = Depends(require_auth),
               tenant: Tenant = Depends(get_tenant)):
    _manage(current_user)
    conn = get_db()
    try:
        domains.add(conn, tenant, hostname, current_user["email"])
    except domains.DomainError as e:
        conn.close()
        _flash(request, "error", str(e))
        return _back()
    host = domains.normalize(hostname)
    log_event(conn, "domain_added", host, current_user["email"], tenant_id=tenant.id)
    conn.commit()
    conn.close()
    domains.kick()
    _flash(request, "success", f"{host} added. Create the DNS record below; we check it every few minutes.")
    return _back()


def _action(request: Request, tenant: Tenant, current_user: dict, domain_id: int, fn, event: str, done: str):
    _manage(current_user)
    conn = get_db()
    try:
        host = fn(conn, tenant.id, domain_id)
    except domains.DomainError as e:
        conn.close()
        _flash(request, "error", str(e))
        return _back()
    log_event(conn, event, host or "", current_user["email"], tenant_id=tenant.id)
    conn.commit()
    conn.close()
    _flash(request, "success", done.format(host=host))
    return _back()


@router.post("/admin/domains/{domain_id}/check")
def domain_check(request: Request, domain_id: int, current_user: dict = Depends(require_auth),
                 tenant: Tenant = Depends(get_tenant)):
    def fn(conn, tid, did):
        domains.check_now(conn, tid, did)
        return conn.execute("SELECT hostname FROM tenant_domains WHERE id = ? AND tenant_id = ?", (did, tid)).fetchone()[0]
    return _action(request, tenant, current_user, domain_id, fn, "domain_check", "Checking {host} now.")


@router.post("/admin/domains/{domain_id}/primary")
def domain_primary(request: Request, domain_id: int, current_user: dict = Depends(require_auth),
                   tenant: Tenant = Depends(get_tenant)):
    return _action(request, tenant, current_user, domain_id, domains.make_primary, "domain_primary",
                   "{host} is now your primary domain: emails, invitations and download links use it.")


@router.post("/admin/domains/{domain_id}/remove")
def domain_remove(request: Request, domain_id: int, current_user: dict = Depends(require_auth),
                  tenant: Tenant = Depends(get_tenant)):
    def fn(conn, tid, did):
        return domains.request_removal(conn, tid, did, f"removed by {current_user['email']}")
    return _action(request, tenant, current_user, domain_id, fn, "domain_remove_requested",
                   "Removing {host}. Links on that domain stop working.")
