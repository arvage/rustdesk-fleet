"""The app must refuse to start without a real SESSION_SECRET.

The secret is checked at import, so each case imports app.main in a fresh
interpreter with its own environment.
"""
import os
import subprocess
import sys

import pytest

from conftest import DASH_DIR, DATA_DIR, SINGLE_TENANT_DIR


def import_app(secret: str | None, **extra_env):
    env = {k: v for k, v in os.environ.items() if k != "SESSION_SECRET"}
    if secret is not None:
        env["SESSION_SECRET"] = secret
    env.update(extra_env)
    env["FLEET_DATA_DIR"] = str(DATA_DIR)
    env["PYTHONPATH"] = os.pathsep.join([str(DASH_DIR), str(SINGLE_TENANT_DIR)])
    return subprocess.run([sys.executable, "-c", "import app.main"], cwd=DASH_DIR, env=env,
                          capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("secret", [None, "", "short-secret", "change-me-in-production"],
                         ids=["missing", "empty", "short", "old-placeholder"])
def test_refuses_weak_secret(secret):
    r = import_app(secret)
    assert r.returncode != 0
    assert "SESSION_SECRET" in r.stderr
    assert "/etc/rustdesk-fleet/dashboard.env" in r.stderr


def test_starts_with_strong_secret():
    r = import_app("a" * 32)
    assert r.returncode == 0, r.stderr


def test_rejects_bad_session_max_age():
    r = import_app("a" * 32, SESSION_MAX_AGE="soon")
    assert r.returncode != 0
    assert "SESSION_MAX_AGE" in r.stderr


def test_session_lifetime_defaults_to_12_hours():
    from app.main import app
    session_mw = next(m for m in app.user_middleware if m.cls.__name__ == "SessionMiddleware")
    assert session_mw.kwargs["max_age"] == 12 * 3600
