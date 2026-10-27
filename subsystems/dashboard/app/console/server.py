"""Provider-level server controls in the console: backups and restore, code
signing, host health and its alerts, the pinned RustDesk client version and
provider settings. These used to live on tenant #1's dashboard."""
from __future__ import annotations

import subprocess
import threading
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from app import provider_config
from app.console import mail
from app.console.auth import require_provider
from app.deps import get_db, provider_log
from app.templates_config import templates

router = APIRouter()


def _set_flash(request: Request, type_: str, msg: str) -> None:
    request.session["flash"] = {"type": type_, "msg": msg}


def restart_all_relays() -> tuple[bool, str]:
    """Restart every tenant's relay (after a restore). Returns (ok, errors)."""
    from app.tenancy import active_tenants
    failed = []
    for t in active_tenants():
        if not t.relay or t.status == "cancelled":
            continue
        try:
            r = subprocess.run(["docker", "compose", "-f", t.relay.compose_path, "restart"],
                               capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                failed.append(f"{t.slug}: {(r.stderr or 'restart failed').strip()[:200]}")
        except Exception as e:
            failed.append(f"{t.slug}: {str(e)[:200]}")
    return not failed, "; ".join(failed)


def _backup_cfg() -> dict:
    conn = get_db()
    conn.execute("INSERT OR IGNORE INTO backup_config (id) VALUES (1)")
    conn.commit()
    row = conn.execute("SELECT * FROM backup_config WHERE id = 1").fetchone()
    conn.close()
    return dict(row)



# ── Backup & restore ─────────────────────────────────────────────────────────

def _signing_cfg() -> dict:
    conn = get_db()
    conn.execute("INSERT OR IGNORE INTO signing_config (id) VALUES (1)")
    conn.commit()
    row = conn.execute("SELECT * FROM signing_config WHERE id = 1").fetchone()
    conn.close()
    return dict(row)


def _save_signing_form(form, user: dict) -> dict:
    def f(name: str, default: str = "") -> str:
        return (form.get(name) or default).strip()

    enabled = 1 if f("enabled") == "1" else 0
    auto_sign = 1 if f("auto_sign") == "1" else 0
    conn = get_db()
    conn.execute("INSERT OR IGNORE INTO signing_config (id) VALUES (1)")
    conn.execute(
        """UPDATE signing_config SET enabled=?, auto_sign=?, provider='azure_trusted_signing',
               azure_tenant_id=?, azure_client_id=?, endpoint=?, account_name=?, profile_name=?,
               updated_at=datetime('now') WHERE id=1""",
        (enabled, auto_sign, f("azure_tenant_id"), f("azure_client_id"),
         f("endpoint"), f("account_name"), f("profile_name")),
    )
    # Secret: blank submission preserves the stored value.
    if f("azure_client_secret"):
        conn.execute("UPDATE signing_config SET azure_client_secret=? WHERE id=1", (f("azure_client_secret"),))
    provider_log(conn, "signing_config_updated", f"enabled={enabled} auto_sign={auto_sign}", user["email"])
    conn.commit()
    conn.close()
    return _signing_cfg()


@router.get("/signing", response_class=HTMLResponse)
async def admin_signing(request: Request, user: dict = Depends(require_provider)):
    cfg = _signing_cfg()
    cfg_view = dict(cfg)
    cfg_view["secret_set"] = bool(cfg.get("azure_client_secret"))
    cfg_view.pop("azure_client_secret", None)
    try:
        import sign_installer
        jsign_present = sign_installer.JSIGN_JAR.exists()
    except Exception:
        jsign_present = False
    return templates.TemplateResponse(
        request, "console/signing.html",
        {"current_user": user, "cfg": cfg_view, "jsign_present": jsign_present},
    )


@router.post("/signing/save")
async def admin_signing_save(request: Request, user: dict = Depends(require_provider)):
    cfg = _save_signing_form(await request.form(), user)
    state = "ON" if cfg["enabled"] else "OFF"
    auto = " Auto-sign is on." if cfg["enabled"] and cfg["auto_sign"] else ""
    _set_flash(request, "success", f"Signing settings saved. Code signing is {state}.{auto}")
    return RedirectResponse("/signing", status_code=303)


@router.post("/signing/test")
async def admin_signing_test(request: Request, user: dict = Depends(require_provider)):
    cfg = _save_signing_form(await request.form(), user)
    try:
        import sign_installer
        res = sign_installer.test(cfg)
    except Exception as e:
        res = {"ok": False, "error": str(e)[:400]}
    if res["ok"]:
        _set_flash(request, "success", "Saved. Azure token acquired and jsign is present — signing is ready.")
    else:
        _set_flash(request, "error", f"Saved, but the signing test failed: {res['error']}")
    return RedirectResponse("/signing", status_code=303)


@router.get("/backup", response_class=HTMLResponse)
async def admin_backup(request: Request, user: dict = Depends(require_provider)):
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
        request, "console/backup.html",
        {
            "current_user": user,
            "cfg": cfg_view,
            "archives": archives,
            "remote_archives": remote_archives,
            "remote_error": remote_error,
            "remote_configured": remote_configured,
            "backup_status": backup_status,
        },
    )


