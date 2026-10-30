"""Where the dashboard's runtime data lives (never inside the repo).

Everything defaults to /opt/rustdesk-fleet. Set FLEET_DATA_DIR to point the
dashboard somewhere else, e.g. a temp directory in tests. The single-tenant
scripts read the same variable with the same default.
"""
import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("FLEET_DATA_DIR") or "/opt/rustdesk-fleet")

FLEET_DB = DATA_DIR / "fleet.sqlite3"
# Tenant #1's hbbs peer DB, where the single-tenant relay always kept it.
# Only the tenancy migration reads this (to fill relays.hbbs_db_path); at
# runtime every tenant's comes from its relays row.
HBBS_DB = DATA_DIR / "data" / "db_v2.sqlite3"
OS_LIFECYCLE_CACHE = DATA_DIR / "os-lifecycle.json"
# The provider's own Terms of Service and Privacy Policy (terms.html,
# privacy.html): HTML fragments the public site shows at /terms and /privacy.
LEGAL_DIR = DATA_DIR / "legal"
# Per-tenant files (installers, branding, email logo): app.tenancy.tenant_files_dir().

# Tests set this so the background watcher threads (routes/api.py,
# routes/home.py) don't start; they call the watcher functions directly.
WATCHERS_ENABLED = os.environ.get("FLEET_DISABLE_WATCHERS", "") != "1"


def close_data_dir() -> bool:
    """Keep other local accounts (www-data and the like) out of DATA_DIR. It
    holds every relay's private key, the hbbs peer databases (whose uuids
    authenticate device reports) and the fleet database (SMTP, backup and
    signing secrets), and the relay containers create their files
    world-readable, so the directory is what protects them. Only the owner
    (the dashboard user) and root need it. Returns True if it was changed."""
    try:
        st = DATA_DIR.stat()
        if st.st_uid != os.geteuid() or not st.st_mode & 0o007:
            return False
        os.chmod(DATA_DIR, st.st_mode & 0o7770)
        return True
    except OSError:
        return False
