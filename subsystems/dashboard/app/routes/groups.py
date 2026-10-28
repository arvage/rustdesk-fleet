import re
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request

SLUG_RE = re.compile(r'^[a-z0-9][a-z0-9\-]{1,48}[a-z0-9]$')
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from app.auth import require_auth
from app.deps import (
    get_db, get_device_info, get_hbbs_peers, label_sort_key, log_event, merge_device_info,
)
from app.notifications import fire_notification
from app.permissions import require_perm, user_can
from app.routes.links import DEFAULT_EXPIRY, EXPIRY_OPTIONS, installers_dir, latest_installers, link_state
from app.tenancy import Tenant, get_tenant
from app.templates_config import templates

router = APIRouter()


def _group(conn, tenant_id: int, slug: str, cols: str = "*"):
    """One of this tenant's groups by slug, or None (another tenant's slug too)."""
    return conn.execute(
        f"SELECT {cols} FROM client_groups WHERE tenant_id = ? AND slug = ?", (tenant_id, slug)
    ).fetchone()


def _group_not_found(request: Request, slug: str = ""):
    _set_flash(request, "error", f"Group '{slug}' not found." if slug else "Group not found.")
    return RedirectResponse("/groups", status_code=303)


def _set_flash(request: Request, type_: str, msg: str) -> None:
    request.session["flash"] = {"type": type_, "msg": msg}


def _pinned_client_version() -> str:
    try:
        from generate_installer import get_pinned_version
        return get_pinned_version()
    except Exception:
        return ""


@router.get("/groups", response_class=HTMLResponse)
async def groups_list(request: Request, current_user: dict = Depends(require_auth)):
    conn = get_db()
    groups = conn.execute(
        """SELECT cg.id, cg.slug, cg.display_name, cg.status, cg.created_at,
                  COUNT(CASE WHEN d.hidden = 0 THEN d.id END) AS device_count,
                  GROUP_CONCAT(CASE WHEN d.hidden = 0 THEN d.rustdesk_id END) AS rustdesk_ids
           FROM client_groups cg
           LEFT JOIN devices d ON d.group_id = cg.id AND d.tenant_id = cg.tenant_id
           WHERE cg.tenant_id = ?
           GROUP BY cg.id ORDER BY cg.created_at""",
        (current_user["tenant_id"],),
    ).fetchall()
    conn.close()
    return templates.TemplateResponse(
        request, "groups.html", {"groups": groups, "current_user": current_user}
    )


@router.post("/groups")
async def groups_create(
    request: Request,
    slug: str = Form(...),
    display_name: str = Form(...),
    unattended_password: str = Form(""),
    current_user: dict = Depends(require_auth),
):
    require_perm(current_user, "manage_groups")
    from setup_server import create_group, ProvisioningError
    try:
        create_group(current_user["tenant_id"], slug, display_name, unattended_password.strip() or None,
                     current_user["email"])
        _set_flash(request, "success", f"Group '{slug}' created.")
        return RedirectResponse(f"/groups/{slug}", status_code=303)
    except ProvisioningError as e:
        _set_flash(request, "error", str(e))
        return RedirectResponse("/groups", status_code=303)


