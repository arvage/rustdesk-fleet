import re
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request

SLUG_RE = re.compile(r'^[a-z0-9][a-z0-9\-]{1,48}[a-z0-9]$')
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from app.auth import require_auth
from app.deps import get_db, get_device_info, get_hbbs_peers, log_event, merge_device_info
from app.notifications import fire_notification
from app.permissions import require_perm
from app.routes.links import DEFAULT_EXPIRY, EXPIRY_OPTIONS, latest_installers, link_state
from app.templates_config import templates

OUTPUT_DIR = Path("/opt/rustdesk-fleet/installers")

router = APIRouter()


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
           LEFT JOIN devices d ON d.group_id = cg.id
           GROUP BY cg.id ORDER BY cg.created_at"""
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
        create_group(slug, display_name, unattended_password.strip() or None, current_user["email"])
        conn = get_db()
        log_event(conn, "group_created", slug, current_user["email"])
        conn.close()
        _set_flash(request, "success", f"Group '{slug}' created.")
        return RedirectResponse(f"/groups/{slug}", status_code=303)
    except ProvisioningError as e:
        _set_flash(request, "error", str(e))
        return RedirectResponse("/groups", status_code=303)


@router.get("/groups/{slug}", response_class=HTMLResponse)
async def group_detail(
    request: Request, slug: str, current_user: dict = Depends(require_auth)
):
    conn = get_db()
    group = conn.execute(
        "SELECT * FROM client_groups WHERE slug = ?", (slug,)
    ).fetchone()
    if group is None:
        conn.close()
        _set_flash(request, "error", f"Group '{slug}' not found.")
        return RedirectResponse("/groups", status_code=303)

    fleet_devices = conn.execute(
        "SELECT * FROM devices WHERE group_id = ? AND hidden = 0 ORDER BY last_seen DESC",
        (group["id"],),
    ).fetchall()

    installers = conn.execute(
        "SELECT * FROM installers WHERE group_id = ? ORDER BY created_at DESC",
        (group["id"],),
    ).fetchall()

    link_rows = conn.execute(
        "SELECT * FROM download_links WHERE group_id = ? ORDER BY created_at DESC",
        (group["id"],),
    ).fetchall()
    shareable = latest_installers(conn, group["id"])
    au = conn.execute(
        """SELECT COUNT(*) AS reporting,
                  SUM(CASE WHEN di.policy_ts = ? THEN 1 ELSE 0 END) AS applied
           FROM devices d JOIN device_info di ON di.rustdesk_id = d.rustdesk_id
           WHERE d.group_id = ? AND d.hidden = 0
             AND di.heartbeat_at > datetime('now', '-1 day')""",
        (group["policy_ts"] or -1, group["id"]),
    ).fetchone()
    conn.close()

    base_url = str(request.base_url).rstrip("/")
    new_link_token = request.session.pop("new_link_token", None)
    links = [
        {**dict(r), "state": link_state(r), "url": f"{base_url}/d/{r['token']}",
         "is_new": r["token"] == new_link_token}
        for r in link_rows
    ]

    peers = get_hbbs_peers()
    info = get_device_info()
    devices = []
    for d in fleet_devices:
        rid = d["rustdesk_id"]
        peer = peers.get(rid, {})
        devices.append(merge_device_info({
            **dict(d),
            "ip": peer.get("ip") or "—",
            "registered_at": (peer.get("registered_at") or "")[:10] or "—",
        }, info))

    installer_rows = [
        {**dict(r), "filename": Path(r["unsigned_path"]).name if r["unsigned_path"] else None}
        for r in installers
    ]

    return templates.TemplateResponse(
        request,
        "group_detail.html",
        {
            "group": group,
            "devices": devices,
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
            "unattended_password": group["unattended_password"] or "",
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
    new_slug = new_slug.strip().lower()
    display_name = display_name.strip()
    unattended_password = unattended_password.strip()

    if not SLUG_RE.match(new_slug):
        _set_flash(request, "error", "Invalid slug — lowercase letters, digits, hyphens, 3–50 chars.")
        return RedirectResponse(f"/groups/{slug}", status_code=303)
    if not display_name:
        _set_flash(request, "error", "Display name cannot be empty.")
        return RedirectResponse(f"/groups/{slug}", status_code=303)

    conn = get_db()
    group = conn.execute("SELECT id FROM client_groups WHERE slug = ?", (slug,)).fetchone()
    if group is None:
        conn.close()
        _set_flash(request, "error", "Group not found.")
        return RedirectResponse("/groups", status_code=303)

    if new_slug != slug:
        conflict = conn.execute(
            "SELECT id FROM client_groups WHERE slug = ? AND id != ?", (new_slug, group["id"])
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
        new_pw = conn.execute(
            "SELECT unattended_password FROM client_groups WHERE id = ?", (group["id"],)
        ).fetchone()["unattended_password"]

    conn.execute(
        "UPDATE client_groups SET slug = ?, display_name = ?, unattended_password = ? WHERE id = ?",
        (new_slug, display_name, new_pw, group["id"]),
    )
    log_event(conn, "group_updated", new_slug, current_user["email"])
    conn.commit()
    conn.close()
    _set_flash(request, "success", "Group updated.")
    return RedirectResponse(f"/groups/{new_slug}", status_code=303)


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
    wanted = {"auto_update": auto_update, "remote_config": remote_config}
    if any(v not in ("", "Y", "N") for v in wanted.values()):
        _set_flash(request, "error", "Unknown client setting.")
        return RedirectResponse(f"/groups/{slug}#client-settings", status_code=303)

    conn = get_db()
    group = conn.execute(
        "SELECT id, auto_update, remote_config, policy_ts FROM client_groups WHERE slug = ?", (slug,)
    ).fetchone()
    if group is None:
        conn.close()
        _set_flash(request, "error", "Group not found.")
        return RedirectResponse("/groups", status_code=303)

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
               WHERE id = ?""",
            (auto_update or None, remote_config or None, new_ts, group["id"]),
        )
        names = {"": "not managed", "Y": "on", "N": "off"}
        detail = " ".join(f"{col}={names[wanted[col]]}" for col in changed)
        log_event(conn, "group_client_settings_changed", f"group={slug} {detail}", current_user["email"])
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


