"""Creating tenants (the provider console's "New tenant" form), as a job:

  1. check the slug       4. publish https://<slug>.<PROVIDER_DOMAIN>
  2. create the tenant    5. generate the client PDF guides
  3. provision its relay  6. invite the first admin

If any step fails, or the dashboard restarts mid-way, everything done so far
is undone (vhost and certificate, relay and its data, files, rows). The port
block stays allocated and is never reused; the slug is free again, so trying
again is safe.
"""
from __future__ import annotations

import hashlib
import importlib.util
import secrets
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import settings
from app.console import jobs, mail, relays, web
from app.deps import get_db, provider_log
from app.tenancy import invalidate_cache, tenant_files_dir
from app.tenant_migration import LEGACY_TENANT_ID, TENANT_TABLES, seed_tenant

INVITE_TTL = timedelta(hours=72)
REPO_ROOT = Path(__file__).resolve().parents[4]

TENANT_EXTRA_SQL = """
    -- One-time links for a tenant's first admin (and resent invitations).
    CREATE TABLE IF NOT EXISTS tenant_invites (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_id     INTEGER NOT NULL REFERENCES tenants(id),
        email         TEXT NOT NULL,
        display_name  TEXT NOT NULL DEFAULT '',
        role          TEXT NOT NULL DEFAULT 'admin',
        token_hash    TEXT NOT NULL UNIQUE,
        expires_at    TEXT NOT NULL,
        used_at       TEXT,
        created_by    TEXT,
        created_at    TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_tenant_invites_tenant ON tenant_invites(tenant_id);
"""

# Everything a tenant owns, children first (for undo and purge).
OWNED_TABLES = ["support_tokens", "support_sessions", "support_grants", "tenant_invites",
                *reversed(list(TENANT_TABLES))]


class CreateError(jobs.JobFailed):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def ensure_tables(conn) -> None:
    conn.executescript(TENANT_EXTRA_SQL)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(tenants)")]
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'tenants'").fetchone()[0]
    if "'purged'" not in sql:
        _rebuild_tenants(conn, cols)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(tenants)")]
    for col, ddl in (("plan", "TEXT NOT NULL DEFAULT ''"), ("cancelled_at", "TEXT"), ("purge_after", "TEXT"),
                     ("purge_warning_sent_at", "TEXT"), ("purged_at", "TEXT"), ("created_by", "TEXT"),
                     ("trial_ends_at", "TEXT"), ("trial_warning_sent_at", "TEXT")):
        if col not in cols:
            conn.execute(f"ALTER TABLE tenants ADD COLUMN {col} {ddl}")
    # Trials from before trial_ends_at existed end TRIAL_DAYS after creation.
    from app.console.lifecycle import TRIAL_DAYS
    conn.execute("UPDATE tenants SET trial_ends_at = datetime(created_at, ?) WHERE status = 'trial' AND trial_ends_at IS NULL",
                 (f"+{TRIAL_DAYS} days",))
    ucols = [r[1] for r in conn.execute("PRAGMA table_info(users)")]
    if "last_sign_in_at" not in ucols:
        conn.execute("ALTER TABLE users ADD COLUMN last_sign_in_at TEXT")
    dcols = [r[1] for r in conn.execute("PRAGMA table_info(tenant_domains)")]
    if "cert_expires_at" not in dcols:
        conn.execute("ALTER TABLE tenant_domains ADD COLUMN cert_expires_at TEXT")
    conn.commit()


def _rebuild_tenants(conn, cols: list[str]) -> None:
    """Allow status 'purged' (a CHECK can't be altered in SQLite). One
    transaction; foreign keys referencing tenants(id) keep pointing at it."""
    conn.commit()
    iso = conn.isolation_level
    conn.isolation_level = None
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            seq = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'tenants'").fetchone()
            conn.execute("""
                CREATE TABLE tenants__new (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    slug          TEXT NOT NULL UNIQUE CHECK (slug = lower(slug) AND slug <> ''),
                    display_name  TEXT NOT NULL,
                    status        TEXT NOT NULL DEFAULT 'trial'
                                      CHECK (status IN ('trial','active','suspended','cancelled','purged')),
                    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
                )""")
            copy = ", ".join(c for c in ("id", "slug", "display_name", "status", "created_at", "updated_at") if c in cols)
            conn.execute(f"INSERT INTO tenants__new ({copy}) SELECT {copy} FROM tenants")
            conn.execute("DROP TABLE tenants")
            conn.execute("ALTER TABLE tenants__new RENAME TO tenants")
            if seq:
                conn.execute("UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = 'tenants'", (seq[0],))
            if conn.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("foreign key problems after rebuilding tenants")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.isolation_level = iso


# ── Validation ───────────────────────────────────────────────────────────────

