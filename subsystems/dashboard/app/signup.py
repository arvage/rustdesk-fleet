"""Self-service sign-up on the public site (phase 2; TENANCY.md, "Billing").

    form (POST /signup)        a `signups` row, password already hashed;
                               an email with a single-use link (24 hours)
    verify (POST /signup/verify)  the email is confirmed; only now do we
                               create the Stripe customer and a Checkout
                               Session (card required, 14-day trial)
    Stripe Checkout            the customer enters a card on Stripe's page
    webhook                    checkout.session.completed: the card is checked
                               and the tenant is created by the creation job
                               (app/billing.py), never by the redirect

**No card, no tenant.** Every Checkout Session is created by
checkout_params() below, with PAYMENT_METHOD_COLLECTION ("always", never
"if_required") and, for the trial, MISSING_PAYMENT_METHOD ("cancel": a trial
that somehow has no card cancels instead of continuing). They are constants,
not settings, and tests check every place a session is created.

Abuse limits (settings): SIGNUP_IP_LIMIT per IP per hour and
SIGNUP_EMAIL_LIMIT per email per day; SIGNUPS_PER_DAY tenants a day in all;
sign-up closes while fewer than SIGNUP_MIN_FREE_BLOCKS relay port blocks are
free; one trial per card (app/billing.py, trial_cards). Unverified rows are
deleted after 48 hours.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt

from app import settings, stripe_api

TRIAL_DAYS = 14

# The hard rule: no card, no tenant. Constants, not settings.
PAYMENT_METHOD_COLLECTION = "always"
MISSING_PAYMENT_METHOD = "cancel"

VERIFY_TTL = timedelta(hours=24)
UNVERIFIED_TTL = timedelta(hours=48)

# pending_verification -> checkout -> creating -> created; failed; cancelled
STATUSES = ("pending_verification", "checkout", "creating", "created", "failed", "cancelled")
OPEN_STATUSES = ("pending_verification", "checkout", "creating", "failed")   # hold their slug...
NO_CARD = "no payment method"     # ...except a sign-up refused for paying without a card


SCHEMA = """
    CREATE TABLE IF NOT EXISTS signups (
        id                      INTEGER PRIMARY KEY AUTOINCREMENT,
        ref                     TEXT NOT NULL UNIQUE,      -- random, in progress-page URLs
        status                  TEXT NOT NULL DEFAULT 'pending_verification',
        company                 TEXT NOT NULL,
        slug                    TEXT NOT NULL,
        admin_name              TEXT NOT NULL,
        email                   TEXT NOT NULL,
        password_hash           TEXT NOT NULL,
        plan_id                 INTEGER REFERENCES plans(id),
        terms_url               TEXT,
        privacy_url             TEXT,
        accepted_at             TEXT NOT NULL,
        accepted_ip             TEXT NOT NULL,
        verify_token_hash       TEXT,
        verify_expires_at       TEXT,
        verified_at             TEXT,
        stripe_customer_id      TEXT,
        stripe_checkout_id      TEXT,
        stripe_subscription_id  TEXT,
        tenant_id               INTEGER REFERENCES tenants(id),
        trial_denied            INTEGER NOT NULL DEFAULT 0,
        failure_reason          TEXT,
        created_at              TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at              TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_signups_slug ON signups(slug);
    CREATE INDEX IF NOT EXISTS idx_signups_email ON signups(email);
    -- One trial per card. Not on tenant rows: a purge must not forget them.
    CREATE TABLE IF NOT EXISTS trial_cards (
        fingerprint  TEXT PRIMARY KEY,
        signup_id    INTEGER,
        tenant_id    INTEGER,
        first_seen   TEXT NOT NULL DEFAULT (datetime('now'))
    );
"""


class SignupError(Exception):
    """The message is shown on the form."""


def ensure_tables(conn) -> None:
    conn.executescript(SCHEMA)


def now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ── Is sign-up open? ─────────────────────────────────────────────────────────

def closed_reason(conn) -> str | None:
    """Why sign-up is closed right now (shown to visitors), or None."""
    from app import plans
    from app.console import relays
    if not stripe_api.enabled():
        return "Sign-up isn't available yet."
    if not plans.public_plans(conn):
        return "Sign-up isn't available yet."
    if relays.free_blocks(conn) < settings.signup_min_free_blocks():
        return "We're at capacity for new accounts right now. Please check back soon."
    if created_today(conn) >= settings.signups_per_day():
        return "We've reached today's limit for new accounts. Please try again tomorrow."
    return None


def created_today(conn) -> int:
    """Self-service tenants created (or being created) since midnight UTC."""
    day = now().strftime("%Y-%m-%d 00:00:00")
    return conn.execute("SELECT COUNT(*) FROM signups WHERE status IN ('creating', 'created', 'failed')"
                        " AND updated_at >= ?", (day,)).fetchone()[0]


# ── The form ─────────────────────────────────────────────────────────────────

def slug_problem(conn, slug: str, ignore_signup: int | None = None) -> str | None:
    """The console's slug rules, plus slugs held by a sign-up in progress."""
    from app.console.tenants import slug_error
    err = slug_error(conn, slug, ignore_signup)
    return f"“{slug}” is taken." if err and "sign-up in progress" in err else err


def rate_limited(conn, ip: str, email: str) -> str | None:
    hour = fmt(now() - timedelta(hours=1))
    day = fmt(now() - timedelta(days=1))
    if conn.execute("SELECT COUNT(*) FROM signups WHERE accepted_ip = ? AND created_at >= ?",
                    (ip, hour)).fetchone()[0] >= settings.signup_ip_limit():
        return "Too many sign-ups from your network. Please try again in an hour."
    if conn.execute("SELECT COUNT(*) FROM signups WHERE email = ? AND created_at >= ?",
                    (email, day)).fetchone()[0] >= settings.signup_email_limit():
        return "Too many sign-ups for this email address today. Check your inbox for the link we sent."
    return None


def start(conn, form: dict, ip: str) -> tuple[int, str]:
    """Check the form and store a pending sign-up. Returns (signup id,
    verification token); the caller emails the link. SignupError otherwise."""
    from app import plans
    reason = closed_reason(conn)
    if reason:
        raise SignupError(reason)
    company = (form.get("company") or "").strip()[:200]
    slug = (form.get("slug") or "").strip().lower()
    admin_name = (form.get("admin_name") or "").strip()[:200]
    email = (form.get("email") or "").strip().lower()
    password = form.get("password") or ""
    errors = []
    if not company:
        errors.append("Enter your company name.")
    err = slug_problem(conn, slug)
    if err:
        errors.append(err)
    if not admin_name:
        errors.append("Enter your name.")
    if "@" not in email or len(email) > 200 or " " in email:
        errors.append("Enter your email address.")
    if len(password) < 10:
        errors.append("Choose a password of at least 10 characters.")
    plan = plans.offered(conn, form.get("plan"))
    if plan is None:
        errors.append("Choose a plan.")
    if form.get("accept") != "1":
        errors.append("Please accept the Terms of Service and the Privacy Policy.")
    if errors:
        raise SignupError(" ".join(errors))
    limited = rate_limited(conn, ip, email)
    if limited:
        raise SignupError(limited)
    token = secrets.token_urlsafe(32)
    t = now()
    cur = conn.execute(
        """INSERT INTO signups (ref, company, slug, admin_name, email, password_hash, plan_id, terms_url, privacy_url,
                                accepted_at, accepted_ip, verify_token_hash, verify_expires_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (secrets.token_urlsafe(18), company, slug, admin_name, email,
         bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode(), plan["id"], settings.terms_url(),
         settings.privacy_url(), fmt(t), ip, _hash(token), fmt(t + VERIFY_TTL)))
    conn.commit()
    return cur.lastrowid, token


def get(conn, signup_id: int):
    return conn.execute("SELECT * FROM signups WHERE id = ?", (signup_id,)).fetchone()


def by_ref(conn, ref: str):
    if not ref or len(ref) > 100:
        return None
    return conn.execute("SELECT * FROM signups WHERE ref = ?", (ref,)).fetchone()


def _set(conn, signup_id: int, **cols) -> None:
    sets = ", ".join(f"{k} = ?" for k in cols)
    conn.execute(f"UPDATE signups SET {sets}, updated_at = datetime('now') WHERE id = ?", (*cols.values(), signup_id))
    conn.commit()


# ── Verification ─────────────────────────────────────────────────────────────

def verify(conn, token: str):
    """Spend a verification link. Returns the sign-up; SignupError if the
    link is unknown, used or expired."""
    if not token or len(token) > 200:
        raise SignupError("This link isn't valid.")
    row = conn.execute("SELECT * FROM signups WHERE verify_token_hash = ?", (_hash(token),)).fetchone()
    if row is None:
        raise SignupError("This link isn't valid, or it was already used.")
    if row["verify_expires_at"] < fmt(now()):
        raise SignupError("This link has expired. Please sign up again.")
    if row["status"] != "pending_verification":
        raise SignupError("This link was already used.")
    _set(conn, row["id"], verify_token_hash=None, verified_at=fmt(now()), status="checkout")
    return get(conn, row["id"])


# ── Checkout ─────────────────────────────────────────────────────────────────

def checkout_params(customer_id: str, plan_id: int, *, success_url: str, cancel_url: str, trial: bool,
                    metadata: dict, conn) -> dict:
    """The one place a Checkout Session's parameters are built (sign-up and
    resubscribe). Card always collected; a trial without a card cancels."""
    from app import plans
    line_items = []
    for p in plans.prices_of(conn, plan_id):
        item = {"price": p["stripe_price_id"]}
        if p["billing_scheme"] == "licensed":
            # Real quantities are pushed during the trial (app/billing.py);
            # Checkout needs a starting quantity of at least 1.
            item["quantity"] = 1
        line_items.append(item)
    if not line_items:
        raise SignupError("This plan has no prices.")
    sub_data = {"metadata": dict(metadata),
                "trial_settings": {"end_behavior": {"missing_payment_method": MISSING_PAYMENT_METHOD}}}
    if trial:
        sub_data["trial_period_days"] = TRIAL_DAYS
    params = {
        "mode": "subscription",
        "customer": customer_id,
        "line_items": line_items,
        "payment_method_collection": PAYMENT_METHOD_COLLECTION,
        "billing_address_collection": "required",
        "customer_update": {"address": "auto", "name": "auto"},
        "subscription_data": sub_data,
        "metadata": dict(metadata),
        "success_url": success_url,
        "cancel_url": cancel_url,
    }
    # Stripe refuses an empty client_reference_id, so it is left out unless set.
    ref = metadata.get("signup_id") or metadata.get("resubscribe_tenant_id") or metadata.get("tenant_id")
    if ref:
        params["client_reference_id"] = str(ref)
    if settings.stripe_tax():
        params["automatic_tax"] = {"enabled": True}
    return params


def create_checkout(conn, signup) -> str:
    """Stripe customer (once) and a Checkout Session for a verified sign-up.
    Returns the URL to send the browser to."""
    if signup["status"] == "failed" and signup["failure_reason"] == NO_CARD:
        # Refused for paying without a card (e.g. with Link): try again with a
        # card, for the same customer, if the address is still free.
        problem = slug_problem(conn, signup["slug"], ignore_signup=signup["id"])
        if problem:
            raise SignupError(problem + " Please sign up again with another address.")
        _set(conn, signup["id"], status="checkout", failure_reason=None, stripe_subscription_id=None)
        signup = get(conn, signup["id"])
    if signup["status"] != "checkout":
        raise SignupError("This sign-up isn't waiting for payment details.")
    client = stripe_api.client()
    customer = signup["stripe_customer_id"]
    if not customer:
        params = {"email": signup["email"], "name": signup["company"],
                  "metadata": {"signup_id": str(signup["id"]), "slug": signup["slug"]}}
        if settings.stripe_test_clock():
            params["test_clock"] = settings.stripe_test_clock()      # test mode: fast-forwardable
        cus = client.v1.customers.create(params)
        customer = cus.id
        _set(conn, signup["id"], stripe_customer_id=customer)
    base = f"https://{settings.public_site_host()}"
    params = checkout_params(customer, signup["plan_id"], trial=True, conn=conn,
                             metadata={"signup_id": str(signup["id"]), "slug": signup["slug"]},
                             success_url=f"{base}/signup/{signup['ref']}/progress?session={{CHECKOUT_SESSION_ID}}",
                             cancel_url=f"{base}/signup/{signup['ref']}/progress?cancelled=1")
    session = client.v1.checkout.sessions.create(params)
    _set(conn, signup["id"], stripe_checkout_id=session.id)
    return session.url


def purge_unverified(conn) -> int:
    cur = conn.execute("DELETE FROM signups WHERE status = 'pending_verification' AND created_at < ?",
                       (fmt(now() - UNVERIFIED_TTL),))
    conn.commit()
    return cur.rowcount
