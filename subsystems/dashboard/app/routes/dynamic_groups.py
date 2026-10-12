import csv
import io
import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.auth import require_auth
from app.deps import get_db, log_event
from app.dynamic_groups import PRESETS, builder_catalogue, describe, members, normalize_rules
from app.fleet import OS_SUPPORT_LABELS, device_name, fleet_snapshot
from app.permissions import require_perm
from app.templates_config import templates

router = APIRouter()

# Viewing dynamic groups is open to any signed-in user (like Devices);
# creating/editing/deleting them needs manage_devices.
_EDIT_CAP = "manage_devices"


def _set_flash(request: Request, type_: str, msg: str) -> None:
    request.session["flash"] = {"type": type_, "msg": msg}


def _client_groups(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT id, slug, display_name FROM client_groups ORDER BY display_name"
    ).fetchall()]


def _load(conn, group_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM dynamic_groups WHERE id = ?", (group_id,)).fetchone()
    if row is None:
        return None
    g = dict(row)
    try:
        g["rules"] = normalize_rules(g["rules"])
    except ValueError:
        g["rules"] = []
    return g


def _group_names(groups: list[dict]) -> dict[str, str]:
    return {str(g["id"]): g["display_name"] for g in groups}


@router.get("/dynamic-groups", response_class=HTMLResponse)
async def dynamic_groups_list(request: Request, current_user: dict = Depends(require_auth)):
    conn = get_db()
    rows = conn.execute("SELECT * FROM dynamic_groups ORDER BY name COLLATE NOCASE").fetchall()
    groups = _client_groups(conn)
    conn.close()
    devices = fleet_snapshot()
    names = _group_names(groups)
    items = []
    for r in rows:
        try:
            rules = normalize_rules(r["rules"])
        except ValueError:
            rules = []
        items.append({
            **dict(r),
            "summary": describe(rules, r["match"], names),
            "count": len(members(devices, r["match"], rules)),
        })
    return templates.TemplateResponse(request, "dynamic_groups.html", {
        "items": items, "total_devices": len(devices), "current_user": current_user,
    })


def _editor(request: Request, current_user: dict, group: dict | None, error: str = ""):
    conn = get_db()
    groups = _client_groups(conn)
    conn.close()
    return templates.TemplateResponse(request, "dynamic_group_edit.html", {
        "group": group,
        "catalogue": builder_catalogue(groups),
        "presets": PRESETS if group is None or not group.get("id") else [],
        "error": error,
        "current_user": current_user,
    })


@router.get("/dynamic-groups/new", response_class=HTMLResponse)
async def dynamic_group_new(request: Request, current_user: dict = Depends(require_auth)):
    require_perm(current_user, _EDIT_CAP)
    return _editor(request, current_user, None)


@router.get("/dynamic-groups/{group_id}/edit", response_class=HTMLResponse)
async def dynamic_group_edit(request: Request, group_id: int, current_user: dict = Depends(require_auth)):
    require_perm(current_user, _EDIT_CAP)
    conn = get_db()
    group = _load(conn, group_id)
    conn.close()
    if group is None:
        _set_flash(request, "error", "Dynamic group not found.")
        return RedirectResponse("/dynamic-groups", status_code=303)
    return _editor(request, current_user, group)


@router.post("/dynamic-groups/save")
async def dynamic_group_save(
    request: Request,
    group_id: str = Form(""),
    name: str = Form(""),
    description: str = Form(""),
    match: str = Form("all"),
    rules: str = Form("[]"),
    current_user: dict = Depends(require_auth),
):
    require_perm(current_user, _EDIT_CAP)
    name, description = name.strip()[:100], description.strip()[:300]
    match = "any" if match == "any" else "all"
    draft = {"id": int(group_id) if group_id.isdigit() else None,
             "name": name, "description": description, "match": match, "rules": []}
    try:
        draft["rules"] = normalize_rules(rules)
        if not name:
            raise ValueError("Give the group a name.")
        if not draft["rules"]:
            raise ValueError("Add at least one rule.")
    except ValueError as e:
        try:
            draft["rules"] = json.loads(rules)   # keep what they typed on re-render
        except json.JSONDecodeError:
            pass
        return _editor(request, current_user, draft, error=str(e))

    conn = get_db()
    if draft["id"]:
        conn.execute(
            "UPDATE dynamic_groups SET name=?, description=?, match=?, rules=?, updated_at=datetime('now') WHERE id=?",
            (name, description, match, json.dumps(draft["rules"]), draft["id"]),
        )
        gid = draft["id"]
        log_event(conn, "dynamic_group_updated", name, current_user["email"])
    else:
        cur = conn.execute(
            "INSERT INTO dynamic_groups (name, description, match, rules, created_by) VALUES (?, ?, ?, ?, ?)",
            (name, description, match, json.dumps(draft["rules"]), current_user["email"]),
        )
        gid = cur.lastrowid
        log_event(conn, "dynamic_group_created", name, current_user["email"])
    conn.commit()
    conn.close()
    _set_flash(request, "success", f"Dynamic group “{name}” saved.")
    return RedirectResponse(f"/dynamic-groups/{gid}", status_code=303)


@router.post("/dynamic-groups/{group_id}/delete")
async def dynamic_group_delete(request: Request, group_id: int, current_user: dict = Depends(require_auth)):
    require_perm(current_user, _EDIT_CAP)
    conn = get_db()
    group = _load(conn, group_id)
    if group:
        conn.execute("DELETE FROM dynamic_groups WHERE id = ?", (group_id,))
        log_event(conn, "dynamic_group_deleted", group["name"], current_user["email"])
        conn.commit()
        _set_flash(request, "success", f"Dynamic group “{group['name']}” deleted.")
    conn.close()
    return RedirectResponse("/dynamic-groups", status_code=303)


@router.post("/dynamic-groups/preview")
async def dynamic_group_preview(request: Request, current_user: dict = Depends(require_auth)):
    """Live match count for the rule builder (rules aren't saved)."""
    body = await request.json()
    try:
        rules = normalize_rules(body.get("rules") or [])
    except ValueError as e:
        return JSONResponse({"error": str(e)})
    match = "any" if body.get("match") == "any" else "all"
    found = members(fleet_snapshot(), match, rules)
    return JSONResponse({
        "count": len(found),
        "sample": [device_name(d) for d in found[:12]],
    })


@router.get("/dynamic-groups/{group_id}", response_class=HTMLResponse)
async def dynamic_group_detail(request: Request, group_id: int, current_user: dict = Depends(require_auth)):
    conn = get_db()
    group = _load(conn, group_id)
    groups = _client_groups(conn)
    conn.close()
    if group is None:
        _set_flash(request, "error", "Dynamic group not found.")
        return RedirectResponse("/dynamic-groups", status_code=303)
    devices = members(fleet_snapshot(), group["match"], group["rules"])
    return templates.TemplateResponse(request, "dynamic_group_detail.html", {
        "group": group,
        "summary": describe(group["rules"], group["match"], _group_names(groups)),
        "devices": devices,
        "os_support_labels": OS_SUPPORT_LABELS,
        "current_user": current_user,
    })


@router.get("/dynamic-groups/{group_id}/export.csv")
async def dynamic_group_export(group_id: int, current_user: dict = Depends(require_auth)):
    conn = get_db()
    group = _load(conn, group_id)
    conn.close()
    if group is None:
        return Response("Not found", status_code=404)
    devices = members(fleet_snapshot(), group["match"], group["rules"])
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["rustdesk_id", "label", "client_group", "hostname", "user", "os", "os_build",
                "os_support", "os_end_of_support", "cpu", "cores", "memory", "client_version",
                "ip", "status", "registered", "last_seen", "last_remote_session"])
    for d in devices:
        w.writerow([
            d["rustdesk_id"], d.get("label", ""), d.get("group_name", ""), d.get("hostname", ""),
            d.get("username", ""), d.get("os_name", ""), d.get("os_build", ""),
            OS_SUPPORT_LABELS.get(d["os_support"], ""), d.get("os_eos", ""),
            d.get("cpu_name", ""), d.get("cores", ""), d.get("ram", ""), d.get("client_version", ""),
            d.get("ip", ""), d.get("live_status", ""), d.get("registered_at", ""),
            d.get("last_seen", ""), d.get("last_session", ""),
        ])
    fname = "".join(c if c.isalnum() else "-" for c in group["name"].lower()).strip("-") or "dynamic-group"
    return Response(
        content=buf.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={fname}.csv"},
    )
