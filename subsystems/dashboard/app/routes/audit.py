from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.auth import require_auth
from app.deps import get_db
from app.routes.sessions import _LIST_SQL, _row
from app.templates_config import templates

router = APIRouter()

# Tabs shown on the Logs page, in order. "sessions" is special — it reads from
# device_sessions rather than provisioning_events.
TABS = [
    ("all", "All activity"),
    ("clients", "Clients"),
    ("sessions", "Remote sessions"),
    ("installers", "Installers"),
    ("groups", "Groups"),
    ("users", "Users & auth"),
    ("notifications", "Notifications"),
    ("server", "Server"),
    ("other", "Other"),
]
TAB_LABELS = dict(TABS)

# Explicit event→category map. category_of() also falls back to prefix rules so
# a new event type still lands somewhere sensible without a code change.
_CATEGORY_EVENTS = {
    "clients": [
        "device_registered", "device_deleted", "device_updated",
        "device_offline", "device_restored",
    ],
    "installers": [
        "installer_built", "installer_build_start", "installer_build_failed",
        "installer_deleted", "installers_rebuilt", "client_version_updated",
        "installer_signed", "installer_sign_failed", "signing_config_updated",
        "client_update_available", "installer_downloaded",
        "download_link_created", "download_link_emailed", "download_link_revoked",
    ],
    "groups": ["group_created", "group_updated", "group_client_settings_changed"],
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
    current_user: dict = Depends(require_auth),
):
    if cat not in TAB_LABELS:
        cat = "all"

    conn = get_db()

    # Per-category counts for the tab badges.
    counts: dict[str, int] = {}
    for row in conn.execute("SELECT event, COUNT(*) AS n FROM provisioning_events GROUP BY event"):
        counts[category_of(row["event"])] = counts.get(category_of(row["event"]), 0) + row["n"]
    counts["all"] = sum(counts.values())
    counts["sessions"] = conn.execute("SELECT COUNT(*) FROM device_sessions").fetchone()[0]
    active_count = conn.execute("SELECT COUNT(*) FROM device_sessions WHERE ended_at IS NULL").fetchone()[0]

    events = None
    sessions = None

    if cat == "sessions":
        if rid:
            rows = conn.execute(_LIST_SQL.format(where="WHERE s.rustdesk_id = ?"), (rid, 300)).fetchall()
        else:
            rows = conn.execute(_LIST_SQL.format(where=""), (300,)).fetchall()
        sessions = [_row(r) for r in rows]
    elif cat == "all":
        rows = conn.execute(
            "SELECT * FROM provisioning_events ORDER BY created_at DESC LIMIT 200"
        ).fetchall()
        events = [dict(r, category=category_of(r["event"])) for r in rows]
    elif cat == "other":
        ph = ",".join("?" * len(_KNOWN_EVENTS))
        rows = conn.execute(
            f"SELECT * FROM provisioning_events WHERE event NOT IN ({ph})"
            " ORDER BY created_at DESC LIMIT 200",
            _KNOWN_EVENTS,
        ).fetchall()
        events = [dict(r, category="other") for r in rows]
    else:
        evs = _CATEGORY_EVENTS.get(cat, [])
        ph = ",".join("?" * len(evs)) or "''"
        rows = conn.execute(
            f"SELECT * FROM provisioning_events WHERE event IN ({ph})"
            " ORDER BY created_at DESC LIMIT 200",
            evs,
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
            "cat": cat,
            "tabs": TABS,
            "tab_labels": TAB_LABELS,
            "counts": counts,
            "active_count": active_count,
            "filter_rid": rid,
        },
    )
