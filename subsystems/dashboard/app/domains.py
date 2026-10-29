"""Tenants' custom domains (phase 1d; TENANCY.md, "Custom domains").

A tenant adds a hostname it owns (rds.abc.example) and points it at us: a
CNAME to its own <slug>.<PROVIDER_DOMAIN>, or A/AAAA records at this box plus
a TXT record holding a per-domain token (app/domain_dns.py). Once DNS passes,
rdf-helper issues a Let's Encrypt certificate and writes the vhost (it checks
DNS again itself first), and the domain serves the tenant's dashboard and
download links. Devices never use a custom domain: installers keep the relay
host and the provider subdomain (TENANCY.md).

Statuses (tenant_domains.status, kind 'custom'):

    verifying            queued for a DNS check now (just added, or Check now)
    pending_dns          DNS doesn't point here yet; re-checked every 5 minutes
                         for 7 days (pending_until)
    issuing_certificate  DNS passes; the certificate is being issued (at most
                         one attempt an hour, at most 5 failures)
    active               served, with its own certificate
    dns_broken           was active, DNS stopped pointing here; still served
                         for 7 days (broken_since), then removed
    failed               gave up (7 days without DNS, 5 failed issuances, or
                         broken for 7 days); the hostname is free for anyone

The provider subdomain and tenant #1's legacy host are always 'active'.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from app import settings, tenancy

STATUSES = ("verifying", "pending_dns", "issuing_certificate", "active", "dns_broken", "failed")
# Statuses whose hostname reaches the tenant (nginx has a vhost for it).
SERVED = ("active", "dns_broken")

PENDING_DAYS = 7          # how long a new domain is checked before we give up
BROKEN_DAYS = 7           # how long a broken domain is still served
CHECK_EVERY_MIN = 5       # pending domains
ISSUE_EVERY_MIN = 60      # Let's Encrypt: at most one attempt an hour per domain
MAX_ISSUE_FAILURES = 5    # then stop until the tenant presses Check now

COLUMNS = (
    ("status", "TEXT NOT NULL DEFAULT 'active'"),
    ("verify_token", "TEXT"),
    ("check_method", "TEXT"),              # 'cname' or 'txt': what passed last
    ("last_checked_at", "TEXT"),
    ("last_check_ok", "INTEGER"),
    ("last_check_message", "TEXT"),
    ("next_check_at", "TEXT"),
    ("pending_until", "TEXT"),
    ("issue_failures", "INTEGER NOT NULL DEFAULT 0"),
    ("last_issue_at", "TEXT"),
    ("broken_since", "TEXT"),
    ("remove_reason", "TEXT"),             # set = removal queued (the worker does it)
    ("created_by", "TEXT"),
)


class DomainError(Exception):
    """A domain can't be added or changed; the message is for the tenant."""


def ensure_columns(conn) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tenant_domains)")}
    for col, ddl in COLUMNS:
        if col not in cols:
            conn.execute(f"ALTER TABLE tenant_domains ADD COLUMN {col} {ddl}")
    conn.commit()


