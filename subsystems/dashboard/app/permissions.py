"""
permissions.py — roles ("permission levels") and capability checks.

A role is a named set of capabilities, stored in the `roles` table. Users
reference a role by key (users.role). Capabilities gate the write/admin actions
below; viewing (devices, sessions, logs) is open to any signed-in user, so a
role with no capabilities is effectively read-only.

`admin` is a locked system role that always has every capability, so an
account can never be edited into being unable to manage roles/users.
"""

from __future__ import annotations

import json
import re
import threading
import time

from app.deps import get_db

# ── Capability catalogue ─────────────────────────────────────────────────────
# (key, label, description, section) — section only groups the matrix UI.
CAPABILITIES = [
    ("manage_devices", "Manage devices",
     "Edit labels and groups, delete/restore devices, sync from the server", "Fleet"),
    ("manage_groups", "Manage client groups & installers",
     "Create/edit groups and client settings, build installers, share download links", "Fleet"),
    ("update_server", "Update server & client versions",
     "Apply RustDesk server/client updates and rebuild installers", "Fleet"),
    ("manage_notifications", "Manage notifications & alerts",
     "SMTP settings, event triggers, and server-health alert thresholds", "System"),
    ("manage_backups", "Manage backups",
     "Configure destinations, run, download and restore backups", "System"),
    ("manage_system", "System maintenance",
     "Restart the relay (hbbs/hbbr) and prune the audit log", "System"),
    ("manage_users", "Manage users",
     "Create, edit and delete user accounts, group access and passkeys", "Administration"),
    ("manage_roles", "Manage roles & permissions",
     "Create and edit permission levels", "Administration"),
]
CAP_KEYS = [c[0] for c in CAPABILITIES]
CAP_LABELS = {c[0]: c[1] for c in CAPABILITIES}

# Seeded system roles. "*" means every capability (and stays that way).
SYSTEM_ROLES = {
    "admin":  {"name": "Administrator",     "perms": "*",
               "description": "Full access to everything."},
    "tech":   {"name": "Technician",        "perms": ["manage_devices", "manage_groups"],
               "description": "Day-to-day support: devices, groups and installers."},
    "viewer": {"name": "Viewer (read-only)", "perms": [],
               "description": "Can view the fleet, sessions and logs but change nothing."},
}

# ── Role storage + a small cache (resolved every request via require_auth) ────
_cache: dict[str, set] = {}
_cache_at = 0.0
_CACHE_TTL = 15.0
_lock = threading.Lock()


def invalidate_cache() -> None:
    global _cache_at
    with _lock:
        _cache_at = 0.0


def _load_roles() -> dict[str, dict]:
    """{key: {name, perms(set), is_system, description}} from the DB."""
    conn = get_db()
    rows = conn.execute("SELECT * FROM roles").fetchall()
    conn.close()
    out: dict[str, dict] = {}
    for r in rows:
        raw = r["permissions"]
        perms = set(CAP_KEYS) if raw == "*" else set(json.loads(raw or "[]")) & set(CAP_KEYS)
        if r["key"] == "admin":
            perms = set(CAP_KEYS)   # admin is always everything
        out[r["key"]] = {
            "name": r["name"], "perms": perms,
            "is_system": bool(r["is_system"]), "description": r["description"] or "",
        }
    return out


def all_roles() -> dict[str, dict]:
    global _cache, _cache_at
    with _lock:
        if time.monotonic() - _cache_at < _CACHE_TTL and _cache:
            return _cache
    roles = _load_roles()
    with _lock:
        _cache = roles
        _cache_at = time.monotonic()
    return roles


def get_role_permissions(role_key: str) -> set:
    role = all_roles().get(role_key)
    if role:
        return role["perms"]
    # Unknown role: admin is all, anything else nothing.
    return set(CAP_KEYS) if role_key == "admin" else set()


def user_can(user: dict, capability: str) -> bool:
    return capability in (user.get("permissions") or set())


def require_perm(user: dict, capability: str) -> None:
    if not user_can(user, capability):
        label = CAP_LABELS.get(capability, capability)
        raise PermissionError(f"You don't have permission to: {label}.")


def slugify_role_key(name: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")[:32]
    return key
