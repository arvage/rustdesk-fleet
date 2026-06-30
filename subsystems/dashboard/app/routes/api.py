import json
import sqlite3
import subprocess
import time
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.auth import require_auth

HBBS_DB_PATH = Path("/opt/rustdesk-fleet/data/db_v2.sqlite3")
HBBS_PORTS = {"21115", "21116", "21117", "21118", "21119"}

# Grace period: keep a device online for this many seconds after the last
# time we saw its IP in an established TCP connection.  RustDesk drops and
# re-establishes the hbbs TCP connection during heartbeat cycles; without
# a grace period a 10-second poll catches the gap and flickers to offline.
_ONLINE_GRACE_S = 60

# {ip: last_seen_epoch}  — module-level so it persists across requests
_ip_last_seen: dict[str, float] = {}

router = APIRouter(prefix="/api")


def _online_ips() -> set[str]:
    """Return IPs that are currently connected OR were seen within the grace period."""
    try:
        result = subprocess.run(
            ["ss", "-tn", "state", "established"],
            capture_output=True, text=True, timeout=3,
        )
    except Exception:
        return set()

    now = time.monotonic()
    for line in result.stdout.splitlines()[1:]:  # skip header row
        parts = line.split()
        if len(parts) < 4:
            continue
        local_port = parts[2].rsplit(":", 1)[-1]
        if local_port not in HBBS_PORTS:
            continue
        remote_addr = parts[3].rsplit(":", 1)[0].strip("[]")
        if remote_addr.startswith("::ffff:"):
            remote_addr = remote_addr[7:]
        _ip_last_seen[remote_addr] = now

    cutoff = now - _ONLINE_GRACE_S
    return {ip for ip, ts in _ip_last_seen.items() if ts >= cutoff}


@router.get("/devices/status")
async def devices_status(_: dict = Depends(require_auth)):
    """Return {rustdesk_id: "online"|"offline"} for every known peer."""
    online_ips = _online_ips()
    status: dict[str, str] = {}

    if HBBS_DB_PATH.exists():
        conn = sqlite3.connect(HBBS_DB_PATH)
        conn.row_factory = sqlite3.Row
        for peer in conn.execute("SELECT id, info FROM peer").fetchall():
            info = json.loads(peer["info"] or "{}")
            peer_ip = info.get("ip", "").replace("::ffff:", "")
            status[peer["id"]] = "online" if peer_ip in online_ips else "offline"
        conn.close()

    return JSONResponse({"devices": status})