def _save_backup_form(form, user: dict) -> dict:
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
    provider_log(conn, "backup_config_updated", f"provider={provider} enabled={is_enabled}", user["email"])
    conn.commit()
    conn.close()
    return _backup_cfg()


@router.post("/backup/save")
async def admin_backup_save(request: Request, user: dict = Depends(require_provider)):
    cfg = _save_backup_form(await request.form(), user)
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
    return RedirectResponse("/backup", status_code=303)


@router.post("/backup/test")
async def admin_backup_test(request: Request, user: dict = Depends(require_provider)):
    """Save what's on screen, then test it — so the test matches the form and nothing is lost."""
    cfg = _save_backup_form(await request.form(), user)
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
    return RedirectResponse("/backup", status_code=303)


@router.post("/backup/run")
def admin_backup_run(request: Request, user: dict = Depends(require_provider)):
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
    return RedirectResponse("/backup", status_code=303)


@router.post("/backup/restore")
def admin_backup_restore(
    request: Request,
    archive: str = Form(""),
    confirm: str = Form(""),
    user: dict = Depends(require_provider),
):
    import backup
    if confirm != "yes":
        _set_flash(request, "error", "Restore not confirmed.")
        return RedirectResponse("/backup", status_code=303)
    # Only allow an archive that actually exists in the backup dir (no traversal).
    names = {a["file"] for a in backup.list_archives()}
    if archive not in names:
        _set_flash(request, "error", "Unknown archive.")
        return RedirectResponse("/backup", status_code=303)

    res = backup.restore(backup.BACKUP_DIR / archive)
    return _finish_restore(request, res, archive, user)


def _finish_restore(request: Request, res: dict, label: str, user: dict) -> RedirectResponse:
    """Shared tail of local + off-site restore: restart relay, audit, flash."""
    if not res["ok"]:
        _set_flash(request, "error", f"Restore failed: {res['error']} (a safety snapshot was taken: {res.get('safety')})")
        return RedirectResponse("/backup", status_code=303)

    # A restore can touch every relay's keypair and peer DB: restart them all.
    ok, relay_msg = restart_all_relays()
    log_event_conn = get_db()
    provider_log(log_event_conn, "backup_restored", f"{label} -> {', '.join(res['applied'])}", user["email"])
    log_event_conn.commit()
    log_event_conn.close()
    msg = (f"Restored {', '.join(res['applied'])} from {label}. "
           f"Safety snapshot: {res.get('safety')}. "
           + ("Relay restarted." if ok else f"Relay restart FAILED: {relay_msg} — restart it from the box.")
           + " If you get logged out, sign in again.")
    _set_flash(request, "success" if ok else "error", msg)
    return RedirectResponse("/backup", status_code=303)


@router.post("/backup/offsite/restore")
def admin_backup_offsite_restore(
    request: Request,
    archive: str = Form(""),
    confirm: str = Form(""),
    user: dict = Depends(require_provider),
):
    """Download an off-site archive to a temp dir and restore from it."""
    import tempfile
    import shutil
    import backup
    import backup_remote
    if confirm != "yes":
        _set_flash(request, "error", "Restore not confirmed.")
        return RedirectResponse("/backup", status_code=303)
    tmp = Path(tempfile.mkdtemp(prefix="rdf-offsite-"))
    try:
        try:
            # download() only accepts names in the off-site listing (no arbitrary keys).
            path = backup_remote.download(_backup_cfg(), archive, tmp)
        except FileNotFoundError:
            _set_flash(request, "error", "Unknown off-site archive.")
            return RedirectResponse("/backup", status_code=303)
        except Exception as e:
            hint = " The access key needs s3:GetObject to restore." if "AccessDenied" in str(e) or "403" in str(e) else ""
            _set_flash(request, "error", f"Could not download {archive}: {type(e).__name__}: {str(e)[:250]}.{hint}")
            return RedirectResponse("/backup", status_code=303)
        res = backup.restore(path)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return _finish_restore(request, res, f"off-site {archive}", user)


