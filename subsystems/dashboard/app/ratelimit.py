"""Sign-in rate limiting: lock an email or an IP after repeated failures.

- Per email: 5 failures in 15 minutes locks that email for 15 minutes.
  Emails that don't exist are counted the same way, so a lockout doesn't
  reveal which accounts exist.
- Per IP: 20 failures in 15 minutes locks that IP for 15 minutes.

The store is in memory. That's fine with the single uvicorn worker this
runs under, but it resets on restart, and it must move to shared storage
(e.g. a SQLite table) if more workers are ever added. nginx adds a coarse
per-IP request limit in front of this (see install.sh).
"""
import math
import threading
import time
from collections import deque

from fastapi import Request

WINDOW_S = 15 * 60
LOCK_S = 15 * 60
MAX_EMAIL_FAILURES = 5
MAX_IP_FAILURES = 20
_MAX_KEYS = 10_000   # bound memory if someone sprays random emails

_lock = threading.Lock()
_failures: dict[str, deque] = {}
_locked_until: dict[str, float] = {}


def client_ip(request: Request) -> str:
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "")


def _keys(email: str, ip: str) -> list[tuple[str, int]]:
    keys = []
    if email:
        keys.append((f"email:{email.lower().strip()}", MAX_EMAIL_FAILURES))
    if ip:
        keys.append((f"ip:{ip}", MAX_IP_FAILURES))
    return keys


def _prune(now: float) -> None:
    for key in [k for k, until in _locked_until.items() if until <= now]:
        del _locked_until[key]
    if len(_failures) > _MAX_KEYS:
        for key in [k for k, q in _failures.items() if not q or q[-1] <= now - WINDOW_S]:
            del _failures[key]


def locked_seconds(email: str, ip: str) -> int:
    """Seconds until both the email and the IP may try again (0 = not locked)."""
    now = time.monotonic()
    with _lock:
        until = max((_locked_until.get(k, 0.0) for k, _ in _keys(email, ip)), default=0.0)
    return max(0, math.ceil(until - now))


def record_failure(email: str, ip: str) -> list[str]:
    """Count a failed attempt. Returns the keys that just became locked
    (e.g. ["email:a@example.com"]), so the caller can log the lockout once."""
    now = time.monotonic()
    newly_locked = []
    with _lock:
        _prune(now)
        for key, limit in _keys(email, ip):
            q = _failures.setdefault(key, deque())
            while q and q[0] <= now - WINDOW_S:
                q.popleft()
            q.append(now)
            if len(q) >= limit:
                _locked_until[key] = now + LOCK_S
                del _failures[key]
                newly_locked.append(key)
    return newly_locked


def clear_email(email: str) -> None:
    with _lock:
        _failures.pop(f"email:{email.lower().strip()}", None)


def reset() -> None:
    """Forget everything (tests)."""
    with _lock:
        _failures.clear()
        _locked_until.clear()


def locked_message(seconds: int) -> str:
    minutes = max(1, math.ceil(seconds / 60))
    return f"Too many attempts. Try again in {minutes} minute{'s' if minutes != 1 else ''}."
