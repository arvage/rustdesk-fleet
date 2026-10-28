#!/usr/bin/env python3
"""Manage provider console admins from the server's shell.

    python3 provider_admin.py --domain DOMAIN add EMAIL NAME     # create, print an enrollment link
    python3 provider_admin.py --domain DOMAIN list
    python3 provider_admin.py --domain DOMAIN disable EMAIL      # also signs them out
    python3 provider_admin.py --domain DOMAIN enroll-link EMAIL  # a new one-time link

Enrollment links are one-time and valid for 1 hour: open one on the device
to sign in from and create a passkey (provider admins have no passwords).

The console host is admin.<PROVIDER_DOMAIN>. The domain comes from
--domain, else the PROVIDER_DOMAIN environment variable, else
/etc/rustdesk-fleet/dashboard.env if this user can read it (install.sh
makes it root-only, so pass --domain, before the command).
Run it as the dashboard's user (it writes the dashboard's database).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

DASH_DIR = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(DASH_DIR), str(DASH_DIR.parent / "single-tenant")]
ENV_FILE = Path("/etc/rustdesk-fleet/dashboard.env")


def _load_domain(cli: str) -> None:
    if cli:
        os.environ["PROVIDER_DOMAIN"] = cli
    if os.environ.get("PROVIDER_DOMAIN"):
        return
    try:
        for line in ENV_FILE.read_text().splitlines():
            if line.startswith("PROVIDER_DOMAIN="):
                os.environ["PROVIDER_DOMAIN"] = line.split("=", 1)[1].strip()
    except OSError:
        pass


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Manage provider console admins.")
    ap.add_argument("--domain", default="", help="PROVIDER_DOMAIN (e.g. rds.example.com)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("add")
    p.add_argument("email")
    p.add_argument("name")
    sub.add_parser("list")
    p = sub.add_parser("disable")
    p.add_argument("email")
    p = sub.add_parser("enroll-link")
    p.add_argument("email")
    args = ap.parse_args(argv)
    _load_domain(args.domain)

    from app import settings
    from app.console.auth import add_provider_user, create_enrollment
    from app.deps import get_db, provider_log, run_migrations

    run_migrations()
    needs_link = args.cmd in ("add", "enroll-link")
    if needs_link and not settings.console_host():
        print("PROVIDER_DOMAIN isn't set (use --domain, the environment or dashboard.env).")
        return 1
    conn = get_db()
    try:
        if args.cmd == "add":
            try:
                uid = add_provider_user(conn, args.email, args.name)
            except ValueError as e:
                print(e)
                return 1
            link = create_enrollment(conn, uid, "cli")
            provider_log(conn, "provider_admin_added", args.email.lower(), "cli")
            print(f"Added {args.email.lower()}. One-time passkey enrollment link (valid 1 hour):\n{link}")
        elif args.cmd == "list":
            rows = conn.execute(
                """SELECT u.email, u.display_name, u.disabled_at, u.last_sign_in_at,
                          (SELECT COUNT(*) FROM provider_webauthn_credentials c WHERE c.provider_user_id = u.id) AS n
                   FROM provider_users u ORDER BY u.id""").fetchall()
            if not rows:
                print("No provider admins yet.")
            for r in rows:
                state = "disabled" if r["disabled_at"] else "active"
                print(f"{r['email']:<36} {state:<9} passkeys={r['n']} last_sign_in={r['last_sign_in_at'] or 'never'}  {r['display_name']}")
        else:
            row = conn.execute("SELECT * FROM provider_users WHERE email = ?", (args.email.strip().lower(),)).fetchone()
            if row is None:
                print(f"No provider admin {args.email}.")
                return 1
            if args.cmd == "disable":
                conn.execute("UPDATE provider_users SET disabled_at = datetime('now'),"
                             " session_version = session_version + 1 WHERE id = ?", (row["id"],))
                conn.commit()
                provider_log(conn, "provider_admin_disabled", row["email"], "cli")
                print(f"Disabled {row['email']} and ended their sessions.")
            else:
                if row["disabled_at"]:
                    print(f"{row['email']} is disabled.")
                    return 1
                link = create_enrollment(conn, row["id"], "cli")
                provider_log(conn, "provider_enroll_link_created", row["email"], "cli")
                print(f"One-time passkey enrollment link for {row['email']} (valid 1 hour):\n{link}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
