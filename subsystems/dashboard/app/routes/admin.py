import csv
import io
import subprocess
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from app.auth import require_auth
from app.deps import get_db, get_devices, log_event
from app.permissions import (
    require_perm, user_can, all_roles, CAPABILITIES, CAP_KEYS, slugify_role_key, invalidate_cache,
)
import json
from app.templates_config import templates

router = APIRouter()


def _set_flash(request: Request, type_: str, msg: str) -> None:
    request.session["flash"] = {"type": type_, "msg": msg}


_ADMIN_AREA_CAPS = ("manage_backups", "manage_system", "manage_users", "manage_roles",
                    "manage_notifications", "update_server")


def _require_admin(current_user: dict) -> None:
    """Landing gate: any admin-area capability may open /admin."""
    if not any(user_can(current_user, c) for c in _ADMIN_AREA_CAPS):
        raise PermissionError("You don't have access to the admin area.")


def _backup_cfg() -> dict:
    conn = get_db()
    conn.execute("INSERT OR IGNORE INTO backup_config (id) VALUES (1)")
    conn.commit()
    row = conn.execute("SELECT * FROM backup_config WHERE id = 1").fetchone()
    conn.close()
    return dict(row)


def _restart_relay() -> tuple[bool, str]:
    """Restart the hbbs/hbbr containers. Returns (ok, message)."""
    try:
        from setup_server import COMPOSE_DST
        r = subprocess.run(
            ["docker", "compose", "-f", str(COMPOSE_DST), "restart"],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode != 0:
            return False, (r.stderr or "docker compose restart failed").strip()[:400]
        return True, "hbbs/hbbr restarted."
    except Exception as e:
        return False, str(e)[:400]


# ── Admin landing ────────────────────────────────────────────────────────────

@router.get("/admin", response_class=HTMLResponse)
async def admin_home(request: Request, current_user: dict = Depends(require_auth)):
    _require_admin(current_user)
    conn = get_db()
    device_count = conn.execute("SELECT COUNT(*) FROM devices WHERE hidden = 0").fetchone()[0]
    active_sessions = conn.execute("SELECT COUNT(*) FROM device_sessions WHERE ended_at IS NULL").fetchone()[0]
    log_count = conn.execute("SELECT COUNT(*) FROM provisioning_events").fetchone()[0]
    user_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    conn.close()
    try:
        import backup
        backup_status = backup.status()
    except Exception:
        backup_status = None
    try:
        import backup_remote
        offsite = backup_remote.offsite_summary()
    except Exception:
        offsite = None
    return templates.TemplateResponse(
        request, "admin.html",
        {
            "current_user": current_user,
            "device_count": device_count,
            "active_sessions": active_sessions,
            "log_count": log_count,
            "user_count": user_count,
            "backup_status": backup_status,
            "offsite": offsite,
        },
    )


# ── Backup & restore ─────────────────────────────────────────────────────────

@router.get("/admin/backup", response_class=HTMLResponse)
async def admin_backup(request: Request, current_user: dict = Depends(require_auth)):
    require_perm(current_user, "manage_backups")
    cfg = _backup_cfg()
    try:
        import backup
        archives = backup.list_archives()
        backup_status = backup.status()
    except Exception:
        archives, backup_status = [], None
    # Off-site listing: shown whenever a destination is configured (even if the toggle is off).
    remote_archives, remote_error, remote_configured = [], None, False
    try:
        import backup_remote
        remote_configured = backup_remote.offsite_summary(cfg)["configured"]
        if remote_configured:
            remote_archives = backup_remote.list_remote(cfg)
    except Exception as e:
        remote_error = f"{type(e).__name__}: {str(e)[:300]}"
    # Never send secrets to the browser; show only whether they're set.
    cfg_view = dict(cfg)
    cfg_view["s3_secret_set"] = bool(cfg.get("s3_secret_key"))
    cfg_view["passphrase_set"] = bool(cfg.get("passphrase"))
    cfg_view.pop("s3_secret_key", None)
    cfg_view.pop("passphrase", None)
    return templates.TemplateResponse(
        request, "admin_backup.html",
        {
            "current_user": current_user,
            "cfg": cfg_view,
            "archives": archives,
            "remote_archives": remote_archives,
            "remote_error": remote_error,
            "remote_configured": remote_configured,
            "backup_status": backup_status,
        },
    )


def _save_backup_form(form, current_user: dict) -> dict:
    """Persist the destination form. Secret + passphrase: blank keeps the stored value."""
    def f(name: str, default: str = "") -> str:
        return (form.get(name) or default).strip()

    provider = f("provider", "s3")
    provider = provider if provider in ("s3", "rclone") else "s3"
    try:
        ret = max(1, min(365, int(f("retention", "14"))))
    except ValueError:
        ret = 14
    try:
        off_ret = max(0, min(3650, int(f("offsite_retention", "0") or "0")))
    except ValueError:
        off_ret = 0
    is_enabled = 1 if f("enabled") == "1" else 0

    conn = get_db()
    conn.execute("INSERT OR IGNORE INTO backup_config (id) VALUES (1)")
    conn.execute(
        """UPDATE backup_config SET enabled=?, provider=?, s3_provider=?, s3_endpoint=?,
               s3_region=?, s3_bucket=?, s3_prefix=?, s3_access_key=?, rclone_remote=?,
               retention=?, offsite_retention=?, updated_at=datetime('now') WHERE id=1""",
        (is_enabled, provider, f("s3_provider", "aws"), f("s3_endpoint"), f("s3_region"),
         f("s3_bucket"), f("s3_prefix"), f("s3_access_key"), f("rclone_remote"), ret, off_ret),
    )
    if f("s3_secret_key"):
        conn.execute("UPDATE backup_config SET s3_secret_key=? WHERE id=1", (f("s3_secret_key"),))
    if f("passphrase"):
        conn.execute("UPDATE backup_config SET passphrase=? WHERE id=1", (f("passphrase"),))
    log_event(conn, "backup_config_updated", f"provider={provider} enabled={is_enabled}", current_user["email"])
    conn.commit()
    conn.close()
    return _backup_cfg()


@router.post("/admin/backup/save")
async def admin_backup_save(request: Request, current_user: dict = Depends(require_auth)):
    require_perm(current_user, "manage_backups")
    cfg = _save_backup_form(await request.form(), current_user)
    try:
        import backup_remote
        summary = backup_remote.offsite_summary(cfg)
    except Exception:
        summary = {"enabled": bool(cfg["enabled"]), "configured": True}
    if summary["enabled"] and not summary["configured"]:
        _set_flash(request, "error",
                   "Saved, but off-site copy is on without a bucket and access keys — uploads will fail.")
    elif summary["enabled"]:
        _set_flash(request, "success", "Saved. Off-site copy is ON — every backup will be uploaded.")
    else:
        _set_flash(request, "success", "Saved. Off-site copy is OFF — backups stay on this server only.")
    return RedirectResponse("/admin/backup", status_code=303)


@router.post("/admin/backup/test")
async def admin_backup_test(request: Request, current_user: dict = Depends(require_auth)):
    """Save what's on screen, then test it — so the test matches the form and nothing is lost."""
    require_perm(current_user, "manage_backups")
    cfg = _save_backup_form(await request.form(), current_user)
    try:
        import backup_remote
        res = backup_remote.test(cfg)
    except Exception as e:
        res = {"ok": False, "error": str(e)[:400]}
    state = "ON" if cfg["enabled"] else "OFF (turn it on to upload backups)"
    if res["ok"]:
        _set_flash(request, "success",
                   f"Saved. Destination reachable — credentials and bucket look good. Off-site copy is {state}.")
    else:
        _set_flash(request, "error", f"Saved, but the destination test failed: {res['error']}")
    return RedirectResponse("/admin/backup", status_code=303)


@router.post("/admin/backup/run")
def admin_backup_run(request: Request, current_user: dict = Depends(require_auth)):
    require_perm(current_user, "manage_backups")
    import backup
    st = backup.run()
    if st.get("ok"):
        msg = f"Backup created — {st['file']} ({st['size_human']})."
        off = st.get("offsite", {})
        if off.get("attempted"):
            msg += " Off-site copy ok." if off.get("ok") else f" Off-site copy FAILED: {off.get('error')}"
        _set_flash(request, "success" if not (off.get("attempted") and not off.get("ok")) else "error", msg)
    else:
        _set_flash(request, "error", f"Backup failed: {st.get('error')}")
    return RedirectResponse("/admin/backup", status_code=303)


@router.post("/admin/backup/restore")
def admin_backup_restore(
    request: Request,
    archive: str = Form(""),
    confirm: str = Form(""),
    current_user: dict = Depends(require_auth),
):
    require_perm(current_user, "manage_backups")
    import backup
    if confirm != "yes":
        _set_flash(request, "error", "Restore not confirmed.")
        return RedirectResponse("/admin/backup", status_code=303)
    # Only allow an archive that actually exists in the backup dir (no traversal).
    names = {a["file"] for a in backup.list_archives()}
    if archive not in names:
        _set_flash(request, "error", "Unknown archive.")
        return RedirectResponse("/admin/backup", status_code=303)

    res = backup.restore(backup.BACKUP_DIR / archive)
    return _finish_restore(request, res, archive, current_user)


def _finish_restore(request: Request, res: dict, label: str, current_user: dict) -> RedirectResponse:
    """Shared tail of local + off-site restore: restart relay, audit, flash."""
    if not res["ok"]:
        _set_flash(request, "error", f"Restore failed: {res['error']} (a safety snapshot was taken: {res.get('safety')})")
        return RedirectResponse("/admin/backup", status_code=303)

    ok, relay_msg = _restart_relay()
    log_event_conn = get_db()
    log_event(log_event_conn, "backup_restored", f"{label} -> {', '.join(res['applied'])}", current_user["email"])
    log_event_conn.commit()
    log_event_conn.close()
    msg = (f"Restored {', '.join(res['applied'])} from {label}. "
           f"Safety snapshot: {res.get('safety')}. "
           + ("Relay restarted." if ok else f"Relay restart FAILED: {relay_msg} — restart it from the box.")
           + " If you get logged out, sign in again.")
    _set_flash(request, "success" if ok else "error", msg)
    return RedirectResponse("/admin/backup", status_code=303)


@router.post("/admin/backup/offsite/restore")
def admin_backup_offsite_restore(
    request: Request,
    archive: str = Form(""),
    confirm: str = Form(""),
    current_user: dict = Depends(require_auth),
):
    """Download an off-site archive to a temp dir and restore from it."""
    require_perm(current_user, "manage_backups")
    import tempfile
    import shutil
    import backup
    import backup_remote
    if confirm != "yes":
        _set_flash(request, "error", "Restore not confirmed.")
        return RedirectResponse("/admin/backup", status_code=303)
    tmp = Path(tempfile.mkdtemp(prefix="rdf-offsite-"))
    try:
        try:
            # download() only accepts names in the off-site listing (no arbitrary keys).
            path = backup_remote.download(_backup_cfg(), archive, tmp)
        except FileNotFoundError:
            _set_flash(request, "error", "Unknown off-site archive.")
            return RedirectResponse("/admin/backup", status_code=303)
        except Exception as e:
            hint = " The access key needs s3:GetObject to restore." if "AccessDenied" in str(e) or "403" in str(e) else ""
            _set_flash(request, "error", f"Could not download {archive}: {type(e).__name__}: {str(e)[:250]}.{hint}")
            return RedirectResponse("/admin/backup", status_code=303)
        res = backup.restore(path)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return _finish_restore(request, res, f"off-site {archive}", current_user)


@router.post("/admin/backup/offsite/delete")
def admin_backup_offsite_delete(
    request: Request,
    archive: str = Form(""),
    confirm: str = Form(""),
    current_user: dict = Depends(require_auth),
):
    """Delete one archive from the off-site destination. Local copies are not touched."""
    require_perm(current_user, "manage_backups")
    import backup_remote
    if confirm != "yes":
        _set_flash(request, "error", "Delete not confirmed.")
        return RedirectResponse("/admin/backup", status_code=303)
    try:
        backup_remote.delete_remote(_backup_cfg(), archive)
    except FileNotFoundError:
        _set_flash(request, "error", "Unknown off-site archive.")
        return RedirectResponse("/admin/backup", status_code=303)
    except Exception as e:
        hint = " The access key needs s3:DeleteObject to delete." if "AccessDenied" in str(e) or "403" in str(e) else ""
        _set_flash(request, "error", f"Could not delete {archive}: {type(e).__name__}: {str(e)[:250]}.{hint}")
        return RedirectResponse("/admin/backup", status_code=303)
    conn = get_db()
    log_event(conn, "backup_offsite_deleted", archive, current_user["email"])
    conn.commit()
    conn.close()
    _set_flash(request, "success", f"Deleted {archive} from the off-site destination. Local copies are unchanged.")
    return RedirectResponse("/admin/backup", status_code=303)


@router.post("/admin/backup/delete")
def admin_backup_delete(
    request: Request,
    archive: str = Form(""),
    confirm: str = Form(""),
    current_user: dict = Depends(require_auth),
):
    """Delete one local archive. Off-site copies are not touched."""
    require_perm(current_user, "manage_backups")
    import backup
    if confirm != "yes":
        _set_flash(request, "error", "Delete not confirmed.")
        return RedirectResponse("/admin/backup", status_code=303)
    # Only allow an archive that actually exists in the backup dir (no traversal).
    names = {a["file"] for a in backup.list_archives()}
    if archive not in names:
        _set_flash(request, "error", "Unknown archive.")
        return RedirectResponse("/admin/backup", status_code=303)
    try:
        (backup.BACKUP_DIR / archive).unlink()
    except OSError as e:
        _set_flash(request, "error", f"Could not delete {archive}: {e.strerror or e}")
        return RedirectResponse("/admin/backup", status_code=303)
    conn = get_db()
    log_event(conn, "backup_deleted", archive, current_user["email"])
    conn.commit()
    conn.close()
    left = len(names) - 1
    _set_flash(request, "success" if left else "error",
               f"Deleted {archive} from this server. "
               + (f"{left} local archive(s) left." if left else
                  "No local archives left — use Back up now to create one."))
    return RedirectResponse("/admin/backup", status_code=303)


@router.get("/admin/backup/download")
def admin_backup_download(archive: str = "", current_user: dict = Depends(require_auth)):
    require_perm(current_user, "manage_backups")
    import backup
    names = {a["file"] for a in backup.list_archives()}
    if archive not in names:
        raise PermissionError("Unknown archive.")
    return FileResponse(str(backup.BACKUP_DIR / archive), filename=archive,
                        media_type="application/octet-stream")


# ── Maintenance tools ────────────────────────────────────────────────────────

@router.get("/admin/export/devices.csv")
async def admin_export_devices(current_user: dict = Depends(require_auth)):
    require_perm(current_user, "manage_devices")
    devices, _ = get_devices()
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


@router.post("/admin/restart-relay")
def admin_restart_relay(request: Request, confirm: str = Form(""), current_user: dict = Depends(require_auth)):
    require_perm(current_user, "manage_system")
    if confirm != "yes":
        _set_flash(request, "error", "Restart not confirmed.")
        return RedirectResponse("/admin", status_code=303)
    ok, msg = _restart_relay()
    conn = get_db()
    log_event(conn, "relay_restarted" if ok else "relay_restart_failed", msg, current_user["email"])
    conn.commit()
    conn.close()
    _set_flash(request, "success" if ok else "error", msg if ok else f"Restart failed: {msg}")
    return RedirectResponse("/admin", status_code=303)


@router.post("/admin/logs/prune")
def admin_logs_prune(request: Request, days: str = Form("90"), current_user: dict = Depends(require_auth)):
    require_perm(current_user, "manage_system")
    try:
        d = max(1, min(3650, int(days)))
    except (TypeError, ValueError):
        d = 90
    conn = get_db()
    cur = conn.execute(
        "DELETE FROM provisioning_events WHERE created_at < datetime('now', ?)", (f"-{d} days",)
    )
    removed = cur.rowcount or 0
    log_event(conn, "logs_pruned", f"removed={removed} older_than_days={d}", current_user["email"])
    conn.commit()
    conn.close()
    _set_flash(request, "success", f"Pruned {removed} log entr{'y' if removed == 1 else 'ies'} older than {d} days.")
    return RedirectResponse("/admin", status_code=303)


# ── Roles & permissions ──────────────────────────────────────────────────────

def _role_usage() -> dict:
    conn = get_db()
    rows = conn.execute("SELECT role, COUNT(*) AS n FROM users GROUP BY role").fetchall()
    conn.close()
    return {r["role"]: r["n"] for r in rows}


@router.get("/admin/roles", response_class=HTMLResponse)
async def admin_roles(request: Request, current_user: dict = Depends(require_auth)):
    require_perm(current_user, "manage_roles")
    roles = all_roles()
    usage = _role_usage()
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
    conn = get_db()
    if conn.execute("SELECT 1 FROM roles WHERE key = ?", (key,)).fetchone():
        conn.close()
        _set_flash(request, "error", f"A role with key '{key}' already exists.")
        return RedirectResponse("/admin/roles", status_code=303)
    conn.execute(
        "INSERT INTO roles (key, name, description, permissions, is_system) VALUES (?, ?, ?, ?, 0)",
        (key, name, description.strip(), json.dumps(chosen)),
    )
    log_event(conn, "role_created", f"{key} perms={chosen}", current_user["email"])
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
    if key == "admin":
        _set_flash(request, "error", "The Administrator role always has every permission and can't be edited.")
        return RedirectResponse("/admin/roles", status_code=303)
    conn = get_db()
    row = conn.execute("SELECT key FROM roles WHERE key = ?", (key,)).fetchone()
    if not row:
        conn.close()
        _set_flash(request, "error", "Role not found.")
        return RedirectResponse("/admin/roles", status_code=303)
    chosen = [p for p in perms if p in CAP_KEYS]
    conn.execute(
        "UPDATE roles SET name = ?, description = ?, permissions = ? WHERE key = ?",
        (name.strip() or key, description.strip(), json.dumps(chosen), key),
    )
    log_event(conn, "role_updated", f"{key} perms={chosen}", current_user["email"])
    conn.commit()
    conn.close()
    invalidate_cache()
    _set_flash(request, "success", "Role updated.")
    return RedirectResponse("/admin/roles", status_code=303)


@router.post("/admin/roles/{key}/delete")
def admin_role_delete(request: Request, key: str, current_user: dict = Depends(require_auth)):
    require_perm(current_user, "manage_roles")
    conn = get_db()
    row = conn.execute("SELECT is_system FROM roles WHERE key = ?", (key,)).fetchone()
    if not row:
        conn.close()
        _set_flash(request, "error", "Role not found.")
        return RedirectResponse("/admin/roles", status_code=303)
    if row["is_system"]:
        conn.close()
        _set_flash(request, "error", "Built-in roles can't be deleted.")
        return RedirectResponse("/admin/roles", status_code=303)
    in_use = conn.execute("SELECT COUNT(*) FROM users WHERE role = ?", (key,)).fetchone()[0]
    if in_use:
        conn.close()
        _set_flash(request, "error", f"{in_use} user(s) still have this role — reassign them first.")
        return RedirectResponse("/admin/roles", status_code=303)
    conn.execute("DELETE FROM roles WHERE key = ?", (key,))
    log_event(conn, "role_deleted", key, current_user["email"])
    conn.commit()
    conn.close()
    invalidate_cache()
    _set_flash(request, "success", "Role deleted.")
    return RedirectResponse("/admin/roles", status_code=303)