def now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def fmt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def parse(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def new_token() -> str:
    return "rdf-verify-" + secrets.token_urlsafe(24)


def is_released(row, at: datetime | None = None) -> bool:
    """A custom domain anyone may claim: given up on, or added more than
    PENDING_DAYS ago and never verified."""
    if row["kind"] != "custom":
        return False
    if row["status"] == "failed":
        return True
    created = parse(row["created_at"])
    return (row["verified_at"] is None and row["status"] in ("verifying", "pending_dns", "issuing_certificate")
            and created is not None and created < (at or now()) - timedelta(days=PENDING_DAYS))


def normalize(raw: str) -> str:
    return (raw or "").strip().lower().rstrip(".")


def add(conn, tenant, raw_hostname: str, added_by: str) -> int:
    """Add a custom domain for `tenant` (status 'verifying'; the worker checks
    it). Returns the row id; DomainError says why it can't be added."""
    from app import domain_dns
    if tenant.status not in tenancy.OPEN_STATUSES:
        raise DomainError("Custom domains can only be added while the account is active.")
    hostname = normalize(raw_hostname)
    problem = domain_dns.domain_problem(hostname)
    if problem:
        raise DomainError(problem)
    existing = conn.execute("SELECT * FROM tenant_domains WHERE hostname = ?", (hostname,)).fetchone()
    if existing is not None:
        if existing["tenant_id"] == tenant.id:
            raise DomainError(f"{hostname} is already on your list.")
        if not is_released(existing):
            raise DomainError(f"{hostname} is already in use.")
        # Another tenant's stale claim: release it (its row goes, nothing was served).
        conn.execute("DELETE FROM tenant_domains WHERE id = ? AND tenant_id = ?", (existing["id"], existing["tenant_id"]))
    count = conn.execute("SELECT COUNT(*) FROM tenant_domains WHERE tenant_id = ? AND kind = 'custom'",
                         (tenant.id,)).fetchone()[0]
    limit = settings.max_custom_domains()
    if count >= limit:
        raise DomainError(f"You can have at most {limit} custom domain{'s' if limit != 1 else ''}. "
                          "Remove one to add another.")
    t = now()
    cur = conn.execute(
        """INSERT INTO tenant_domains (tenant_id, hostname, kind, is_primary, status, verify_token, next_check_at,
                                       pending_until, created_by, created_at)
           VALUES (?, ?, 'custom', 0, 'verifying', ?, ?, ?, ?, ?)""",
        (tenant.id, hostname, new_token(), fmt(t), fmt(t + timedelta(days=PENDING_DAYS)), added_by[:200], fmt(t)))
    conn.commit()
    tenancy.invalidate_cache()
    return cur.lastrowid


# ── Lookups ──────────────────────────────────────────────────────────────────

def home_host(conn, tenant_id: int) -> str:
    """The tenant's own web host that always works: its provider subdomain,
    or tenant #1's legacy host. Sign-in falls back to it, passkeys belong to
    it (its RP ID), and it is primary unless an active custom domain is."""
    row = conn.execute("SELECT hostname FROM tenant_domains WHERE tenant_id = ? AND kind <> 'custom'"
                       " ORDER BY kind = 'provider_subdomain' DESC, id LIMIT 1", (tenant_id,)).fetchone()
    return row[0] if row else ""


def primary_host(conn, tenant_id: int) -> str:
    """The host emails, invites, download links and client guides use."""
    row = conn.execute("SELECT hostname FROM tenant_domains WHERE tenant_id = ? AND is_primary = 1"
                       " AND (kind <> 'custom' OR status IN ('active', 'dns_broken'))", (tenant_id,)).fetchone()
    return row[0] if row else home_host(conn, tenant_id)


def primary_base_url(tenant_id: int) -> str:
    from app.deps import get_db
    conn = get_db()
    try:
        return f"https://{primary_host(conn, tenant_id)}"
    finally:
        conn.close()


def served_custom_hosts(conn, tenant_id: int) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT hostname FROM tenant_domains WHERE tenant_id = ? AND kind = 'custom' AND status IN ('active', 'dns_broken')"
        " AND remove_reason IS NULL ORDER BY id", (tenant_id,))]


def list_for(conn, tenant_id: int) -> list:
    return conn.execute("SELECT * FROM tenant_domains WHERE tenant_id = ?"
                        " ORDER BY kind = 'custom', id", (tenant_id,)).fetchall()


def _row(conn, tenant_id: int, domain_id: int):
    row = conn.execute("SELECT * FROM tenant_domains WHERE id = ? AND tenant_id = ?", (domain_id, tenant_id)).fetchone()
    if row is None:
        raise DomainError("That domain isn't on your list.")
    return row


# ── Tenant actions (the page; the slow parts happen in the worker) ───────────

