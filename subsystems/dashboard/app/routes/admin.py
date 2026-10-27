import csv
import io

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.auth import require_auth
from app.deps import get_db, get_devices, log_event
from app.permissions import (
    require_perm, user_can, all_roles, CAPABILITIES, CAP_KEYS, slugify_role_key, invalidate_cache,
)
import json
from app.tenancy import Tenant, get_tenant
from app.templates_config import templates

router = APIRouter()

# Backups, code signing, relay restarts and upgrades are provider-level: they
# are in the provider console (app/console), not here.


def _set_flash(request: Request, type_: str, msg: str) -> None:
    request.session["flash"] = {"type": type_, "msg": msg}


_ADMIN_AREA_CAPS = ("manage_system", "manage_users", "manage_roles", "manage_notifications")


def _require_admin(current_user: dict) -> None:
    """Landing gate: any admin-area capability may open /admin."""
    if not any(user_can(current_user, c) for c in _ADMIN_AREA_CAPS):
        raise PermissionError("You don't have access to the admin area.")


# ── Admin landing ────────────────────────────────────────────────────────────

@router.get("/admin", response_class=HTMLResponse)
async def admin_home(request: Request, current_user: dict = Depends(require_auth),
                     tenant: Tenant = Depends(get_tenant)):
    _require_admin(current_user)
    tid = tenant.id
    conn = get_db()
    device_count = conn.execute("SELECT COUNT(*) FROM devices WHERE tenant_id = ? AND hidden = 0", (tid,)).fetchone()[0]
    active_sessions = conn.execute(
        "SELECT COUNT(*) FROM device_sessions WHERE tenant_id = ? AND ended_at IS NULL", (tid,)).fetchone()[0]
    # Same events the Logs page shows.
    log_count = conn.execute(
        "SELECT COUNT(*) FROM provisioning_events WHERE tenant_id = ? AND source IS NOT 'provider'",
        (tid,)).fetchone()[0]
    user_count = conn.execute("SELECT COUNT(*) FROM users WHERE tenant_id = ?", (tid,)).fetchone()[0]
    conn.close()
    return templates.TemplateResponse(
        request, "admin.html",
        {
            "current_user": current_user,
            "device_count": device_count,
            "active_sessions": active_sessions,
            "log_count": log_count,
            "user_count": user_count,
        },
    )


# ── Maintenance tools ────────────────────────────────────────────────────────

@router.get("/admin/export/devices.csv")
async def admin_export_devices(current_user: dict = Depends(require_auth),
                               tenant: Tenant = Depends(get_tenant)):
    require_perm(current_user, "manage_devices")
    devices, _ = get_devices(tenant)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["rustdesk_id", "label", "group", "hostname", "user", "os", "os_build",
                "cpu", "cores", "memory", "client_version", "ip", "registered", "last_seen"])
    for d in devices:
        w.writerow([
            d.get("rustdesk_id", ""), d.get("label", ""), d.get("group_name", ""),
            d.get("hostname", ""), d.get("username", ""), d.get("os_name", ""), d.get("os_build", ""),
            d.get("cpu_name", ""), d.get("cores", ""), d.get("ram", ""), d.get("client_version", ""),
            d.get("ip", ""), d.get("registered_at", ""), d.get("last_seen", ""),
        ])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=rustdesk-devices.csv"},
    )


@router.post("/admin/logs/prune")
def admin_logs_prune(request: Request, days: str = Form("90"), current_user: dict = Depends(require_auth)):
    require_perm(current_user, "manage_system")
    try:
        d = max(1, min(3650, int(days)))
    except (TypeError, ValueError):
        d = 90
    tid = current_user["tenant_id"]
    conn = get_db()
    cur = conn.execute(
        "DELETE FROM provisioning_events WHERE tenant_id = ? AND created_at < datetime('now', ?)",
        (tid, f"-{d} days"),
    )
    removed = cur.rowcount or 0
    log_event(conn, "logs_pruned", f"removed={removed} older_than_days={d}", current_user["email"], tenant_id=tid)
    conn.commit()
    conn.close()
    _set_flash(request, "success", f"Pruned {removed} log entr{'y' if removed == 1 else 'ies'} older than {d} days.")
    return RedirectResponse("/admin", status_code=303)


# ── Roles & permissions ──────────────────────────────────────────────────────

def _role_usage(tenant_id: int) -> dict:
    conn = get_db()
    rows = conn.execute("SELECT role, COUNT(*) AS n FROM users WHERE tenant_id = ? GROUP BY role",
                        (tenant_id,)).fetchall()
    conn.close()
    return {r["role"]: r["n"] for r in rows}


