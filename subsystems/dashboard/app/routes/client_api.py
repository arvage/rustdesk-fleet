"""
Receiver for the RustDesk client's built-in "API server" reporting.

The community RustDesk client (src/hbbs_http/sync.rs) POSTs to its API
server — `api-server` if configured, otherwise http://<rendezvous-host>:21114:

  /api/sysinfo    hostname, os, username, cpu, memory, client version
                  (on start, every 120 s until accepted, and when the
                  logged-in user changes)
  /api/heartbeat  id + version every ~15 s, every 3 s while sessions are open
                  (with "conns": the active connection ids)

These endpoints are unauthenticated by design (the client sends no
credentials), so every report is checked against the hbbs peer table: the
id must be registered and the uuid must match the one hbbs has on file.

The heartbeat response can also push config ("strategy") or kill sessions
("disconnect"). We never send "disconnect", and the only config we ever push
is the device's group policy: `allow-auto-update` (client_groups.auto_update)
and `allow-remote-config-modification` (client_groups.remote_config), each
only when the group manages it. The client echoes our "modified_at"
(client_groups.policy_ts) back on later heartbeats once it has applied a
strategy, so each policy change is sent once.
"""

import base64
import json
import sqlite3
import threading
import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from app.deps import get_db, log_event

HBBS_DB_PATH = Path("/opt/rustdesk-fleet/data/db_v2.sqlite3")
_MAX_BODY = 64 * 1024

# Keys the client may include that we never want to store (preset
# address-book password etc.). Anything containing "password" is dropped too.
_STORED_FIELDS = ("hostname", "os", "username", "cpu", "memory", "version")

router = APIRouter()

# client_groups column -> RustDesk config option it controls. These are the
# only options ever pushed to clients.
_MANAGED_OPTIONS = {
    "auto_update": "allow-auto-update",
    "remote_config": "allow-remote-config-modification",
}

# ── hbbs peer lookup (cached briefly; heartbeats arrive every few seconds) ───

_PEER_CACHE_TTL_S = 30
_peer_cache: dict[str, bytes] = {}
_peer_cache_at = 0.0
_peer_lock = threading.Lock()


def _peer_uuids() -> dict[str, bytes]:
    global _peer_cache, _peer_cache_at
    with _peer_lock:
        if time.monotonic() - _peer_cache_at < _PEER_CACHE_TTL_S:
            return _peer_cache
        cache: dict[str, bytes] = {}
        if HBBS_DB_PATH.exists():
            try:
                conn = sqlite3.connect(f"file:{HBBS_DB_PATH}?mode=ro", uri=True, timeout=2)
                for rid, uuid in conn.execute("SELECT id, uuid FROM peer"):
                    cache[rid] = bytes(uuid) if uuid is not None else b""
                conn.close()
            except Exception:
                return _peer_cache  # keep serving the last good copy
        _peer_cache, _peer_cache_at = cache, time.monotonic()
        return cache


def _verify(payload: dict) -> str | None:
    """Return the RustDesk id if the report matches a registered peer, else None."""
    rid = str(payload.get("id") or "").strip()
    uuid_b64 = payload.get("uuid") or ""
    if not rid or not uuid_b64:
        return None
    try:
        sent_uuid = base64.b64decode(uuid_b64, validate=False)
    except Exception:
        return None
    peers = _peer_uuids()
    if rid not in peers:
        # A brand-new device may register moments before reporting; refresh once.
        global _peer_cache_at
        with _peer_lock:
            _peer_cache_at = 0.0
        peers = _peer_uuids()
    expected = peers.get(rid)
    if not expected or expected != sent_uuid:
        return None
    return rid


async def _read_json(request: Request) -> dict | None:
    body = await request.body()
    if len(body) > _MAX_BODY:
        return None
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _client_ip(request: Request) -> str:
    ip = request.headers.get("x-real-ip") or (request.client.host if request.client else "")
    return ip.replace("::ffff:", "")


# ── Session audit (see deps.device_sessions) ─────────────────────────────────
# RustDesk's heartbeat carries "conns": the connection ids active on the device
# right now. We turn those into a session log: a conn id that newly appears opens
# a session, one that disappears closes it (at the last time we saw it active).
# Devices that stop heartbeating mid-session are closed by sweep_stale_sessions.

# How long a session may go without a heartbeat confirming it before the sweeper
# closes it. Heartbeats arrive every ~3 s during a session, ~15 s otherwise.
SESSION_STALE_S = 120


def _reconcile_sessions(conn, rid: str, conns: list) -> None:
    """Open/close device_sessions rows to match the conns in this heartbeat."""
    current = {str(c) for c in conns}
    open_rows = conn.execute(
        "SELECT id, conn_ref FROM device_sessions WHERE rustdesk_id = ? AND ended_at IS NULL",
        (rid,),
    ).fetchall()
    open_refs = {r["conn_ref"]: r["id"] for r in open_rows}

    # Close sessions whose conn is no longer active (end at last confirmed time).
    for ref, sid in open_refs.items():
        if ref not in current:
            conn.execute(
                "UPDATE device_sessions SET ended_at = last_seen_at WHERE id = ?", (sid,)
            )
    # Touch still-active sessions; open newly-seen ones.
    for ref in current:
        if ref in open_refs:
            conn.execute(
                "UPDATE device_sessions SET last_seen_at = datetime('now') WHERE id = ?",
                (open_refs[ref],),
            )
        else:
            conn.execute(
                "INSERT INTO device_sessions (rustdesk_id, conn_ref) VALUES (?, ?)",
                (rid, ref),
            )


