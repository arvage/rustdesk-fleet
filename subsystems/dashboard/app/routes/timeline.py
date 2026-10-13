"""
Per-device connection timeline (Devices → ⓘ details → Timeline, or the
Remote sessions log), modelled on ScreenConnect's session timeline.

One horizontal track per lane over a chosen window:

  Device online   reconstructed from device_online / device_offline events
                  (api.py's status watcher). device_online was only logged
                  from 2026-10-12, so two offline events in a row mean the
                  device came back unrecorded in between — shown as unknown.
  one per peer    each connecting computer's sessions (device_sessions,
                  enriched by /api/audit/conn), colored by connection type
  Events          device registered / updated / deleted / restored

File transfers (device_file_transfers, from /api/audit/file) are drawn as
markers on the connecting computer's lane.

plus the same history as a chronological event list. All times are stored
UTC; positions are sent as epoch seconds and labels rendered in the
dashboard's display timezone.
"""
import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import require_auth
from app.deps import get_db
from app.routes.client_api import CONN_TYPES
from app.routes.sessions import fmt_duration
from app.templates_config import _DISPLAY_TZ, templates

router = APIRouter()

RANGES = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}

_DEVICE_EVENTS = {
    "device_registered": "Registered with the server",
    "device_updated": "Label or client group changed",
    "device_deleted": "Deleted from the dashboard",
    "device_restored": "Reappeared after deletion",
}


_DIRECTIONS = {0: ("Sent from device", "↑"), 1: ("Sent to device", "↓")}