@router.get("/admin/roles", response_class=HTMLResponse)
async def admin_roles(request: Request, current_user: dict = Depends(require_auth)):
    require_perm(current_user, "manage_roles")
    roles = all_roles(current_user["tenant_id"])
    usage = _role_usage(current_user["tenant_id"])
    # Stable display order: system roles first (admin, tech, viewer), then custom.
    order = ["admin", "tech", "viewer"]
    keys = [k for k in order if k in roles] + sorted(k for k in roles if k not in order)
    role_list = [{
        "key": k, "name": roles[k]["name"], "description": roles[k]["description"],
        "is_system": roles[k]["is_system"], "perms": roles[k]["perms"],
        "locked": k == "admin", "users": usage.get(k, 0),
    } for k in keys]
    # Group capabilities by section, preserving catalogue order.
    sec_order, by_sec = [], {}
    for cap in CAPABILITIES:
        sec = cap[3]
        if sec not in by_sec:
            by_sec[sec] = []
            sec_order.append(sec)
        by_sec[sec].append(cap)
    sections = [(sec, by_sec[sec]) for sec in sec_order]
    return templates.TemplateResponse(
        request, "admin_roles.html",
        {"current_user": current_user, "roles": role_list, "sections": sections},
    )


@router.post("/admin/roles")
def admin_role_create(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    perms: list[str] = Form(default=[]),
    current_user: dict = Depends(require_auth),
):
    require_perm(current_user, "manage_roles")
    name = name.strip()
    key = slugify_role_key(name)
    if not name or not key:
        _set_flash(request, "error", "Enter a role name.")
        return RedirectResponse("/admin/roles", status_code=303)
    chosen = [p for p in perms if p in CAP_KEYS]
    tid = current_user["tenant_id"]
    conn = get_db()
    if conn.execute("SELECT 1 FROM roles WHERE tenant_id = ? AND key = ?", (tid, key)).fetchone():
        conn.close()
        _set_flash(request, "error", f"A role with key '{key}' already exists.")
        return RedirectResponse("/admin/roles", status_code=303)
    conn.execute(
        "INSERT INTO roles (tenant_id, key, name, description, permissions, is_system) VALUES (?, ?, ?, ?, ?, 0)",
        (tid, key, name, description.strip(), json.dumps(chosen)),
    )
    log_event(conn, "role_created", f"{key} perms={chosen}", current_user["email"], tenant_id=tid)
    conn.commit()
    conn.close()
    invalidate_cache()
    _set_flash(request, "success", f"Role '{name}' created.")
    return RedirectResponse("/admin/roles", status_code=303)


@router.post("/admin/roles/{key}/update")
def admin_role_update(
    request: Request,
    key: str,
    name: str = Form(...),
    description: str = Form(""),
    perms: list[str] = Form(default=[]),
    current_user: dict = Depends(require_auth),
):
    require_perm(current_user, "manage_roles")
    tid = current_user["tenant_id"]
    conn = get_db()
    row = conn.execute("SELECT key FROM roles WHERE tenant_id = ? AND key = ?", (tid, key)).fetchone()
    if not row:
        conn.close()
        _set_flash(request, "error", "Role not found.")
        return RedirectResponse("/admin/roles", status_code=303)
    if key == "admin":
        conn.close()
        _set_flash(request, "error", "The Administrator role always has every permission and can't be edited.")
        return RedirectResponse("/admin/roles", status_code=303)
    chosen = [p for p in perms if p in CAP_KEYS]
    conn.execute(
        "UPDATE roles SET name = ?, description = ?, permissions = ? WHERE tenant_id = ? AND key = ?",
        (name.strip() or key, description.strip(), json.dumps(chosen), tid, key),
    )
    log_event(conn, "role_updated", f"{key} perms={chosen}", current_user["email"], tenant_id=tid)
    conn.commit()
    conn.close()
    invalidate_cache()
    _set_flash(request, "success", "Role updated.")
    return RedirectResponse("/admin/roles", status_code=303)


@router.post("/admin/roles/{key}/delete")
def admin_role_delete(request: Request, key: str, current_user: dict = Depends(require_auth)):
    require_perm(current_user, "manage_roles")
    tid = current_user["tenant_id"]
    conn = get_db()
    row = conn.execute("SELECT is_system FROM roles WHERE tenant_id = ? AND key = ?", (tid, key)).fetchone()
    if not row:
        conn.close()
        _set_flash(request, "error", "Role not found.")
        return RedirectResponse("/admin/roles", status_code=303)
    if row["is_system"]:
        conn.close()
        _set_flash(request, "error", "Built-in roles can't be deleted.")
        return RedirectResponse("/admin/roles", status_code=303)
    in_use = conn.execute("SELECT COUNT(*) FROM users WHERE tenant_id = ? AND role = ?", (tid, key)).fetchone()[0]
    if in_use:
        conn.close()
        _set_flash(request, "error", f"{in_use} user(s) still have this role — reassign them first.")
        return RedirectResponse("/admin/roles", status_code=303)
    conn.execute("DELETE FROM roles WHERE tenant_id = ? AND key = ?", (tid, key))
    log_event(conn, "role_deleted", key, current_user["email"], tenant_id=tid)
    conn.commit()
    conn.close()
    invalidate_cache()
    _set_flash(request, "success", "Role deleted.")
    return RedirectResponse("/admin/roles", status_code=303)
