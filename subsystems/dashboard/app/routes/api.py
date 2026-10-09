import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.auth import require_auth
from app.deps import get_db, get_devices, log_event
from app.notifications import fire_notification

HBBS_DB_PATH = Path("/opt/rustdesk-fleet/data/db_v2.sqlite3")
HBBS_PORTS = {"21115", "21116", "21117", "21118", "21119"}

# How long to keep a device marked online after its IP was last seen in an
# established TCP connection.  RustDesk reconnects every ~12-30 s; 90 s
# gives a comfortable margin without marking genuinely-offline devices online.
_ONLINE_GRACE_S = 90

# Client heartbeats (routes/client_api.py) arrive every ~15 s, every 3 s while
# a session is open. For devices that report, the heartbeat is authoritative.
_HEARTBEAT_ONLINE_S = 60
_HEARTBEAT_SESSION_S = 30
# Devices that have heartbeated within this window are "reporting" devices.
_HEARTBEAT_REPORTING_S = 86400
# If *no* device has heartbeated this recently, assume the reporting channel
# itself is down (e.g. port 21114 closed) and fall back to the old signals,
# rather than marking every device offline at once.
_HEARTBEAT_CHANNEL_S = 120

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


def _heartbeat_ages() -> dict[str, tuple[float, int]]:
    """{rustdesk_id: (seconds since last heartbeat, active conns)} for reporting
    devices — or {} when the reporting channel looks down as a whole."""
    try:
        conn = get_db()
        rows = conn.execute(
            """SELECT rustdesk_id, active_conns,
                      (julianday('now') - julianday(heartbeat_at)) * 86400 AS age
               FROM device_info WHERE heartbeat_at IS NOT NULL"""
        ).fetchall()
        conn.close()
    except Exception:
        return {}
    ages = {r["rustdesk_id"]: (r["age"], r["active_conns"] or 0)
            for r in rows if r["age"] is not None and r["age"] < _HEARTBEAT_REPORTING_S}
    if not ages or min(a for a, _ in ages.values()) > _HEARTBEAT_CHANNEL_S:
        return {}
    return ages


def _compute_status() -> dict[str, str]:
    """Return {rustdesk_id: "online"|"in session"|"offline"} for every known peer.

    Two signals are combined:
    1. Live TCP connection to an hbbs port seen within the grace period.
    2. created_at in the hbbs peer table updated within the last 15 minutes.
       RustDesk sessions often go P2P (bypassing our server), so the client's
       TCP connection to hbbs becomes idle and gets killed by NAT — especially
       on Japanese networks.  created_at is refreshed each time the client
       re-registers, so a recent value means the device was alive recently.

    Shared by the /devices/status endpoint and the offline-notification
    background watcher below, so both agree on what "online" means.
    """
    online_ips = _online_ips()
    status: dict[str, str] = {}
    heartbeats = _heartbeat_ages()

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

            hb = heartbeats.get(peer["id"])
            if hb is not None:
                age, conns = hb
                if age < _HEARTBEAT_ONLINE_S:
                    fresh_session = conns > 0 and age < _HEARTBEAT_SESSION_S
                    status[peer["id"]] = "in session" if fresh_session else "online"
                else:
                    status[peer["id"]] = "offline"
                continue

            status[peer["id"]] = "online" if (tcp_online or recently_registered) else "offline"

        conn.close()

    return status


@router.get("/devices/status")
async def devices_status(_: dict = Depends(require_auth)):
    return JSONResponse({"devices": _compute_status()})


# ── Offline-transition watcher ──────────────────────────────────────────────
# Fires a "device_offline" notification the first time a device that was
# online drops to offline. Runs on its own, coarser interval (separate from
# the 1s ss-poll above) so a brief blip doesn't fire a notification, and so
# it's decoupled from the per-request /devices/status calls.

_OFFLINE_WATCH_INTERVAL_S = 30

_prev_status: dict[str, str] | None = None
_status_lock = threading.Lock()


def _check_offline_transitions() -> None:
    global _prev_status
    current = _compute_status()

    with _status_lock:
        previous = _prev_status
        _prev_status = current

    if previous is None:
        return  # first run — nothing to compare against yet

    newly_offline = [
        rid for rid, state in current.items()
        if state == "offline" and previous.get(rid) in ("online", "in session")
    ]
    if not newly_offline:
        return

    devices, _ = get_devices()
    by_id = {d["rustdesk_id"]: d for d in devices}

    conn = get_db()
    for rid in newly_offline:
        info = by_id.get(rid)
        if info is None:
            continue  # hidden/unregistered — nothing useful to notify about
        log_event(conn, "device_offline", rid, "")
        fire_notification("device_offline", {
            "rustdesk_id": rid,
            "label": info.get("label") or "",
            "group_name": info.get("group_name") or "",
            "last_seen": info.get("last_seen") or "",
        })
    conn.commit()
    conn.close()