def slug_error(conn, slug: str, ignore_signup: int | None = None) -> str | None:
    problem = settings.slug_problem(slug)
    if problem:
        return problem
    if conn.execute("SELECT 1 FROM tenants WHERE slug = ?", (slug,)).fetchone():
        return f"“{slug}” is taken (slugs of removed tenants stay reserved)."
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'signups'").fetchone() and conn.execute(
            "SELECT 1 FROM signups WHERE slug = ? AND id IS NOT ?"
            " AND status IN ('pending_verification', 'checkout', 'creating', 'failed')", (slug, ignore_signup)).fetchone():
        return f"“{slug}” is held by a sign-up in progress."
    return None


def validate(conn, name: str, slug: str, email: str, signup_id: int | None = None) -> list[str]:
    errors = []
    if not settings.provider_domain():
        errors.append("PROVIDER_DOMAIN isn't set, so tenants have nowhere to live.")
    if not name.strip():
        errors.append("Give the tenant a name.")
    err = slug_error(conn, slug, signup_id)
    if err:
        errors.append(err)
    if "@" not in email or len(email) > 200:
        errors.append("Enter the first admin's email address.")
    return errors


# ── Invites ──────────────────────────────────────────────────────────────────

def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_invite(conn, tenant_id: int, slug: str, email: str, name: str, created_by: str, role: str = "admin") -> str:
    """A one-time sign-up link for the tenant's host, valid 72 hours. Earlier
    unused links for the same email in the same tenant stop working."""
    token = secrets.token_urlsafe(32)
    conn.execute("UPDATE tenant_invites SET used_at = ? WHERE tenant_id = ? AND email = ? AND used_at IS NULL",
                 (_now(), tenant_id, email))
    conn.execute(
        "INSERT INTO tenant_invites (tenant_id, email, display_name, role, token_hash, expires_at, created_by)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (tenant_id, email.lower().strip(), name.strip(), role, _hash(token),
         (datetime.now(timezone.utc) + INVITE_TTL).strftime("%Y-%m-%d %H:%M:%S"), created_by))
    conn.commit()
    from app.domains import primary_host
    return f"https://{primary_host(conn, tenant_id) or settings.tenant_web_host(slug)}/invite/{token}"


def find_invite(conn, tenant_id: int, token: str):
    return conn.execute(
        "SELECT * FROM tenant_invites WHERE tenant_id = ? AND token_hash = ? AND used_at IS NULL AND expires_at > ?",
        (tenant_id, _hash(token), _now())).fetchone()


def email_invite(email: str, tenant_name: str, link: str) -> tuple[bool, str]:
    return mail.send([email], f"Your {tenant_name} RustDesk Fleet account", mail.simple_html(
        f"Welcome to RustDesk Fleet, {tenant_name}",
        ["Your remote-support dashboard is ready. Open the link below to set your name and password "
         "(you can add a passkey afterwards). The link works once and expires in 72 hours."],
        link, "Set up my account"), event="tenant_invite_emailed")


# ── Guides ───────────────────────────────────────────────────────────────────

def guides_dir(tenant_id: int) -> Path:
    return tenant_files_dir(tenant_id) / "guides"


