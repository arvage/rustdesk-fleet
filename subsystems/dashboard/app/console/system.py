"""Everything the console runs outside Python: the root-owned helper
(rdf-helper) for relays (docker compose), certificates and nginx. The
dashboard user has no Docker access of its own. One object, so tests can
replace it with a fake (SYSTEM = FakeSystem()).

Commands are argument lists, never shell strings.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass

from app import settings


@dataclass
class Result:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def error(self) -> str:
        return (self.stderr or self.stdout or f"exit {self.returncode}").strip()[-600:]


class System:
    def run(self, args: list[str], timeout: int = 300) -> Result:
        try:
            p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
            return Result(p.returncode, p.stdout, p.stderr)
        except subprocess.TimeoutExpired:
            return Result(124, "", f"timed out after {timeout}s: {' '.join(args[:4])}")
        except OSError as e:
            return Result(127, "", str(e))

    def helper(self, *args: str, timeout: int = 300) -> Result:
        """Run the root-owned helper through sudo (exactly that binary is
        allowed by the sudoers entry install.sh writes)."""
        return self.run(["sudo", "-n", settings.helper_path(), *args], timeout=timeout)

    def relay(self, action: str, tenant_id: int, *args: str, timeout: int = 300) -> Result:
        """rdf-helper relay-<action> <tenant id> [args]: write-compose, up,
        start, stop, restart or down. The helper renders and checks the
        compose file; the dashboard never hands root a file to run."""
        return self.helper(f"relay-{action}", str(int(tenant_id)), *args, timeout=timeout)

    def running_containers(self) -> dict[str, str] | None:
        """{container name: status} of running containers (docker ps, through
        the helper), or None if that failed."""
        import json
        r = self.helper("relay-status", timeout=15)
        if not r.ok:
            return None
        try:
            data = json.loads(r.stdout)
        except ValueError:
            return None
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else None


SYSTEM = System()
