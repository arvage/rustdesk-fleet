import dataclasses
import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app import paths
from app.auth import require_auth
from app.deps import device_known, get_db, get_devices, get_hbbs_peers, log_event
from app.notifications import fire_notification
from app.tenancy import Tenant, active_tenants, get_tenant, is_provider_home

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

# {(local port, remote ip): last_seen monotonic timestamp} — written by the
# background thread, read by the API handler. Keyed by port so each tenant's
# relay (its own ports) only sees its own connections. Lock guards access.
_ip_last_seen: dict[tuple[int, str], float] = {}
_lock = threading.Lock()

# Relay ports to watch: every active tenant's, refreshed now and then.
_PORTS_REFRESH_S = 30
_watched_ports: frozenset[int] = frozenset()
_watched_at = 0.0


def _relay_ports() -> frozenset[int]:
    global _watched_ports, _watched_at
    now = time.monotonic()
    if now - _watched_at >= _PORTS_REFRESH_S or not _watched_ports:
        try:
            ports: set[int] = set()
            for t in active_tenants():
                if t.relay:
                    ports |= t.relay.ports
            _watched_ports = frozenset(ports)
        except Exception:
            pass
        _watched_at = now
    return _watched_ports


def _poll_once() -> None:
    """Run ss and update _ip_last_seen for every relay-connected remote IP."""
    ports = _relay_ports()
    if not ports:
        return
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
        try:
            local_port = int(parts[2].rsplit(":", 1)[-1])
        except ValueError:
            continue
        if local_port not in ports:
            continue
        remote_addr = parts[3].rsplit(":", 1)[0].strip("[]")
        if remote_addr.startswith("::ffff:"):
            remote_addr = remote_addr[7:]
        with _lock:
            _ip_last_seen[(local_port, remote_addr)] = now


def _bg_poll_loop() -> None:
    """Background daemon thread — polls ss every second so we catch even
    sub-second TCP connection windows that a 10-second frontend poll would miss."""
    while True:
        _poll_once()
        time.sleep(1)


# Start background polling immediately when the module is imported.
threading.Thread(target=_bg_poll_loop, daemon=True, name="hbbs-ss-poll").start()

router = APIRouter(prefix="/api")


def _online_ips(tenant: Tenant) -> set[str]:
    """IPs seen connected to one of the tenant's relay ports within the grace period."""
    if tenant.relay is None:
        return set()
    ports = tenant.relay.ports
    cutoff = time.monotonic() - _ONLINE_GRACE_S
    with _lock:
        return {ip for (port, ip), ts in _ip_last_seen.items() if ts >= cutoff and port in ports}


def _heartbeat_ages(tenant_id: int) -> dict[str, tuple[float, int]]:
    """{rustdesk_id: (seconds since last heartbeat, active conns)} for the
    tenant's reporting devices — or {} when its reporting channel looks down
    as a whole."""
    try:
        conn = get_db()
        rows = conn.execute(
            """SELECT rustdesk_id, active_conns,
                      (julianday('now') - julianday(heartbeat_at)) * 86400 AS age
               FROM device_info WHERE tenant_id = ? AND heartbeat_at IS NOT NULL""",
            (tenant_id,),
        ).fetchall()
        conn.close()
    except Exception:
        return {}
    ages = {r["rustdesk_id"]: (r["age"], r["active_conns"] or 0)
            for r in rows if r["age"] is not None and r["age"] < _HEARTBEAT_REPORTING_S}
    if not ages or min(a for a, _ in ages.values()) > _HEARTBEAT_CHANNEL_S:
        return {}
    return ages