def check_now(conn, tenant_id: int, domain_id: int) -> None:
    """Queue a DNS check now. Restarts the 7-day window and the issuance
    failure count (Let's Encrypt's hourly limit still applies)."""
    row = _row(conn, tenant_id, domain_id)
    if row["kind"] != "custom" or row["remove_reason"]:
        raise DomainError("Only custom domains are checked.")
    t = now()
    if row["status"] in SERVED:
        conn.execute("UPDATE tenant_domains SET next_check_at = ? WHERE id = ? AND tenant_id = ?",
                     (fmt(t), domain_id, tenant_id))
    else:
        conn.execute(
            "UPDATE tenant_domains SET status = 'verifying', next_check_at = ?, pending_until = ?, issue_failures = 0"
            " WHERE id = ? AND tenant_id = ?", (fmt(t), fmt(t + timedelta(days=PENDING_DAYS)), domain_id, tenant_id))
    conn.commit()
    kick()


def make_primary(conn, tenant_id: int, domain_id: int) -> str:
    row = _row(conn, tenant_id, domain_id)
    if row["remove_reason"] or (row["kind"] == "custom" and row["status"] != "active"):
        raise DomainError("Only an active domain can be primary.")
    _set_primary(conn, tenant_id, row["id"])
    conn.commit()
    tenancy.invalidate_cache()
    queue_guides(tenant_id)
    return row["hostname"]


def _set_primary(conn, tenant_id: int, domain_id: int) -> None:
    conn.execute("UPDATE tenant_domains SET is_primary = 0 WHERE tenant_id = ? AND is_primary = 1", (tenant_id,))
    conn.execute("UPDATE tenant_domains SET is_primary = 1 WHERE id = ? AND tenant_id = ?", (domain_id, tenant_id))


def _fall_back_to_home(conn, tenant_id: int) -> None:
    home = conn.execute("SELECT id FROM tenant_domains WHERE tenant_id = ? AND kind <> 'custom'"
                        " ORDER BY kind = 'provider_subdomain' DESC, id LIMIT 1", (tenant_id,)).fetchone()
    if home:
        _set_primary(conn, tenant_id, home[0])
        queue_guides(tenant_id)


def request_removal(conn, tenant_id: int, domain_id: int, reason: str) -> str:
    """Stop using a custom domain now and queue the removal of its vhost,
    certificate and row (the worker does it). The home host becomes primary
    again if this one was."""
    row = _row(conn, tenant_id, domain_id)
    if row["kind"] != "custom":
        raise DomainError("This domain is part of your account and can't be removed.")
    if row["is_primary"]:
        _fall_back_to_home(conn, tenant_id)
    conn.execute("UPDATE tenant_domains SET remove_reason = ?, is_primary = 0, next_check_at = ?"
                 " WHERE id = ? AND tenant_id = ?", (reason[:300] or "removed", fmt(now()), domain_id, tenant_id))
    conn.commit()
    tenancy.invalidate_cache()
    kick()
    return row["hostname"]


# ── Background worker ────────────────────────────────────────────────────────
# One thread: DNS lookups and certificate issuance take seconds to minutes
# and must never run in the single uvicorn worker.

_wake = None
_started = False
_guides: set[int] = set()


def kick() -> None:
    if _wake is not None:
        _wake.set()


def queue_guides(tenant_id: int) -> None:
    """Rebuild the tenant's client PDF guides (they show its primary host)."""
    _guides.add(tenant_id)
    kick()


def start() -> None:
    global _started, _wake
    from app import paths
    if _started or not paths.WATCHERS_ENABLED:
        return
    import threading
    _started = True
    _wake = threading.Event()

    def loop():
        import logging
        log = logging.getLogger("rdf.domains")
        while True:
            try:
                run_due()
            except Exception:
                log.exception("custom-domain worker")
            _wake.wait(30)
            _wake.clear()

    threading.Thread(target=loop, daemon=True, name="custom-domains").start()


def run_due(at: datetime | None = None) -> None:
    """Everything due now: removals, DNS checks, certificate issuance, guide
    rebuilds. Tests call this directly."""
    from app.deps import get_db
    t = at or now()
    conn = get_db()
    removals = conn.execute("SELECT id, tenant_id FROM tenant_domains WHERE kind = 'custom'"
                            " AND remove_reason IS NOT NULL").fetchall()
    checks = conn.execute(
        "SELECT id, tenant_id FROM tenant_domains WHERE kind = 'custom' AND remove_reason IS NULL"
        " AND status IN ('verifying', 'pending_dns', 'issuing_certificate') AND next_check_at <= ?", (fmt(t),)).fetchall()
    conn.close()
    for r in removals:
        _remove(r["tenant_id"], r["id"])
    for r in checks:
        _advance(r["tenant_id"], r["id"], t)
    while _guides:
        _rebuild_guides(_guides.pop())