def _bg_offline_watch_loop() -> None:
    from app.routes.client_api import sweep_stale_sessions
    while True:
        try:
            _check_offline_transitions()
        except Exception:
            pass
        try:
            sweep_stale_sessions()   # close sessions for devices that vanished mid-session
        except Exception:
            pass
        time.sleep(_OFFLINE_WATCH_INTERVAL_S)


threading.Thread(target=_bg_offline_watch_loop, daemon=True, name="device-offline-watch").start()


@router.get("/devices")
async def api_devices_list(
    group: str = "",
    _: dict = Depends(require_auth),
):
    """Return the full device list as JSON for dynamic table updates."""
    devices, peer_count = get_devices(group)
    return JSONResponse({"devices": devices, "peer_count": peer_count})


@router.get("/devices/{rid}/sessions")
async def api_device_sessions(rid: str, _: dict = Depends(require_auth)):
    """Recent remote-access sessions for one device (for the detail modal)."""
    from app.routes.sessions import _LIST_SQL, fmt_duration
    conn = get_db()
    rows = conn.execute(_LIST_SQL.format(where="WHERE s.rustdesk_id = ?"), (rid, 20)).fetchall()
    conn.close()
    return JSONResponse({"sessions": [
        {
            "started_at": r["started_at"],
            "ended_at": r["ended_at"],
            "active": r["ended_at"] is None,
            "duration_str": fmt_duration(r["duration_s"]),
        }
        for r in rows
    ]})


# ── Server health (host + containers) ────────────────────────────────────────
# Lightweight, dependency-free metrics for the Server Status dashboard tiles.
# Everything here reads /proc, statvfs or `docker ps` — nothing blocks for long
# (psutil isn't installed on the box, so we stick to the stdlib).

_DATA_DIR = Path("/opt/rustdesk-fleet")

_cpu_prev: tuple[int, int] | None = None   # (busy, total) jiffies from last sample
_cpu_lock = threading.Lock()


def _cpu_percent() -> float | None:
    """Percent CPU busy since the previous call (None on the very first call)."""
    global _cpu_prev
    try:
        with open("/proc/stat") as f:
            vals = [int(x) for x in f.readline().split()[1:]]   # user nice system idle iowait…
    except Exception:
        return None
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)          # idle + iowait
    total = sum(vals)
    busy = total - idle
    with _cpu_lock:
        prev = _cpu_prev
        _cpu_prev = (busy, total)
    if prev is None or total - prev[1] <= 0:
        return None
    return round((busy - prev[0]) / (total - prev[1]) * 100, 1)


def _meminfo() -> dict[str, int]:
    out: dict[str, int] = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, rest = line.partition(":")
                out[k] = int(rest.split()[0])      # kB
    except Exception:
        pass
    return out


def _uptime_s() -> float | None:
    try:
        with open("/proc/uptime") as f:
            return float(f.readline().split()[0])
    except Exception:
        return None


def _container_states() -> dict[str, dict]:
    """{'hbbs': {running, status}, 'hbbr': {...}} from `docker ps`."""
    names = ("hbbs", "hbbr")
    states = {n: {"running": False, "status": "not running"} for n in names}
    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"],
            capture_output=True, text=True, timeout=3,
        )
    except Exception:
        return {n: {"running": None, "status": "unknown"} for n in names}
    for line in result.stdout.splitlines():
        name, _, status = line.partition("\t")
        if name in states:
            states[name] = {"running": True, "status": status or "running"}
    return states


def _pct(used: float, total: float) -> float | None:
    return round(used / total * 100, 1) if total else None


