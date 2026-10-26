"""
Receiver for the RustDesk client's built-in "API server" reporting.

The community RustDesk client (src/hbbs_http/sync.rs) POSTs to its API
server — `api-server` if configured, otherwise http://<rendezvous-host>:21114:

  /api/sysinfo    hostname, os, username, cpu, memory, client version
                  (on start, every 120 s until accepted, and when the
                  logged-in user changes)
  /api/heartbeat  id + version every ~15 s, every 3 s while sessions are open
                  (with "conns": the active connection ids)
  /api/audit/conn src/server/connection.rs, per incoming connection:
                  {"action":"new","ip":…} when it opens, {"peer":[id,name],
                  "type":…} once authorized, {"action":"close"} when it ends;
                  each tagged with the device id/uuid and "conn_id"
  /api/audit/file per file transfer or clipboard file paste: direction
                  ("type" 0 = sent from the device, 1 = received), "path",
                  "peer_id", "conn_id", and "info" (a JSON string with the
                  peer's ip/name, file count and the largest ≤10 files)

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

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from app import paths
from app.deps import get_db, log_event

HBBS_DB_PATH = paths.HBBS_DB
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
# An unknown id forces an early re-read, but no more often than this, so a
# stream of reports with made-up ids can't make every request hit hbbs's DB.
_FORCED_REFRESH_MIN_S = 5
_peer_cache: dict[str, bytes] = {}
_peer_cache_at = 0.0        # when the cache was last loaded successfully
_peer_read_at = 0.0         # when a read was last attempted
_peer_lock = threading.Lock()


def _peer_uuids(force: bool = False) -> dict[str, bytes]:
    global _peer_cache, _peer_cache_at, _peer_read_at
    with _peer_lock:
        now = time.monotonic()
        fresh = now - _peer_cache_at < _PEER_CACHE_TTL_S
        may_force = force and now - _peer_read_at >= _FORCED_REFRESH_MIN_S
        if fresh and not may_force:
            return _peer_cache
        _peer_read_at = now
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
        # A brand-new device may register moments before reporting; re-read
        # (at most once every _FORCED_REFRESH_MIN_S).
        peers = _peer_uuids(force=True)
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


# Connection types the client reports in /api/audit/conn ("type").
CONN_TYPES = {0: "Remote control", 1: "File transfer", 2: "Port forward",
              3: "View camera", 4: "Terminal"}

# The client retries an audit post for up to ~2 min, so a "new" that arrives
# for a conn we opened that recently is a retry, not a fresh connection.
_AUDIT_RETRY_WINDOW_S = 300


def _audit_session_id(conn, rid: str, ref: str, create: bool) -> int | None:
    """The device_sessions row for this conn: the open one, else (when the
    report is a late retry) one started moments ago; inserted if `create`."""
    row = conn.execute(
        f"""SELECT id FROM device_sessions
            WHERE rustdesk_id = ? AND conn_ref = ?
              AND (ended_at IS NULL OR started_at > datetime('now', '-{_AUDIT_RETRY_WINDOW_S} seconds'))
            ORDER BY (ended_at IS NULL) DESC, id DESC LIMIT 1""",
        (rid, ref),
    ).fetchone()
    if row:
        return row["id"]
    if not create:
        return None
    cur = conn.execute(
        "INSERT INTO device_sessions (rustdesk_id, conn_ref) VALUES (?, ?)", (rid, ref)
    )
    return cur.lastrowid


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


@router.post("/api/audit/conn")
async def client_audit_conn(request: Request):
    data = await _read_json(request)
    rid = _verify(data) if data else None
    if rid is None or data.get("conn_id") is None:
        # 4xx: the client treats it as final and doesn't retry.
        return PlainTextResponse("REJECTED", status_code=400)

    ref = str(data.get("conn_id"))
    peer = data.get("peer")
    conn = get_db()
    if data.get("action") == "close":
        sid = _audit_session_id(conn, rid, ref, create=False)
        if sid:
            conn.execute(
                """UPDATE device_sessions SET ended_at = datetime('now'), last_seen_at = datetime('now')
                   WHERE id = ? AND ended_at IS NULL""",
                (sid,),
            )
    elif data.get("action") == "new":
        sid = _audit_session_id(conn, rid, ref, create=True)
        ip = str(data.get("ip") or "").replace("::ffff:", "")[:64]
        if ip:
            conn.execute("UPDATE device_sessions SET peer_ip = ? WHERE id = ?", (ip, sid))
    elif isinstance(peer, list) and peer:
        sid = _audit_session_id(conn, rid, ref, create=True)
        try:
            ctype = int(data.get("type"))
        except (TypeError, ValueError):
            ctype = None
        conn.execute(
            "UPDATE device_sessions SET peer_id = ?, peer_name = ?, conn_type = ? WHERE id = ?",
            (str(peer[0] or "")[:64] or None,
             str(peer[1] if len(peer) > 1 and peer[1] is not None else "")[:200] or None,
             ctype, sid),
        )
    conn.commit()
    conn.close()
    return PlainTextResponse("")


@router.post("/api/audit/file")
async def client_audit_file(request: Request):
    data = await _read_json(request)
    rid = _verify(data) if data else None
    try:
        direction = int(data.get("type")) if rid else None
    except (TypeError, ValueError):
        direction = None
    if rid is None or direction not in (0, 1):
        return PlainTextResponse("REJECTED", status_code=400)

    try:
        info = json.loads(data.get("info") or "{}")
        if not isinstance(info, dict):
            info = {}
    except (TypeError, ValueError):
        info = {}
    files = []
    for f in info.get("files") or []:
        if isinstance(f, list) and len(f) >= 2:
            try:
                files.append([str(f[0])[:260], int(f[1])])
            except (TypeError, ValueError):
                continue
    files = files[:10]
    try:
        count = int(info.get("num") or len(files))
    except (TypeError, ValueError):
        count = len(files)
    if count <= 0:
        return PlainTextResponse("REJECTED", status_code=400)

    ref = str(data.get("conn_id")) if data.get("conn_id") is not None else None
    peer_id = str(data.get("peer_id") or "")[:64] or None
    peer_name = str(info.get("name") or "")[:200] or None
    peer_ip = str(info.get("ip") or "").replace("::ffff:", "")[:64] or None
    nonce = str(data.get("nonce") or "")[:64] or None

    conn = get_db()
    if nonce and conn.execute(
        "SELECT 1 FROM device_file_transfers WHERE nonce = ?", (nonce,)
    ).fetchone():
        conn.close()
        return PlainTextResponse("")   # a retry of a report we already stored

    sid = _audit_session_id(conn, rid, ref, create=False) if ref else None
    if sid:
        # Fill in who connected if the conn audit didn't reach us.
        conn.execute(
            """UPDATE device_sessions SET peer_id = COALESCE(peer_id, ?),
                   peer_name = COALESCE(peer_name, ?), peer_ip = COALESCE(peer_ip, ?)
               WHERE id = ?""",
            (peer_id, peer_name, peer_ip, sid),
        )
    conn.execute(
        """INSERT INTO device_file_transfers
               (rustdesk_id, session_id, conn_ref, peer_id, peer_name, peer_ip,
                direction, path, is_file, file_count, files, nonce)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (rid, sid, ref, peer_id, peer_name, peer_ip, direction,
         str(data.get("path") or "")[:1000], 1 if data.get("is_file") else 0,
         count, json.dumps(files), nonce),
    )
    conn.commit()
    conn.close()
    return PlainTextResponse("")


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
    # "Last seen" on the device lists; otherwise only a manual Sync moves it.
    conn.execute("UPDATE devices SET last_seen = datetime('now') WHERE rustdesk_id = ?", (rid,))
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