@router.post("/groups/{slug}/installers/{installer_id}/delete")
async def installer_delete(
    request: Request,
    slug: str,
    installer_id: int,
    current_user: dict = Depends(require_auth),
):
    require_perm(current_user, "manage_groups")
    conn = get_db()
    row = conn.execute(
        """SELECT i.id, i.unsigned_path, i.platform, i.rustdesk_version, cg.display_name AS group_name
           FROM installers i
           JOIN client_groups cg ON cg.id = i.group_id
           WHERE i.id = ? AND cg.slug = ?""",
        (installer_id, slug),
    ).fetchone()
    if row is None:
        conn.close()
        _set_flash(request, "error", "Installer not found.")
        return RedirectResponse(f"/groups/{slug}", status_code=303)

    if row["unsigned_path"]:
        candidate = Path(row["unsigned_path"]).resolve()
        if candidate.parent == OUTPUT_DIR.resolve() and candidate.exists():
            candidate.unlink()

    conn.execute("DELETE FROM installers WHERE id = ?", (installer_id,))
    log_event(conn, "installer_deleted", f"group={slug} installer_id={installer_id}", current_user["email"])
    conn.commit()
    conn.close()

    fire_notification("installer_deleted", {
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
    if platform not in PLATFORMS:
        _set_flash(request, "error", f"Unknown platform: {platform}")
        return RedirectResponse(f"/groups/{slug}", status_code=303)
    try:
        result = build_installer(slug, platform, current_user["email"])
        sha_short = (result["sha256_unsigned"] or "")[:16]
        label = PLATFORMS[platform]["label"]
        _set_flash(request, "success", f"{label} installer ready. SHA256: {sha_short}…")
        conn = get_db()
        grp = conn.execute("SELECT display_name FROM client_groups WHERE slug=?", (slug,)).fetchone()
        log_event(conn, "installer_built", f"group={slug} platform={platform}", current_user["email"])
        conn.close()
        fire_notification("installer_built", {
            "group_name": grp["display_name"] if grp else slug,
            "group_slug": slug,
            "platform": label,
            "version": result.get("rustdesk_version", ""),
            "sha256": result.get("sha256_unsigned", ""),
        })
    except InstallerError as e:
        _set_flash(request, "error", f"Build failed: {e}")
    return RedirectResponse(f"/groups/{slug}", status_code=303)


@router.get("/download/{filename}")
async def download(
    request: Request, filename: str, current_user: dict = Depends(require_auth)
):
    candidate = (OUTPUT_DIR / filename).resolve()
    if candidate.parent != OUTPUT_DIR.resolve():
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Invalid filename.")

    conn = get_db()
    row = conn.execute(
        "SELECT id FROM installers WHERE unsigned_path = ? AND status = 'built'",
        (str(candidate),),
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