def _fmt_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _sql(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _ticks(start: datetime, end: datetime, rng: str) -> list[dict]:
    """Axis ticks aligned to local hours/days in the display timezone."""
    local = start.astimezone(_DISPLAY_TZ)
    if rng == "24h":
        step_h = 3
        t = local.replace(minute=0, second=0, microsecond=0)
        t += timedelta(hours=(step_h - t.hour % step_h) % step_h)
        fmt = lambda d: d.strftime("%a %H:%M") if d.hour == 0 else d.strftime("%H:%M")
        step = timedelta(hours=step_h)
    else:
        step_d = 1 if rng == "7d" else 5
        t = local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        fmt = lambda d: d.strftime("%a %b %-d") if rng == "7d" else d.strftime("%b %-d")
        step = timedelta(days=step_d)
    out = []
    while t < end:
        if t > start:
            out.append({"t": int(t.timestamp()), "label": fmt(t)})
        # zoneinfo datetimes add in wall-clock time, so ticks stay on local
        # midnight across DST changes.
        t = t + step
    return out


def _online_spans(conn, rid: str, start: datetime, end: datetime) -> list[dict]:
    """[{s, e, state}] with state 'online' | 'offline' | 'unknown', covering
    the window wherever the history says something."""
    before = conn.execute(
        """SELECT event FROM provisioning_events
           WHERE detail = ? AND event IN ('device_online','device_offline') AND created_at < ?
           ORDER BY created_at DESC, id DESC LIMIT 1""",
        (rid, _sql(start)),
    ).fetchone()
    rows = conn.execute(
        """SELECT event, created_at FROM provisioning_events
           WHERE detail = ? AND event IN ('device_online','device_offline')
             AND created_at >= ? AND created_at < ?
           ORDER BY created_at, id""",
        (rid, _sql(start), _sql(end)),
    ).fetchall()

    def state_of(ev):
        return "online" if ev == "device_online" else "offline"

    spans: list[dict] = []
    state = state_of(before["event"]) if before else None
    cursor = start
    for r in rows:
        at = _parse(r["created_at"])
        new = state_of(r["event"])
        if state == "offline" and new == "offline":
            state = "unknown"   # came back online without a recorded event
        if state and at > cursor:
            spans.append({"s": cursor, "e": at, "state": state})
        state, cursor = new, at
    if state and cursor < end:
        spans.append({"s": cursor, "e": end, "state": state})
    return [
        {"s": int(s["s"].timestamp()), "e": int(s["e"].timestamp()), "state": s["state"]}
        for s in spans
    ]


@router.get("/devices/{rid}/timeline", response_class=HTMLResponse)
async def device_timeline(
    rid: str,
    request: Request,
    range: str = "7d",
    back: int = 0,
    current_user: dict = Depends(require_auth),
):
    rng = range if range in RANGES else "7d"
    back = max(0, min(back, 365))
    now = datetime.now(timezone.utc).replace(microsecond=0)
    end = now - RANGES[rng] * back
    start = end - RANGES[rng]

    conn = get_db()
    dev = conn.execute(
        """SELECT d.rustdesk_id, d.label, di.hostname, di.os, di.heartbeat_at, di.active_conns,
                  cg.display_name AS group_name, cg.slug AS group_slug
           FROM devices d
           LEFT JOIN device_info di   ON di.rustdesk_id = d.rustdesk_id
           LEFT JOIN client_groups cg ON cg.id = d.group_id
           WHERE d.rustdesk_id = ?""",
        (rid,),
    ).fetchone()
    if not dev:
        conn.close()
        request.session["flash"] = {"type": "error", "msg": f"Device {rid} not found."}
        return RedirectResponse("/devices", status_code=303)

    sess_rows = conn.execute(
        """SELECT s.id, s.started_at, s.ended_at, s.peer_id, s.peer_name, s.peer_ip, s.conn_type,
                  COALESCE(pd.label, pdi.hostname) AS peer_device_name
           FROM device_sessions s
           LEFT JOIN devices pd      ON pd.rustdesk_id  = s.peer_id
           LEFT JOIN device_info pdi ON pdi.rustdesk_id = s.peer_id
           WHERE s.rustdesk_id = ? AND s.started_at < ?
             AND COALESCE(s.ended_at, ?) > ?
           ORDER BY s.started_at""",
        (rid, _sql(end), _sql(now), _sql(start)),
    ).fetchall()

    dev_events = conn.execute(
        f"""SELECT event, created_at, user_email FROM provisioning_events
            WHERE detail = ? AND event IN ({','.join('?' * len(_DEVICE_EVENTS))})
              AND created_at >= ? AND created_at <= ?
            ORDER BY created_at""",
        (rid, *_DEVICE_EVENTS, _sql(start), _sql(end)),
    ).fetchall()

    xfer_rows = conn.execute(
        """SELECT f.*, s.peer_id AS s_peer_id, s.peer_name AS s_peer_name
           FROM device_file_transfers f
           LEFT JOIN device_sessions s ON s.id = f.session_id
           WHERE f.rustdesk_id = ? AND f.created_at >= ? AND f.created_at <= ?
           ORDER BY f.created_at""",
        (rid, _sql(start), _sql(end)),
    ).fetchall()

    online = _online_spans(conn, rid, start, end)
    conn.close()

    # ── Sessions → one lane per connecting computer ──────────────────────────
    lanes: dict[str, dict] = {}
    sessions = []
    total_s = 0
    for r in sess_rows:
        s_at = _parse(r["started_at"])
        e_at = _parse(r["ended_at"])
        active = e_at is None
        dur = int(((e_at or now) - s_at).total_seconds())
        key = r["peer_id"] or r["peer_name"] or ""
        lane = lanes.setdefault(key, {
            "key": key,
            "name": r["peer_name"] or r["peer_device_name"] or ("Unknown" if not key else ""),
            "peer_id": r["peer_id"] or "",
            "peer_device_name": r["peer_device_name"] or "",
            "total_s": 0, "count": 0, "bars": [],
        })
        # Time inside the window only, so the summary matches the chart.
        in_window = int((min(e_at or now, end) - max(s_at, start)).total_seconds())
        lane["total_s"] += max(0, in_window)
        lane["count"] += 1
        total_s += max(0, in_window)
        ctype = r["conn_type"]
        bar = {
            "id": r["id"],
            "s": int(s_at.timestamp()),
            "e": int((e_at or now).timestamp()),
            "active": active,
            "type": ctype if ctype in CONN_TYPES else -1,
            "type_label": CONN_TYPES.get(ctype, "Connection"),
            "ip": r["peer_ip"] or "",
            "duration": fmt_duration(dur),
        }
        lane["bars"].append(bar)
        sessions.append({**bar, "lane": lane["name"], "peer_id": r["peer_id"] or ""})

    # ── File transfers → markers on the connecting computer's lane ──────────
    transfers = []
    for r in xfer_rows:
        peer_id = r["peer_id"] or r["s_peer_id"] or ""
        name = r["peer_name"] or r["s_peer_name"] or ""
        key = peer_id or name
        lane = lanes.setdefault(key, {
            "key": key, "name": name or ("Unknown" if not key else ""),
            "peer_id": peer_id, "peer_device_name": "",
            "total_s": 0, "count": 0, "bars": [],
        })
        try:
            files = json.loads(r["files"] or "[]")
        except ValueError:
            files = []
        label, arrow = _DIRECTIONS[r["direction"]]
        count = r["file_count"] or len(files)
        size = sum(f[1] for f in files)
        what = f"{count} file{'s' if count != 1 else ''}" + (
            f" ({'≥ ' if count > len(files) else ''}{_fmt_size(size)})" if files else "")
        shown = [f[0] for f in files[:5] if f[0]]
        names = ", ".join(shown) + (f", +{count - len(shown)} more" if shown and count > len(shown) else "")
        via = "clipboard paste" if not r["path"] else r["path"]
        x = {
            "t": int(_parse(r["created_at"]).timestamp()),
            "dir": r["direction"], "arrow": arrow, "label": label,
            "what": what, "via": via, "names": names,
            "sid": r["session_id"], "who": lane["name"],
        }
        lane.setdefault("xfers", []).append(x)
        transfers.append(x)
    lane_list = sorted(lanes.values(), key=lambda l: (l["key"] == "", -l["total_s"]))

    # ── Chronological event list ─────────────────────────────────────────────
    events = []
    for s in sessions:
        who = s["lane"] + (f" ({s['peer_id']})" if s["peer_id"] and s["lane"] != s["peer_id"] else "")
        if s["s"] >= int(start.timestamp()):
            events.append({"t": s["s"], "kind": "connect", "sid": s["id"],
                           "title": f"{s['type_label']} started",
                           "detail": who + (f" from {s['ip']}" if s["ip"] else "")})
        if not s["active"] and s["e"] < int(end.timestamp()):
            events.append({"t": s["e"], "kind": "disconnect", "sid": s["id"],
                           "title": f"{s['type_label']} ended",
                           "detail": f"{who} · {s['duration']}"})
    for x in transfers:
        events.append({"t": x["t"], "kind": "file", "sid": x["sid"],
                       "title": f"{x['label']}: {x['what']}",
                       "detail": f"{x['who']} · {x['via']}" + (f" · {x['names']}" if x["names"] else "")})
    prev = None
    for sp in online:
        if sp["state"] != prev and sp["s"] > int(start.timestamp()) and sp["state"] != "unknown":
            events.append({"t": sp["s"], "kind": sp["state"],
                           "title": "Came online" if sp["state"] == "online" else "Went offline",
                           "detail": ""})
        prev = sp["state"]
    markers = []
    for r in dev_events:
        t = int(_parse(r["created_at"]).timestamp())
        label = _DEVICE_EVENTS[r["event"]]
        markers.append({"t": t, "label": label})
        events.append({"t": t, "kind": "device", "title": label,
                       "detail": f"by {r['user_email']}" if r["user_email"] else ""})
    events.sort(key=lambda e: e["t"], reverse=True)

    online_s = sum(sp["e"] - sp["s"] for sp in online if sp["state"] == "online")
    known_s = sum(sp["e"] - sp["s"] for sp in online if sp["state"] != "unknown")
    window_s = int((end - start).total_seconds())

    return templates.TemplateResponse(
        request,
        "device_timeline.html",
        {
            "current_user": current_user,
            "device": dict(dev),
            "device_name": dev["label"] or dev["hostname"] or rid,
            "rng": rng,
            "ranges": list(RANGES),
            "back": back,
            "start": int(start.timestamp()),
            "end": int(end.timestamp()),
            "now": int(now.timestamp()),
            "ticks": _ticks(start, end, rng),
            "online": online,
            "lanes": lane_list,
            "markers": markers,
            "events": events,
            "conn_types": CONN_TYPES,
            "summary": {
                "sessions": len(sessions),
                "connected": fmt_duration(total_s) if sessions else "—",
                "peers": len([l for l in lane_list if l["key"]]),
                "transfers": len(transfers),
                "uptime": f"{round(100 * online_s / known_s)}%" if known_s >= window_s * 0.5 else "—",
            },
        },
    )
