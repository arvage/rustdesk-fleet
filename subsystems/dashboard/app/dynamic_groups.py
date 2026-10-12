"""
dynamic_groups.py — rule-based ("dynamic") device groups.

A dynamic group is a saved filter, not a membership list: its devices are
recomputed from fleet.fleet_snapshot() every time it's viewed, so a device
joins or leaves as soon as its details change (OS upgraded, goes quiet, …).
A device can be in any number of dynamic groups, independent of its one
client group.

Stored in the dynamic_groups table: match ('all' | 'any') plus a JSON list of
rules [{"field", "op", "value"}]. FIELDS is the catalogue the rule builder UI
is generated from; normalize_rules() validates anything posted against it.
"""

from __future__ import annotations

import json

from app.fleet import OS_SUPPORT_LABELS

# ── Field catalogue ──────────────────────────────────────────────────────────
# type decides the operators: text | number | age | bool | choice.
# For 'choice', options are (value, label); 'group' options come from the DB.
FIELDS: dict[str, dict] = {
    "os_name":        {"label": "Operating system", "type": "text", "hint": "e.g. Windows 10, Server 2016, MacOS"},
    "os_support":     {"label": "OS support status", "type": "choice",
                       "options": list(OS_SUPPORT_LABELS.items())},
    "os_build":       {"label": "OS build", "type": "number"},
    "last_seen":      {"label": "Last seen", "type": "age", "key": "last_seen_days"},
    "registered_at":  {"label": "Registered", "type": "age", "key": "registered_days"},
    "last_session":   {"label": "Last remote session", "type": "age", "key": "last_session_days"},
    "live_status":    {"label": "Current status", "type": "choice",
                       "options": [("online", "Online"), ("in session", "In session"), ("offline", "Offline")]},
    "group":          {"label": "Client group", "type": "choice", "key": "group_id"},
    "label":          {"label": "Label", "type": "text"},
    "hostname":       {"label": "Computer name", "type": "text"},
    "username":       {"label": "Logged-in user", "type": "text"},
    "client_version": {"label": "RustDesk version", "type": "text"},
    "client_outdated": {"label": "RustDesk older than installers", "type": "bool"},
    "reporting":      {"label": "Reports device details", "type": "bool"},
    "ram_gb":         {"label": "Memory (GB)", "type": "number"},
    "cores_n":        {"label": "CPU cores", "type": "number"},
    "cpu_name":       {"label": "CPU model", "type": "text"},
    "ip":             {"label": "IP address", "type": "text"},
}

OPS: dict[str, list[tuple[str, str]]] = {
    "text":   [("contains", "contains"), ("not_contains", "does not contain"),
               ("equals", "is"), ("not_equals", "is not"), ("starts_with", "starts with"),
               ("is_empty", "is empty"), ("not_empty", "is not empty")],
    "number": [("lt", "less than"), ("lte", "at most"), ("gt", "greater than"),
               ("gte", "at least"), ("eq", "equals"), ("is_empty", "is unknown")],
    "age":    [("older_than", "more than N days ago"), ("within", "within the last N days"),
               ("never", "never / unknown")],
    "bool":   [("is_true", "yes"), ("is_false", "no")],
    "choice": [("is", "is"), ("is_not", "is not")],
}
_NO_VALUE_OPS = {"is_empty", "not_empty", "never", "is_true", "is_false"}

# Starting points offered on the New Dynamic Group page.
PRESETS = [
    {"name": "Windows 10 devices", "match": "all",
     "description": "Still on Windows 10 (end of support Oct 2025) — upgrade candidates.",
     "rules": [{"field": "os_name", "op": "contains", "value": "Windows 10"}]},
    {"name": "Not seen in over a year", "match": "all",
     "description": "Haven't checked in for 365+ days — likely retired or reimaged.",
     "rules": [{"field": "last_seen", "op": "older_than", "value": "365"}]},
    {"name": "Unsupported operating system", "match": "any",
     "description": "OS past end of support (Windows or macOS), or ending within 6 months.",
     "rules": [{"field": "os_support", "op": "is", "value": "ended"},
               {"field": "os_support", "op": "is", "value": "ending_soon"}]},
    {"name": "Outdated RustDesk client", "match": "all",
     "description": "RustDesk older than the version your installers use.",
     "rules": [{"field": "client_outdated", "op": "is_true", "value": ""}]},
    {"name": "Low memory (under 8 GB)", "match": "all",
     "description": "Hardware upgrade candidates.",
     "rules": [{"field": "ram_gb", "op": "lt", "value": "8"}]},
    {"name": "Not reporting details", "match": "all",
     "description": "No OS/hardware reported — older client or never online since install.",
     "rules": [{"field": "reporting", "op": "is_false", "value": ""}]},
    {"name": "No client group", "match": "all",
     "description": "Devices that still need to be assigned to a client.",
     "rules": [{"field": "group", "op": "is", "value": ""}]},
]