def sweep_stale_sessions() -> int:
    """Close sessions for devices that stopped heartbeating mid-session.
    Returns the number of sessions closed. Safe to call periodically."""
    conn = get_db()
    try:
        cur = conn.execute(
            f"""UPDATE device_sessions SET ended_at = last_seen_at
                WHERE ended_at IS NULL
                  AND last_seen_at < datetime('now', '-{SESSION_STALE_S} seconds')"""
        )
        conn.commit()
        return cur.rowcount or 0
    finally:
        conn.close()


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.post("/api/sysinfo")
async def client_sysinfo(request: Request):
    data = await _read_json(request)
    rid = _verify(data) if data else None
    if rid is None:
        # Any reply other than SYSINFO_UPDATED / ID_NOT_FOUND makes the client
        # back off for 120 s (ID_NOT_FOUND would make it retry every 3 s).
        return PlainTextResponse("REJECTED")

    fields = {k: str(data.get(k) or "")[:300] for k in _STORED_FIELDS}
    extra = {
        k: v for k, v in data.items()
        if k not in _STORED_FIELDS and k not in ("id", "uuid") and "password" not in k.lower()
    }
    conn = get_db()
    conn.execute(
        """INSERT INTO device_info
               (rustdesk_id, hostname, os, username, cpu, memory, client_version,
                extra, report_ip, sysinfo_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
           ON CONFLICT(rustdesk_id) DO UPDATE SET
               hostname = excluded.hostname, os = excluded.os,
               username = excluded.username, cpu = excluded.cpu,
               memory = excluded.memory, client_version = excluded.client_version,
               extra = excluded.extra, report_ip = excluded.report_ip,
               sysinfo_at = excluded.sysinfo_at""",
        (rid, fields["hostname"], fields["os"], fields["username"], fields["cpu"],
         fields["memory"], fields["version"], json.dumps(extra)[:4000] if extra else None,
         _client_ip(request)),
    )
    conn.commit()
    conn.close()
    return PlainTextResponse("SYSINFO_UPDATED")


@router.post("/api/sysinfo_ver")
async def client_sysinfo_ver(request: Request):
    # Only used by clients for rustdesk.com-hosted servers; harmless to answer.
    return PlainTextResponse("")


@router.post("/api/heartbeat")
async def client_heartbeat(request: Request):
    data = await _read_json(request)
    rid = _verify(data) if data else None
    if rid is None:
        return JSONResponse({})

    conns = data.get("conns") if isinstance(data.get("conns"), list) else []
    try:
        client_policy_ts = int(data.get("modified_at") or 0)
    except (TypeError, ValueError):
        client_policy_ts = 0
    conn = get_db()
    cur = conn.execute(
        """UPDATE device_info
           SET heartbeat_at = datetime('now'), active_conns = ?, report_ip = ?, policy_ts = ?
           WHERE rustdesk_id = ?""",
        (len(conns), _client_ip(request), client_policy_ts, rid),
    )
    need_sysinfo = cur.rowcount == 0
    if need_sysinfo:
        conn.execute(
            """INSERT INTO device_info (rustdesk_id, heartbeat_at, active_conns, report_ip, policy_ts)
               VALUES (?, datetime('now'), ?, ?, ?)""",
            (rid, len(conns), _client_ip(request), client_policy_ts),
        )
    else:
        need_sysinfo = conn.execute(
            "SELECT sysinfo_at IS NULL FROM device_info WHERE rustdesk_id = ?", (rid,)
        ).fetchone()[0]
    # A verified heartbeat means this device is genuinely communicating again.
    # If it had been deleted (hidden), un-hide it so it re-registers on its own.
    unhidden = conn.execute(
        "UPDATE devices SET hidden = 0, status = 'registered' WHERE rustdesk_id = ? AND hidden = 1",
        (rid,),
    ).rowcount
    if unhidden:
        log_event(conn, "device_reappeared", rid, "")
    _reconcile_sessions(conn, rid, conns)
    policy = conn.execute(
        """SELECT cg.auto_update, cg.remote_config, cg.policy_ts
           FROM devices d JOIN client_groups cg ON cg.id = d.group_id
           WHERE d.rustdesk_id = ? AND d.hidden = 0""",
        (rid,),
    ).fetchone()
    conn.commit()
    conn.close()

    # Asking for sysinfo is safe: it only makes the client re-send its details.
    resp: dict = {"sysinfo": True} if need_sysinfo else {}
    if policy and policy["policy_ts"] and client_policy_ts != policy["policy_ts"]:
        options = {
            key: policy[col]
            for col, key in _MANAGED_OPTIONS.items()
            if policy[col] in ("Y", "N")
        }
        if options:
            resp["modified_at"] = policy["policy_ts"]
            resp["strategy"] = {"config_options": options}
    return JSONResponse(resp)