@router.get("/groups/{slug}", response_class=HTMLResponse)
async def group_detail(
    request: Request, slug: str, current_user: dict = Depends(require_auth),
    tenant: Tenant = Depends(get_tenant),
):
    tid = tenant.id
    conn = get_db()
    group = _group(conn, tid, slug)
    if group is None:
        conn.close()
        return _group_not_found(request, slug)

    fleet_devices = conn.execute(
        "SELECT * FROM devices WHERE tenant_id = ? AND group_id = ? AND hidden = 0",
        (tid, group["id"]),
    ).fetchall()

    all_groups = conn.execute(
        "SELECT id, display_name FROM client_groups WHERE tenant_id = ? ORDER BY display_name", (tid,)
    ).fetchall()

    installers = conn.execute(
        "SELECT * FROM installers WHERE tenant_id = ? AND group_id = ? ORDER BY created_at DESC",
        (tid, group["id"]),
    ).fetchall()

    link_rows = conn.execute(
        "SELECT * FROM download_links WHERE tenant_id = ? AND group_id = ? ORDER BY created_at DESC",
        (tid, group["id"]),
    ).fetchall()
    shareable = latest_installers(conn, tid, group["id"])
    au = conn.execute(
        """SELECT COUNT(*) AS reporting,
                  SUM(CASE WHEN di.policy_ts = ? THEN 1 ELSE 0 END) AS applied
           FROM devices d JOIN device_info di ON di.rustdesk_id = d.rustdesk_id AND di.tenant_id = d.tenant_id
           WHERE d.tenant_id = ? AND d.group_id = ? AND d.hidden = 0
             AND di.heartbeat_at > datetime('now', '-1 day')""",
        (group["policy_ts"] or -1, tid, group["id"]),
    ).fetchone()
    conn.close()

    base_url = str(request.base_url).rstrip("/")
    new_link_token = request.session.pop("new_link_token", None)
    links = [
        {**dict(r), "state": link_state(r), "url": f"{base_url}/d/{r['token']}",
         "is_new": r["token"] == new_link_token}
        for r in link_rows
    ]

    peers = get_hbbs_peers(tenant)
    info = get_device_info(tid)
    devices = []
    for d in fleet_devices:
        rid = d["rustdesk_id"]
        peer = peers.get(rid, {})
        devices.append(merge_device_info({
            **dict(d),
            "ip": peer.get("ip") or "—",
            "registered_at": (peer.get("registered_at") or "")[:10] or "—",
        }, info))
    devices.sort(key=label_sort_key)

    installer_rows = [
        {**dict(r), "filename": Path(r["signed_path"] or r["unsigned_path"]).name
                               if (r["signed_path"] or r["unsigned_path"]) else None}
        for r in installers
    ]
    try:
        import sign_installer
        signing_enabled = bool(sign_installer.get_config().get("enabled"))
    except Exception:
        signing_enabled = False
    try:
        import branding
        icon_png = branding.icon_paths(tid, group["id"])[1]
        brand_icon_v = int(icon_png.stat().st_mtime) if branding.has_icon(tid, group["id"]) else None
    except Exception:
        brand_icon_v = None

    return templates.TemplateResponse(
        request,
        "group_detail.html",
        {
            "group": group,
            "devices": devices,
            "all_groups": all_groups,
            "installers": installer_rows,
            "links": links,
            "auto_update": group["auto_update"] or "",
            "remote_config": group["remote_config"] or "",
            "policy_reporting": au["reporting"] or 0,
            "policy_applied": au["applied"] or 0,
            "shareable_platforms": [i["label"] for i in shareable],
            "shareable": shareable,
            "pinned_version": _pinned_client_version(),
            "expiry_options": EXPIRY_OPTIONS,
            "default_expiry": DEFAULT_EXPIRY,
            "current_user": current_user,
            "has_password": bool(group["unattended_password"]),
            # Only managers get the password itself (copy badge, edit form).
            "unattended_password": (group["unattended_password"] or "")
                                   if user_can(current_user, "manage_groups") else "",
            "signing_enabled": signing_enabled,
            "brand_app_name": group["brand_app_name"] or "",
            "brand_icon_v": brand_icon_v,
        },
    )


@router.post("/groups/{slug}/edit")
async def group_edit(
    request: Request,
    slug: str,
    new_slug: str = Form(...),
    display_name: str = Form(...),
    unattended_password: str = Form(""),
    clear_password: str = Form(""),
    current_user: dict = Depends(require_auth),
):
    require_perm(current_user, "manage_groups")
    tid = current_user["tenant_id"]
    new_slug = new_slug.strip().lower()
    display_name = display_name.strip()
    unattended_password = unattended_password.strip()

    conn = get_db()
    group = _group(conn, tid, slug, "id, unattended_password")
    conn.close()
    if group is None:
        return _group_not_found(request)

    if not SLUG_RE.match(new_slug):
        _set_flash(request, "error", "Invalid slug — lowercase letters, digits, hyphens, 3–50 chars.")
        return RedirectResponse(f"/groups/{slug}", status_code=303)
    if not display_name:
        _set_flash(request, "error", "Display name cannot be empty.")
        return RedirectResponse(f"/groups/{slug}", status_code=303)
    if clear_password != "1":
        from setup_server import password_problem
        problem = password_problem(unattended_password)
        if problem:
            _set_flash(request, "error", problem)
            return RedirectResponse(f"/groups/{slug}", status_code=303)

    conn = get_db()
    if new_slug != slug:
        conflict = conn.execute(
            "SELECT id FROM client_groups WHERE tenant_id = ? AND slug = ? AND id != ?",
            (tid, new_slug, group["id"])
        ).fetchone()
        if conflict:
            conn.close()
            _set_flash(request, "error", f"Slug '{new_slug}' is already taken.")
            return RedirectResponse(f"/groups/{slug}", status_code=303)

    if clear_password == "1":
        new_pw = None
    elif unattended_password:
        new_pw = unattended_password
    else:
        new_pw = group["unattended_password"]

    conn.execute(
        "UPDATE client_groups SET slug = ?, display_name = ?, unattended_password = ? WHERE id = ? AND tenant_id = ?",
        (new_slug, display_name, new_pw, group["id"], tid),
    )
    log_event(conn, "group_updated", new_slug, current_user["email"], tenant_id=tid)
    conn.commit()
    conn.close()
    _set_flash(request, "success", "Group updated.")
    return RedirectResponse(f"/groups/{new_slug}", status_code=303)