def normalize_rules(raw) -> list[dict]:
    """Validate rules from the UI: drop unknown fields/ops, coerce values.
    Raises ValueError with a user-facing message for a bad value."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "[]")
        except json.JSONDecodeError:
            raise ValueError("The rules couldn't be read — please try again.")
    out = []
    for r in raw if isinstance(raw, list) else []:
        if not isinstance(r, dict):
            continue
        field, op = r.get("field"), r.get("op")
        spec = FIELDS.get(field)
        if not spec or op not in dict(OPS[spec["type"]]):
            continue
        value = "" if op in _NO_VALUE_OPS else str(r.get("value", "")).strip()
        if op not in _NO_VALUE_OPS and spec["type"] in ("number", "age"):
            try:
                float(value)
            except ValueError:
                raise ValueError(f"“{spec['label']}” needs a number.")
        if op not in _NO_VALUE_OPS and spec["type"] == "text" and not value:
            raise ValueError(f"“{spec['label']}” needs a value.")
        out.append({"field": field, "op": op, "value": value})
    return out


def _match_rule(d: dict, rule: dict) -> bool:
    spec = FIELDS[rule["field"]]
    t, op, v = spec["type"], rule["op"], rule["value"]
    actual = d.get(spec.get("key", rule["field"]))

    if t == "text":
        a = str(actual or "").lower()
        v = v.lower()
        return {
            "contains": v in a, "not_contains": v not in a,
            "equals": a == v, "not_equals": a != v,
            "starts_with": a.startswith(v),
            "is_empty": a == "", "not_empty": a != "",
        }[op]
    if t == "number":
        if op == "is_empty":
            return actual in (None, "")
        try:
            a, n = float(actual), float(v)
        except (TypeError, ValueError):
            return False   # unknown never satisfies a comparison
        return {"lt": a < n, "lte": a <= n, "gt": a > n, "gte": a >= n, "eq": a == n}[op]
    if t == "age":   # actual = whole days since, or None for never
        if op == "never":
            return actual is None
        if actual is None:
            return False
        n = float(v)
        return actual > n if op == "older_than" else actual <= n
    if t == "bool":
        return bool(actual) == (op == "is_true")
    if t == "choice":
        if rule["field"] == "group":
            a = str(actual) if actual is not None else ""
        else:
            a = str(actual or "")
        return (a == v) if op == "is" else (a != v)
    return False


def matches(d: dict, match: str, rules: list[dict]) -> bool:
    if not rules:
        return False   # an empty group matches nothing rather than everything
    hits = (_match_rule(d, r) for r in rules)
    return any(hits) if match == "any" else all(hits)


def members(devices: list[dict], match: str, rules: list[dict]) -> list[dict]:
    return [d for d in devices if matches(d, match, rules)]


def describe(rules: list[dict], match: str, group_names: dict[str, str] | None = None) -> str:
    """Human summary, e.g. 'Operating system contains “Windows 10”'."""
    group_names = group_names or {}
    parts = []
    for r in rules:
        spec = FIELDS[r["field"]]
        op_label = dict(OPS[spec["type"]])[r["op"]]
        v = r["value"]
        if spec["type"] == "choice":
            opts = dict(spec.get("options") or [])
            v = (group_names.get(v, v) or "no group") if r["field"] == "group" else opts.get(v, v)
            parts.append(f"{spec['label']} {op_label} {v}")
        elif spec["type"] == "age":
            parts.append(f"{spec['label']} " + (op_label.replace("N", v) if r["op"] != "never" else op_label))
        elif spec["type"] == "bool":
            parts.append(f"{spec['label']}: {op_label}")
        elif r["op"] in _NO_VALUE_OPS:
            parts.append(f"{spec['label']} {op_label}")
        else:
            parts.append(f"{spec['label']} {op_label} “{v}”")
    return (" or " if match == "any" else " and ").join(parts) or "No rules"


def builder_catalogue(groups: list[dict]) -> dict:
    """FIELDS/OPS shaped for the JS rule builder, with client groups filled in."""
    fields = {}
    for key, spec in FIELDS.items():
        f = {"label": spec["label"], "type": spec["type"], "hint": spec.get("hint", "")}
        if key == "group":
            f["options"] = [["", "— No client group —"]] + [[str(g["id"]), g["display_name"]] for g in groups]
        elif spec.get("options"):
            f["options"] = [list(o) for o in spec["options"]]
        fields[key] = f
    return {"fields": fields, "ops": {t: [list(o) for o in ops] for t, ops in OPS.items()},
            "no_value_ops": sorted(_NO_VALUE_OPS)}