def generate_guides(tenant_id: int, name: str, host: str) -> list[Path]:
    spec = importlib.util.spec_from_file_location("build_client_docs", REPO_ROOT / "client-docs" / "build_client_docs.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.generate_guides(guides_dir(tenant_id), app_name=f"{name} Remote Support", host=host)


# ── Undo ─────────────────────────────────────────────────────────────────────

def delete_tenant_rows(conn, tenant_id: int, keep_provider_events: bool) -> None:
    """Every tenant-owned row of a tenant. Provider audit entries about it
    (source 'provider') are kept when asked."""
    for table in OWNED_TABLES:
        if table == "provisioning_events" and keep_provider_events:
            conn.execute("DELETE FROM provisioning_events WHERE tenant_id = ? AND source IS NOT 'provider'", (tenant_id,))
        else:
            conn.execute(f'DELETE FROM "{table}" WHERE tenant_id = ?', (tenant_id,))
    conn.commit()


def undo_creation(tenant_id: int | None, slug: str) -> list[str]:
    """Remove whatever a (failed) creation made. Idempotent. Returns problems."""
    problems = []
    if tenant_id is None or tenant_id == LEGACY_TENANT_ID:
        return problems
    conn = get_db()
    host = settings.tenant_web_host(slug)
    if conn.execute("SELECT 1 FROM tenant_domains WHERE tenant_id = ?", (tenant_id,)).fetchone():
        try:
            web.unpublish(host)
        except web.WebError as e:
            problems.append(f"web domain: {e}")
        conn.execute("DELETE FROM tenant_domains WHERE tenant_id = ?", (tenant_id,))
        conn.commit()
    if relays.relay_dir(tenant_id).exists() or conn.execute("SELECT 1 FROM relays WHERE tenant_id = ?", (tenant_id,)).fetchone():
        try:
            relays.remove(tenant_id)
        except relays.RelayError as e:
            problems.append(f"relay: {e}")
        try:
            web.purge_relay_data(tenant_id)
        except web.WebError as e:
            problems.append(f"relay data: {e}")
        conn.execute("DELETE FROM relays WHERE tenant_id = ?", (tenant_id,))
        conn.commit()
    shutil.rmtree(tenant_files_dir(tenant_id), ignore_errors=True)
    conn.execute("UPDATE relay_port_blocks SET note = 'unused: tenant creation failed' WHERE tenant_id = ?", (tenant_id,))
    delete_tenant_rows(conn, tenant_id, keep_provider_events=True)
    # The audit trail of the attempt stays in the provider log, but without
    # pointing at a tenant row that no longer exists.
    conn.execute("UPDATE provisioning_events SET tenant_id = NULL WHERE tenant_id = ?", (tenant_id,))
    conn.execute("UPDATE provider_jobs SET tenant_id = NULL WHERE tenant_id = ?", (tenant_id,))
    conn.execute("DELETE FROM tenants WHERE id = ?", (tenant_id,))
    conn.commit()
    conn.close()
    invalidate_cache()
    return problems


def _recover(job) -> None:
    problems = undo_creation(job.tenant_id, job.params["slug"])
    if problems:
        raise RuntimeError("; ".join(problems))


# ── The job ──────────────────────────────────────────────────────────────────

@jobs.handler("create_tenant", recover=_recover)
def create_tenant(job) -> None:
    p = job.params
    slug, name, email, admin_name = p["slug"], p["name"].strip(), p["admin_email"].strip().lower(), p.get("admin_name", "")
    host = settings.tenant_web_host(slug)
    try:
        with job.step("Check the slug"):
            conn = get_db()
            errors = validate(conn, name, slug, email)
            conn.close()
            if errors:
                raise CreateError(" ".join(errors))

        with job.step("Create the tenant"):
            conn = get_db()
            from app.console.lifecycle import TRIAL_DAYS
            cur = conn.execute("INSERT INTO tenants (slug, display_name, status, created_by, trial_ends_at)"
                               " VALUES (?, ?, 'trial', ?, datetime('now', ?))",
                               (slug, name, job.created_by, f"+{TRIAL_DAYS} days"))
            job.tenant_id = cur.lastrowid
            seed_tenant(conn, job.tenant_id)
            conn.commit()
            conn.close()
            job._save()

        with job.step("Provision the relay") as st:
            version = relays.current_server_version()
            if not version:
                raise CreateError("No rustdesk-server version known (tenant #1's compose file names none).")
            conn = get_db()
            block = relays.allocate_block(conn, job.tenant_id, slug)
            conn.commit()
            try:
                relays.provision(conn, job.tenant_id, slug, block, version)
            except relays.RelayError as e:
                raise CreateError(str(e))
            finally:
                conn.close()
            st["detail"] = f"{settings.tenant_relay_host(slug)} ports {block.nat}–{block.ws_relay}, rustdesk-server {version}"

        with job.step(f"Publish https://{host}"):
            conn = get_db()
            conn.execute(
                "INSERT INTO tenant_domains (tenant_id, hostname, kind, verified_at, tls_status, is_primary)"
                " VALUES (?, ?, 'provider_subdomain', datetime('now'), 'pending', 1)", (job.tenant_id, host))
            conn.commit()
            try:
                web.publish(host)
            except web.WebError as e:
                raise CreateError(str(e))
            conn.execute("UPDATE tenant_domains SET tls_status = 'active', cert_expires_at = ? WHERE hostname = ?",
                         (web.cert_expiry(host), host))
            conn.commit()
            conn.close()
            invalidate_cache()

        with job.step("Generate the client guides"):
            try:
                generate_guides(job.tenant_id, name, host)
            except Exception as e:
                raise CreateError(f"Couldn't build the PDF guides: {e}")

        with job.step(f"Invite {email}") as st:
            conn = get_db()
            link = create_invite(conn, job.tenant_id, slug, email, admin_name, job.created_by)
            conn.close()
            ok, err = email_invite(email, name, link)
            st["detail"] = (f"emailed; link: {link}" if ok else f"not emailed ({err}); send them this link: {link}")

        conn = get_db()
        provider_log(conn, "tenant_created", f"{slug} ({name}) admin={email}", job.created_by, job.tenant_id)
        conn.close()
    except BaseException as e:
        job.note("Undo what was done", "running")
        problems = undo_creation(job.tenant_id, slug)
        job.steps[-1].update(status="failed" if problems else "done", detail="; ".join(problems))
        job.tenant_id = None
        job._save()
        conn = get_db()
        provider_log(conn, "tenant_create_failed", f"{slug}: {str(e)[:300]}", job.created_by)
        conn.close()
        raise