@router.post("/groups/{slug}/delete")
async def group_delete(
    request: Request,
    slug: str,
    current_user: dict = Depends(require_auth),
):
    """Delete an empty group with its installers, download links, access
    grants and icon. Devices have to be moved out first; hidden (deleted)
    devices just lose the group."""
    require_perm(current_user, "manage_groups")
    tid = current_user["tenant_id"]
    conn = get_db()
    group = _group(conn, tid, slug, "id, display_name")
    if group is None:
        conn.close()
        return _group_not_found(request, slug)
    gid = group["id"]

    in_use = conn.execute(
        "SELECT COUNT(*) FROM devices WHERE tenant_id = ? AND group_id = ? AND hidden = 0", (tid, gid)
    ).fetchone()[0]
    if in_use:
        conn.close()
        _set_flash(request, "error",
                   f"Move this group's {in_use} device{'s' if in_use != 1 else ''} to another group first.")
        return RedirectResponse(f"/groups/{slug}", status_code=303)

    files = [r["unsigned_path"] for r in conn.execute(
        "SELECT unsigned_path FROM installers WHERE tenant_id = ? AND group_id = ? AND unsigned_path IS NOT NULL",
        (tid, gid))]
    conn.execute("UPDATE devices SET group_id = NULL WHERE tenant_id = ? AND group_id = ?", (tid, gid))
    for table in ("installers", "download_links", "user_group_access"):
        conn.execute(f"DELETE FROM {table} WHERE tenant_id = ? AND group_id = ?", (tid, gid))
    conn.execute("DELETE FROM client_groups WHERE tenant_id = ? AND id = ?", (tid, gid))
    log_event(conn, "group_deleted", slug, current_user["email"], tenant_id=tid)
    conn.commit()
    conn.close()

    allowed_dir = installers_dir(tid).resolve()
    for f in files:
        candidate = Path(f).resolve()
        if candidate.parent == allowed_dir and candidate.exists():
            candidate.unlink()
    import branding
    branding.delete_icon(tid, gid)

    _set_flash(request, "success", f"Group '{group['display_name']}' deleted.")
    return RedirectResponse("/groups", status_code=303)


# Managed client settings: form field / client_groups column -> label.
_CLIENT_SETTINGS = {
    "auto_update": "automatic updates",
    "remote_config": "remote settings changes",
}


