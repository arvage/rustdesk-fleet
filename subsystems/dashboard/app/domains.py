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
