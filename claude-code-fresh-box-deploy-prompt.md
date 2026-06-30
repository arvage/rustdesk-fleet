# RustDesk Fleet — Deploy Single-Tenant System on a Fresh Box

## Context

You're working on a **brand new, clean Lightsail Ubuntu instance** —
nothing has been installed or configured on it yet for this project.
Public host: `rds.pacificmit.com`. This will run self-hosted RustDesk,
replacing ScreenConnect for GoVirtual365's helpdesk and client support.

I'm handing you a zip of the project. Unzip it into `~/rustdesk-fleet` on
this box.

Read these in order before touching anything:
1. `README.md` (project root) — explains the project's history and why
   the current architecture is single-tenant
2. `subsystems/single-tenant/README.md` — the system you're deploying

**Important context on architecture history, so you don't need to
rediscover it:** an earlier version of this project used per-tenant
isolated `hbbs`/`hbbr` stacks (one per client, for security isolation).
That was abandoned because the native RustDesk desktop client can only
hold one server/key configuration at a time — confirmed via RustDesk's
own GitHub discussions as a deliberate upstream limitation, not a config
gap — which made switching between clients impractical for a helpdesk
team. The current design is single-tenant: one shared server, clients
separated by a `client_groups` label rather than separate infrastructure.
This was a deliberate, informed tradeoff, already made. Don't relitigate
it or suggest reverting to per-tenant. You'll see
`subsystems/provisioning/` in the repo — that's the old, superseded
system, kept only as reference. Ignore it for this task; don't run it,
don't build on it.

## The actual task

Get the single-tenant system (`subsystems/single-tenant/setup_server.py`)
running for real on this clean box, from scratch.

### 1. Base system setup

```bash
sudo apt update && sudo apt upgrade -y
```

**Check available memory before doing anything else:**
```bash
free -h
```
If total RAM is under ~1.5GB (common on small Lightsail tiers), add swap
before installing anything — a prior attempt on a similarly-sized
instance hit an OOM kill during a native binary install, and Docker
operations can also spike memory on small instances:
```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
free -h   # confirm swap shows ~2GiB
```
If RAM is already comfortably above that, you can skip swap — use your
judgment, but lean toward adding it anyway given it's cheap insurance and
this box will be running multiple Docker containers long-term.

**Install Docker + Compose:**
```bash
sudo apt install -y docker.io docker-compose-v2
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
```
Group membership requires a fresh login to take effect — if you're
running as a persistent SSH session, note that `docker` commands may
need `sudo` until that's resolved, or the session is restarted.

**Install Python + pip:**
```bash
sudo apt install -y python3 python3-pip
python3 --version
python3 -m pip --version
```

Confirm all of the above actually worked (don't just assume — run
`docker --version`, `docker compose version`, `docker ps` and check for
real output, not just that install commands exited 0).

### 2. Bring up the single-tenant server

```bash
cd ~/rustdesk-fleet/subsystems/single-tenant
python3 setup_server.py init --host rds.pacificmit.com
```

This script has only been tested against **mocked** Docker calls before
now — this is its first real run, on a clean box, ever. If it fails,
don't paper over it: read the actual error, check `docker logs hbbs` /
`docker logs hbbr`, and fix the root cause rather than guessing. Things
worth knowing going in:
- The compose file hardcodes image tag `rustdesk/rustdesk-server:1.1.14`
  — if that tag fails to pull, check what tags actually exist on Docker
  Hub and report back rather than silently substituting a different one
  without flagging it
- The script writes to `/opt/rustdesk-fleet/` — on a fresh box this
  directory won't exist yet; the script should create it, but confirm
  ownership ends up sane for whatever user will run this day-to-day
  (likely `ubuntu`) rather than ending up root-owned by an incidental
  `sudo` somewhere in the install chain

### 3. Firewall — both layers, required

The single-tenant compose file uses `network_mode: host` with RustDesk's
default port range (21115-21119, mostly TCP, with UDP on the heartbeat
port). Two separate places need this opened:

- **Lightsail's own networking tab** (AWS console) — you likely can't
  reach this directly from the box's shell. Tell me the exact
  ports/protocols to open (don't guess — confirm against what the
  containers actually bind via `docker port hbbs` and `docker port
  hbbr` once they're running) and I'll open them myself.
- **OS firewall** (`ufw`), if you enable it:
  ```bash
  sudo ufw status
  ```
  If you choose to enable `ufw` on this fresh box, make sure SSH (port
  22) is allowed *before* enabling it, or you'll lock yourself out:
  ```bash
  sudo ufw allow 22/tcp
  ```
  then add the RustDesk port range once confirmed.

### 4. Verify the real server works, not just that the script exited 0

```bash
python3 setup_server.py status
docker ps          # confirm hbbs and hbbr both running
docker logs hbbs
docker logs hbbr
```

Then test external reachability from a machine other than this box if
you have access to one. If not, tell me the exact ports to test
(`nc -zv rds.pacificmit.com <port>`) and I'll run it from my side.

### 5. Create the initial client group(s)

```bash
python3 setup_server.py group create --slug govirtual365-internal --display-name "GoVirtual365 Internal"
```
Ask me what other client groups to create rather than inventing names —
I'll provide the real client list.

### 6. Update the single-tenant README with real results

The doc currently has a "Tested so far (mocked Docker...)" /
"Not yet tested" structure. Replace the mocked-Docker caveat with what
you actually observed on real hardware: any errors hit and how resolved,
the real pubkey generated (fine to include — it's meant to be shared
with every client device, not secret), actual port-binding behavior,
anything that didn't match the doc's existing assumptions.

## Things to flag back to me rather than deciding unilaterally

- If `setup_server.py` has a real bug (not just an environment/config
  issue) — fix it, but tell me what was wrong, since this code was only
  validated against mocks before now
- If the Docker image tag doesn't exist or behaves unexpectedly
- Anything involving the Lightsail firewall console, since you can't
  reach that from the box directly
- If actual available RAM/disk on this instance seems too small for
  comfortably running this long-term — flag it rather than just making
  it technically work via swap and moving on

## Explicitly out of scope for this session

- Don't build the dashboard, signing subsystem, or installer generation
  — this session is "get the single-tenant server running for real on
  this box," nothing past that
- Don't touch `subsystems/provisioning/` at all — it's old, superseded
  reference code on a box that's never run it; nothing to migrate
- Don't re-architect anything. If something about the single-tenant
  design seems wrong once you're actually running it, tell me — don't
  silently change the schema or the compose file's approach

## Definition of done

- Docker, Compose, Python all installed and confirmed working with real
  output (not just install-command exit codes)
- Swap added if RAM warranted it
- Single-tenant `hbbs`/`hbbr` running for real, confirmed via `docker
  ps` + logs showing no errors
- Pubkey actually captured in `server_config` (`status` command shows
  `active`, not `provisioning`)
- External reachability confirmed (via `nc` from outside this box, or
  you've told me what to test myself)
- At least the `govirtual365-internal` client group created
- `subsystems/single-tenant/README.md` updated with real results
- Nothing pushed anywhere (this project isn't using GitHub) — just
  confirm the working state and summarize what changed
