"""Provider-wide settings from the environment (dashboard.env), and the names
derived from them. Read on each call, so tests can change them.

CSP_MODE             report-only (default) or enforce: whether the
                     Content-Security-Policy is only reported or enforced.
PUBLIC_IPV4,         this box's public addresses: a custom domain may point
PUBLIC_IPV6          here with A/AAAA records (plus a TXT record) instead
                     of a CNAME.
DNS_CHECK_RESOLVERS  resolvers custom-domain DNS checks ask (default
                     1.1.1.1,8.8.8.8; they must agree).
MAX_CUSTOM_DOMAINS   custom domains per tenant (default 3).
STRIPE_*, PUBLIC_SITE_HOST, TERMS_URL, PRIVACY_URL, SIGNUP_*, SIGNUPS_PER_DAY,
PAST_DUE_SUSPEND_DAYS: provider billing and sign-up (phase 2; DEPLOYMENT.md).
PROVIDER_DOMAIN      e.g. rds.example.com. Tenants live at <slug>.<domain>
                     (web) and <slug>.relay.<domain> (relay); the provider
                     console at admin.<domain>. Empty = no console and no new
                     tenants (a single-tenant install).
"""
from __future__ import annotations

import ipaddress
import os
import re

from app.tenant_schema import normalize_host

# Subdomains that can never be a tenant slug: what the provider domain itself
# uses or may use, and common names it would be confusing to hand out.
RESERVED_SLUGS = frozenset({
    "admin", "relay", "www", "api", "mail", "status", "support", "billing", "app", "static",
    "console", "provider", "portal", "dashboard", "help", "docs", "cdn", "assets", "ws",
    "smtp", "imap", "pop", "ftp", "ns1", "ns2", "mx", "autodiscover", "autoconfig",
    "default", "localhost", "test", "dev", "staging", "invite", "login", "setup",
})

# 3-32 characters: lowercase letters, digits and hyphens, starting with a
# letter and not ending with a hyphen.
SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}[a-z0-9]$")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def csp_mode() -> str:
    """'enforce' or 'report-only' (the default, and for anything else)."""
    return "enforce" if _env("CSP_MODE").lower() == "enforce" else "report-only"


def provider_domain() -> str:
    return normalize_host(_env("PROVIDER_DOMAIN"))


def console_host() -> str:
    d = provider_domain()
    return f"admin.{d}" if d else ""


def tenant_web_host(slug: str) -> str:
    return f"{slug}.{provider_domain()}"


def tenant_relay_host(slug: str) -> str:
    return f"{slug}.relay.{provider_domain()}"


def slug_problem(slug: str) -> str | None:
    """Why a slug can't be used (format or reserved), or None."""
    if not SLUG_RE.match(slug or ""):
        return ("Use 3–32 characters: lowercase letters, digits and hyphens, "
                "starting with a letter and not ending with a hyphen.")
    if slug in RESERVED_SLUGS:
        return f"“{slug}” is reserved."
    return None


def suggest_slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    s = re.sub(r"^[^a-z]+", "", s)[:32].rstrip("-")
    return s


def provider_session_max_age() -> int:
    raw = _env("PROVIDER_SESSION_MAX_AGE")
    try:
        value = int(raw) if raw else 8 * 3600
    except ValueError:
        raise RuntimeError(f"PROVIDER_SESSION_MAX_AGE must be a number of seconds, got {raw!r}.")
    if value <= 0:
        raise RuntimeError("PROVIDER_SESSION_MAX_AGE must be positive.")
    return value


def provider_allowed_networks() -> list:
    """PROVIDER_ALLOWED_IPS as networks; empty = no restriction."""
    nets = []
    for part in _env("PROVIDER_ALLOWED_IPS").split(","):
        part = part.strip()
        if part:
            nets.append(ipaddress.ip_network(part, strict=False))
    return nets


def ip_allowed(ip: str) -> bool:
    nets = provider_allowed_networks()
    if not nets:
        return True
    try:
        addr = ipaddress.ip_address((ip or "").replace("::ffff:", ""))
    except ValueError:
        return False
    return any(addr in n for n in nets)


def relay_port_base() -> int:
    return int(_env("RELAY_PORT_BASE", "21200"))


def relay_port_max() -> int:
    return int(_env("RELAY_PORT_MAX", "21999"))


def helper_path() -> str:
    return _env("RDF_HELPER", "/usr/local/sbin/rdf-helper")


# ── Custom domains (phase 1d) ────────────────────────────────────────────────