@router.post("/groups/{slug}/client-settings")
async def group_client_settings(
    request: Request,
    slug: str,
    auto_update: str = Form(""),
    remote_config: str = Form(""),
    current_user: dict = Depends(require_auth),
):
    require_perm(current_user, "manage_groups")
    tid = current_user["tenant_id"]
    conn = get_db()
    group = _group(conn, tid, slug, "id, auto_update, remote_config, policy_ts")
    if group is None:
        conn.close()
        return _group_not_found(request)
    wanted = {"auto_update": auto_update, "remote_config": remote_config}
    if any(v not in ("", "Y", "N") for v in wanted.values()):
        conn.close()
        _set_flash(request, "error", "Unknown client setting.")
        return RedirectResponse(f"/groups/{slug}#client-settings", status_code=303)

    changed = [col for col, v in wanted.items() if (group[col] or "") != v]
    if changed:
        # A new policy timestamp makes every reporting device in the group
        # pick the change up on its next heartbeat (see routes/client_api.py).
        # Always strictly newer than the previous timestamp: two saves in the
        # same second must still look like a change to devices.
        managed = any(wanted.values())
        new_ts = max(int(time.time()), (group["policy_ts"] or 0) + 1) if managed else None
        conn.execute(
            """UPDATE client_groups SET auto_update = ?, remote_config = ?, policy_ts = ?
               WHERE id = ? AND tenant_id = ?""",
            (auto_update or None, remote_config or None, new_ts, group["id"], tid),
        )
        names = {"": "not managed", "Y": "on", "N": "off"}
        detail = " ".join(f"{col}={names[wanted[col]]}" for col in changed)
        log_event(conn, "group_client_settings_changed", f"group={slug} {detail}", current_user["email"],
                  tenant_id=tid)
        conn.close()
        summary = "; ".join(
            f"{_CLIENT_SETTINGS[col]} {names[wanted[col]]}" for col in changed
        )
        _set_flash(request, "success",
                   f"Client settings saved ({summary}). Reporting devices pick changes up within a few seconds.")
    else:
        conn.close()
        _set_flash(request, "success", "No changes.")
    return RedirectResponse(f"/groups/{slug}#client-settings", status_code=303)


@router.post("/groups/{slug}/branding")
async def group_branding(request: Request, slug: str, current_user: dict = Depends(require_auth)):
    require_perm(current_user, "manage_groups")
    import branding
    tid = current_user["tenant_id"]
    conn = get_db()
    group = _group(conn, tid, slug, "id")
    if group is None:
        conn.close()
        return _group_not_found(request)
    form = await request.form()
    app_name = (form.get("brand_app_name") or "").strip()
    icon = form.get("icon")
    remove_icon = form.get("remove_icon") == "1"

    problem = branding.app_name_problem(app_name)
    if problem:
        conn.close()
        _set_flash(request, "error", problem)
        return RedirectResponse(f"/groups/{slug}#branding", status_code=303)

    changes = []
    if hasattr(icon, "read") and icon.filename:
        try:
            branding.save_icon(tid, group["id"], await icon.read(branding.MAX_UPLOAD_BYTES + 1))
        except branding.BrandingError as e:
            conn.close()
            _set_flash(request, "error", str(e))
            return RedirectResponse(f"/groups/{slug}#branding", status_code=303)
        changes.append("icon updated")
    elif remove_icon:
        branding.delete_icon(tid, group["id"])
        changes.append("icon removed")

    conn.execute("UPDATE client_groups SET brand_app_name = ? WHERE id = ? AND tenant_id = ?",
                 (app_name or None, group["id"], tid))
    changes.append(f"app name={app_name or 'default'}")
    log_event(conn, "group_branding_updated", f"group={slug} " + ", ".join(changes), current_user["email"],
              tenant_id=tid)
    conn.commit()
    conn.close()
    _set_flash(request, "success", "Branding saved. Build new installers to apply it.")
    return RedirectResponse(f"/groups/{slug}#branding", status_code=303)


@router.get("/groups/{slug}/branding/icon.png")
async def group_branding_icon(request: Request, slug: str, current_user: dict = Depends(require_auth)):
    import branding
    tid = current_user["tenant_id"]
    conn = get_db()
    group = _group(conn, tid, slug, "id")
    conn.close()
    if group is None or not branding.has_icon(tid, group["id"]):
        return HTMLResponse("Not found", status_code=404)
    return FileResponse(branding.icon_paths(tid, group["id"])[1], media_type="image/png")