def _server_health() -> dict:
    mem = _meminfo()
    mem_total = mem.get("MemTotal", 0)
    mem_used = mem_total - mem.get("MemAvailable", 0) if mem_total else 0
    swap_total = mem.get("SwapTotal", 0)
    swap_used = swap_total - mem.get("SwapFree", 0) if swap_total else 0

    try:
        du = shutil.disk_usage(_DATA_DIR if _DATA_DIR.exists() else "/")
    except Exception:
        du = None

    try:
        load1, load5, load15 = os.getloadavg()
    except Exception:
        load1 = load5 = load15 = None

    status = _compute_status()
    online = sum(1 for s in status.values() if s in ("online", "in session"))

    try:
        from app.notifications import get_settings
        s = get_settings()
        mem_thr = int(s.get("mem_threshold") or 90)
        disk_thr = int(s.get("disk_threshold") or 90)
    except Exception:
        mem_thr = disk_thr = None

    return {
        "cpu": {
            "pct": _cpu_percent(),
            "cores": os.cpu_count(),
            "load1": round(load1, 2) if load1 is not None else None,
            "load5": round(load5, 2) if load5 is not None else None,
            "load15": round(load15, 2) if load15 is not None else None,
        },
        "mem": {"used_kb": mem_used, "total_kb": mem_total, "pct": _pct(mem_used, mem_total)},
        "swap": {"used_kb": swap_used, "total_kb": swap_total, "pct": _pct(swap_used, swap_total)},
        "disk": ({"used_b": du.used, "total_b": du.total, "pct": _pct(du.used, du.total)}
                 if du else {"used_b": 0, "total_b": 0, "pct": None}),
        "uptime_s": _uptime_s(),
        "containers": _container_states(),
        "devices": {"online": online, "total": len(status)},
        "thresholds": {"mem": mem_thr, "disk": disk_thr},
    }


# Prime the CPU sampler so the first dashboard request returns a real figure.
_cpu_percent()


@router.get("/server/health")
async def server_health(_: dict = Depends(require_auth)):
    return JSONResponse(_server_health())


# ── Server-health threshold alerts ───────────────────────────────────────────
# Periodically evaluates memory/disk against the thresholds configured on the
# Notifications page and fires the memory_high / disk_high email alerts. Edge-
# triggered with a short debounce and hysteresis so a crossing emails once, not
# every cycle, and a brief spike doesn't alert.

_HEALTH_ALERT_INTERVAL_S = 60
_HEALTH_ALERT_DEBOUNCE = 2     # consecutive readings above threshold before firing
_HEALTH_ALERT_HYSTERESIS = 5   # must fall this many points below to re-arm

_health_alert_state = {
    "memory_high": {"high": False, "count": 0},
    "disk_high":   {"high": False, "count": 0},
}


def _fmt_kb(kb: int) -> str:
    mb = kb / 1024
    return f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{round(mb)} MB"


def _fmt_b(b: int) -> str:
    gb = b / 1e9
    return f"{gb:.1f} GB" if gb >= 1 else f"{round(b / 1e6)} MB"


def _server_host() -> str:
    try:
        conn = get_db()
        row = conn.execute("SELECT host FROM server_config WHERE id = 1").fetchone()
        conn.close()
        return row["host"] if row else ""
    except Exception:
        return ""


def _check_health_thresholds() -> None:
    from app.notifications import get_settings, fire_notification

    settings = get_settings()
    if not settings.get("enabled"):
        for st in _health_alert_state.values():   # don't fire a stale crossing on re-enable
            st["count"] = 0
        return

    health = _server_health()
    host = _server_host() or "—"
    mem = health.get("mem") or {}
    disk = health.get("disk") or {}

    checks = [
        ("memory_high", mem.get("pct"), int(settings.get("mem_threshold") or 90),
         f"{_fmt_kb(mem.get('used_kb', 0))} / {_fmt_kb(mem.get('total_kb', 0))}" if mem.get("total_kb") else "—"),
        ("disk_high", disk.get("pct"), int(settings.get("disk_threshold") or 90),
         f"{_fmt_b(disk.get('used_b', 0))} / {_fmt_b(disk.get('total_b', 0))}" if disk.get("total_b") else "—"),
    ]
    for event, pct, threshold, usage in checks:
        if pct is None:
            continue
        st = _health_alert_state[event]
        st["count"] = st["count"] + 1 if pct >= threshold else 0
        if not st["high"] and st["count"] >= _HEALTH_ALERT_DEBOUNCE:
            st["high"] = True
            # fire_notification also honours the per-event on/off toggle and SMTP config.
            fire_notification(event, {
                "pct": pct, "pct_str": f"{pct}%", "threshold": threshold,
                "usage": usage, "host": host,
            })
        elif st["high"] and pct < max(threshold - _HEALTH_ALERT_HYSTERESIS, 0):
            st["high"] = False
            st["count"] = 0


def _bg_health_alert_loop() -> None:
    time.sleep(10)   # let the app finish starting and the CPU sampler settle
    while True:
        try:
            _check_health_thresholds()
        except Exception:
            pass
        time.sleep(_HEALTH_ALERT_INTERVAL_S)


threading.Thread(target=_bg_health_alert_loop, daemon=True, name="health-alert-watch").start()
