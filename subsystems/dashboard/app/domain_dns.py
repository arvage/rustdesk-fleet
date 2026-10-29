"""DNS checks for tenants' custom domains (phase 1d; TENANCY.md, "Custom
domains").

The rules live in rdf-helper (subsystems/provisioning/rdf_helper.py), which
runs them itself before it issues a certificate or writes a vhost for a
custom domain: it is the last gate, and it doesn't trust the dashboard's
verdict. The dashboard loads that same file for its own checks, so the
Domains page and the helper always apply the same rules.

Lookups use dig against DNS_CHECK_RESOLVERS. They take seconds, so they run
in background jobs only (app/domains.py), never in a request.
"""
from __future__ import annotations

import importlib.util
import threading
from pathlib import Path

from app import settings

HELPER_FILE = Path(__file__).resolve().parents[2] / "provisioning" / "rdf_helper.py"

_module = None
_lock = threading.Lock()

# Tests replace this with a fake: query(resolver, name, rtype) -> (status, records).
QUERY = None


def helper():
    """rdf_helper.py, loaded once (it only defines functions and constants)."""
    global _module
    with _lock:
        if _module is None:
            spec = importlib.util.spec_from_file_location("rdf_helper_rules", HELPER_FILE)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _module = mod
    return _module


def _query(resolver: str, name: str, rtype: str):
    if QUERY is not None:
        return QUERY(resolver, name, rtype)
    h = helper()
    return h.dig_query(h._run, resolver, name, rtype)


def domain_problem(domain: str) -> str | None:
    """Why this can't be a custom domain (format rules only), or None."""
    return helper().custom_domain_problem(domain, settings.provider_domain())


def check(domain: str, target: str, token: str):
    """Is DOMAIN pointed at the tenant whose own web host (CNAME target) is
    TARGET: its provider subdomain, or tenant #1's legacy host? A DnsResult
    (.ok, .method 'cname'/'txt', .code, .reason in plain words). Blocking:
    call from a background job."""
    return helper().check_domain(_query, domain, target, settings.provider_domain(),
                                 token, settings.public_ipv4(), settings.public_ipv6(),
                                 settings.dns_check_resolvers())
