# RustDesk Fleet — User Guide

How to use the management dashboard day to day. For installing/deploying the
system see [`DEPLOYMENT.md`](DEPLOYMENT.md); for architecture see
[`README.md`](README.md).

Throughout, replace `rds.example.com` with your own server address.

---

## 1. Signing in

- Open `https://rds.example.com/` in a browser.
- **First ever visit:** if no account exists yet, you're sent to a one-time
  **setup** page — enter the email and password for the first administrator.
  This page locks permanently once that account exists.
- **Normal sign-in:** enter your email and password. If you've registered a
  passkey (see [My account](#9-my-account)), you'll be asked for it after the
  password, or it may replace the password entirely.

The left sidebar is the main navigation. Which items you see depends on your
**role** (see [Roles & permissions](#7-roles--permissions)); a read-only
account sees the fleet but none of the admin tools.

---

## 2. Server Status (home)

The landing page, **Server Status**, has three sections:

- **Server Health** — live tiles for the containers (hbbs/hbbr), CPU, memory,
  disk, devices online, and host uptime. Memory and disk show a marker at the
  alert threshold and turn amber/red as they approach it. Tiles refresh
  automatically.
- **Server Info** — the server status, host, ports, the running server and
  client versions (with an "update available" badge when newer releases
  exist), and the **public key** clients use.
- **Backups** — the last backup's status and, for admins, **Back up now** and
  **Download latest**.

Admins also see one-click banners here to **update the RustDesk server** or
**update the client version used for new installers** when updates are
available.

---

## 3. Adding devices (installing the client)

Devices appear automatically once the RustDesk client is installed and
pointed at your server. The normal flow:

1. Go to **Client Groups**, open the group the machine belongs to (or create
   one first — see [below](#5-client-groups)).
2. Under **Installers**, build an installer for the platform you need
   (Windows / macOS / Linux). It comes pre-configured with your server
   address, public key, and the group's settings — nothing to type on the
   client.
3. Share it: use a **download link** (see [Download links](#6-download-links))
   or hand over the built file, and run it on the target machine.
4. Within a minute the device shows up on the **Devices** page and in its
   group, reporting its name, OS, user, CPU, memory and client version.

You don't configure anything on the client by hand — the installer does it.

---

## 4. Devices

The **Devices** page lists every machine, with live status
(**online** / **in session** / **offline**), computer name, OS, IP and group.

- **Filter** with the search box (by ID, label, group, hostname, user, OS).
- **ⓘ Details** — opens a read-only panel with the full reported details
  (CPU, cores, memory, OS build, logged-in user, client version) and that
  device's recent remote sessions.
- **Connect** (the video icon) — launches your local RustDesk app to connect
  to that device.
- **Edit** (needs permission) — set a friendly **label** and move the device
  to a different **client group**.
- **Delete** (needs permission) — removes the device from the dashboard and
  clears its details/history. It does **not** block the machine: if that
  client ever communicates with the server again, it reappears automatically.
  Use this to clear out duplicates or decommissioned machines.

Status updates live; no need to refresh.

### Customizing tables (sorting & columns)

The tables throughout the dashboard can be adjusted to show what you care about:

- **Sort** — click any column header to sort by it; click again to reverse.
  Numbers, data sizes (e.g. `16 GB`), percentages and session durations sort
  by value, not alphabetically. Action columns (Connect, Edit, View) aren't
  sortable. Available on every table: Devices, Client Groups, the device list
  inside a group, Installers, Download Links, Logs, Users, the Notifications
  delivery log and Admin backups.
- **Columns** (Devices and the device list inside a group) — click the
  **Columns** button above the table to show or hide columns. The extra
  client-reported detail columns (**User, OS Build, CPU, Cores, Memory, Client
  Version**) are hidden by default; tick them on to see them inline instead of
  opening ⓘ Details. The **Client Groups** list has the same button for its
  own columns. **Reset** restores the defaults.

Your sort order and column choices are remembered **per browser** (stored
locally on your machine), and each table remembers its own layout. They don't
affect other users or other devices you sign in from.

---

## 5. Client Groups

**Client Groups** organize devices (e.g. per customer, or internal vs.
external). A group is just a label plus its own installers and settings — not
separate infrastructure.

- **Create a group** from the Client Groups page: give it a name/slug and an
  optional **unattended password** that gets baked into its installers.
- **Open a group** to see its devices, build installers, manage download
  links, and set managed client settings.

Both the groups list and the device list inside a group support sorting and
column show/hide — see [Customizing tables](#customizing-tables-sorting--columns).

### Managed client settings (per group)

Each group has two settings, each **Not managed** (leave devices alone), **On**
or **Off**. When managed, the value is pushed to that group's devices and
written into new installers:

- **Automatic RustDesk updates** — devices update themselves to the latest
  RustDesk release (when no session is active).
- **Remote settings changes** — whether someone connected to a device can open
  and change RustDesk's own settings on it.

---

## 6. Download links

Instead of emailing a binary, share a **download link** from a group page:

- Create a link with an optional **expiry** and **download limit**.
- Optionally **email it** to the client directly from the dashboard.
- **Revoke** a link at any time.
- The public page (`/d/<token>`) always serves the group's newest installer
  and guides the client through installing it. It's unlisted (no indexing).

---

## 7. Roles & permissions

Access is controlled by **roles** (permission levels) assigned to each user.
Viewing the fleet, logs and sessions is always allowed; capabilities gate who
can *change* things. Built-in roles:

- **Administrator** — everything (cannot be edited, so you can't lock yourself
  out).
- **Technician** — manage devices and client groups/installers.
- **Viewer** — read-only.

Admins can create **custom roles** and tick exactly which capabilities they
grant (manage devices, manage groups, update server, manage notifications,
manage backups, system maintenance, manage users, manage roles). Manage them
under **Admin → Roles & permissions**, and assign a role to each user on the
**Users** page. The interface hides actions a role can't perform, and the
server enforces them regardless.

---

## 8. Admin tools

Under **Admin** (admins, or roles with the matching capability):

- **Backup & Restore** — configure an off-site destination (Amazon S3,
  Wasabi, Backblaze B2, DigitalOcean Spaces, or MinIO), set an optional
  encryption passphrase, **Test connection**, **Back up now**, and **download**
  or **restore** any archive. Backups also run nightly on their own. See
  [Backups](#11-backups).
- **Roles & permissions** — see [above](#7-roles--permissions).
- **Export device inventory (CSV)** — download the whole fleet as a spreadsheet.
- **Restart relay** — restart the hbbs/hbbr containers (drops active sessions
  briefly).
- **Prune logs** — delete audit entries older than a chosen age.

---

## 9. Users

**Admin → Users** (requires the manage-users capability):

- **Add a user** — email, display name, **role**, and group access. Choose an
  auto-generated or manual password; optionally email the credentials.
- **Change a user's role** or **group access** from the list.
- **Reset password** — generates a new temporary password.
- **Passkeys / MFA** — revoke a user's passkeys, reset them to password
  sign-in, or send a reminder to set up a passkey.

---

## 10. Logs

**Logs** is the audit trail, split into category tabs:

- **All activity**, **Clients**, **Remote sessions**, **Installers**,
  **Groups**, **Users & auth**, **Server**.
- **Remote sessions** shows each remote connection to a device — which device
  was accessed, when, and for how long. (The RustDesk client reports the
  device side of the connection, not which technician connected.)

Each tab has a filter box and a count badge.

---

## 11. Backups

Backups protect the one thing that can't be regenerated — the server keypair
(every client is tied to it) — plus the databases.

- A **nightly** backup runs automatically and is also available on demand
  (**Back up now** on the home page or Admin → Backup & Restore).
- Configure an **off-site destination** (S3-compatible storage) and an
  **encryption passphrase** under Admin → Backup & Restore, then **Test
  connection** and enable it. Local-only backups don't survive losing the
  server, so an off-site destination is strongly recommended.
- **Retention** is set separately for each copy. *Keep this many local
  archives* (Local archives card, default 14) controls what stays on the
  server. *Keep this many off-site copies* (destination card, default 0 =
  keep all) prunes the oldest copies from the bucket after each successful
  upload; it needs the access key to have `s3:DeleteObject`.
- **Restore** from any archive on the Backup & Restore page: it takes a safety
  snapshot first, replaces the keypair and databases, and restarts the relay.
  Because the keypair is preserved, existing clients reconnect without
  reconfiguration.
- **Off-site archives** are listed on the same page, each with **Restore**
  (downloads it from the bucket, then restores as above) and **Delete**.
  Restoring needs `s3:GetObject` on the access key; deleting needs
  `s3:DeleteObject`. Local archives can be deleted from their own list too.

---

## 12. Notifications & alerts

**Admin → Notifications** (manage-notifications capability):

- **SMTP** — the outbound mail server used for all alerts; send a **test
  email** to verify.
- **Event triggers** — toggle which events send email (device registered /
  deleted / offline, installer built, new server/client version, user
  created, etc.).
- **Server health alerts** — set the **memory** and **disk** thresholds; you're
  emailed when usage crosses them (debounced, so you're not spammed). These
  thresholds are the markers shown on the Server Health tiles.
- **Delivery log** — the last notifications sent and whether they succeeded.

---

## 13. My account

**My Account** (every user):

- Update your display name and email.
- Change your password.
- Register or remove **passkeys** (Touch ID, Windows Hello, a security key)
  for faster, stronger sign-in, and choose whether password sign-in stays
  enabled.

---

## Quick task index

| I want to… | Go to |
|---|---|
| Onboard a new machine | Client Groups → group → build/share installer |
| Connect to a device | Devices → Connect |
| Remove a duplicate/old device | Devices → Edit/Delete |
| Group devices by customer | Client Groups → create group, set device groups |
| Push auto-update / lock settings | Client Groups → group → Client Settings |
| Give a colleague access | Admin → Users → add user + role |
| Limit what a role can do | Admin → Roles & permissions |
| Set up off-site backups | Admin → Backup & Restore |
| Restore the server | Admin → Backup & Restore → Restore |
| Get alerted on low disk/memory | Admin → Notifications → health thresholds |
| See who connected to what | Logs → Remote sessions |
| Export the device list | Admin → Export device inventory (CSV) |
