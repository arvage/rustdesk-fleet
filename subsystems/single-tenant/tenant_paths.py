"""Where each tenant's files live under the fleet data directory.

Tenant #1 (the deployment that existed before tenancy) keeps the data
directory itself, so its installers, group icons and email logo stay exactly
where they were. Every other tenant gets <data dir>/tenants/<tenant id>/.
Used by the dashboard (app/tenancy.py) and by the scripts in this directory.
"""
from __future__ import annotations

import os
from pathlib import Path

FLEET_ROOT = Path(os.environ.get("FLEET_DATA_DIR") or "/opt/rustdesk-fleet")
LEGACY_TENANT_ID = 1


def tenant_files_dir(tenant_id: int) -> Path:
    if int(tenant_id) == LEGACY_TENANT_ID:
        return FLEET_ROOT
    return FLEET_ROOT / "tenants" / str(int(tenant_id))


def installers_dir(tenant_id: int) -> Path:
    return tenant_files_dir(tenant_id) / "installers"


def branding_dir(tenant_id: int) -> Path:
    return tenant_files_dir(tenant_id) / "branding"
