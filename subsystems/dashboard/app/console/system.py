"""Everything the console runs outside Python: docker compose for relays and
the root-owned helper (rdf-helper) for certificates and nginx. One object,
so tests can replace it with a fake (SYSTEM = FakeSystem()).

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

    def compose(self, compose_path: str, *args: str, timeout: int = 300) -> Result:
        return self.run(["docker", "compose", "-f", compose_path, *args], timeout=timeout)

    def helper(self, *args: str, timeout: int = 300) -> Result:
        """Run the root-owned helper through sudo (exactly that binary is
        allowed by the sudoers entry install.sh writes)."""
        return self.run(["sudo", "-n", settings.helper_path(), *args], timeout=timeout)

    def running_containers(self) -> dict[str, str] | None:
        """{container name: status} from docker ps, or None if docker failed."""
        r = self.run(["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"], timeout=10)
        if not r.ok:
            return None
        return dict(line.partition("\t")[::2] for line in r.stdout.splitlines() if line)


SYSTEM = System()