def _ip_setting(name: str, version: int) -> str:
    raw = _env(name)
    if not raw:
        return ""
    try:
        addr = ipaddress.ip_address(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be an IPv{version} address, got {raw!r}.")
    if addr.version != version:
        raise RuntimeError(f"{name} must be an IPv{version} address, got {raw!r}.")
    return str(addr)


def public_ipv4() -> str:
    """This box's public IPv4 address, for custom domains pointed here with an
    A record (plus a TXT record). Empty = only the CNAME method works."""
    return _ip_setting("PUBLIC_IPV4", 4)


def public_ipv6() -> str:
    return _ip_setting("PUBLIC_IPV6", 6)


def dns_check_resolvers() -> tuple[str, ...]:
    """DNS_CHECK_RESOLVERS: public resolvers custom-domain checks ask (they
    must all agree). Default 1.1.1.1,8.8.8.8."""
    from app import domain_dns
    try:
        return domain_dns.helper().parse_resolvers(_env("DNS_CHECK_RESOLVERS"))
    except domain_dns.helper().HelperError as e:
        raise RuntimeError(str(e))


def max_custom_domains() -> int:
    raw = _env("MAX_CUSTOM_DOMAINS")
    try:
        value = int(raw) if raw else 3
    except ValueError:
        raise RuntimeError(f"MAX_CUSTOM_DOMAINS must be a number, got {raw!r}.")
    return max(0, value)


# ── Provider billing (phase 2) ──────────────────────────────────────────────

def stripe_secret_key() -> str:
    """A restricted key (rk_…) with only the permissions DEPLOYMENT.md lists."""
    return _env("STRIPE_SECRET_KEY")


def stripe_webhook_secret() -> str:
    return _env("STRIPE_WEBHOOK_SECRET")


def stripe_api_version() -> str:
    from app.stripe_api import API_VERSION
    return _env("STRIPE_API_VERSION") or API_VERSION


def stripe_tax() -> bool:
    """STRIPE_TAX=on: Stripe Tax calculates tax on sign-up and invoices."""
    return _env("STRIPE_TAX").lower() in ("on", "1", "yes", "true")


def stripe_proration() -> str:
    """How quantity changes are prorated (Stripe's proration_behavior)."""
    v = _env("STRIPE_PRORATION") or "create_prorations"
    if v not in ("create_prorations", "none", "always_invoice"):
        raise RuntimeError(f"STRIPE_PRORATION must be create_prorations, none or always_invoice, got {v!r}.")
    return v


def stripe_test_clock() -> str:
    """STRIPE_TEST_CLOCK (test mode only): new sign-ups' Stripe customers are
    created on this test clock, so their trials can be fast-forwarded. Ignored
    with a live key."""
    clock = _env("STRIPE_TEST_CLOCK")
    return clock if clock.startswith("clock_") and "_test_" in stripe_secret_key() else ""


def public_site_host() -> str:
    """The product page, pricing and sign-up: a provider host like the
    console (default www.<PROVIDER_DOMAIN>)."""
    raw = _env("PUBLIC_SITE_HOST")
    if raw:
        return normalize_host(raw)
    d = provider_domain()
    return f"www.{d}" if d else ""


def legal_page(name: str):
    """The provider's terms.html or privacy.html, or None if not written."""
    from app.paths import LEGAL_DIR
    path = LEGAL_DIR / f"{name}.html"
    return path if path.is_file() else None


def terms_url() -> str:
    """TERMS_URL, else the public site's /terms when terms.html exists."""
    host = public_site_host()
    return _env("TERMS_URL") or (f"https://{host}/terms" if host and legal_page("terms") else "")


def privacy_url() -> str:
    """PRIVACY_URL, else the public site's /privacy when privacy.html exists."""
    host = public_site_host()
    return _env("PRIVACY_URL") or (f"https://{host}/privacy" if host and legal_page("privacy") else "")


def _int_setting(name: str, default: int, minimum: int = 0) -> int:
    raw = _env(name)
    try:
        value = int(raw) if raw else default
    except ValueError:
        raise RuntimeError(f"{name} must be a whole number, got {raw!r}.")
    return max(minimum, value)


def signups_per_day() -> int:
    """At most this many tenants created by self-service sign-up per day."""
    return _int_setting("SIGNUPS_PER_DAY", 20)


def signup_min_free_blocks() -> int:
    """Sign-up closes when fewer relay port blocks than this are free."""
    return _int_setting("SIGNUP_MIN_FREE_BLOCKS", 5)


def signup_ip_limit() -> int:
    """Sign-up form submissions per IP per hour."""
    return _int_setting("SIGNUP_IP_LIMIT", 5, 1)


def signup_email_limit() -> int:
    """Sign-up form submissions per email address per day."""
    return _int_setting("SIGNUP_EMAIL_LIMIT", 3, 1)


def past_due_suspend_days() -> int:
    """A past-due Stripe tenant is suspended after this many days."""
    return _int_setting("PAST_DUE_SUSPEND_DAYS", 7, 1)