@router.post("/backup/offsite/delete")
def admin_backup_offsite_delete(
    request: Request,
    archive: str = Form(""),
    confirm: str = Form(""),
    user: dict = Depends(require_provider),
):
    """Delete one archive from the off-site destination. Local copies are not touched."""
    import backup_remote
    if confirm != "yes":
        _set_flash(request, "error", "Delete not confirmed.")
        return RedirectResponse("/backup", status_code=303)
    try:
        backup_remote.delete_remote(_backup_cfg(), archive)
    except FileNotFoundError:
        _set_flash(request, "error", "Unknown off-site archive.")
        return RedirectResponse("/backup", status_code=303)
    except Exception as e:
        hint = " The access key needs s3:DeleteObject to delete." if "AccessDenied" in str(e) or "403" in str(e) else ""
        _set_flash(request, "error", f"Could not delete {archive}: {type(e).__name__}: {str(e)[:250]}.{hint}")
        return RedirectResponse("/backup", status_code=303)
    conn = get_db()
    provider_log(conn, "backup_offsite_deleted", archive, user["email"])
    conn.commit()
    conn.close()
    _set_flash(request, "success", f"Deleted {archive} from the off-site destination. Local copies are unchanged.")
    return RedirectResponse("/backup", status_code=303)


@router.post("/backup/delete")
def admin_backup_delete(
    request: Request,
    archive: str = Form(""),
    confirm: str = Form(""),
    user: dict = Depends(require_provider),
):
    """Delete one local archive. Off-site copies are not touched."""
    import backup
    if confirm != "yes":
        _set_flash(request, "error", "Delete not confirmed.")
        return RedirectResponse("/backup", status_code=303)
    # Only allow an archive that actually exists in the backup dir (no traversal).
    names = {a["file"] for a in backup.list_archives()}
    if archive not in names:
        _set_flash(request, "error", "Unknown archive.")
        return RedirectResponse("/backup", status_code=303)
    try:
        (backup.BACKUP_DIR / archive).unlink()
    except OSError as e:
        _set_flash(request, "error", f"Could not delete {archive}: {e.strerror or e}")
        return RedirectResponse("/backup", status_code=303)
    conn = get_db()
    provider_log(conn, "backup_deleted", archive, user["email"])
    conn.commit()
    conn.close()
    left = len(names) - 1
    _set_flash(request, "success" if left else "error",
               f"Deleted {archive} from this server. "
               + (f"{left} local archive(s) left." if left else
                  "No local archives left — use Back up now to create one."))
    return RedirectResponse("/backup", status_code=303)


@router.get("/backup/download")
def admin_backup_download(archive: str = "", user: dict = Depends(require_provider)):
    import backup
    names = {a["file"] for a in backup.list_archives()}
    if archive not in names:
        raise PermissionError("Unknown archive.")
    return FileResponse(str(backup.BACKUP_DIR / archive), filename=archive,
                        media_type="application/octet-stream")



@router.get("/backup/download/latest")
def backup_download_latest(user: dict = Depends(require_provider)):
    import backup
    latest = backup.latest_archive()
    if not latest:
        raise PermissionError("No backup archive available yet.")
    return FileResponse(str(latest), filename=latest.name, media_type="application/octet-stream")


# ── Host health ──────────────────────────────────────────────────────────────

def host_health_payload() -> dict:
    from app.routes.api import _container_states, _host_health
    from app.tenancy import active_tenants
    relays = []
    for t in active_tenants():
        if t.relay:
            relays.append({"tenant": t.slug, "name": t.display_name, "status": t.status,
                           "containers": _container_states(t)})
    return {**_host_health(), "relays": relays}


@router.get("/health", response_class=HTMLResponse)
async def health(request: Request, user: dict = Depends(require_provider)):
    return templates.TemplateResponse(request, "console/health.html", {"current_user": user})


@router.get("/health.json")
def health_json(user: dict = Depends(require_provider)):
    return JSONResponse(host_health_payload())


# ── RustDesk client version (one pin for every tenant's installers) ──────────

_rebuild_lock = threading.Lock()


