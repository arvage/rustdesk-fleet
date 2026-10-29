"""Tenant lifecycle: activate, suspend, reactivate, cancel, purge (TENANCY.md).

- activate:    trial → active, once the tenant has subscribed.
- trial:       new tenants get TRIAL_DAYS; their admins are emailed
               TRIAL_WARN_DAYS before the end, and at the end the tenant is
               suspended. Reactivating goes back to trial while trial time is
               left (see extend_trial), otherwise to active.
- suspend:     the dashboard and download links show "not active"; the relay
               keeps running, so devices stay registered for reactivation.
- reactivate:  from suspended, or from cancelled within the grace period
               (the relay is started again with the same keys and ports).
- cancel:      the relay stops at once (containers and data kept), and the
               tenant's admins can sign in to a read-only export page only.
               After GRACE_DAYS the tenant is purged.
- purge:       relay containers, compose file, keypair and hbbs data; the
               tenant's files; its vhosts and certificates; every tenant-owned
               row. Kept: the tenants row (status purged, slug reserved), the
               port block, tenant_usage_daily and the provider audit log.

Tenant #1 can't be cancelled or purged.
"""
from __future__ import annotations

import shutil
from datetime import datetime, timedelta, timezone

from app.console import jobs, mail, relays, web
from app.deps import get_db, provider_log
from app.tenancy import invalidate_cache, load_tenant, tenant_files_dir
from app.tenant_migration import LEGACY_TENANT_ID

GRACE_DAYS = 30
WARN_DAYS = 7
TRIAL_DAYS = 14
TRIAL_WARN_DAYS = 3


class LifecycleError(jobs.JobFailed):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _row(conn, tenant_id: int):
    return conn.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()


def _set(conn, tenant_id: int, **cols) -> None:
    sets = ", ".join(f"{k} = ?" for k in cols)
    conn.execute(f"UPDATE tenants SET {sets}, updated_at = datetime('now') WHERE id = ?", (*cols.values(), tenant_id))
    conn.commit()
    invalidate_cache()
    if cols.get("status") in ("suspended", "cancelled", "purged"):
        # A closed tenant's support sessions end now (and its grant can't be
        # used while it's closed: app/support.py checks the tenant).
        from app import support
        support.end_all_sessions(tenant_id, f"tenant {cols['status']}")


def guard(tenant_id: int, action: str) -> None:
    if tenant_id == LEGACY_TENANT_ID and action in ("cancel", "purge"):
        raise LifecycleError(f"Tenant #1 can't be {'cancelled' if action == 'cancel' else 'purged'}.")
    if action == "purge":
        conn = get_db()
        live = conn.execute("SELECT s.status FROM billing_subscriptions s JOIN tenants t ON t.id = s.tenant_id"
                            " WHERE s.tenant_id = ? AND t.billing_mode = 'stripe'"
                            " AND s.status NOT IN ('canceled', 'incomplete_expired')", (tenant_id,)).fetchone()
        conn.close()
        if live:
            raise LifecycleError(f"Its Stripe subscription is {live[0]}; cancel it first (purging requires that).")


def admin_emails(conn, tenant_id: int) -> list[str]:
    return [r[0] for r in conn.execute("SELECT email FROM users WHERE tenant_id = ? AND role = 'admin'", (tenant_id,))]


# ── Immediate actions (no docker work) ───────────────────────────────────────

def suspend(tenant_id: int, by: str) -> None:
    conn = get_db()
    t = _row(conn, tenant_id)
    if t is None or t["status"] not in ("trial", "active"):
        conn.close()
        raise LifecycleError("Only a trial or active tenant can be suspended.")
    _set(conn, tenant_id, status="suspended", billing_suspended=0)     # a provider's: paying doesn't lift it
    provider_log(conn, "tenant_suspended", t["slug"], by, tenant_id)
    conn.close()


def activate(tenant_id: int, by: str) -> None:
    """Trial → active: the tenant has subscribed."""
    conn = get_db()
    t = _row(conn, tenant_id)
    if t is None or t["status"] != "trial":
        conn.close()
        raise LifecycleError("Only a trial tenant can be activated.")
    _set(conn, tenant_id, status="active", trial_ends_at=None, trial_warning_sent_at=None)
    provider_log(conn, "tenant_activated", t["slug"], by, tenant_id)
    conn.close()