@router.post("/groups/{slug}/installers/{installer_id}/delete")
async def installer_delete(
    request: Request,
    slug: str,
    installer_id: int,
    current_user: dict = Depends(require_auth),
):
    require_perm(current_user, "manage_groups")
    tid = current_user["tenant_id"]
    conn = get_db()
    row = conn.execute(
        """SELECT i.id, i.unsigned_path, i.platform, i.rustdesk_version, cg.display_name AS group_name
           FROM installers i
           JOIN client_groups cg ON cg.id = i.group_id AND cg.tenant_id = i.tenant_id
           WHERE i.id = ? AND i.tenant_id = ? AND cg.slug = ?""",
        (installer_id, tid, slug),
    ).fetchone()
    if row is None:
        conn.close()
        _set_flash(request, "error", "Installer not found.")
        return RedirectResponse(f"/groups/{slug}", status_code=303)

    if row["unsigned_path"]:
        candidate = Path(row["unsigned_path"]).resolve()
        if candidate.parent == installers_dir(tid).resolve() and candidate.exists():
            candidate.unlink()

    conn.execute("DELETE FROM installers WHERE id = ? AND tenant_id = ?", (installer_id, tid))
    log_event(conn, "installer_deleted", f"group={slug} installer_id={installer_id}", current_user["email"],
              tenant_id=tid)
    conn.commit()
    conn.close()

    fire_notification(tid, "installer_deleted", {
        "group_name": row["group_name"],
        "group_slug": slug,
        "platform": row["platform"],
        "version": row["rustdesk_version"],
        "deleted_by": current_user["email"],
    })

    _set_flash(request, "success", "Installer deleted.")
    return RedirectResponse(f"/groups/{slug}", status_code=303)


@router.post("/groups/{slug}/build")
async def group_build(
    request: Request,
    slug: str,
    platform: str = Form("windows-x64"),
    current_user: dict = Depends(require_auth),
):
    require_perm(current_user, "manage_groups")
    from generate_installer import build_installer, InstallerError, PLATFORMS
    tid = current_user["tenant_id"]
    conn = get_db()
    grp = _group(conn, tid, slug, "display_name")
    conn.close()
    if grp is None:
        return _group_not_found(request)
    if platform not in PLATFORMS:
        _set_flash(request, "error", f"Unknown platform: {platform}")
        return RedirectResponse(f"/groups/{slug}", status_code=303)
    try:
        result = build_installer(tid, slug, platform, current_user["email"])
        label = PLATFORMS[platform]["label"]
        if result["status"] == "signed":
            sha_short = (result["sha256_signed"] or "")[:16]
            _set_flash(request, "success", f"{label} installer built and signed. SHA256: {sha_short}…")
        else:
            sha_short = (result["sha256_unsigned"] or "")[:16]
            _set_flash(request, "success", f"{label} installer ready (unsigned). SHA256: {sha_short}…")
        conn = get_db()
        log_event(conn, "installer_built", f"group={slug} platform={platform}", current_user["email"], tenant_id=tid)
        conn.close()
        fire_notification(tid, "installer_built", {
            "group_name": grp["display_name"],
            "group_slug": slug,
            "platform": label,
            "version": result.get("rustdesk_version", ""),
            "sha256": result.get("sha256_unsigned", ""),
        })
    except InstallerError as e:
        _set_flash(request, "error", f"Build failed: {e}")
    return RedirectResponse(f"/groups/{slug}", status_code=303)


@router.post("/groups/{slug}/installers/{installer_id}/sign")
async def group_sign_installer(
    request: Request, slug: str, installer_id: int,
    current_user: dict = Depends(require_auth),
):
    require_perm(current_user, "manage_groups")
    from generate_installer import sign_existing, InstallerError
    try:
        row = sign_existing(current_user["tenant_id"], slug, installer_id, current_user["email"])
        _set_flash(request, "success", f"Installer signed. SHA256: {(row['sha256_signed'] or '')[:16]}…")
    except InstallerError as e:
        _set_flash(request, "error", f"Signing failed: {e}")
    return RedirectResponse(f"/groups/{slug}", status_code=303)


@router.get("/download/{filename}")
async def download(
    request: Request, filename: str, current_user: dict = Depends(require_auth)
):
    tid = current_user["tenant_id"]
    out_dir = installers_dir(tid)
    candidate = (out_dir / filename).resolve()
    if candidate.parent != out_dir.resolve():
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Invalid filename.")

    conn = get_db()
    row = conn.execute(
        "SELECT id FROM installers WHERE tenant_id = ? AND (unsigned_path = ? OR signed_path = ?) "
        "AND status IN ('built','signed')",
        (tid, str(candidate), str(candidate)),
    ).fetchone()
    conn.close()
    if row is None or not candidate.exists():
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Installer not found.")

    return FileResponse(
        path=str(candidate),
        filename=filename,
        media_type="application/octet-stream",
    )
