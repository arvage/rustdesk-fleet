import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import require_auth
from app.deps import get_db, get_devices, get_hbbs_peers, log_event
from app.notifications import fire_notification
from app.templates_config import templates

HBBS_DB_PATH = Path("/opt/rustdesk-fleet/data/db_v2.sqlite3")

router = APIRouter()


def _set_flash(request: Request, type_: str, msg: str) -> None:
    request.session["flash"] = {"type": type_, "msg": msg}


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


@router.get("/devices", response_class=HTMLResponse)
async def devices_list(
    request: Request,
    group: str = "",
    current_user: dict = Depends(require_auth),
):
    devices, peer_count = get_devices(group)

    conn = get_db()
    groups = conn.execute(
        "SELECT id, slug, display_name FROM client_groups ORDER BY display_name"
    ).fetchall()
    deleted_rows = conn.execute(
        """SELECT d.rustdesk_id, d.label, d.last_seen,
                  cg.display_name AS group_name, cg.slug AS group_slug
           FROM devices d
           LEFT JOIN client_groups cg ON cg.id = d.group_id
           WHERE d.hidden = 1
           ORDER BY d.last_seen DESC"""
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
            "peer_count": peer_count,
            "deleted_devices": [dict(r) for r in deleted_rows],
        },
    )


@router.post("/devices/{rustdesk_id}/edit")
async def device_edit(
    request: Request,
    rustdesk_id: str,
    label: str = Form(""),
    group_id: str = Form(""),
    current_user: dict = Depends(require_auth),
):
    label = label.strip() or None
    gid = int(group_id) if group_id else None

    conn = get_db()
    existing = conn.execute(
        "SELECT id FROM devices WHERE rustdesk_id = ?", (rustdesk_id,)
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE devices SET label = ?, group_id = ? WHERE rustdesk_id = ?",
            (label, gid, rustdesk_id),
        )
        log_event(conn, "device_updated", rustdesk_id, current_user["email"])
        conn.commit()
        conn.close()
    else:
        conn.execute(
            "INSERT INTO devices (rustdesk_id, label, group_id, status, last_seen) VALUES (?, ?, ?, 'registered', ?)",
            (rustdesk_id, label, gid, _now_utc()),
        )
        log_event(conn, "device_registered", rustdesk_id, current_user["email"])
        conn.commit()
        group_name = ""
        if gid:
            row = conn.execute("SELECT display_name FROM client_groups WHERE id=?", (gid,)).fetchone()
            if row:
                group_name = row["display_name"]
        conn.close()
        fire_notification("device_registered", {
            "rustdesk_id": rustdesk_id,
            "label": label or "",
            "group_name": group_name,
            "ip": "—",
            "registered_at": _now_utc(),
        })
    _set_flash(request, "success", "Device updated.")
    return RedirectResponse("/devices", status_code=303)


@router.post("/devices/{rustdesk_id}/delete")
async def device_delete(
    request: Request,
    rustdesk_id: str,
    current_user: dict = Depends(require_auth),
):
    conn = get_db()
    existing = conn.execute(
        """SELECT d.rustdesk_id, d.label, d.last_seen, cg.display_name AS group_name
           FROM devices d LEFT JOIN client_groups cg ON cg.id = d.group_id
           WHERE d.rustdesk_id = ?""",
        (rustdesk_id,),
    ).fetchone()
    if existing:
        notif_ctx = {
            "rustdesk_id": existing["rustdesk_id"],
            "label": existing["label"] or "",
            "group_name": existing["group_name"] or "",
            "last_seen": existing["last_seen"] or "",
        }
        conn.execute(
            "UPDATE devices SET hidden = 1 WHERE rustdesk_id = ?", (rustdesk_id,)
        )
    else:
        notif_ctx = {"rustdesk_id": rustdesk_id, "label": "", "group_name": "", "last_seen": ""}
        conn.execute(
            "INSERT INTO devices (rustdesk_id, hidden, status, last_seen) VALUES (?, 1, 'deleted', ?)",
            (rustdesk_id, _now_utc()),
        )
    log_event(conn, "device_deleted", rustdesk_id, current_user["email"])
    conn.commit()
    conn.close()
    fire_notification("device_deleted", notif_ctx)

    # Best-effort removal from hbbs peer DB (may fail if daemon has a write lock)
    if HBBS_DB_PATH.exists():
        try:
            hconn = sqlite3.connect(HBBS_DB_PATH, timeout=2)
            hconn.execute("DELETE FROM peer WHERE id = ?", (rustdesk_id,))
            hconn.commit()
            hconn.close()
        except Exception:
            pass

    _set_flash(request, "success", f"Device {rustdesk_id} removed.")
    return RedirectResponse("/devices", status_code=303)


@router.post("/devices/{rustdesk_id}/restore")
async def device_restore(
    request: Request,
    rustdesk_id: str,
    current_user: dict = Depends(require_auth),
):
    conn = get_db()
    conn.execute(
        "UPDATE devices SET hidden = 0, status = 'registered' WHERE rustdesk_id = ?",
        (rustdesk_id,),
    )
    log_event(conn, "device_restored", rustdesk_id, current_user["email"])
    conn.commit()
    conn.close()
    _set_flash(request, "success", f"Device {rustdesk_id} restored.")
    return RedirectResponse("/devices", status_code=303)


@router.post("/devices/sync")
async def devices_sync(request: Request, current_user: dict = Depends(require_auth)):
    peers = get_hbbs_peers()
    if not peers:
        _set_flash(request, "error", "No peers found in RustDesk server database.")
        return RedirectResponse("/devices", status_code=303)

    now = _now_utc()
    conn = get_db()
    new_count = 0
    for rustdesk_id in peers:
        existing = conn.execute(
            "SELECT id, hidden FROM devices WHERE rustdesk_id = ?", (rustdesk_id,)
        ).fetchone()
        if existing:
            if not existing["hidden"]:
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
            log_event(conn, "device_registered", rustdesk_id, current_user["email"])
            peer = peers[rustdesk_id]
            fire_notification("device_registered", {
                "rustdesk_id": rustdesk_id,
                "label": "",
                "group_name": "",
                "ip": peer.get("ip") or "—",
                "registered_at": peer.get("registered_at") or now,
            })
    conn.commit()
    conn.close()

    msg = f"Synced {len(peers)} device{'s' if len(peers) != 1 else ''}"
    if new_count:
        msg += f" — {new_count} new"
    _set_flash(request, "success", msg)
    return RedirectResponse("/devices", status_code=303)