def extend_trial(tenant_id: int, days: int, by: str) -> str:
    """Push the trial end out by `days` (from now if it has already passed).
    A tenant suspended at the end of its trial stays suspended until it is
    reactivated, which then puts it back on trial."""
    conn = get_db()
    t = _row(conn, tenant_id)
    if t is None or not t["trial_ends_at"] or t["status"] not in ("trial", "suspended"):
        conn.close()
        raise LifecycleError("Only a tenant on trial, or suspended at the end of one, has a trial to extend.")
    days = max(1, min(365, int(days)))
    base = max(_now(), datetime.strptime(t["trial_ends_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc))
    new = base + timedelta(days=days)
    _set(conn, tenant_id, trial_ends_at=_fmt(new), trial_warning_sent_at=None)
    provider_log(conn, "tenant_trial_extended", f"{t['slug']} +{days}d → {_fmt(new)}", by, tenant_id)
    conn.close()
    return _fmt(new)


def reopened_status(row) -> str:
    """The status a reactivated tenant goes back to: trial while its trial
    has time left, otherwise active."""
    return "trial" if row["trial_ends_at"] and row["trial_ends_at"] > _fmt(_now()) else "active"


def extend_grace(tenant_id: int, days: int, by: str) -> str:
    conn = get_db()
    t = _row(conn, tenant_id)
    if t is None or t["status"] != "cancelled" or not t["purge_after"]:
        conn.close()
        raise LifecycleError("Only a cancelled tenant has a grace period.")
    days = max(1, min(365, int(days)))
    new = datetime.strptime(t["purge_after"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) + timedelta(days=days)
    _set(conn, tenant_id, purge_after=_fmt(new), purge_warning_sent_at=None)
    provider_log(conn, "tenant_grace_extended", f"{t['slug']} +{days}d → {_fmt(new)}", by, tenant_id)
    conn.close()
    return _fmt(new)


def set_plan(tenant_id: int, plan: str, by: str) -> None:
    conn = get_db()
    t = _row(conn, tenant_id)
    if t is None:
        conn.close()
        raise LifecycleError("Tenant not found.")
    _set(conn, tenant_id, plan=plan.strip()[:200])
    provider_log(conn, "tenant_plan_changed", f"{t['slug']}: {plan.strip()[:200]}", by, tenant_id)
    conn.close()


# ── Jobs (docker / helper work) ──────────────────────────────────────────────

def _notify_admins(conn, tenant_id: int, subject: str, lines: list[str]) -> None:
    to = admin_emails(conn, tenant_id)
    if to:
        mail.send(to, subject, mail.simple_html(subject, lines), event="tenant_lifecycle_email")


@jobs.handler("cancel_tenant")
def _cancel(job) -> None:
    guard(job.tenant_id, "cancel")
    t = load_tenant(job.tenant_id)
    if t is None or t.status not in ("trial", "active", "suspended"):
        raise LifecycleError("This tenant can't be cancelled from its current status.")
    if t.relay:
        with job.step("Stop the relay (containers and keys are kept)"):
            try:
                relays.stop(t)
            except relays.RelayError as e:
                raise LifecycleError(str(e))
    with job.step("Mark the tenant cancelled"):
        now = _now()
        purge_after = now + timedelta(days=GRACE_DAYS)
        conn = get_db()
        _set(conn, t.id, status="cancelled", cancelled_at=_fmt(now), purge_after=_fmt(purge_after),
             purge_warning_sent_at=None)
        provider_log(conn, "tenant_cancelled", f"{t.slug}; purge after {_fmt(purge_after)}", job.created_by, t.id)
        conn.close()
        from app import billing_emails
        billing_emails.cancelled(t.id, purge_after.strftime("%b %d, %Y"), t.billing_mode == "stripe")


@jobs.handler("reactivate_tenant")
def _reactivate(job) -> None:
    conn = get_db()
    row = _row(conn, job.tenant_id)
    conn.close()
    if row is None or row["status"] not in ("suspended", "cancelled"):
        raise LifecycleError("Only a suspended or cancelled tenant can be reactivated.")
    if row["status"] == "cancelled":
        if not row["purge_after"] or row["purge_after"] <= _fmt(_now()):
            raise LifecycleError("The grace period is over; this tenant can't be reactivated.")
        t = load_tenant(job.tenant_id)
        if t.relay:
            with job.step("Start the relay again (same keys and ports)"):
                try:
                    relays.start(t)
                except relays.RelayError as e:
                    raise LifecycleError(str(e))
    status = reopened_status(row)
    with job.step(f"Mark the tenant {'back on trial' if status == 'trial' else 'active'}"):
        conn = get_db()
        trial = {} if status == "trial" else {"trial_ends_at": None, "trial_warning_sent_at": None}
        _set(conn, job.tenant_id, status=status, cancelled_at=None, purge_after=None, purge_warning_sent_at=None,
             billing_suspended=0, **trial)
        provider_log(conn, "tenant_reactivated", row["slug"], job.created_by, job.tenant_id)
        conn.close()
    if row["billing_mode"] == "stripe":
        # Stripe decides trial or active, and the dates.
        from app import billing
        sub = billing.subscription_of(job.tenant_id)
        if sub is not None:
            with job.step("Sync the subscription from Stripe"):
                billing.sync_subscription(sub["stripe_subscription_id"])


def purge_now(tenant_id: int, job=None, by: str = "") -> None:
    """Delete everything of a cancelled tenant except what TENANCY.md says to
    keep. Idempotent: a purge that stopped half-way can simply run again."""
    from app.console.tenants import delete_tenant_rows

    guard(tenant_id, "purge")

    def step(name):
        return job.step(name) if job else _nullstep()

    conn = get_db()
    row = _row(conn, tenant_id)
    conn.close()
    if row is None or row["status"] not in ("cancelled", "purged"):
        raise LifecycleError("Only a cancelled tenant can be purged.")
    slug = row["slug"]
    with step("Remove the relay"):
        try:
            relays.remove(tenant_id)
            web.purge_relay_data(tenant_id)
        except (relays.RelayError, web.WebError) as e:
            raise LifecycleError(str(e))
    conn = get_db()
    hosts = conn.execute("SELECT hostname, kind FROM tenant_domains WHERE tenant_id = ?", (tenant_id,)).fetchall()
    conn.close()
    for host, kind in hosts:
        with step(f"Remove https://{host} (vhost and certificate)"):
            try:
                # A custom domain is released whatever its DNS says now (the
                # tenant is cancelled, so the helper won't treat it as live).
                web.release(host) if kind == "custom" else web.unpublish(host)
            except web.WebError as e:
                raise LifecycleError(str(e))
    with step("Delete the tenant's files"):
        shutil.rmtree(tenant_files_dir(tenant_id), ignore_errors=True)
        if tenant_files_dir(tenant_id).exists():
            raise LifecycleError(f"Couldn't delete {tenant_files_dir(tenant_id)}.")
    with step("Delete the tenant's data"):
        conn = get_db()
        conn.execute("DELETE FROM tenant_domains WHERE tenant_id = ?", (tenant_id,))
        conn.execute("DELETE FROM relays WHERE tenant_id = ?", (tenant_id,))
        delete_tenant_rows(conn, tenant_id, keep_provider_events=True)
        _set(conn, tenant_id, status="purged", purged_at=_fmt(_now()))
        provider_log(conn, "tenant_purged", slug, by or (job.created_by if job else "scheduled"), tenant_id)
        conn.close()


class _nullstep:
    def __enter__(self):
        return {}

    def __exit__(self, *exc):
        return False


@jobs.handler("purge_tenant")
def _purge(job) -> None:
    purge_now(job.tenant_id, job)


# ── Daily: warnings and due purges ───────────────────────────────────────────

def daily(now: datetime | None = None) -> None:
    now = now or _now()
    conn = get_db()
    rows = conn.execute("SELECT * FROM tenants WHERE status = 'cancelled' AND purge_after IS NOT NULL").fetchall()
    for t in rows:
        due = datetime.strptime(t["purge_after"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        if due <= now:
            if not jobs.active_for(t["id"]):
                jobs.enqueue("purge_tenant", t["id"], {"reason": "grace period over"}, "scheduled")
        elif due - now <= timedelta(days=WARN_DAYS) and not t["purge_warning_sent_at"]:
            _notify_admins(conn, t["id"], f"Your {t['display_name']} RustDesk Fleet data will be deleted soon", [
                f"This cancelled account and all its data will be deleted on {due.strftime('%Y-%m-%d')}.",
                "Sign in before then to download your data from the export page.",
            ])
            _set(conn, t["id"], purge_warning_sent_at=_fmt(now))
            provider_log(conn, "tenant_purge_warning_sent", t["slug"], "scheduled", t["id"])
    _trials(conn, now)
    conn.close()


def _trials(conn, now: datetime) -> None:
    # Only manual tenants: Stripe runs Stripe tenants' trials, internal ones have none.
    rows = conn.execute("SELECT * FROM tenants WHERE status = 'trial' AND trial_ends_at IS NOT NULL AND id != ?"
                        " AND billing_mode = 'manual'", (LEGACY_TENANT_ID,)).fetchall()
    for t in rows:
        due = datetime.strptime(t["trial_ends_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        if due <= now:
            _set(conn, t["id"], status="suspended")
            provider_log(conn, "tenant_trial_expired", f"{t['slug']}; suspended", "scheduled", t["id"])
            _notify_admins(conn, t["id"], f"Your {t['display_name']} RustDesk Fleet trial has ended", [
                "Your trial has ended and the account is suspended: sign-in and installer downloads are off.",
                "Your devices, groups and settings are kept. Contact your provider to subscribe and reopen it.",
            ])
        elif due - now <= timedelta(days=TRIAL_WARN_DAYS) and not t["trial_warning_sent_at"]:
            _notify_admins(conn, t["id"], f"Your {t['display_name']} RustDesk Fleet trial ends soon", [
                f"Your trial ends on {due.strftime('%Y-%m-%d')}. After that the account is suspended:"
                " sign-in and installer downloads stop until you subscribe.",
                "Contact your provider to subscribe.",
            ])
            _set(conn, t["id"], trial_warning_sent_at=_fmt(now))
            provider_log(conn, "tenant_trial_warning_sent", t["slug"], "scheduled", t["id"])
