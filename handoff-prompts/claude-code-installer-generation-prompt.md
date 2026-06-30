# RustDesk Fleet — Installer Generation (rdgen-cli integration)

## Context

You're working in `~/rustdesk-fleet` on a Lightsail Ubuntu box. This is a
monorepo for a tenant-isolated, self-hosted RustDesk deployment replacing
ScreenConnect. Read `README.md` at the repo root first, then
`subsystems/provisioning/README.md` — both explain the architecture and
why it's built this way (tenant isolation for security, not a shared
hbbs/hbbr server).

**What already exists and is verified working on this box:**

- `subsystems/provisioning/allocate_tenant.py` — provisions a tenant's
  isolated `hbbs`/`hbbr` Docker Compose stack: allocates a permanent,
  never-reused port block, renders the compose file, brings up the
  containers, captures the generated Ed25519 keypair, persists everything
  to `shared/db/schema.sql`'s `tenants` table (SQLite, lives at
  `/opt/rustdesk-fleet/fleet.sqlite3` — NOT in the repo).
- This has been run for real on this box (`create_tenant` for a test
  tenant) and confirmed end-to-end: containers come up, ports are
  reachable from the public internet, keypair is captured correctly.
- The `installers` table already exists in the schema
  (`shared/db/schema.sql`) but nothing writes to it yet — columns:
  `tenant_id`, `platform`, `rustdesk_version`, `status`
  (pending/built/signing/signed/failed), `unsigned_path`, `signed_path`,
  `sha256_unsigned`, `sha256_signed`, `error_message`, `created_at`,
  `signed_at`.

**What does NOT exist yet:**

- Anything that actually generates an installer. This session's job.
- The signing subsystem (`subsystems/signing/` is a README-only
  placeholder — Authenticode signing needs a Windows agent, planned via
  GitHub Actions + Azure Trusted Signing, but that's explicitly OUT OF
  SCOPE for this session). For now, treat "produce an unsigned installer
  and stop" as the deliverable; the signing handoff is a separate,
  later piece.

## The actual task

Build the installer-generation step: given a tenant's `host`, `hbbs_port`,
`hbbr_port`, and `pubkey` (everything `allocate_tenant.py` already
captures and stores), produce an unsigned RustDesk Windows installer
pre-configured to connect to that tenant's server, with no manual
configuration step required on the end user's machine.

This is meant to plug into the existing provisioning flow, not replace
it — `create_tenant()` in `allocate_tenant.py` should eventually call into
this as an additional step, but build it as its own module first
(`subsystems/provisioning/generate_installer.py` or similar) so it can be
tested in isolation before wiring it in.

## What to use: rdgen-cli

We're using `rdgen-cli` (https://github.com/wztx/rustdesk-client — also
mirrored at https://github.com/bryangerlach/rdgen), a community tool that
patches an official RustDesk release binary with server config
(ID/relay server, public key) and optional branding, rather than
compiling RustDesk from source. It's invoked like:

```
python rdgen-cli -f my_config.json --set-version 1.4.5 --set-platform windows -s https://rdgen.crayoneater.org
```

**Important: investigate before implementing.** I have NOT verified this
tool's actual current interface, what `my_config.json` needs to contain,
whether `-s https://rdgen.crayoneater.org` means it calls out to a remote
build service (vs. building fully locally), what's required as
prerequisites (Rust toolchain? Flutter? just Python + the released
binaries?), or how large/slow a single build is. Do NOT assume the
command-line example above is complete or correct — treat it as a
starting pointer, not a spec. Start by:

1. Cloning/reading the actual rdgen-cli repo and its README/docs
2. Understanding what `my_config.json` schema actually looks like
3. Understanding whether builds happen locally on this box or require an
   external service call (if external, that's a real constraint worth
   flagging back to me before going further — we'd want to know about
   that dependency)
4. Checking what this box needs installed to actually run it (and
   installing those prerequisites)
5. Doing one fully manual, by-hand test build BEFORE writing any
   automation code — confirm you can produce *a* patched installer at
   all before wrapping it in a script

## Design constraints (carry over from the existing codebase's style)

- Match `allocate_tenant.py`'s patterns: a library of functions with a
  thin CLI wrapper (argparse), not a single monolithic script. The
  dashboard (not built yet) will eventually call these functions
  directly, not shell out to a CLI.
- Idempotent, debuggable failure handling: if a build fails partway, the
  `installers` table row should reflect `status='failed'` with a
  populated `error_message`, not disappear or leave an ambiguous state.
  Look at how `create_tenant()` in `allocate_tenant.py` handles its own
  failure path (provisioning_events logging, status left visible rather
  than rolled back silently) and follow the same philosophy.
- Compute and store `sha256_unsigned` for whatever gets built — this
  matters later for verifying nothing changed between build and signing.
- Don't hardcode tenant values — pull `host`, `hbbs_port`, `hbbr_port`,
  `pubkey` from the `tenants` table via the existing DB connection
  pattern (`get_db()` in `allocate_tenant.py`), keyed by `slug`.
- Add/update that subsystem's README as you go, same style as
  `subsystems/provisioning/README.md` — what's built, what's tested,
  what's explicitly NOT done yet. Future-me (or future Claude) reading
  this repo cold needs to be able to tell at a glance what's real vs.
  aspirational.
- This box has `git` configured and the repo is already cloned with a
  working push credential — commit your work with clear messages as you
  go (don't wait until the very end to make one giant commit), but don't
  push to GitHub without telling me first; I'll review the diff.

## Things to flag back to me rather than deciding unilaterally

- If rdgen-cli turns out to require a remote build service call (the
  `-s https://...` flag), rather than building fully locally — this
  changes our architecture (another external dependency in the
  provisioning path) and I want to know before you build around it.
- If rdgen-cli's prerequisites are heavy (e.g. needs a Rust/Flutter
  toolchain on this box just to patch a binary) — that's worth a
  sanity check on whether this is really the right tool before we
  invest further.
- If you find rdgen-cli is unmaintained, broken, or behaves differently
  than its README claims — don't silently work around it with hacks;
  tell me and we'll decide whether to find an alternative.
- Scope creep: do NOT start building the signing subsystem, the
  dashboard, or the web viewer in this session, even if it's tempting
  once you're in the code. Stay on installer generation only. If you
  notice something those subsystems will need, note it in a README or
  a TODO comment rather than building it now.

## Definition of done for this session

- A documented, working manual process for producing one unsigned
  Windows installer for a real tenant on this box (the `test-tenant`
  from earlier, or a fresh one)
- `generate_installer.py` (or similar) wrapping that process as a
  library + CLI, following the existing codebase's patterns
- The `installers` table actually gets a row reflecting what happened
  (built, with a real `sha256_unsigned`, or failed with a real error)
- A subsystem README documenting what's built, what's verified, and
  what's explicitly deferred (signing, dashboard wiring, branding
  customization beyond the bare minimum, multi-platform builds beyond
  Windows if you only get to Windows)
- Nothing pushed to GitHub yet — show me the diff/commit log first
