#!/usr/bin/env python3
"""Dry-run the dashboard's database migrations on a COPY of fleet.sqlite3.

    sudo cp /opt/rustdesk-fleet/fleet.sqlite3 /tmp/fleet-copy.sqlite3
    sudo chown "$USER" /tmp/fleet-copy.sqlite3
    python3 subsystems/dashboard/scripts/check_migration.py /tmp/fleet-copy.sqlite3

Runs exactly what the dashboard runs on startup (deps.run_migrations, which
includes the tenancy migration) against the copy, twice, and prints:

- row counts per table before and after;
- any row with a NULL tenant_id where one is required, and any tenant_id
  other than 1 (everything that exists today belongs to tenant #1);
- foreign-key and integrity checks;
- tenant #1, its relay and its domain;
- whether the second run changed anything (it must not).

Exits 0 when everything checks out, 1 otherwise. The live database is never
opened: the script refuses the configured data directory's fleet.sqlite3.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import sys
from pathlib import Path

DASH_DIR = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(DASH_DIR), str(DASH_DIR.parent / "single-tenant")]

from app import paths  # noqa: E402
from app.deps import run_migrations  # noqa: E402
from app.tenant_migration import (  # noqa: E402
    LEGACY_TENANT_ID, NULLABLE_TENANT_TABLES, TENANT_TABLES, migrate,
)


def tables(conn) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]


def counts(path: Path) -> dict[str, int]:
    conn = sqlite3.connect(path)
    out = {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables(conn)}
    conn.close()
    return out


def fingerprint(path: Path) -> str:
    """Hash of the whole schema and every row, to prove a run changed nothing."""
    conn = sqlite3.connect(path)
    h = hashlib.sha256()
    for (sql,) in conn.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type, name"):
        h.update(sql.encode())
    for t in tables(conn):
        for row in conn.execute(f'SELECT * FROM "{t}" ORDER BY rowid'):
            h.update(repr(row).encode())
    conn.close()
    return h.hexdigest()


def run_once(path: Path) -> None:
    run_migrations(path)
    # run_migrations includes the tenancy migration; this is a no-op then,
    # and covers a checkout where it is not yet wired into startup.
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    migrate(conn, str(paths.HBBS_DB))
    conn.close()


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("copy", type=Path, help="path to a COPY of fleet.sqlite3")
    args = ap.parse_args(argv)

    path = args.copy.resolve()
    if not path.is_file():
        print(f"No such file: {path}")
        return 1
    live = paths.FLEET_DB.resolve()
    if path == live or (live.exists() and os.path.samefile(path, live)):
        print(f"Refusing to run on the live database ({live}). Copy it first.")
        return 1

    problems: list[str] = []
    before = counts(path)
    run_once(path)
    after = counts(path)
    first_fp = fingerprint(path)

    print(f"Database: {path}\n")
    print(f"{'table':<26} {'before':>8} {'after':>8}")
    for t in sorted(set(before) | set(after)):
        b, a = before.get(t), after.get(t)
        note = ""
        if t in ("tenants", "relays", "tenant_domains", "sqlite_sequence") and b != a:
            note = "  (new tenancy rows)"
        elif t == "roles" and b is not None and a > b:
            note = "  (missing built-in roles added)"
        elif b is not None and a != b:
            note = "  <-- CHANGED"
            problems.append(f"{t}: row count {b} -> {a}")
        elif b is None:
            note = "  (new table)"
        print(f"{t:<26} {b if b is not None else '-':>8} {a if a is not None else '-':>8}{note}")

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    print("\ntenant_id checks:")
    for t in TENANT_TABLES:
        cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{t}")')]
        if "tenant_id" not in cols:
            problems.append(f"{t}: no tenant_id column")
            print(f"  {t:<24} NO tenant_id COLUMN")
            continue
        nulls = conn.execute(f'SELECT COUNT(*) FROM "{t}" WHERE tenant_id IS NULL').fetchone()[0]
        other = conn.execute(f'SELECT COUNT(*) FROM "{t}" WHERE tenant_id <> ?', (LEGACY_TENANT_ID,)).fetchone()[0]
        notnull = next(r["notnull"] for r in conn.execute(f'PRAGMA table_info("{t}")') if r["name"] == "tenant_id")
        status = "ok"
        if nulls and t not in NULLABLE_TENANT_TABLES:
            status = f"{nulls} NULL"
            problems.append(f"{t}: {nulls} rows with NULL tenant_id")
            for row in conn.execute(f'SELECT rowid, * FROM "{t}" WHERE tenant_id IS NULL LIMIT 5'):
                print(f"      NULL row: {dict(row)}")
        if other:
            status = f"{other} rows not tenant 1"
            problems.append(f"{t}: {other} rows with tenant_id other than 1")
        if not notnull and t not in NULLABLE_TENANT_TABLES:
            status += ", column allows NULL"
            problems.append(f"{t}: tenant_id allows NULL")
        print(f"  {t:<24} {status}")

    fk = conn.execute("PRAGMA foreign_key_check").fetchall()
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    print(f"\nforeign_key_check: {'ok' if not fk else fk[:10]}")
    print(f"integrity_check:   {integrity}")
    if fk:
        problems.append(f"foreign key violations: {len(fk)}")
    if integrity != "ok":
        problems.append(f"integrity_check: {integrity}")

    print("\ntenants:")
    for r in conn.execute("SELECT * FROM tenants"):
        print(f"  {dict(r)}")
    print("relays:")
    for r in conn.execute("SELECT * FROM relays"):
        d = dict(r)
        d["pubkey"] = (d["pubkey"] or "")[:8] + "…" if d["pubkey"] else None
        print(f"  {d}")
    print("tenant_domains:")
    for r in conn.execute("SELECT * FROM tenant_domains"):
        print(f"  {dict(r)}")
    if not conn.execute("SELECT 1 FROM tenants WHERE id = ?", (LEGACY_TENANT_ID,)).fetchone():
        problems.append("tenant #1 missing")
    if not conn.execute("SELECT 1 FROM relays WHERE tenant_id = ?", (LEGACY_TENANT_ID,)).fetchone():
        problems.append("tenant #1 has no relay (server_config was empty?)")
    if not conn.execute("SELECT 1 FROM tenant_domains WHERE tenant_id = ? AND is_primary = 1",
                        (LEGACY_TENANT_ID,)).fetchone():
        problems.append("tenant #1 has no primary domain")
    conn.close()

    run_once(path)
    second_ok = fingerprint(path) == first_fp
    print(f"\nsecond run: {'no changes' if second_ok else 'CHANGED THE DATABASE'}")
    if not second_ok:
        problems.append("the second run changed the database")

    if problems:
        print("\nPROBLEMS:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
