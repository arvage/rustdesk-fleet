import json
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.auth import require_auth
from app.deps import get_devices

HBBS_DB_PATH = Path("/opt/rustdesk-fleet/data/db_v2.sqlite3")
HBBS_PORTS = {"21115", "21116", "21117", "21118", "21119"}

# How long to keep a device marked online after its IP was last seen in an
# established TCP connection.  RustDesk reconnects every ~12-30 s; 90 s
# gives a comfortable margin without marking genuinely-offline devices online.
_ONLINE_GRACE_S = 90

# {ip: last_seen monotonic timestamp} — written by background thread,
# read by the API handler.  Lock guards concurrent access.
_ip_last_seen: dict[str, float] = {}
_lock = threading.Lock()


def _poll_once() -> None:
    """Run ss and update _ip_last_seen for every hbbs-connected remote IP."""
    try:
        result = subprocess.run(
            ["ss", "-tn", "state", "established"],
            capture_output=True, text=True, timeout=2,
        )
    except Exception:
        return
    now = time.monotonic()
    for line in result.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 4:
            continue
        local_port = parts[2].rsplit(":", 1)[-1]
        if local_port not in HBBS_PORTS:
            continue
        remote_addr = parts[3].rsplit(":", 1)[0].strip("[]")
        if remote_addr.startswith("::ffff:"):
            remote_addr = remote_addr[7:]
        with _lock:
            _ip_last_seen[remote_addr] = now


def _bg_poll_loop() -> None:
    """Background daemon thread — polls ss every second so we catch even
    sub-second TCP connection windows that a 10-second frontend poll would miss."""
    while True:
        _poll_once()
        time.sleep(1)


# Start background polling immediately when the module is imported.
threading.Thread(target=_bg_poll_loop, daemon=True, name="hbbs-ss-poll").start()

router = APIRouter(prefix="/api")


def _online_ips() -> set[str]:
    """Return IPs seen connected to an hbbs port within the grace period."""
    cutoff = time.monotonic() - _ONLINE_GRACE_S
    with _lock:
        return {ip for ip, ts in _ip_last_seen.items() if ts >= cutoff}


@router.get("/devices/status")
async def devices_status(_: dict = Depends(require_auth)):
    """Return {rustdesk_id: "online"|"offline"} for every known peer.

    Two signals are combined:
    1. Live TCP connection to an hbbs port seen within the grace period.
    2. created_at in the hbbs peer table updated within the last 15 minutes.
       RustDesk sessions often go P2P (bypassing our server), so the client's
       TCP connection to hbbs becomes idle and gets killed by NAT — especially
       on Japanese networks.  created_at is refreshed each time the client
       re-registers, so a recent value means the device was alive recently.
    """
    online_ips = _online_ips()
    status: dict[str, str] = {}

    if HBBS_DB_PATH.exists():
        conn = sqlite3.connect(HBBS_DB_PATH)
        conn.row_factory = sqlite3.Row
        now_utc = datetime.now(timezone.utc)

        for peer in conn.execute("SELECT id, info, created_at FROM peer").fetchall():
            info = json.loads(peer["info"] or "{}")
            peer_ip = info.get("ip", "").replace("::ffff:", "")

            tcp_online = peer_ip in online_ips

            recently_registered = False
            if peer["created_at"]:
                try:
                    ts = datetime.fromisoformat(peer["created_at"]).replace(tzinfo=timezone.utc)
                    recently_registered = (now_utc - ts).total_seconds() < 1800  # 30 min
                except Exception:
                    pass

            status[peer["id"]] = "online" if (tcp_online or recently_registered) else "offline"

        conn.close()

    return JSONResponse({"devices": status})


@router.get("/devices")
async def api_devices_list(
    group: str = "",
    _: dict = Depends(require_auth),
):
    """Return the full device list as JSON for dynamic table updates."""
    devices, peer_count = get_devices(group)
    return JSONResponse({"devices": devices, "peer_count": peer_count})
