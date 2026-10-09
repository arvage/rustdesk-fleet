"""
Shareable installer download links.

Staff create a link per client group and send it to the client, who can then
download and run the group's installer themselves — no login, and no need to
remote in first with another tool.

A link always serves the group's *latest* built installer for each platform,
so rebuilding an installer doesn't invalidate links already sent out. Links
can expire, be capped to N downloads, and be revoked at any time.
"""

import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from app.auth import require_auth
from app.deps import get_db, log_event
from app.notifications import fire_notification, send_download_link_email
from app.permissions import require_perm
from app.templates_config import _localtime, templates

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

OUTPUT_DIR = Path("/opt/rustdesk-fleet/installers")

# Expiry choices offered in the UI → hours (None = never expires)
EXPIRY_OPTIONS: dict[str, int | None] = {
    "24h": 24,
    "3d": 72,
    "7d": 168,
    "30d": 720,
    "never": None,
}
DEFAULT_EXPIRY = "7d"

PLATFORM_INFO: dict[str, dict] = {
    "windows-x64":   {"label": "Windows",         "os": "windows", "order": 0},
    "windows-arm64": {"label": "Windows (ARM)",   "os": "windows-arm", "order": 1},
    "macos":         {"label": "macOS",           "os": "mac",     "order": 2},
    "linux":         {"label": "Linux",           "os": "linux",   "order": 3},
}

# Public pages: keep them out of search engines and don't leak the token
# to third parties via the Referer header.
_PUBLIC_HEADERS = {
    "X-Robots-Tag": "noindex, nofollow",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}

router = APIRouter()


def _set_flash(request: Request, type_: str, msg: str) -> None:
    request.session["flash"] = {"type": type_, "msg": msg}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def link_state(link) -> str:
    """'active' | 'expired' | 'used_up' | 'revoked' for a download_links row."""
    if link["revoked"]:
        return "revoked"
    if link["expires_at"] and link["expires_at"] <= _fmt(_now_utc()):
        return "expired"
    if link["max_uses"] is not None and link["use_count"] >= link["max_uses"]:
        return "used_up"
    return "active"


def latest_installers(conn, group_id: int) -> list[dict]:
    """Latest successfully built installer per platform for a group."""
    rows = conn.execute(
        """SELECT * FROM installers
           WHERE id IN (
               SELECT MAX(id) FROM installers
               WHERE group_id = ? AND status IN ('built', 'signed')
               GROUP BY platform
           )""",
        (group_id,),
    ).fetchall()
    result = []
    for r in rows:
        path = _installer_file(r)
        if path is None:
            continue
        info = PLATFORM_INFO.get(r["platform"], {"label": r["platform"], "os": "", "order": 9})
        result.append({**dict(r), **info, "filename": path.name, "size_mb": round(path.stat().st_size / 1_048_576, 1)})
    result.sort(key=lambda r: r["order"])
    return result


def _installer_file(row) -> Path | None:
    """Resolve the file to serve for an installer row (signed preferred), or None."""
    for col in ("signed_path", "unsigned_path"):
        if row[col]:
            candidate = Path(row[col]).resolve()
            if candidate.parent == OUTPUT_DIR.resolve() and candidate.exists():
                return candidate
    return None


def _email_link(request: Request, conn, link_id: int, to_email: str, recipient_name: str,
                message: str, current_user: dict) -> tuple[bool, str]:
    """Email an active link to a client and record who it went to."""
    link = conn.execute(
        """SELECT dl.*, cg.display_name AS group_name, cg.slug AS group_slug
           FROM download_links dl JOIN client_groups cg ON cg.id = dl.group_id
           WHERE dl.id = ?""",
        (link_id,),
    ).fetchone()
    if link is None or link_state(link) != "active":
        return False, "That link is no longer active."

    url = f"{str(request.base_url).rstrip('/')}/d/{link['token']}"
    ok, err = send_download_link_email(
        to_email=to_email,
        recipient_name=recipient_name,
        group_name=link["group_name"],
        url=url,
        expires_at_local=_localtime(link["expires_at"], "%b %-d, %Y at %-I:%M %p") if link["expires_at"] else "",
        message=message,
        sender_name=current_user.get("name") or "",
        reply_to=current_user.get("email") or "",
    )
    if ok:
        conn.execute(
            "UPDATE download_links SET emailed_to = ?, emailed_at = datetime('now') WHERE id = ?",
            (to_email, link_id),
        )
        log_event(conn, "download_link_emailed",
                  f"group={link['group_slug']} link_id={link_id} to={to_email}", current_user["email"])
    return ok, err


