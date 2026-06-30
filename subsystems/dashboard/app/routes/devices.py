import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import require_auth
from app.deps import get_db
from app.templates_config import templates

HBBS_DB_PATH = Path("/opt/rustdesk-fleet/data/db_v2.sqlite3")

router = APIRouter()


def _set_flash(request: Request, type_: str, msg: str) -> None:
    request.session["flash"] = {"type": type_, "msg": msg}


def _read_peers() -> dict:
    """Return {rustdesk_id: peer_row} from the hbbs database."""
    if not HBBS_DB_PATH.exists():
        return {}
    conn = sqlite3.connect(HBBS_DB_PATH)
    conn.row_factory = sqlite3.Row
    peers = {r["id"]: dict(r) for r in conn.execute("SELECT id, info, created_at FROM peer").fetchall()}
    conn.close()
    return peers


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


@router.get("/devices", response_class=HTMLResponse)
async def devices_list(
    request: Request,
    group: str = "",
    current_user: dict = Depends(require_auth),
):
    peers = _read_peers()

    conn = get_db()
    groups = conn.execute(
        "SELECT id, slug, display_name FROM client_groups ORDER BY display_name"
    ).fetchall()

    fleet_rows = conn.execute(
        """SELECT d.*, cg.display_name AS group_name, cg.slug AS group_slug
           FROM devices d
           LEFT JOIN client_groups cg ON cg.id = d.group_id"""
    ).fetchall()
    conn.close()

    fleet_by_id = {r["rustdesk_id"]: dict(r) for r in fleet_rows if r["rustdesk_id"]}

    # Merge hbbs peers + fleet records into one list
    all_ids = set(peers) | set(fleet_by_id)
    devices = []
    for rid in all_ids:
        peer = peers.get(rid, {})
        fleet = fleet_by_id.get(rid, {})
        info = json.loads(peer.get("info") or "{}")
        ip = info.get("ip", "").replace("::ffff:", "")

        devices.append({
            "rustdesk_id": rid,
            "hostname": fleet.get("hostname") or "—",
            "os": fleet.get("os") or "—",
            "ip": ip or "—",
            "status": "registered" if rid in peers else (fleet.get("status") or "unknown"),
            "last_seen": fleet.get("last_seen") or peer.get("created_at") or "—",
            "group_name": fleet.get("group_name"),
            "group_slug": fleet.get("group_slug"),
            "group_id": fleet.get("group_id"),
        })

    if group:
        devices = [d for d in devices if d.get("group_slug") == group]

    devices.sort(key=lambda d: d["last_seen"] or "", reverse=True)

    return templates.TemplateResponse(
        request,
        "devices.html",
        {
            "devices": devices,
            "groups": groups,
            "active_group": group,
            "current_user": current_user,
            "peer_count": len(peers),
        },
    )


@router.post("/devices/sync")
async def devices_sync(request: Request, current_user: dict = Depends(require_auth)):
    peers = _read_peers()
    if not peers:
        _set_flash(request, "error", "No peers found in RustDesk server database.")
        return RedirectResponse("/devices", status_code=303)

    now = _now_utc()
    conn = get_db()
    new_count = 0
    for rustdesk_id in peers:
        existing = conn.execute(
            "SELECT id FROM devices WHERE rustdesk_id = ?", (rustdesk_id,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE devices SET last_seen = ? WHERE rustdesk_id = ?",
                (now, rustdesk_id),
            )
        else:
            conn.execute(
                "INSERT INTO devices (rustdesk_id, status, last_seen) VALUES (?, 'registered', ?)",
                (rustdesk_id, now),
            )
            new_count += 1
    conn.commit()
    conn.close()

    msg = f"Synced {len(peers)} device{'s' if len(peers) != 1 else ''}"
    if new_count:
        msg += f" — {new_count} new"
    _set_flash(request, "success", msg)
    return RedirectResponse("/devices", status_code=303)


@router.post("/devices/{rustdesk_id}/assign")
async def device_assign(
    request: Request,
    rustdesk_id: str,
    group_id: str = Form(""),
    current_user: dict = Depends(require_auth),
):
    conn = get_db()
    gid = int(group_id) if group_id else None
    existing = conn.execute(
        "SELECT id FROM devices WHERE rustdesk_id = ?", (rustdesk_id,)
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE devices SET group_id = ? WHERE rustdesk_id = ?", (gid, rustdesk_id)
        )
    else:
        conn.execute(
            "INSERT INTO devices (rustdesk_id, group_id, status, last_seen) VALUES (?, ?, 'registered', ?)",
            (rustdesk_id, gid, _now_utc()),
        )
    conn.commit()
    conn.close()
    _set_flash(request, "success", "Device updated.")
    return RedirectResponse("/devices", status_code=303)
