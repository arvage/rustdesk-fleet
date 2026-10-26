from urllib.parse import urlencode

from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse

from app.auth import require_auth

router = APIRouter()

# Rows shown on the Sessions page. Open sessions sort to the top (ended_at NULL
# first), then most-recently-started.
# Dashboard user behind session `s`: whoever clicked Connect on the device
# shortly before it started, else the user the connecting RustDesk id is
# linked to (rustdesk_peer_users).
# Every lookup stays inside the session's own tenant.
DASHBOARD_USER_SQL = """COALESCE(
           (SELECT COALESCE(u.display_name, c.user_email)
              FROM connect_clicks c
              LEFT JOIN users u ON u.email = c.user_email AND u.tenant_id = c.tenant_id
             WHERE c.tenant_id = s.tenant_id AND c.rustdesk_id = s.rustdesk_id
               AND c.clicked_at BETWEEN datetime(s.started_at, '-3 minutes')
                                    AND datetime(s.started_at, '+1 minute')
             ORDER BY c.clicked_at DESC LIMIT 1),
           (SELECT COALESCE(u.display_name, pu.user_email)
              FROM rustdesk_peer_users pu
              LEFT JOIN users u ON u.email = pu.user_email AND u.tenant_id = pu.tenant_id
             WHERE pu.tenant_id = s.tenant_id AND pu.peer_id = s.peer_id))"""

_LIST_SQL = """
    SELECT s.rustdesk_id, s.started_at, s.ended_at,
           s.peer_id, s.peer_name, s.peer_ip, s.conn_type,
           d.label, cg.display_name AS group_name, cg.slug AS group_slug,
           di.hostname, COALESCE(pd.label, pdi.hostname) AS peer_device_name,
           """ + DASHBOARD_USER_SQL + """ AS dashboard_user,
           CAST((julianday(COALESCE(s.ended_at, 'now')) - julianday(s.started_at)) * 86400
                AS INTEGER) AS duration_s
    FROM device_sessions s
    LEFT JOIN devices d       ON d.rustdesk_id  = s.rustdesk_id AND d.tenant_id   = s.tenant_id
    LEFT JOIN client_groups cg ON cg.id         = d.group_id    AND cg.tenant_id  = s.tenant_id
    LEFT JOIN device_info di   ON di.rustdesk_id = s.rustdesk_id AND di.tenant_id = s.tenant_id
    -- The controller may itself be a fleet device; if so, show its name too.
    LEFT JOIN devices pd       ON pd.rustdesk_id  = s.peer_id AND pd.tenant_id  = s.tenant_id
    LEFT JOIN device_info pdi  ON pdi.rustdesk_id = s.peer_id AND pdi.tenant_id = s.tenant_id
    WHERE s.tenant_id = ? {where}
    ORDER BY (s.ended_at IS NULL) DESC, s.started_at DESC
    LIMIT ?
"""
# Use as _LIST_SQL.format(where="AND s.rustdesk_id = ?") with params
# (tenant_id, rid, limit), or where="" with (tenant_id, limit).


def fmt_duration(secs) -> str:
    try:
        s = int(secs)
    except (TypeError, ValueError):
        return "—"
    if s < 0:
        s = 0
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s}s" if s else f"{m}m"
    h, m = divmod(m, 60)
    return f"{h}h {m}m" if m else f"{h}h"


def _row(r) -> dict:
    d = dict(r)
    d["active"] = r["ended_at"] is None
    d["duration_str"] = fmt_duration(r["duration_s"])
    d["device_name"] = r["label"] or r["hostname"] or ""
    from app.routes.client_api import CONN_TYPES
    d["conn_type_label"] = CONN_TYPES.get(r["conn_type"], "") if r["conn_type"] is not None else ""
    return d


@router.get("/sessions")
async def sessions_page(rid: str = "", _: dict = Depends(require_auth)):
    """Remote sessions now live under the categorized Logs page; redirect there
    (preserving a per-device filter) so old links keep working."""
    params = {"cat": "sessions"}
    if rid:
        params["rid"] = rid
    return RedirectResponse("/audit?" + urlencode(params), status_code=307)
