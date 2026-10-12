"""
reports.py — the Admin → Reports catalogue.

Each report is a function taking a ReportContext (optional client-group filter
and look-back period) and returning:

    {"stats":    [(label, value, sub), ...],          # tiles across the top
     "sections": [{"title", "note", "columns", "rows"}]}  # tables; each is CSV-exportable

One template (report.html) renders every report, and routes/reports.py serves
each section as CSV, so adding a report is just adding a function to REPORTS.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from app.deps import get_db
from app.fleet import OS_SUPPORT_LABELS, device_name, fleet_snapshot
from app.routes.sessions import fmt_duration

PERIODS = [(7, "Last 7 days"), (30, "Last 30 days"), (90, "Last 90 days"),
           (180, "Last 6 months"), (365, "Last year")]
INACTIVE_THRESHOLDS = [(30, "30 days"), (90, "90 days"), (180, "6 months"), (365, "1 year")]


@dataclass
class ReportContext:
    group: str = ""     # client group slug, "__none__" = unassigned, "" = all
    days: int = 30
    _devices: list | None = field(default=None, repr=False)
    _all: list | None = field(default=None, repr=False)

    @property
    def devices(self) -> list[dict]:
        if self._devices is None:
            devs = fleet_snapshot()
            if self.group == "__none__":
                devs = [d for d in devs if not d.get("group_id")]
            elif self.group:
                devs = [d for d in devs if d.get("group_slug") == self.group]
            self._devices = devs
        return self._devices

    @property
    def all_devices(self) -> list[dict]:
        """The whole fleet, ignoring the group filter (for stable colors)."""
        if self.group == "":
            return self.devices
        if self._all is None:
            self._all = fleet_snapshot()
        return self._all

    @property
    def device_ids(self) -> set[str]:
        return {d["rustdesk_id"] for d in self.devices}

    @property
    def since(self) -> str:
        return f"-{int(self.days)} days"


def _pct(n: int, total: int) -> str:
    return f"{round(100 * n / total)}%" if total else "—"


def _ago(days) -> str:
    if days is None:
        return "never"
    if days == 0:
        return "today"
    return f"{days} day{'s' if days != 1 else ''} ago"


def _dev_cells(d: dict) -> list:
    return [device_name(d), d["rustdesk_id"], d.get("group_name") or "unassigned"]


_DEV_COLS = ["Device", "RustDesk ID", "Client group"]


# ── Color ────────────────────────────────────────────────────────────────────
# Categorical slots (--viz-1..8, defined in report.html for light and dark) are
# assigned in fixed order by each category's rank across the WHOLE fleet, so a
# category keeps its color when the report is filtered to one client group.
# Past 7 categories the rest fold into a gray "Other"; blank/unknown is gray.
# Status values use status tones (with an icon), never a categorical slot.
_MAX_SLOTS = 8
STATUS_TONES = {
    "End of support": "critical", "Ending within 6 months": "warning", "Supported": "good", "Unknown": "neutral",
    "online": "good", "in session": "info", "offline": "neutral",
    "yes": "good", "no": "warning",
    "active": "good", "expired": "neutral", "used up": "neutral", "revoked": "critical",
    "built": "good", "signed": "good", "signing": "info", "pending": "warning", "failed": "critical",
}
STATUS_COLUMNS = {"Status", "State", "Up to date", "Current status", "OS support status"}


def _breakdown(ctx: ReportContext, devs: list[dict], key, label: str, blank: str = "Not reported",
               status: bool = False) -> dict:
    """'By <label>' section: share-of-devices table plus a stacked share bar."""
    counter = Counter(key(d) or "" for d in devs)
    total = len(devs)
    rows = [[k or blank, n, _pct(n, total)] for k, n in counter.most_common()]
    if status:
        colors = [f"var(--status-{STATUS_TONES.get(k or blank, 'neutral')})" for k, _ in counter.most_common()]
    else:
        fleet_rank = [k for k, _ in Counter(key(d) or "" for d in ctx.all_devices).most_common() if k]
        many = len([k for k in counter if k]) > _MAX_SLOTS
        slot = {k: i + 1 for i, k in enumerate(fleet_rank[:_MAX_SLOTS - 1 if many else _MAX_SLOTS])}
        colors = [f"var(--viz-{slot[k]})" if k in slot else "var(--viz-neutral)" for k, _ in counter.most_common()]
    # Chart: colored categories individually, folded ones merged into "Other".
    items, other = [], 0
    for (name, n, _), color in zip(rows, colors):
        if color == "var(--viz-neutral)" and name != blank and not status:
            other += n
        else:
            items.append({"label": name, "value": n, "pct": 100 * n / total if total else 0, "color": color})
    if other:
        items.append({"label": "Other", "value": other, "pct": 100 * other / total, "color": "var(--viz-neutral)"})
    return {
        "title": f"By {label.lower()}", "note": "",
        "columns": [label, "Devices", "Share"],
        "rows": rows,
        "row_colors": colors,
        "chart": {"type": "share", "items": items, "total": total, "status": status},
    }


# Stat tiles whose number is a state get a status tone when it's non-zero.
_STAT_TONES = [
    ("Unsupported OS", "critical"), ("End of support", "critical"),
    ("Ending within", "warning"), ("Outdated", "warning"), ("Inactive", "warning"),
    ("Not seen in", "warning"), ("Need rebuilding", "warning"), ("Under 8 GB", "warning"),
    ("Fewer than 4", "warning"), ("Not reporting", "warning"), ("No client group", "warning"),
    ("No label", "warning"), ("Duplicate", "warning"),
    ("Online now", "good"), ("Supported", "good"), ("Reporting details", "good"),
]


def stat_tone(label: str, value) -> str:
    """'critical' | 'warning' | 'good' | '' (plain) for a stat tile."""
    try:
        nonzero = float(str(value).split()[0]) > 0
    except (ValueError, IndexError):
        nonzero = False
    for prefix, tone in _STAT_TONES:
        if label.startswith(prefix):
            return tone if nonzero else ""
    return ""


def _columns_chart(points: list[tuple[str, int]], unit: str) -> dict:
    """Single-series column chart over time (chronological, oldest first)."""
    return {"type": "columns", "unit": unit,
            "points": [{"label": k, "value": v} for k, v in points],
            "max": max((v for _, v in points), default=0)}


# ── Fleet & devices ──────────────────────────────────────────────────────────

def r_fleet_overview(ctx: ReportContext) -> dict:
    devs = ctx.devices
    total = len(devs)
    online = sum(d["live_status"] != "offline" for d in devs)
    reporting = sum(bool(d.get("reporting")) for d in devs)
    outdated = sum(bool(d.get("client_outdated")) for d in devs)
    eos = sum(d["os_support"] == "ended" for d in devs)
    stale = sum(d["last_seen_days"] is None or d["last_seen_days"] > 90 for d in devs)
    return {
        "stats": [
            ("Devices", total, ""),
            ("Online now", online, _pct(online, total)),
            ("Reporting details", reporting, _pct(reporting, total)),
            ("Unsupported OS", eos, "past end of support"),
            ("Outdated RustDesk", outdated, "older than installers"),
            ("Inactive 90+ days", stale, "or never seen"),
        ],
        "sections": [
            _breakdown(ctx, devs, lambda d: d.get("group_name"), "Client group", "Unassigned"),
            _breakdown(ctx, devs, lambda d: d.get("os_name"), "Operating system"),
            _breakdown(ctx, devs, lambda d: OS_SUPPORT_LABELS[d["os_support"]], "OS support status", status=True),
            _breakdown(ctx, devs, lambda d: d.get("client_version"), "RustDesk version"),
            _breakdown(ctx, devs, lambda d: d["live_status"], "Current status", status=True),
        ],
    }


def r_inventory(ctx: ReportContext) -> dict:
    rows = [
        _dev_cells(d) + [d.get("hostname", ""), d.get("username", ""), d.get("os_name", ""),
                         d.get("os_build", ""), d.get("cpu_name", ""), d.get("cores", ""), d.get("ram", ""),
                         d.get("client_version", ""), d.get("ip", ""), d["live_status"],
                         d.get("registered_at", ""), d.get("last_seen", "")]
        for d in ctx.devices
    ]
    return {
        "stats": [("Devices", len(rows), "")],
        "sections": [{
            "title": "All devices", "note": "Everything the dashboard knows about each device.",
            "columns": _DEV_COLS + ["Computer", "User", "OS", "Build", "CPU", "Cores", "Memory",
                                    "RustDesk", "IP", "Status", "Registered", "Last seen"],
            "rows": rows,
        }],
    }


def r_os_support(ctx: ReportContext) -> dict:
    devs = ctx.devices
    by_os: dict[str, dict] = {}
    for d in devs:
        key = (d.get("os_name") or "Not reported", d["os_eos"])
        e = by_os.setdefault(key, {"n": 0, "status": d["os_support"]})
        e["n"] += 1
    order = {"ended": 0, "ending_soon": 1, "supported": 2, "unknown": 3}
    summary = sorted(by_os.items(), key=lambda kv: (order[kv[1]["status"]], kv[0][1] or "9999", kv[0][0]))
    at_risk = sorted((d for d in devs if d["os_support"] in ("ended", "ending_soon")),
                     key=lambda d: (d["os_eos"], device_name(d).lower()))
    c = Counter(d["os_support"] for d in devs)
    from app import os_lifecycle
    src = os_lifecycle.info()
    notice = (f"End-of-support dates from {src['source']} (Microsoft lifecycle), checked daily — "
              f"last updated {src['fetched_at']} UTC." if src else
              "End-of-support dates from the dashboard's built-in table (live lifecycle data not downloaded yet).")
    return {
        "notice": notice,
        "stats": [
            ("End of support", c["ended"], "no security updates"),
            ("Ending within 6 months", c["ending_soon"], "plan upgrades"),
            ("Supported", c["supported"], ""),
            ("Unknown", c["unknown"], "macOS/Linux or not reported"),
        ],
        "sections": [
            {"title": "Devices needing an OS upgrade",
             "note": "Past Microsoft's end-of-support date, or reaching it within 6 months. "
                     "Windows dates depend on the release (build) and edition — Enterprise/Education and LTSC "
                     "editions are supported longer.",
             "columns": _DEV_COLS + ["OS", "Build", "End of support", "Status", "Last seen"],
             "rows": [_dev_cells(d) + [d.get("os_name", ""), d.get("os_build", ""), d["os_eos"],
                                       OS_SUPPORT_LABELS[d["os_support"]], _ago(d["last_seen_days"])]
                      for d in at_risk]},
            {"title": "Operating systems and their support dates", "note": "",
             "columns": ["Operating system", "End of support", "Status", "Devices"],
             "rows": [[os, eos or "—", OS_SUPPORT_LABELS[e["status"]], e["n"]] for (os, eos), e in summary]},
        ],
    }


def r_inactive(ctx: ReportContext) -> dict:
    devs = ctx.devices
    threshold = ctx.days
    buckets = [(t, label, sum(d["last_seen_days"] is not None and d["last_seen_days"] > t for d in devs))
               for t, label in INACTIVE_THRESHOLDS]
    never = sum(d["last_seen_days"] is None for d in devs)
    stale = sorted((d for d in devs if d["last_seen_days"] is None or d["last_seen_days"] > threshold),
                   key=lambda d: -(d["last_seen_days"] if d["last_seen_days"] is not None else 10**6))
    return {
        "stats": [(f"Not seen in {label}", n, _pct(n, len(devs))) for _, label, n in buckets]
                 + [("Never seen", never, "no check-in recorded")],
        "sections": [{
            "title": f"Devices not seen in more than {threshold} days",
            "note": "Candidates to retire, reinstall the client on, or follow up with the client about. "
                    "Change the period above to adjust the threshold.",
            "columns": _DEV_COLS + ["Computer", "OS", "Last seen", "Inactive for", "Last remote session"],
            "rows": [_dev_cells(d) + [d.get("hostname", ""), d.get("os_name", ""),
                                      d.get("last_seen") if d.get("last_seen") != "—" else "",
                                      _ago(d["last_seen_days"]), d["last_session"] or "never"]
                     for d in stale],
        }],
    }


def r_client_versions(ctx: ReportContext) -> dict:
    devs = ctx.devices
    try:
        from generate_installer import get_pinned_version
        pinned = get_pinned_version()
    except Exception:
        pinned = ""
    outdated = [d for d in devs if d.get("client_outdated")]
    not_reporting = [d for d in devs if not d.get("reporting")]
    return {
        "stats": [
            ("Installer version", pinned or "—", "what new installs get"),
            ("Outdated clients", len(outdated), _pct(len(outdated), len(devs))),
            ("Not reporting", len(not_reporting), "version unknown"),
        ],
        "sections": [
            _breakdown(ctx, devs, lambda d: d.get("client_version"), "RustDesk version"),
            {"title": "Outdated clients", "note": "Older than the version your installers use — turn on auto-update for the "
                                                   "client group, or reinstall with a fresh installer.",
             "columns": _DEV_COLS + ["RustDesk", "Status", "Last seen"],
             "rows": [_dev_cells(d) + [d.get("client_version", ""), d["live_status"], _ago(d["last_seen_days"])]
                      for d in outdated]},
            {"title": "Not reporting details", "note": "No OS/hardware/version reported — an older client, "
                                                        "or the device hasn't been online since install.",
             "columns": _DEV_COLS + ["Status", "Last seen"],
             "rows": [_dev_cells(d) + [d["live_status"], _ago(d["last_seen_days"])] for d in not_reporting]},
        ],
    }


def r_hardware(ctx: ReportContext) -> dict:
    devs = [d for d in ctx.devices if d.get("reporting")]

    def ram_bucket(gb):
        if gb is None:
            return "Not reported"
        for limit, label in ((4, "Under 4 GB"), (8, "4–7 GB"), (16, "8–15 GB"), (32, "16–31 GB")):
            if gb < limit:
                return label
        return "32 GB or more"

    low_ram = sorted((d for d in devs if d["ram_gb"] is not None and d["ram_gb"] < 8),
                     key=lambda d: d["ram_gb"])
    low_cores = sorted((d for d in devs if d["cores_n"] is not None and d["cores_n"] < 4),
                       key=lambda d: d["cores_n"])
    return {
        "stats": [
            ("Devices reporting hardware", len(devs), ""),
            ("Under 8 GB memory", len(low_ram), "upgrade candidates"),
            ("Fewer than 4 cores", len(low_cores), ""),
        ],
        "sections": [
            _breakdown(ctx, devs, lambda d: ram_bucket(d["ram_gb"]), "Memory"),
            _breakdown(ctx, devs, lambda d: d.get("cores"), "CPU cores"),
            _breakdown(ctx, devs, lambda d: d.get("cpu_name"), "CPU model"),
            {"title": "Low memory (under 8 GB)", "note": "Windows 11 runs poorly below 8 GB — upgrade or replace.",
             "columns": _DEV_COLS + ["Computer", "OS", "Memory", "CPU"],
             "rows": [_dev_cells(d) + [d.get("hostname", ""), d.get("os_name", ""), d.get("ram", ""),
                                       d.get("cpu_name", "")] for d in low_ram]},
            {"title": "Fewer than 4 CPU cores", "note": "",
             "columns": _DEV_COLS + ["Computer", "CPU", "Cores", "Memory"],
             "rows": [_dev_cells(d) + [d.get("hostname", ""), d.get("cpu_name", ""), d.get("cores", ""),
                                       d.get("ram", "")] for d in low_cores]},
        ],
    }


def r_data_quality(ctx: ReportContext) -> dict:
    devs = ctx.devices
    unassigned = [d for d in devs if not d.get("group_id")]
    unlabeled = [d for d in devs if not d.get("label")]
    hostnames = Counter((d.get("hostname") or "").lower() for d in devs if d.get("hostname"))
    dupes = sorted((d for d in devs if d.get("hostname") and hostnames[d["hostname"].lower()] > 1),
                   key=lambda d: (d["hostname"].lower(), d.get("last_seen") or ""))
    return {
        "stats": [
            ("No client group", len(unassigned), ""),
            ("No label", len(unlabeled), ""),
            ("Duplicate computer names", len(dupes), "often a reinstall"),
        ],
        "sections": [
            {"title": "Devices with no client group", "note": "",
             "columns": ["Device", "RustDesk ID", "Computer", "Registered", "Last seen"],
             "rows": [[device_name(d), d["rustdesk_id"], d.get("hostname", ""), d.get("registered_at", ""),
                       _ago(d["last_seen_days"])] for d in unassigned]},
            {"title": "Devices with no label", "note": "",
             "columns": _DEV_COLS + ["Computer", "User", "Last seen"],
             "rows": [_dev_cells(d) + [d.get("hostname", ""), d.get("username", ""), _ago(d["last_seen_days"])]
                      for d in unlabeled]},
            {"title": "Same computer name on more than one device",
             "note": "Usually the client was reinstalled and got a new ID — the older entry can often be deleted.",
             "columns": ["Computer"] + _DEV_COLS + ["Registered", "Last seen"],
             "rows": [[d["hostname"]] + _dev_cells(d) + [d.get("registered_at", ""), _ago(d["last_seen_days"])]
                      for d in dupes]},
        ],
    }


def r_registrations(ctx: ReportContext) -> dict:
    devs = ctx.devices
    by_month = Counter((d.get("registered_at") or "")[:7] for d in devs if d.get("registered_at") not in ("", "—"))
    recent = sorted((d for d in devs if d["registered_days"] is not None and d["registered_days"] <= ctx.days),
                    key=lambda d: d.get("registered_at", ""), reverse=True)
    return {
        "stats": [(f"Registered in the last {ctx.days} days", len(recent), ""), ("Total devices", len(devs), "")],
        "sections": [
            {"title": f"New devices in the last {ctx.days} days", "note": "",
             "columns": _DEV_COLS + ["Computer", "OS", "Registered"],
             "rows": [_dev_cells(d) + [d.get("hostname", ""), d.get("os_name", ""), d.get("registered_at", "")]
                      for d in recent]},
            {"title": "Registrations by month", "note": "",
             "columns": ["Month", "New devices"],
             "rows": [[m, n] for m, n in sorted(by_month.items(), reverse=True)],
             "chart": _columns_chart(sorted(by_month.items()), "new device")},
        ],
    }


# ── Clients & activity ───────────────────────────────────────────────────────

def _sessions(ctx: ReportContext) -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        """SELECT rustdesk_id, started_at, ended_at,
                  CAST((julianday(COALESCE(ended_at, 'now')) - julianday(started_at)) * 86400 AS INTEGER) AS secs
           FROM device_sessions WHERE started_at >= datetime('now', ?)""",
        (ctx.since,),
    ).fetchall()
    conn.close()
    ids = ctx.device_ids
    return [dict(r) for r in rows if r["rustdesk_id"] in ids]


def r_client_groups(ctx: ReportContext) -> dict:
    devs = ctx.devices
    sessions = _sessions(ctx)
    by_rid = {d["rustdesk_id"]: d for d in devs}
    conn = get_db()
    groups = conn.execute("SELECT id, slug, display_name, created_at FROM client_groups ORDER BY display_name").fetchall()
    installers = Counter(r["group_id"] for r in conn.execute(
        "SELECT group_id FROM installers WHERE status IN ('built','signed')").fetchall())
    links = conn.execute("SELECT group_id, revoked, expires_at, max_uses, use_count FROM download_links").fetchall()
    conn.close()
    from app.routes.links import link_state
    active_links = Counter(r["group_id"] for r in links if link_state(r) == "active")
    downloads = defaultdict(int)
    for r in links:
        downloads[r["group_id"]] += r["use_count"] or 0

    sess_by_group = defaultdict(lambda: [0, 0])
    for s in sessions:
        gid = by_rid.get(s["rustdesk_id"], {}).get("group_id")
        sess_by_group[gid][0] += 1
        sess_by_group[gid][1] += max(0, s["secs"] or 0)

    rows = []
    for g in groups:
        if ctx.group and ctx.group != g["slug"]:
            continue
        gd = [d for d in devs if d.get("group_id") == g["id"]]
        n, secs = sess_by_group[g["id"]]
        rows.append([
            g["display_name"], len(gd),
            sum(d["live_status"] != "offline" for d in gd),
            sum(d["os_support"] == "ended" for d in gd),
            sum(d["os_support"] == "ending_soon" for d in gd),
            sum(bool(d.get("client_outdated")) for d in gd),
            sum(d["last_seen_days"] is None or d["last_seen_days"] > 90 for d in gd),
            n, fmt_duration(secs) if secs else "—",
            installers[g["id"]], active_links[g["id"]], downloads[g["id"]],
        ])
    return {
        "stats": [("Client groups", len(rows), ""), ("Devices", sum(r[1] for r in rows), "")],
        "sections": [{
            "title": "Health by client group",
            "note": f"Sessions are for the last {ctx.days} days. Use this to prepare per-client recommendations.",
            "columns": ["Client group", "Devices", "Online", "OS ended", "OS ending <6mo", "Outdated RustDesk",
                        "Inactive 90d+", "Sessions", "Session time", "Installers", "Active links", "Downloads"],
            "rows": rows,
            # Non-zero counts in these columns are flagged with a status badge.
            "tone_cols": {"OS ended": "critical", "OS ending <6mo": "warning",
                          "Outdated RustDesk": "warning", "Inactive 90d+": "warning"},
        }],
    }


def _every_day(per_day: Counter, days: int) -> list[tuple[str, int]]:
    """Continuous time axis, oldest first, zero-filled. Daily up to a month;
    weekly buckets (labelled by their first day) beyond, to keep columns readable."""
    from datetime import date, timedelta
    start = date.today() - timedelta(days=days - 1)
    step = 1 if days <= 31 else 7
    out = []
    for b in range(0, days, step):
        first = start + timedelta(days=b)
        n = sum(per_day.get((first + timedelta(days=i)).isoformat(), 0)
                for i in range(min(step, days - b)))
        out.append(((("Week of " if step > 1 else "") + first.isoformat()), n))
    return out


def r_sessions(ctx: ReportContext) -> dict:
    devs = ctx.devices
    by_rid = {d["rustdesk_id"]: d for d in devs}
    sessions = _sessions(ctx)
    per_dev = defaultdict(lambda: {"n": 0, "secs": 0, "last": ""})
    per_group = defaultdict(lambda: {"n": 0, "secs": 0, "devices": set()})
    per_day = Counter()
    for s in sessions:
        secs = max(0, s["secs"] or 0)
        e = per_dev[s["rustdesk_id"]]
        e["n"] += 1
        e["secs"] += secs
        e["last"] = max(e["last"], s["started_at"])
        g = by_rid.get(s["rustdesk_id"], {}).get("group_name") or "Unassigned"
        per_group[g]["n"] += 1
        per_group[g]["secs"] += secs
        per_group[g]["devices"].add(s["rustdesk_id"])
        per_day[s["started_at"][:10]] += 1
    total_secs = sum(e["secs"] for e in per_dev.values())
    untouched = [d for d in devs if d["rustdesk_id"] not in per_dev]
    return {
        "stats": [
            ("Sessions", len(sessions), f"last {ctx.days} days"),
            ("Total time", fmt_duration(total_secs) if total_secs else "—", ""),
            ("Devices accessed", len(per_dev), _pct(len(per_dev), len(devs))),
            ("Not accessed", len(untouched), "in this period"),
        ],
        "sections": [
            {"title": "By device", "note": "",
             "columns": _DEV_COLS + ["Sessions", "Total time", "Last session"],
             "rows": [_dev_cells(by_rid[rid]) + [e["n"], fmt_duration(e["secs"]), e["last"]]
                      for rid, e in sorted(per_dev.items(), key=lambda kv: -kv[1]["secs"]) if rid in by_rid]},
            {"title": "By client group", "note": "",
             "columns": ["Client group", "Sessions", "Devices accessed", "Total time"],
             "rows": [[g, e["n"], len(e["devices"]), fmt_duration(e["secs"])]
                      for g, e in sorted(per_group.items(), key=lambda kv: -kv[1]["secs"])]},
            {"title": "By day", "note": "",
             "columns": ["Date (UTC)", "Sessions"],
             "rows": [[day, n] for day, n in sorted(per_day.items(), reverse=True)],
             "chart": _columns_chart(_every_day(per_day, ctx.days), "session")},
            {"title": f"Devices with no remote session in the last {ctx.days} days", "note": "",
             "columns": _DEV_COLS + ["Last remote session", "Last seen"],
             "rows": [_dev_cells(d) + [d["last_session"] or "never", _ago(d["last_seen_days"])] for d in untouched]},
        ],
        "notice": "Session history only goes back to when session tracking was added to the dashboard.",
    }


# ── Installers & administration ──────────────────────────────────────────────

def r_installers(ctx: ReportContext) -> dict:
    try:
        from generate_installer import get_pinned_version
        pinned = get_pinned_version()
    except Exception:
        pinned = ""
    conn = get_db()
    rows = conn.execute(
        """SELECT cg.display_name, cg.slug, i.platform, i.rustdesk_version, i.status, i.created_at, i.signed_at
           FROM installers i JOIN client_groups cg ON cg.id = i.group_id
           WHERE i.id IN (SELECT MAX(id) FROM installers GROUP BY group_id, platform)
           ORDER BY cg.display_name, i.platform"""
    ).fetchall()
    links = conn.execute(
        """SELECT l.*, cg.display_name, cg.slug FROM download_links l
           JOIN client_groups cg ON cg.id = l.group_id ORDER BY l.created_at DESC"""
    ).fetchall()
    conn.close()
    from app.routes.links import link_state
    if ctx.group:
        rows = [r for r in rows if r["slug"] == ctx.group]
        links = [r for r in links if r["slug"] == ctx.group]
    stale = [r for r in rows if pinned and r["rustdesk_version"] != pinned]
    states = Counter(link_state(r) for r in links)
    return {
        "stats": [
            ("Installer version", pinned or "—", ""),
            ("Latest builds", len(rows), "per group & platform"),
            ("Need rebuilding", len(stale), "older than the pinned version"),
            ("Active download links", states["active"], f"{sum(r['use_count'] or 0 for r in links)} downloads total"),
        ],
        "sections": [
            {"title": "Latest installer per client group and platform", "note": "",
             "columns": ["Client group", "Platform", "RustDesk", "Status", "Built", "Signed", "Up to date"],
             "rows": [[r["display_name"], r["platform"], r["rustdesk_version"], r["status"], r["created_at"],
                       r["signed_at"] or "—", "yes" if not pinned or r["rustdesk_version"] == pinned else "no"]
                      for r in rows]},
            {"title": "Download links", "note": "",
             "columns": ["Client group", "Note", "State", "Downloads", "Limit", "Expires", "Emailed to",
                         "Created by", "Created", "Last used"],
             "rows": [[r["display_name"], r["note"] or "", link_state(r).replace("_", " "), r["use_count"] or 0,
                       r["max_uses"] if r["max_uses"] is not None else "∞", r["expires_at"] or "never",
                       r["emailed_to"] or "", r["created_by"] or "", r["created_at"], r["last_used_at"] or "—"]
                      for r in links]},
        ],
    }


def r_user_activity(ctx: ReportContext) -> dict:
    conn = get_db()
    rows = conn.execute(
        """SELECT event, detail, user_email, created_at FROM provisioning_events
           WHERE created_at >= datetime('now', ?) ORDER BY created_at DESC""",
        (ctx.since,),
    ).fetchall()
    users = conn.execute("SELECT email, display_name, role, auth_method, created_at FROM users ORDER BY email").fetchall()
    passkeys = Counter(r["user_id"] for r in conn.execute("SELECT user_id FROM webauthn_credentials").fetchall())
    user_ids = {r["email"]: r["id"] for r in conn.execute("SELECT id, email FROM users").fetchall()}
    conn.close()
    by_user = defaultdict(lambda: {"n": 0, "last": ""})
    for r in rows:
        if not r["user_email"]:
            continue
        e = by_user[r["user_email"]]
        e["n"] += 1
        e["last"] = max(e["last"], r["created_at"])
    by_event = Counter(r["event"] for r in rows)
    return {
        "stats": [
            ("Events", len(rows), f"last {ctx.days} days"),
            ("Active users", len(by_user), f"of {len(users)}"),
        ],
        "sections": [
            {"title": "Activity by user", "note": "",
             "columns": ["User", "Actions", "Last action"],
             "rows": [[u, e["n"], e["last"]] for u, e in sorted(by_user.items(), key=lambda kv: -kv[1]["n"])]},
            {"title": "Activity by event type", "note": "",
             "columns": ["Event", "Count"],
             "rows": [[ev.replace("_", " "), n] for ev, n in by_event.most_common()]},
            {"title": "User accounts", "note": "",
             "columns": ["Email", "Name", "Role", "Sign-in method", "Passkeys", "Created", "Last action"],
             "rows": [[u["email"], u["display_name"] or "", u["role"], u["auth_method"],
                       passkeys[user_ids.get(u["email"])], u["created_at"], by_user[u["email"]]["last"] or "—"]
                      for u in users]},
            {"title": "All events", "note": "",
             "columns": ["When (UTC)", "Event", "Detail", "User"],
             "rows": [[r["created_at"], r["event"].replace("_", " "), r["detail"] or "", r["user_email"] or ""]
                      for r in rows]},
        ],
    }


# ── Catalogue ────────────────────────────────────────────────────────────────
# key -> (title, description, category, uses_period, function)
# Reports that ignore the client-group filter (they aren't about devices):
NO_GROUP_FILTER = {"user-activity"}
# Default look-back (days) where 30 isn't the natural choice.
DEFAULT_DAYS = {"inactive": 90}
REPORTS: dict[str, tuple] = {
    "fleet-overview": ("Fleet overview", "Totals and breakdowns by client group, OS, support status and RustDesk version.",
                       "Fleet", False, r_fleet_overview),
    "inventory": ("Device inventory", "Every device with its hardware, OS, version and activity — one row each.",
                  "Fleet", False, r_inventory),
    "os-support": ("Operating system support", "Devices on an OS past or near Microsoft end of support (e.g. Windows 10, Server 2012/2016).",
                   "Maintenance", False, r_os_support),
    "inactive": ("Inactive devices", "Devices that haven't checked in for 30 days to over a year.",
                 "Maintenance", True, r_inactive),
    "client-versions": ("RustDesk client versions", "Version spread, outdated clients and devices not reporting.",
                        "Maintenance", False, r_client_versions),
    "hardware": ("Hardware", "Memory, CPU and cores — with low-spec upgrade candidates.",
                 "Maintenance", False, r_hardware),
    "data-quality": ("Data quality", "Unassigned and unlabeled devices, and duplicate computer names.",
                     "Maintenance", False, r_data_quality),
    "client-groups": ("Client group health", "One row per client: devices, OS risk, outdated clients, inactivity, sessions, installers.",
                      "Clients", True, r_client_groups),
    "sessions": ("Remote sessions", "Sessions and time by device, client group and day — and devices never accessed.",
                 "Clients", True, r_sessions),
    "registrations": ("New devices", "Devices registered in a period, and registrations by month.",
                      "Clients", True, r_registrations),
    "installers": ("Installers & download links", "Latest installer per client and platform, and download link usage.",
                   "Administration", False, r_installers),
    "user-activity": ("User activity", "Who did what in the dashboard, sign-ins, and account overview.",
                      "Administration", True, r_user_activity),
}
CATEGORIES = ["Fleet", "Maintenance", "Clients", "Administration"]
