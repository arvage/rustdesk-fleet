"""
permissions.py — roles ("permission levels") and capability checks.

A role is a named set of capabilities, stored in the `roles` table. Users
reference a role by key (users.role). Capabilities gate the write/admin actions
below; viewing (devices, sessions, logs) is open to any signed-in user, so a
role with no capabilities is effectively read-only.

Relay restarts and upgrades, backups and the client version are the
provider's (the provider console), so no tenant role has them.

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
    ("manage_system", "System maintenance",
     "Prune the audit log", "System"),
    ("manage_users", "Manage users",
     "Create, edit and delete user accounts, group access and passkeys", "Administration"),
    ("manage_roles", "Manage roles & permissions",
     "Create and edit permission levels", "Administration"),
    ("manage_support_access", "Manage support access",
     "Grant, change and revoke provider support access to this dashboard", "Administration"),
    ("manage_domains", "Manage custom domains",
     "Add, check, remove custom domains and choose the primary one", "Administration"),
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
# Roles are per tenant (TENANCY.md): every tenant has its own copy of the
# built-in roles plus any custom ones.
_cache: dict[int, tuple[float, dict]] = {}
_CACHE_TTL = 15.0
_lock = threading.Lock()


def invalidate_cache() -> None:
    with _lock:
        _cache.clear()


def _load_roles(tenant_id: int) -> dict[str, dict]:
    """{key: {name, perms(set), is_system, description}} for one tenant."""
    conn = get_db()
    rows = conn.execute("SELECT * FROM roles WHERE tenant_id = ?", (tenant_id,)).fetchall()
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


def all_roles(tenant_id: int) -> dict[str, dict]:
    with _lock:
        hit = _cache.get(tenant_id)
        if hit and time.monotonic() - hit[0] < _CACHE_TTL and hit[1]:
            return hit[1]
    roles = _load_roles(tenant_id)
    with _lock:
        _cache[tenant_id] = (time.monotonic(), roles)
    return roles


def get_role_permissions(tenant_id: int, role_key: str) -> set:
    role = all_roles(tenant_id).get(role_key)
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
