"""The Stripe client for provider billing (phase 2; TENANCY.md, "Billing").

One StripeClient per call site, built from dashboard.env: the secret key
(a restricted key: DEPLOYMENT.md lists the permissions), the API version
(pinned: API_VERSION is the version this release of the `stripe` library
speaks, and install.sh pins the library), a short timeout and a couple of
retries. Stripe calls run in background jobs or with that timeout, so a
slow Stripe never holds a page for long.

Card data never reaches us: sign-up uses Stripe Checkout and card changes
the Customer Portal. Nothing here logs request bodies or webhook payloads.

Billing is optional: without STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET
the app starts as usual, sign-up is closed, and the console says why.
"""
from __future__ import annotations

from app import settings

# stripe==16.0.1 (install.sh) speaks this version; STRIPE_API_VERSION may
# override it only with care (the code is written against this one).
API_VERSION = "2026-09-30.endive"
TIMEOUT_S = 10
RETRIES = 2

# Tests replace this with an in-memory Stripe (tests/fake_stripe.py).
HTTP_CLIENT = None


class BillingUnavailable(Exception):
    """Billing isn't configured; the message says what's missing."""


def missing() -> list[str]:
    """Settings billing needs that aren't set, in plain words."""
    out = []
    if not settings.stripe_secret_key():
        out.append("STRIPE_SECRET_KEY")
    if not settings.stripe_webhook_secret():
        out.append("STRIPE_WEBHOOK_SECRET")
    if not settings.provider_domain():
        out.append("PROVIDER_DOMAIN")
    return out


def enabled() -> bool:
    return not missing()


def mode() -> str:
    """'test', 'live' or '' (no key), from the key's prefix."""
    key = settings.stripe_secret_key()
    if not key:
        return ""
    return "live" if "_live_" in key else "test"


def client():
    """A StripeClient, or BillingUnavailable."""
    gaps = missing()
    if gaps:
        raise BillingUnavailable("Billing isn't configured: set " + ", ".join(gaps) + " in dashboard.env.")
    import stripe
    http = HTTP_CLIENT or stripe.RequestsClient(timeout=TIMEOUT_S)
    return stripe.StripeClient(settings.stripe_secret_key(), stripe_version=settings.stripe_api_version(),
                               max_network_retries=RETRIES, http_client=http)


def dashboard_url(kind: str, object_id: str) -> str:
    """A link to an object in the Stripe dashboard (test or live)."""
    base = "https://dashboard.stripe.com/" + ("test/" if mode() == "test" else "")
    return f"{base}{kind}/{object_id}"
