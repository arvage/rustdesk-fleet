from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.auth import require_auth
from app.deps import get_db
from app.templates_config import templates

router = APIRouter()


@router.get("/devices", response_class=HTMLResponse)
async def devices_list(
    request: Request,
    group: str = "",
    current_user: dict = Depends(require_auth),
):
    conn = get_db()
    groups = conn.execute(
        "SELECT id, slug, display_name FROM client_groups ORDER BY display_name"
    ).fetchall()

    if group:
        devices = conn.execute(
            """SELECT d.*, cg.display_name AS group_name, cg.slug AS group_slug
               FROM devices d
               LEFT JOIN client_groups cg ON cg.id = d.group_id
               WHERE cg.slug = ?
               ORDER BY d.last_seen DESC NULLS LAST, d.hostname""",
            (group,),
        ).fetchall()
    else:
        devices = conn.execute(
            """SELECT d.*, cg.display_name AS group_name, cg.slug AS group_slug
               FROM devices d
               LEFT JOIN client_groups cg ON cg.id = d.group_id
               ORDER BY d.last_seen DESC NULLS LAST, d.hostname"""
        ).fetchall()

    conn.close()
    return templates.TemplateResponse(
        request,
        "devices.html",
        {
            "devices": devices,
            "groups": groups,
            "active_group": group,
            "current_user": current_user,
        },
    )