def _rebuild_all(version: str, email: str) -> None:
    from app.routes.home import _rebuild_outdated_installers
    from app.tenancy import active_tenants
    if not _rebuild_lock.acquire(blocking=False):
        return
    try:
        for t in active_tenants():
            if t.is_open:
                _rebuild_outdated_installers(t.id, version, email)
    finally:
        _rebuild_lock.release()


@router.get("/client-version", response_class=HTMLResponse)
def client_version(request: Request, user: dict = Depends(require_provider)):
    from generate_installer import get_latest_client_version, get_pinned_version, version_tuple
    pinned, latest = get_pinned_version(), get_latest_client_version()
    return templates.TemplateResponse(request, "console/client_version.html", {
        "current_user": user, "pinned": pinned, "latest": latest,
        "update_available": bool(pinned and latest and version_tuple(latest) > version_tuple(pinned)),
        "rebuild_running": _rebuild_lock.locked(),
    })


@router.post("/client-version/update")
def client_version_update(request: Request, rebuild: str = Form(""), user: dict = Depends(require_provider)):
    from generate_installer import InstallerError, update_version
    try:
        result = update_version()
    except InstallerError as e:
        _set_flash(request, "error", f"Client update failed: {e}")
        return RedirectResponse("/client-version", status_code=303)
    conn = get_db()
    if result["updated"]:
        archs = ", ".join(a["arch"] for a in result["archs"])
        provider_log(conn, "client_version_updated", f"{result['current']} -> {result['latest']} ({archs})", user["email"])
    conn.close()
    version = result["latest"]
    msg = (f"Installers now use RustDesk {version}." if result["updated"]
           else f"Installers already use RustDesk {version}.")
    if rebuild == "1":
        threading.Thread(target=_rebuild_all, args=(version, user["email"]), daemon=True,
                         name="installer-rebuild-all").start()
        msg += " Rebuilding every tenant's outdated installers in the background."
    _set_flash(request, "success", msg)
    return RedirectResponse("/client-version", status_code=303)


# ── Provider settings ────────────────────────────────────────────────────────

_SETTING_FIELDS = ("smtp_host", "smtp_port", "smtp_tls", "smtp_user", "from_addr", "mem_threshold",
                   "disk_threshold", "letsencrypt_email")


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, user: dict = Depends(require_provider)):
    s = provider_config.get_all()
    view = {k: v for k, v in s.items() if k != "smtp_pass"}
    view["smtp_pass_set"] = bool(s.get("smtp_pass"))
    from app import settings as env
    return templates.TemplateResponse(request, "console/settings.html", {
        "current_user": user, "s": view, "admins": provider_config.provider_admin_emails(),
        "env": {"PROVIDER_DOMAIN": env.provider_domain(), "console": env.console_host(),
                "port_range": f"{env.relay_port_base()}–{env.relay_port_max()}"},
    })


def _clamp(v: str, default: int) -> int:
    try:
        return max(1, min(100, int(float(v))))
    except (TypeError, ValueError):
        return default


@router.post("/settings")
async def settings_save(request: Request, user: dict = Depends(require_provider)):
    form = await request.form()
    values = {k: (form.get(k) or "").strip() for k in _SETTING_FIELDS}
    values["smtp_tls"] = values["smtp_tls"] if values["smtp_tls"] in ("starttls", "ssl", "none") else "starttls"
    values["mem_threshold"] = str(_clamp(values["mem_threshold"], 90))
    values["disk_threshold"] = str(_clamp(values["disk_threshold"], 90))
    values["alerts_enabled"] = "1" if form.get("alerts_enabled") == "1" else "0"
    if (form.get("smtp_pass") or "").strip():
        values["smtp_pass"] = form.get("smtp_pass").strip()
    provider_config.set_many(values)
    conn = get_db()
    provider_log(conn, "provider_settings_updated", f"smtp={values['smtp_host']} alerts={values['alerts_enabled']}",
                 user["email"])
    conn.close()
    _set_flash(request, "success", "Settings saved.")
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/test-email")
async def settings_test_email(request: Request, user: dict = Depends(require_provider)):
    ok, err = mail.send([user["email"]], "RustDesk Fleet provider console — test email",
                        mail.simple_html("Test email", ["Provider email works."]), event="provider_test_email")
    _set_flash(request, "success" if ok else "error", f"Test email sent to {user['email']}." if ok else f"Test failed: {err}")
    return RedirectResponse("/settings", status_code=303)
