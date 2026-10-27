from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.auth import require_auth
from app.deps import get_db
from app.notifications import EVENT_LABELS, get_log
from app.permissions import user_can
from app.routes.sessions import _LIST_SQL, _row
from app.tenancy import Tenant, get_tenant
from app.templates_config import templates

router = APIRouter()

# Tabs shown on the Logs page, in order. "sessions" and "deliveries" are special —
# they read from device_sessions / notification_log rather than provisioning_events.
TABS = [
    ("all", "All activity"),
    ("clients", "Clients"),
    ("sessions", "Remote sessions"),
    ("installers", "Installers"),
    ("groups", "Groups"),
    ("users", "Users & auth"),
    ("notifications", "Notifications"),
    ("deliveries", "Email deliveries"),
    ("server", "Server"),
    ("other", "Other"),
]
TAB_LABELS = dict(TABS)

# Explicit event→category map. category_of() also falls back to prefix rules so
# a new event type still lands somewhere sensible without a code change.
_CATEGORY_EVENTS = {
    "clients": [
        "device_registered", "device_deleted", "device_updated",
        "device_offline", "device_online", "device_restored",
    ],
    "installers": [
        "installer_built", "installer_build_start", "installer_build_failed",
        "installer_deleted", "installers_rebuilt", "client_version_updated",
        "installer_signed", "installer_sign_failed", "signing_config_updated",
        "client_update_available", "installer_downloaded",
        "download_link_created", "download_link_emailed", "download_link_revoked",
    ],
    "groups": ["group_created", "group_updated", "group_client_settings_changed",
               "dynamic_group_created", "dynamic_group_updated", "dynamic_group_deleted"],
    "users": [
        "user_created", "user_deleted", "user_updated", "user_role_changed",
        "user_access_updated", "user_password_reset", "password_changed", "profile_updated",
        "auth_method_changed", "auth_method_force_reset", "passkey_login",
        "passkey_registered", "passkey_deleted", "passkey_revoked_by_admin",
        "mfa_reminder_sent", "mfa_reminder_failed",
        "role_created", "role_updated", "role_deleted",
    ],
    "notifications": [
        "notification_settings_updated", "notification_test_sent",
        "notification_triggers_updated", "health_alert_thresholds_updated",
        "email_template_updated", "email_design_updated",
    ],
    "server": [
        "server_updated", "server_update_start", "server_update_available",
        "stack_up", "stack_up_attempt", "compose_written", "key_captured",
        "backup_succeeded", "backup_failed", "backup_restored", "backup_restore_failed", "backup_deleted", "backup_offsite_deleted",
        "backup_config_updated", "relay_restarted", "relay_restart_failed", "logs_pruned",
    ],
}
_EVENT_CATEGORY = {e: c for c, evs in _CATEGORY_EVENTS.items() for e in evs}
_KNOWN_EVENTS = list(_EVENT_CATEGORY)


_ALL_LIMIT = 200

# Device status flips. The status watcher logs one per device per change, so
# on a real fleet they drown out everything else; the All activity and Clients
# tabs hide them unless "Hide online/offline" is switched off.
_STATUS_EVENTS = ("device_online", "device_offline")


def _events_scope(tenant: Tenant) -> str:
    """WHERE fragment for this tenant's audit events; pair it with the
    (tenant.id,) parameter. Provider events (no tenant: backups, signing) and
    provider console actions about the tenant (source 'provider') are the
    provider's record, in the console's audit log, not the tenant's."""
    return "tenant_id = ? AND source IS NOT 'provider'"


def _all_activity(conn, tenant: Tenant, include_deliveries: bool, hide_status: bool) -> list[dict]:
    """The All activity tab: the event log merged with remote sessions, file
    transfers and (if the viewer may see them) notification emails — the
    other activity that lives in its own tables — newest first."""
    from app.notifications import EVENT_LABELS

    tid = tenant.id
    where = "AND event NOT IN ('device_online', 'device_offline')" if hide_status else ""
    rows = conn.execute(
        f"SELECT * FROM provisioning_events WHERE {_events_scope(tenant)} {where}"
        " ORDER BY created_at DESC LIMIT ?", (tid, _ALL_LIMIT)
    ).fetchall()
    items = [dict(r, category=category_of(r["event"])) for r in rows]

    for s in map(_row, conn.execute(_LIST_SQL.format(where=""), (tid, _ALL_LIMIT)).fetchall()):
        device = s["device_name"] or s["rustdesk_id"]
        parts = [f"{device} ({s['rustdesk_id']})"]
        if s["peer_id"] and s["peer_name"]:
            parts.append(f"by {s['peer_name']} ({s['peer_id']})")
        if s["peer_ip"]:
            parts.append(f"from {s['peer_ip']}")
        parts.append(f"{s['conn_type_label'] or 'connection'} · "
                     + ("active" if s["active"] else s["duration_str"]))
        items.append({
            "created_at": s["started_at"], "category": "sessions",
            "event": "remote_session", "user_email": s["peer_name"] or s["peer_id"] or "",
            "detail": " · ".join(parts), "rid": s["rustdesk_id"],
        })

    for f in conn.execute(
        """SELECT f.*, COALESCE(d.label, di.hostname) AS device_name
           FROM device_file_transfers f
           LEFT JOIN devices d      ON d.rustdesk_id  = f.rustdesk_id AND d.tenant_id  = f.tenant_id
           LEFT JOIN device_info di ON di.rustdesk_id = f.rustdesk_id AND di.tenant_id = f.tenant_id
           WHERE f.tenant_id = ?
           ORDER BY f.created_at DESC LIMIT ?""",
        (tid, _ALL_LIMIT),
    ):
        n = f["file_count"]
        where = f["path"] or "clipboard paste"
        verb = "sent from" if f["direction"] == 0 else "sent to"
        items.append({
            "created_at": f["created_at"], "category": "sessions",
            "event": "file_transfer", "user_email": f["peer_name"] or f["peer_id"] or "",
            "detail": f"{n} file{'s' if n != 1 else ''} {verb} "
                      f"{f['device_name'] or f['rustdesk_id']} ({f['rustdesk_id']}) · {where}",
            "rid": f["rustdesk_id"],
        })

    if include_deliveries:
        for d in conn.execute(
            "SELECT * FROM notification_log WHERE tenant_id = ? ORDER BY created_at DESC LIMIT ?", (tid, _ALL_LIMIT)
        ):
            items.append({
                "created_at": d["created_at"], "category": "deliveries",
                "event": "email_sent" if d["status"] == "sent" else "email_failed",
                "user_email": "",
                "detail": f"{EVENT_LABELS.get(d['event_type'], d['event_type'])} → {d['recipients']}"
                          + (f" · {d['error']}" if d["error"] else ""),
            })

    items.sort(key=lambda e: e["created_at"] or "", reverse=True)
    return items[:_ALL_LIMIT]


