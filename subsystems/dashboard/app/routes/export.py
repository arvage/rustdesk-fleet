"""Data export for a tenant's admins: CSVs of devices, client groups, users,
remote-session history and file-transfer history. A cancelled tenant can
reach only this (and sign-in) until it is purged (TenantMiddleware)."""
import csv
import io

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.auth import require_auth
from app.deps import get_db, get_devices
from app.permissions import require_perm
from app.tenancy import Tenant, get_tenant
from app.templates_config import templates

router = APIRouter()

EXPORTS = {
    "devices": "Devices",
    "client-groups": "Client groups",
    "users": "Users",
    "sessions": "Remote-session history",
    "file-transfers": "File-transfer history",
}


def _rows(tenant: Tenant, name: str) -> tuple[list[str], list[list]]:
    tid = tenant.id
    conn = get_db()
    try:
        if name == "devices":
            devices, _ = get_devices(tenant)
            cols = ["rustdesk_id", "label", "group", "hostname", "user", "os", "os_build", "client_version",
                    "ip", "registered", "last_seen"]
            return cols, [[d.get("rustdesk_id", ""), d.get("label", ""), d.get("group_name", ""),
                           d.get("hostname", ""), d.get("username", ""), d.get("os_name", ""),
                           d.get("os_build", ""), d.get("client_version", ""), d.get("ip", ""),
                           d.get("registered_at", ""), d.get("last_seen", "")] for d in devices]
        if name == "client-groups":
            cols = ["slug", "display_name", "status", "created_at", "devices"]
            rows = conn.execute(
                """SELECT cg.slug, cg.display_name, cg.status, cg.created_at,
                          (SELECT COUNT(*) FROM devices d WHERE d.tenant_id = cg.tenant_id AND d.group_id = cg.id
                           AND d.hidden = 0)
                   FROM client_groups cg WHERE cg.tenant_id = ? ORDER BY cg.display_name""", (tid,)).fetchall()
            return cols, [list(r) for r in rows]
        if name == "users":
            cols = ["email", "display_name", "role", "auth_method", "created_at", "last_sign_in_at"]
            rows = conn.execute("SELECT email, display_name, role, auth_method, created_at, last_sign_in_at"
                                " FROM users WHERE tenant_id = ? ORDER BY email", (tid,)).fetchall()
            return cols, [list(r) for r in rows]
        if name == "sessions":
            cols = ["rustdesk_id", "device", "started_at", "ended_at", "peer_id", "peer_name", "peer_ip", "conn_type"]
            rows = conn.execute(
                """SELECT s.rustdesk_id, COALESCE(d.label, di.hostname, ''), s.started_at, s.ended_at,
                          s.peer_id, s.peer_name, s.peer_ip, s.conn_type
                   FROM device_sessions s
                   LEFT JOIN devices d ON d.rustdesk_id = s.rustdesk_id AND d.tenant_id = s.tenant_id
                   LEFT JOIN device_info di ON di.rustdesk_id = s.rustdesk_id AND di.tenant_id = s.tenant_id
                   WHERE s.tenant_id = ? ORDER BY s.started_at""", (tid,)).fetchall()
            return cols, [list(r) for r in rows]
        cols = ["rustdesk_id", "created_at", "direction", "path", "file_count", "peer_id", "peer_name", "peer_ip"]
        rows = conn.execute(
            """SELECT rustdesk_id, created_at, CASE direction WHEN 0 THEN 'from device' ELSE 'to device' END,
                      path, file_count, peer_id, peer_name, peer_ip
               FROM device_file_transfers WHERE tenant_id = ? ORDER BY created_at""", (tid,)).fetchall()
        return cols, [list(r) for r in rows]
    finally:
        conn.close()


@router.get("/export", response_class=HTMLResponse)
async def export_page(request: Request, current_user: dict = Depends(require_auth),
                      tenant: Tenant = Depends(get_tenant)):
    require_perm(current_user, "manage_users")
    from app.permissions import user_can
    can_resubscribe = (tenant.billing_mode == "stripe" and tenant.status == "cancelled"
                       and user_can(current_user, "manage_billing") and not current_user.get("is_support"))
    return templates.TemplateResponse(request, "export.html", {
        "current_user": current_user, "exports": EXPORTS, "tenant": tenant, "can_resubscribe": can_resubscribe,
        "resubscribed": request.query_params.get("resubscribed") == "1"})


@router.post("/export/resubscribe")
def export_resubscribe(request: Request, current_user: dict = Depends(require_auth),
                       tenant: Tenant = Depends(get_tenant)):
    """Subscribe again during the cancellation grace period: a new Checkout
    Session for the same Stripe customer (card required). The webhook
    reopens the tenant with the same relay, keys and devices."""
    require_perm(current_user, "manage_billing")
    if current_user.get("is_support") or tenant.billing_mode != "stripe" or tenant.status != "cancelled":
        return RedirectResponse("/export", status_code=303)
    from app import billing, domains, signup
    conn = get_db()
    home = domains.home_host(conn, tenant.id)
    conn.close()
    try:
        url = billing.resubscribe_url(tenant.id, home)
    except signup.SignupError as e:
        request.session["flash"] = {"type": "error", "msg": str(e)}
        return RedirectResponse("/export", status_code=303)
    except Exception:
        request.session["flash"] = {"type": "error", "msg": "We couldn't reach our payment provider; try again shortly."}
        return RedirectResponse("/export", status_code=303)
    return RedirectResponse(url, status_code=303)


@router.get("/export/{name}.csv")
async def export_csv(name: str, current_user: dict = Depends(require_auth), tenant: Tenant = Depends(get_tenant)):
    require_perm(current_user, "manage_users")
    if name not in EXPORTS:
        return Response("Not found", status_code=404)
    cols, rows = _rows(tenant, name)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    w.writerows(rows)
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={tenant.slug}-{name}.csv"})
