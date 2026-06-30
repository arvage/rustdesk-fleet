import re
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request

SLUG_RE = re.compile(r'^[a-z0-9][a-z0-9\-]{1,48}[a-z0-9]$')
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from app.auth import require_auth
from app.deps import get_db
from app.templates_config import templates

OUTPUT_DIR = Path("/opt/rustdesk-fleet/installers")

router = APIRouter()


def _set_flash(request: Request, type_: str, msg: str) -> None:
    request.session["flash"] = {"type": type_, "msg": msg}


@router.get("/groups", response_class=HTMLResponse)
async def groups_list(request: Request, current_user: dict = Depends(require_auth)):
    conn = get_db()
    groups = conn.execute(
        """SELECT cg.id, cg.slug, cg.display_name, cg.status, cg.created_at,
                  COUNT(d.id) AS device_count
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
    current_user: dict = Depends(require_auth),
):
    from setup_server import create_group, ProvisioningError
    try:
        create_group(slug, display_name)
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

    devices = conn.execute(
        "SELECT * FROM devices WHERE group_id = ? ORDER BY last_seen DESC", (group["id"],)
    ).fetchall()

    installers = conn.execute(
        "SELECT * FROM installers WHERE group_id = ? ORDER BY created_at DESC",
        (group["id"],),
    ).fetchall()
    conn.close()

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
            "current_user": current_user,
        },
    )


@router.post("/groups/{slug}/edit")
async def group_edit(
    request: Request,
    slug: str,
    new_slug: str = Form(...),
    display_name: str = Form(...),
    current_user: dict = Depends(require_auth),
):
    new_slug = new_slug.strip().lower()
    display_name = display_name.strip()

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

    conn.execute(
        "UPDATE client_groups SET slug = ?, display_name = ? WHERE id = ?",
        (new_slug, display_name, group["id"]),
    )
    conn.commit()
    conn.close()
    _set_flash(request, "success", "Group updated.")
    return RedirectResponse(f"/groups/{new_slug}", status_code=303)


@router.post("/groups/{slug}/build")
async def group_build(
    request: Request, slug: str, current_user: dict = Depends(require_auth)
):
    from generate_installer import build_installer, InstallerError
    try:
        result = build_installer(slug)
        sha_short = (result["sha256_unsigned"] or "")[:16]
        _set_flash(request, "success", f"Installer ready. SHA256: {sha_short}…")
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
