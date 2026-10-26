"""Where the dashboard's runtime data lives (never inside the repo).

Everything defaults to /opt/rustdesk-fleet. Set FLEET_DATA_DIR to point the
dashboard somewhere else, e.g. a temp directory in tests. The single-tenant
scripts read the same variable with the same default.
"""
import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("FLEET_DATA_DIR") or "/opt/rustdesk-fleet")

FLEET_DB = DATA_DIR / "fleet.sqlite3"
HBBS_DB = DATA_DIR / "data" / "db_v2.sqlite3"
INSTALLERS_DIR = DATA_DIR / "installers"
EMAIL_LOGO = DATA_DIR / "branding" / "email-logo.png"
OS_LIFECYCLE_CACHE = DATA_DIR / "os-lifecycle.json"