def _compute_status(tenant: Tenant) -> dict[str, str]:
    """Return {rustdesk_id: "online"|"in session"|"offline"} for every peer
    registered with the tenant's relay.

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
    online_ips = _online_ips(tenant)
    status: dict[str, str] = {}
    heartbeats = _heartbeat_ages(tenant.id)

    hbbs_db = tenant.hbbs_db
    if hbbs_db is not None and hbbs_db.exists():
        conn = sqlite3.connect(f"file:{hbbs_db}?mode=ro", uri=True, timeout=2)
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
async def devices_status(_: dict = Depends(require_auth), tenant: Tenant = Depends(get_tenant)):
    return JSONResponse({"devices": _compute_status(tenant)})


# ── Offline-transition watcher ──────────────────────────────────────────────
# Fires a "device_offline" notification the first time a device that was
# online drops to offline. Runs on its own, coarser interval (separate from
# the 1s ss-poll above) so a brief blip doesn't fire a notification, and so
# it's decoupled from the per-request /devices/status calls. Each tenant has
# its own baseline, events and recipients.

_OFFLINE_WATCH_INTERVAL_S = 30

_prev_status: dict[int, dict[str, str]] = {}
_status_lock = threading.Lock()


def _check_offline_transitions(tenant: Tenant) -> None:
    current = _compute_status(tenant)

    with _status_lock:
        previous = _prev_status.get(tenant.id)
        _prev_status[tenant.id] = current

    if previous is None:
        return  # first run for this tenant — nothing to compare against yet

    newly_offline = [
        rid for rid, state in current.items()
        if state == "offline" and previous.get(rid) in ("online", "in session")
    ]
    # Only offline -> online counts. A device missing from the previous pass is
    # new, and the device_registered notification covers it.
    newly_online = [
        rid for rid, state in current.items()
        if state in ("online", "in session") and previous.get(rid) == "offline"
    ]
    if not newly_offline and not newly_online:
        return

    devices, _ = get_devices(tenant)
    by_id = {d["rustdesk_id"]: d for d in devices}

    conn = get_db()
    for rid in newly_offline:
        info = by_id.get(rid)
        if info is None:
            continue  # hidden/unregistered — nothing useful to notify about
        log_event(conn, "device_offline", rid, "", tenant_id=tenant.id)
        fire_notification(tenant.id, "device_offline", {
            "rustdesk_id": rid,
            "label": info.get("label") or "",
            "group_name": info.get("group_name") or "",
            "last_seen": info.get("last_seen") or "",
        })
    for rid in newly_online:
        info = by_id.get(rid)
        if info is None:
            continue
        log_event(conn, "device_online", rid, "", tenant_id=tenant.id)
        fire_notification(tenant.id, "device_online", {
            "rustdesk_id": rid,
            "name": info.get("label") or info.get("hostname") or rid,
            "label": info.get("label") or "",
            "hostname": info.get("hostname") or "",
            "group_name": info.get("group_name") or "",
            "ip": info.get("ip") or "",
            "online_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        })
    conn.commit()
    conn.close()


# ── New-device watcher ──────────────────────────────────────────────────────
# Devices register with the relay (hbbs), not the dashboard, so nothing else
# notices a new one until someone edits it. Each pass, any hbbs peer without a
# fleet row gets one (unassigned, unlabeled) and fires "device_registered".
# The fleet row is the "already announced" marker: later edits/assignments
# update it rather than re-notifying, and deleted (hidden) devices keep their
# row so they don't re-announce either.

def _check_new_devices(tenant: Tenant) -> None:
    peers = get_hbbs_peers(tenant)
    if not peers:
        return
    tid = tenant.id
    conn = get_db()
    known = {r[0] for r in conn.execute(
        "SELECT rustdesk_id FROM devices WHERE tenant_id = ? AND rustdesk_id IS NOT NULL", (tid,))}
    new = [rid for rid in peers if rid not in known]
    announced = []
    for rid in new:
        # OR IGNORE: a tech may have just created the row from the UI.
        if conn.execute(
            "INSERT OR IGNORE INTO devices (tenant_id, rustdesk_id, status, last_seen)"
            " VALUES (?, ?, 'registered', datetime('now'))",
            (tid, rid),
        ).rowcount:
            log_event(conn, "device_registered", rid, "", tenant_id=tid)
            announced.append(rid)
    conn.commit()
    info = {}
    if announced:
        marks = ",".join("?" * len(announced))
        info = {r["rustdesk_id"]: r for r in conn.execute(
            f"SELECT rustdesk_id, hostname FROM device_info WHERE tenant_id = ? AND rustdesk_id IN ({marks})",
            (tid, *announced))}
    conn.close()
    for rid in announced:
        peer = peers[rid]
        fire_notification(tid, "device_registered", {
            "rustdesk_id": rid,
            "label": "",
            "group_name": "",
            "hostname": (info[rid]["hostname"] if rid in info else "") or "",
            "ip": peer.get("ip") or "—",
            "registered_at": peer.get("registered_at") or "",
        })


def watch_tenant_devices(tenant: Tenant) -> None:
    """One pass of the device watcher for one tenant."""
    from app.routes.client_api import sweep_stale_sessions
    for step in (_check_new_devices, _check_offline_transitions, sweep_stale_sessions):
        try:
            step(tenant)   # sweep: close sessions for devices that vanished mid-session
        except Exception:
            pass


def _bg_offline_watch_loop() -> None:
    while True:
        try:
            tenants = active_tenants()
        except Exception:
            tenants = []
        for tenant in tenants:
            watch_tenant_devices(tenant)
        time.sleep(_OFFLINE_WATCH_INTERVAL_S)


threading.Thread(target=_bg_offline_watch_loop, daemon=True, name="device-offline-watch").start()


@router.get("/devices")
async def api_devices_list(
    group: str = "",
    _: dict = Depends(require_auth),
    tenant: Tenant = Depends(get_tenant),
):
    """Return the full device list as JSON for dynamic table updates."""
    devices, peer_count = get_devices(tenant, group)
    return JSONResponse({"devices": devices, "peer_count": peer_count})


@router.post("/devices/{rid}/connect")
async def api_device_connect(rid: str, current_user: dict = Depends(require_auth),
                             tenant: Tenant = Depends(get_tenant)):
    """Record that this dashboard user clicked Connect/Terminal on a device,
    so the session that follows can be credited to them."""
    conn = get_db()
    if not device_known(conn, tenant, rid):
        conn.close()
        return JSONResponse({"ok": False, "error": "Device not found."}, status_code=404)
    conn.execute(
        "INSERT INTO connect_clicks (tenant_id, rustdesk_id, user_email) VALUES (?, ?, ?)",
        (tenant.id, rid, current_user["email"]),
    )
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})


@router.get("/devices/{rid}/sessions")
async def api_device_sessions(rid: str, current_user: dict = Depends(require_auth)):
    """Recent remote-access sessions for one device (for the detail modal).
    Another tenant's id just has no sessions here."""
    from app.routes.sessions import _LIST_SQL, _row
    conn = get_db()
    rows = conn.execute(_LIST_SQL.format(where="AND s.rustdesk_id = ?"),
                        (current_user["tenant_id"], rid, 20)).fetchall()
    conn.close()
    return JSONResponse({"sessions": [
        {k: s[k] for k in ("started_at", "ended_at", "active", "duration_str",
                           "peer_id", "peer_name", "peer_device_name", "peer_ip", "conn_type_label",
                           "dashboard_user")}
        for s in map(_row, rows)
    ]})