def _tenant_row(conn, tenant_id: int):
    return conn.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()


def _advance(tenant_id: int, domain_id: int, t: datetime) -> None:
    from app.deps import get_db
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM tenant_domains WHERE id = ? AND tenant_id = ?", (domain_id, tenant_id)).fetchone()
        tenant = _tenant_row(conn, tenant_id)
        if row is None or tenant is None or tenant["status"] not in tenancy.OPEN_STATUSES:
            return
        host = row["hostname"]
        until = parse(row["pending_until"])
        if row["status"] != "issuing_certificate" and until and t > until:
            conn.execute("UPDATE tenant_domains SET status = 'failed', next_check_at = NULL, last_check_message = ?"
                         " WHERE id = ? AND tenant_id = ?",
                         (f"We stopped checking after {PENDING_DAYS} days without the DNS record. Fix the record and "
                          "press Check now to try again.", domain_id, tenant_id))
            _log(conn, tenant_id, "domain_failed", f"{host}: no DNS after {PENDING_DAYS} days")
            conn.commit()
            notify_admins(tenant_id, f"{host} wasn't set up",
                          [f"We checked {host} for {PENDING_DAYS} days and its DNS never pointed at us, so we "
                           "stopped. Open Admin → Domains, fix the record and press Check now to try again."])
            return
        if row["status"] == "issuing_certificate":
            last = parse(row["last_issue_at"])
            if last and t - last < timedelta(minutes=ISSUE_EVERY_MIN):
                conn.execute("UPDATE tenant_domains SET next_check_at = ? WHERE id = ? AND tenant_id = ?",
                             (fmt(last + timedelta(minutes=ISSUE_EVERY_MIN)), domain_id, tenant_id))
                conn.commit()
                return
        else:
            result = _dns_check(host, home_host(conn, tenant_id), row["verify_token"])
            conn.execute(
                "UPDATE tenant_domains SET last_checked_at = ?, last_check_ok = ?, last_check_message = ?,"
                " check_method = COALESCE(?, check_method), status = ?, next_check_at = ? WHERE id = ? AND tenant_id = ?",
                (fmt(t), 1 if result.ok else 0, "" if result.ok else result.reason, result.method or None,
                 "issuing_certificate" if result.ok else "pending_dns",
                 fmt(t) if result.ok else fmt(t + timedelta(minutes=CHECK_EVERY_MIN)), domain_id, tenant_id))
            conn.commit()
            if not result.ok:
                return
        _issue(conn, tenant_id, domain_id, host, t)
    finally:
        conn.close()


def _dns_check(host: str, target: str, token: str):
    from app import domain_dns
    try:
        return domain_dns.check(host, target, token or "")
    except Exception as e:      # dig missing, bad settings: say so on the page
        return domain_dns.helper().DnsResult(False, "lookup_failed", f"The DNS check couldn't run: {e}")