# ── Staff: create / revoke / email ───────────────────────────────────────────

@router.post("/groups/{slug}/links")
async def link_create(
    request: Request,
    slug: str,
    expiry: str = Form(DEFAULT_EXPIRY),
    max_uses: str = Form(""),
    note: str = Form(""),
    email_to: str = Form(""),
    recipient_name: str = Form(""),
    message: str = Form(""),
    current_user: dict = Depends(require_auth),
):
    require_perm(current_user, "manage_groups")
    email_to = email_to.strip()
    if email_to and not EMAIL_RE.match(email_to):
        _set_flash(request, "error", f"'{email_to}' doesn't look like a valid email address.")
        return RedirectResponse(f"/groups/{slug}#download-links", status_code=303)
    if expiry not in EXPIRY_OPTIONS:
        expiry = DEFAULT_EXPIRY
    hours = EXPIRY_OPTIONS[expiry]
    expires_at = _fmt(_now_utc() + timedelta(hours=hours)) if hours else None

    uses = None
    if max_uses.strip():
        try:
            uses = int(max_uses)
        except ValueError:
            uses = 0
        if uses < 1:
            _set_flash(request, "error", "Download limit must be a positive number (or blank for unlimited).")
            return RedirectResponse(f"/groups/{slug}#download-links", status_code=303)

    conn = get_db()
    group = conn.execute("SELECT id FROM client_groups WHERE slug = ?", (slug,)).fetchone()
    if group is None:
        conn.close()
        _set_flash(request, "error", "Group not found.")
        return RedirectResponse("/groups", status_code=303)

    token = secrets.token_urlsafe(24)
    cur = conn.execute(
        """INSERT INTO download_links (token, group_id, note, expires_at, max_uses, created_by)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (token, group["id"], note.strip() or None, expires_at, uses, current_user["email"]),
    )
    link_id = cur.lastrowid
    log_event(conn, "download_link_created", f"group={slug} expiry={expiry} max_uses={uses or '∞'}", current_user["email"])

    request.session["new_link_token"] = token
    if email_to:
        ok, err = _email_link(request, conn, link_id, email_to, recipient_name.strip(), message.strip(), current_user)
        if ok:
            _set_flash(request, "success", f"Download link created and emailed to {email_to}.")
        else:
            _set_flash(request, "error", f"Link created, but the email failed: {err}")
    else:
        _set_flash(request, "success", "Download link created — copy it below and send it to your client.")
    conn.close()
    return RedirectResponse(f"/groups/{slug}#download-links", status_code=303)


@router.post("/groups/{slug}/links/{link_id}/email")
async def link_email(
    request: Request,
    slug: str,
    link_id: int,
    email_to: str = Form(...),
    recipient_name: str = Form(""),
    message: str = Form(""),
    current_user: dict = Depends(require_auth),
):
    require_perm(current_user, "manage_groups")
    email_to = email_to.strip()
    if not EMAIL_RE.match(email_to):
        _set_flash(request, "error", f"'{email_to}' doesn't look like a valid email address.")
        return RedirectResponse(f"/groups/{slug}#download-links", status_code=303)

    conn = get_db()
    owned = conn.execute(
        """SELECT 1 FROM download_links
           WHERE id = ? AND group_id = (SELECT id FROM client_groups WHERE slug = ?)""",
        (link_id, slug),
    ).fetchone()
    if owned is None:
        conn.close()
        _set_flash(request, "error", "Link not found.")
        return RedirectResponse(f"/groups/{slug}#download-links", status_code=303)

    ok, err = _email_link(request, conn, link_id, email_to, recipient_name.strip(), message.strip(), current_user)
    conn.close()
    if ok:
        _set_flash(request, "success", f"Download link emailed to {email_to}.")
    else:
        _set_flash(request, "error", f"Email failed: {err}")
    return RedirectResponse(f"/groups/{slug}#download-links", status_code=303)


@router.post("/groups/{slug}/links/{link_id}/revoke")
async def link_revoke(
    request: Request,
    slug: str,
    link_id: int,
    current_user: dict = Depends(require_auth),
):
    require_perm(current_user, "manage_groups")
    conn = get_db()
    cur = conn.execute(
        """UPDATE download_links SET revoked = 1
           WHERE id = ? AND group_id = (SELECT id FROM client_groups WHERE slug = ?)""",
        (link_id, slug),
    )
    if cur.rowcount:
        log_event(conn, "download_link_revoked", f"group={slug} link_id={link_id}", current_user["email"])
        _set_flash(request, "success", "Link revoked — it can no longer be used.")
    else:
        conn.close()
        _set_flash(request, "error", "Link not found.")
        return RedirectResponse(f"/groups/{slug}#download-links", status_code=303)
    conn.close()
    return RedirectResponse(f"/groups/{slug}#download-links", status_code=303)


# ── Public: client-facing download page (no login) ───────────────────────────

def _load_public_link(conn, token: str):
    return conn.execute(
        """SELECT dl.*, cg.display_name AS group_name, cg.slug AS group_slug, cg.status AS group_status
           FROM download_links dl
           JOIN client_groups cg ON cg.id = dl.group_id
           WHERE dl.token = ?""",
        (token,),
    ).fetchone()


def _unavailable(request: Request, reason: str) -> HTMLResponse:
    resp = templates.TemplateResponse(
        request, "client_download.html",
        {"unavailable": reason, "installers": [], "group_name": "", "token": ""},
        status_code=410 if reason != "not_found" else 404,
    )
    resp.headers.update(_PUBLIC_HEADERS)
    return resp


@router.get("/d/{token}", response_class=HTMLResponse)
async def public_download_page(request: Request, token: str):
    conn = get_db()
    link = _load_public_link(conn, token)
    if link is None:
        conn.close()
        return _unavailable(request, "not_found")
    state = link_state(link)
    if state != "active" or link["group_status"] != "active":
        conn.close()
        return _unavailable(request, state if state != "active" else "revoked")

    installers = latest_installers(conn, link["group_id"])
    conn.close()

    resp = templates.TemplateResponse(
        request,
        "client_download.html",
        {
            "unavailable": None,
            "group_name": link["group_name"],
            "installers": installers,
            "token": token,
            "expires_at": link["expires_at"],
        },
    )
    resp.headers.update(_PUBLIC_HEADERS)
    return resp


@router.get("/d/{token}/{platform}")
async def public_download_file(request: Request, token: str, platform: str):
    conn = get_db()
    link = _load_public_link(conn, token)
    if link is None or link["group_status"] != "active":
        conn.close()
        return _unavailable(request, "not_found" if link is None else "revoked")

    installer = next((i for i in latest_installers(conn, link["group_id"]) if i["platform"] == platform), None)
    if installer is None:
        conn.close()
        return RedirectResponse(f"/d/{token}", status_code=303)

    # Count the download atomically, re-checking every limit in the same
    # statement so two clicks can't both slip past a max_uses cap.
    cur = conn.execute(
        """UPDATE download_links
           SET use_count = use_count + 1, last_used_at = datetime('now')
           WHERE id = ? AND revoked = 0
             AND (expires_at IS NULL OR expires_at > datetime('now'))
             AND (max_uses IS NULL OR use_count < max_uses)""",
        (link["id"],),
    )
    conn.commit()
    if not cur.rowcount:
        conn.close()
        return _unavailable(request, link_state(link) if link_state(link) != "active" else "used_up")

    client_ip = request.headers.get("x-real-ip") or (request.client.host if request.client else "")
    log_event(
        conn, "installer_downloaded",
        f"group={link['group_slug']} platform={platform} link_id={link['id']} ip={client_ip}", "",
    )
    conn.close()

    fire_notification("installer_downloaded", {
        "group_name": link["group_name"],
        "platform": installer["label"],
        "version": installer["rustdesk_version"],
        "ip": client_ip or "—",
        "note": link["note"] or "",
        "downloads": f"{link['use_count'] + 1}" + (f" of {link['max_uses']}" if link["max_uses"] else ""),
    })

    resp = FileResponse(
        path=str(OUTPUT_DIR / installer["filename"]),
        filename=installer["filename"],
        media_type="application/octet-stream",
    )
    resp.headers.update(_PUBLIC_HEADERS)
    return resp