# ── Server health (host + containers) ────────────────────────────────────────
# Lightweight, dependency-free metrics for the Server Status dashboard tiles.
# Everything here reads /proc, statvfs or `docker ps` — nothing blocks for long
# (psutil isn't installed on the box, so we stick to the stdlib).

_DATA_DIR = paths.DATA_DIR

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


def _container_states(tenant: Tenant) -> dict[str, dict]:
    """{'hbbs': {running, status}, 'hbbr': {...}} for the tenant's relay
    containers (named in its compose file), from `docker ps`."""
    from setup_server import relay_containers
    relay = dataclasses.asdict(tenant.relay) if tenant.relay else None
    by_role = {("hbbr" if "hbbr" in n else "hbbs"): n for n in relay_containers(relay)}
    states = {role: {"running": False, "status": "not running"} for role in ("hbbs", "hbbr")}
    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"],
            capture_output=True, text=True, timeout=3,
        )
    except Exception:
        return {role: {"running": None, "status": "unknown"} for role in states}
    running = dict(line.partition("\t")[::2] for line in result.stdout.splitlines())
    for role, name in by_role.items():
        if name in running:
            states[role] = {"running": True, "status": running[name] or "running"}
    return states


def _pct(used: float, total: float) -> float | None:
    return round(used / total * 100, 1) if total else None


def _server_health(tenant: Tenant) -> dict:
    """The tenant's relay containers and devices online. Host metrics (CPU,
    memory, disk) are about our server, so only the provider home gets them."""
    status = _compute_status(tenant)
    online = sum(1 for s in status.values() if s in ("online", "in session"))
    tenant_part = {
        "containers": _container_states(tenant),
        "devices": {"online": online, "total": len(status)},
    }
    if not is_provider_home(tenant):
        return tenant_part
    return {**_host_health(tenant.id), **tenant_part}


def _host_health(tenant_id: int) -> dict:
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

    try:
        from app.notifications import get_settings
        s = get_settings(tenant_id)
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
        "thresholds": {"mem": mem_thr, "disk": disk_thr},
    }


# Prime the CPU sampler so the first dashboard request returns a real figure.
_cpu_percent()


@router.get("/server/health")
async def server_health(_: dict = Depends(require_auth), tenant: Tenant = Depends(get_tenant)):
    return JSONResponse(_server_health(tenant))


# ── Server-health threshold alerts ───────────────────────────────────────────
# Periodically evaluates memory/disk against the thresholds configured on the
# Notifications page and fires the memory_high / disk_high email alerts. Edge-
# triggered with a short debounce and hysteresis so a crossing emails once, not
# every cycle, and a brief spike doesn't alert. This is about our server, so
# it uses (and alerts) the provider home tenant only (TENANCY.md).

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


def _check_health_thresholds() -> None:
    from app.notifications import get_settings, fire_notification
    from app.tenancy import LEGACY_TENANT_ID, load_tenant

    home = load_tenant(LEGACY_TENANT_ID)
    if home is None:
        return
    settings = get_settings(home.id)
    if not settings.get("enabled"):
        for st in _health_alert_state.values():   # don't fire a stale crossing on re-enable
            st["count"] = 0
        return

    health = _host_health(home.id)
    host = (home.relay.host if home.relay else "") or "—"
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
            fire_notification(home.id, event, {
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