def category_of(event: str) -> str:
    if event in _EVENT_CATEGORY:
        return _EVENT_CATEGORY[event]
    if event.startswith("device_"):
        return "clients"
    if event.startswith(("installer", "download_link")):
        return "installers"
    if event.startswith("group_"):
        return "groups"
    if event.startswith(("user_", "passkey_", "auth_", "password_", "profile_", "role_", "mfa_")):
        return "users"
    if event.startswith("notification") or event.startswith("health_alert"):
        return "notifications"
    if (event.startswith("server") or event.startswith("stack_") or event.startswith("backup_")
            or event.startswith("relay_")
            or event in ("compose_written", "key_captured", "logs_pruned")):
        return "server"
    return "other"


@router.get("/audit", response_class=HTMLResponse)
async def audit(
    request: Request,
    cat: str = "all",
    rid: str = "",
    onoff: int = 0,
    current_user: dict = Depends(require_auth),
    tenant: Tenant = Depends(get_tenant),
):
    hide_status = not onoff
    tid = tenant.id
    scope = _events_scope(tenant)
    can_see_deliveries = user_can(current_user, "manage_notifications")
    tabs = [t for t in TABS if t[0] != "deliveries" or can_see_deliveries]
    if cat not in TAB_LABELS or (cat == "deliveries" and not can_see_deliveries):
        cat = "all"

    conn = get_db()

    # Per-category counts for the tab badges.
    counts: dict[str, int] = {}
    for row in conn.execute(f"SELECT event, COUNT(*) AS n FROM provisioning_events WHERE {scope} GROUP BY event",
                            (tid,)):
        counts[category_of(row["event"])] = counts.get(category_of(row["event"]), 0) + row["n"]
    counts["sessions"] = conn.execute("SELECT COUNT(*) FROM device_sessions WHERE tenant_id = ?", (tid,)).fetchone()[0]
    active_count = conn.execute("SELECT COUNT(*) FROM device_sessions WHERE tenant_id = ? AND ended_at IS NULL",
                                (tid,)).fetchone()[0]
    if can_see_deliveries:
        counts["deliveries"] = conn.execute("SELECT COUNT(*) FROM notification_log WHERE tenant_id = ?",
                                            (tid,)).fetchone()[0]
    counts["all"] = sum(counts.values()) + conn.execute(
        "SELECT COUNT(*) FROM device_file_transfers WHERE tenant_id = ?", (tid,)).fetchone()[0]

    events = None
    sessions = None
    deliveries = None

    if cat == "sessions":
        if rid:
            rows = conn.execute(_LIST_SQL.format(where="AND s.rustdesk_id = ?"), (tid, rid, 300)).fetchall()
        else:
            rows = conn.execute(_LIST_SQL.format(where=""), (tid, 300)).fetchall()
        sessions = [_row(r) for r in rows]
    elif cat == "deliveries":
        deliveries = get_log(tid, 200)
    elif cat == "all":
        events = _all_activity(conn, tenant, can_see_deliveries, hide_status)
    elif cat == "other":
        ph = ",".join("?" * len(_KNOWN_EVENTS))
        rows = conn.execute(
            f"SELECT * FROM provisioning_events WHERE {scope} AND event NOT IN ({ph})"
            " ORDER BY created_at DESC LIMIT 200",
            (tid, *_KNOWN_EVENTS),
        ).fetchall()
        events = [dict(r, category="other") for r in rows]
    else:
        evs = _CATEGORY_EVENTS.get(cat, [])
        if hide_status:
            evs = [e for e in evs if e not in _STATUS_EVENTS]
        ph = ",".join("?" * len(evs)) or "''"
        rows = conn.execute(
            f"SELECT * FROM provisioning_events WHERE {scope} AND event IN ({ph})"
            " ORDER BY created_at DESC LIMIT 200",
            (tid, *evs),
        ).fetchall()
        events = [dict(r, category=cat) for r in rows]

    conn.close()

    return templates.TemplateResponse(
        request,
        "audit.html",
        {
            "current_user": current_user,
            "events": events,
            "sessions": sessions,
            "deliveries": deliveries,
            "event_labels": EVENT_LABELS,
            "cat": cat,
            "tabs": tabs,
            "tab_labels": TAB_LABELS,
            "counts": counts,
            "active_count": active_count,
            "filter_rid": rid,
            "hide_status": hide_status,
            "status_toggle": cat in ("all", "clients"),
        },
    )