def _issue(conn, tenant_id: int, domain_id: int, host: str, t: datetime) -> None:
    """Certificate, vhost and a checked nginx reload (rdf-helper checks DNS
    again itself). At most one attempt an hour, MAX_ISSUE_FAILURES in all."""
    from app.console import web
    conn.execute("UPDATE tenant_domains SET last_issue_at = ? WHERE id = ? AND tenant_id = ?", (fmt(t), domain_id, tenant_id))
    conn.commit()
    try:
        web.publish(host)
    except Exception as e:
        failures = (conn.execute("SELECT issue_failures FROM tenant_domains WHERE id = ?", (domain_id,)).fetchone()[0]
                    or 0) + 1
        gave_up = failures >= MAX_ISSUE_FAILURES
        msg = f"Getting a certificate failed: {str(e)[-300:]}"
        if gave_up:
            msg += f" We stopped after {MAX_ISSUE_FAILURES} attempts; press Check now to try again."
        conn.execute(
            "UPDATE tenant_domains SET issue_failures = ?, last_check_message = ?, status = ?, next_check_at = ?"
            " WHERE id = ? AND tenant_id = ?",
            (failures, msg, "failed" if gave_up else "issuing_certificate",
             None if gave_up else fmt(t + timedelta(minutes=ISSUE_EVERY_MIN)), domain_id, tenant_id))
        _log(conn, tenant_id, "domain_certificate_failed", f"{host}: {str(e)[-200:]}")
        conn.commit()
        if gave_up:
            notify_admins(tenant_id, f"{host} couldn't get a certificate",
                          [f"We tried {MAX_ISSUE_FAILURES} times to get a certificate for {host} and stopped. "
                           "Open Admin → Domains to see why, then press Check now to try again."])
        return
    conn.execute(
        "UPDATE tenant_domains SET status = 'active', verified_at = COALESCE(verified_at, ?), tls_status = 'active',"
        " cert_expires_at = ?, issue_failures = 0, next_check_at = NULL, last_check_message = '', broken_since = NULL"
        " WHERE id = ? AND tenant_id = ?", (fmt(t), web.cert_expiry(host), domain_id, tenant_id))
    _log(conn, tenant_id, "domain_active", host)
    conn.commit()
    tenancy.invalidate_cache()
    notify_admins(tenant_id, f"{host} is live",
                  [f"https://{host}/ now opens your dashboard, with its own certificate. To use it in emails, "
                   "invitations and download links, make it the primary domain in Admin → Domains."])


def _remove(tenant_id: int, domain_id: int) -> None:
    from app.console import web
    from app.deps import get_db
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM tenant_domains WHERE id = ? AND tenant_id = ?", (domain_id, tenant_id)).fetchone()
        if row is None:
            return
        try:
            web.release(row["hostname"])
        except Exception as e:
            conn.execute("UPDATE tenant_domains SET last_check_message = ? WHERE id = ?",
                         (f"Removing it failed, retrying: {str(e)[-300:]}", domain_id))
            conn.commit()
            return
        if row["is_primary"]:
            _fall_back_to_home(conn, tenant_id)
        conn.execute("DELETE FROM tenant_domains WHERE id = ? AND tenant_id = ?", (domain_id, tenant_id))
        _log(conn, tenant_id, "domain_removed", f"{row['hostname']} ({row['remove_reason']})")
        conn.commit()
        tenancy.invalidate_cache()
    finally:
        conn.close()


def _rebuild_guides(tenant_id: int) -> None:
    from app.console import tenants as console_tenants
    from app.deps import get_db
    conn = get_db()
    t = _tenant_row(conn, tenant_id)
    host = primary_host(conn, tenant_id)
    conn.close()
    if t is None or not console_tenants.guides_dir(tenant_id).exists():
        return      # tenant #1 uses the generic guides
    try:
        console_tenants.generate_guides(tenant_id, t["display_name"], host)
    except Exception:
        import logging
        logging.getLogger("rdf.domains").exception("rebuilding the client guides of tenant %s", tenant_id)


def _log(conn, tenant_id: int, event: str, detail: str) -> None:
    from app.deps import log_event
    log_event(conn, event, detail, "", tenant_id=tenant_id)


def notify_admins(tenant_id: int, subject: str, lines: list[str]) -> None:
    """Email the tenant's admins (provider mail). Best effort."""
    try:
        from app.console import mail
        from app.deps import get_db
        conn = get_db()
        to = [r[0] for r in conn.execute("SELECT email FROM users WHERE tenant_id = ? AND role = 'admin'", (tenant_id,))]
        name = (conn.execute("SELECT display_name FROM tenants WHERE id = ?", (tenant_id,)).fetchone() or [""])[0]
        conn.close()
        if to:
            mail.send(to, f"{name}: {subject}", mail.simple_html(subject, lines), event="custom_domain_email")
    except Exception:
        pass
