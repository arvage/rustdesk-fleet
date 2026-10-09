from urllib.parse import urlencode

from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse

from app.auth import require_auth

router = APIRouter()

# Rows shown on the Sessions page. Open sessions sort to the top (ended_at NULL
# first), then most-recently-started.
_LIST_SQL = """
    SELECT s.rustdesk_id, s.started_at, s.ended_at,
           d.label, cg.display_name AS group_name, cg.slug AS group_slug,
           di.hostname,
           CAST((julianday(COALESCE(s.ended_at, 'now')) - julianday(s.started_at)) * 86400
                AS INTEGER) AS duration_s
    FROM device_sessions s
    LEFT JOIN devices d       ON d.rustdesk_id  = s.rustdesk_id
    LEFT JOIN client_groups cg ON cg.id         = d.group_id
    LEFT JOIN device_info di   ON di.rustdesk_id = s.rustdesk_id
    {where}
    ORDER BY (s.ended_at IS NULL) DESC, s.started_at DESC
    LIMIT ?
"""


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
    return d


@router.get("/sessions")
async def sessions_page(rid: str = "", _: dict = Depends(require_auth)):
    """Remote sessions now live under the categorized Logs page; redirect there
    (preserving a per-device filter) so old links keep working."""
    params = {"cat": "sessions"}
    if rid:
        params["rid"] = rid
    return RedirectResponse("/audit?" + urlencode(params), status_code=307)
