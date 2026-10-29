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
  passkey (see [My account](#13-my-account)), you'll be asked for it after the
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
It's sorted by **label** (A→Z, ignoring case), with unlabeled devices at the
bottom. Click any column header to sort differently.

**Recently registered**: devices registered in the last 7 days that don't
have a client group yet appear in a highlighted panel at the top, newest
first. **Assign to Group** (needs permission) opens a popup showing the
device's computer name, where you pick the group and can set or change its
label in the same step. If the device has no label yet, the label is
prefilled from the computer name: whole words get a capital first letter and
codes or numbers go upper-case. If the name contains a word, hyphens become
spaces (`file-server-01` → `File Server 01`); a name made only of codes keeps
them (`ws-0001` → `WS-0001`, `dc1` → `DC1`). Once a device has a group it leaves
the panel.

- **Filter** with the search box (by ID, label, group, hostname, user, OS).
- **ⓘ Details** — opens a read-only panel with the full reported details
  (CPU, cores, memory, OS build, logged-in user, client version) and that
  device's recent remote sessions with who connected.
- **Connect** (the video icon) — launches your local RustDesk app to connect
  to that device.
- **Terminal** (the `>_` icon) — opens a RustDesk terminal session to the
  device.
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
  inside a group, Installers, Download Links, Logs (including Email
  deliveries), Users and Admin backups.
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
  links, and set managed client settings. Its device list is sorted by label.
- **Edit a device from the group page** (needs *Manage devices*): each device
  has the same **Edit** button as the Devices page (label, client group,
  delete). Afterwards you stay on the group page. A device moved to another
  group drops off this list.
- **Branding** (needs *Manage groups*): give the group's installers their own
  **app name** and **icon** (PNG or ICO, square, 256×256 is best). New
  installers for that group use them.
- **Unattended password**: managers can click the password badge to copy it.

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

### Dynamic groups

**Dynamic Groups** (sidebar) are saved filters, e.g. "Windows 10 devices" or
"not seen in over a year". Unlike a client group, you never add devices to
one by hand. Its devices are worked out from its rules each time you open it,
so the list keeps itself current. A device can be in any number of dynamic
groups, whatever client group it belongs to.

- **New Dynamic Group**: start from a template (Windows 10, not seen in a year,
  unsupported OS, outdated RustDesk, low memory, not reporting, no client
  group) or build rules yourself. Rules can test the OS, OS support status, last
  seen, registration date, last remote session, current status, client group,
  label, computer name, user, RustDesk version, memory, CPU, cores and IP.
  Choose whether a device must match **all** rules or **any** of them. The
  preview shows how many devices match while you edit.
- **Open a group** to see its devices with Connect/Terminal buttons, or
  **Export CSV** to share with a client.
- Anyone signed in can view dynamic groups. Creating, editing and deleting
  them needs **Manage devices**.

**OS support status** uses Microsoft's and Apple's end-of-support dates: *End
of support* (already past), *Ending within 6 months*, *Supported*, or *Unknown*
(Linux, or no OS reported). Each Windows device is judged by its exact release
(build number) and edition. Enterprise, Education and LTSC editions are
supported longer than Home and Pro. Macs are judged by their macOS version.
Apple doesn't announce end dates in advance, so a supported Mac has no date
and never shows *Ending within 6 months*. It moves to *End of support* once
Apple stops releasing security updates for that version (typically around
when a new macOS ships each autumn). The dates update
automatically once a day from endoflife.date, which tracks Microsoft's
lifecycle pages and Apple's security releases, so new Windows and macOS
releases are covered without any action. The OS support report shows when
they were last updated.

---

## 6. Download links

Instead of emailing a binary, share a **download link** from a group page:

- Create a link with an optional **expiry** and **download limit**.
- Optionally **email it** to the client directly from the dashboard.
- **Revoke** a link at any time.
- The public page (`/d/<token>`) always serves the group's newest installer
  and guides the client through installing it. It's unlisted (no indexing).
- The page also links two PDF guides for the client: how to install, and
  what to do during a support session. They live in `client-docs/`; see its
  README to rebuild them with a branded app name.

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

The **Admin** menu, including **Reports**, appears for any role with at least
one admin capability: update server, manage notifications, backups, system,
users or roles. A Technician (devices + groups only) doesn't see it.

---

## 8. Admin tools

Under **Admin** (admins, or roles with the matching capability):

- **Backup & Restore** — configure an off-site destination (Amazon S3,
  Wasabi, Backblaze B2, DigitalOcean Spaces, or MinIO), set an optional
  encryption passphrase, **Test connection**, **Back up now**, and **download**
  or **restore** any archive. Backups also run nightly on their own. See
  [Backups](#11-backups).
- **Installer signing** — Authenticode-sign Windows installers with Azure
  Trusted Signing so clients don't get the SmartScreen "unknown publisher"
  warning. Set the Azure details once, then signing runs automatically after
  each build, or per installer with **Sign / Re-sign** on the group page.
  Azure setup steps are in [`DEPLOYMENT.md`](DEPLOYMENT.md) §10.
- **Roles & permissions** — see [above](#7-roles--permissions).
- **Email templates** — the wording and design of every email; see
  [Email templates](#email-templates).
- **Reports** — see [Reports](#reports) below.
- **Export device inventory (CSV)** — download the whole fleet as a spreadsheet.
- **Restart relay** — restart the hbbs/hbbr containers (drops active sessions
  briefly).
- **Prune logs** — delete audit entries older than a chosen age.

### Reports

**Admin → Reports** (also in the sidebar). Every report can be filtered to one
client group, or to devices with no group. Reports about activity also take a
time period (7 days to a year). Each table has **Download CSV**, and **Print**
gives a clean printout or PDF for a client meeting.

| Report | What it shows |
|---|---|
| **Fleet overview** | Totals (online, reporting, unsupported OS, outdated RustDesk, inactive) and breakdowns by client group, OS, OS support status, RustDesk version and status |
| **Device inventory** | Every device, one row each, with hardware, OS, version and activity |
| **Operating system support** | Devices past or within 6 months of Microsoft end of support, and every OS with its end date |
| **Inactive devices** | Devices not seen for 30 days to a year (the period picks the threshold), plus "never seen" |
| **RustDesk client versions** | Version spread, clients older than your installers, devices not reporting |
| **Hardware** | Memory, cores and CPU models, with under-8 GB and under-4-core lists |
| **Data quality** | Devices with no client group or no label, and duplicate computer names (usually a reinstall) |
| **Client group health** | One row per client: devices, online, OS risk, outdated clients, inactivity, sessions, installers, links |
| **Remote sessions** | Sessions and time by device, client group and day, and devices not accessed |
| **New devices** | Devices registered in the period, and registrations by month |
| **Installers & download links** | Latest installer per client and platform (and whether it's current), and link usage |
| **User activity** | Actions per user and per event type, accounts, and the full event list |

**Reading the colors.** Color always means something:

- **Breakdowns** ("By operating system", etc.) show a colored bar of the split,
  with a matching square and share bar on each table row. A category keeps its
  color when you filter to one client. Beyond 7 categories the smallest are
  grouped into a gray **Other**. *Not reported* and *Unassigned* are gray.
- **Charts over time** (sessions by day, new devices by month) are column
  charts with the peak labeled. Periods over a month use one column per week.
- **Status badges**: green ✓ good, amber ! needs attention, red ✕ problem,
  gray – neutral/unknown. The word is always shown too.
- **Summary tiles** get a red or amber stripe and icon when the number is a
  problem, green when it's good news.
- In **Client group health**, any non-zero OS-ended, OS-ending, outdated or
  inactive count is badged, so you can scan for clients that need attention.

Hover over a bar or column to see its exact value. The table under each chart
has the same data.

---

## 9. Users

**Admin → Users** (requires the manage-users capability):

- **Add a user** — email, display name, **role**, and group access. Choose an
  auto-generated or manual password; optionally email the credentials (they
  go to the Email address at the top of the form — on a small screen, scroll
  the form up to reach it).
- **Change a user's role** or **group access** from the list.
- **Reset password** — generates a new temporary password.
- **Passkeys / MFA** — revoke a user's passkeys, reset them to password
  sign-in, or send a reminder to set up a passkey.

---

## 10. Logs

**Logs** is the audit trail, split into category tabs:

- **All activity**, **Clients**, **Remote sessions**, **Installers**,
  **Groups**, **Users & auth**, **Notifications**, **Email deliveries**,
  **Server**, **Other**.
  **All activity** (the tab the page opens on) merges everything into one
  timeline — the event log plus remote sessions, file transfers and, for
  roles that can manage notifications, notification emails; click a
  `remote_session` or `file_transfer` entry to open that device's timeline.
  **Hide online/offline** (on by default, on All activity and Clients)
  leaves out devices coming online and going offline, which otherwise
  crowd out everything else; switch it off to see them.
  Changes to dynamic groups are under Groups. Email template and design
  changes are under Notifications. Devices registering, going offline and
  coming back online are under Clients.
- **Remote sessions** shows each remote connection to a device — which device
  was accessed, who connected, from which IP, the connection type (remote
  control, file transfer, port forward, view camera or terminal), when, and
  for how long.
  - **Connected by** shows the **dashboard user** who connected, in bold, when
    the dashboard knows it, with the RustDesk name and ID of the connecting
    computer underneath. RustDesk itself only reports the connecting
    computer (its RustDesk ID and the name its client reports, usually the
    technician's OS username), so the dashboard works out the person two ways:
    - **Connect clicks** — clicking **Connect** or **Terminal** on a device
      anywhere in the dashboard is recorded; a session that starts on that
      device within about 3 minutes is credited to whoever clicked (the latest
      click wins if two people click).
    - **Linked RustDesk IDs** — a technician's own computers can be linked to
      their dashboard user, so every session from those IDs is theirs, even
      ones started from the RustDesk app directly. Links are stored in the
      `rustdesk_peer_users` table (no page for them yet).
    Otherwise the RustDesk name is shown. If the connecting computer is itself
    a fleet device, its device name is shown too.
  - Sessions recorded before this was added, or from clients that can't send
    connection reports, show **—** in these columns. Clients on older
    installers send them over port 21114, which must be open (see
    `DEPLOYMENT.md` §7).
- **Connection timeline** — click a device's name in Remote sessions (or
  **Connection timeline →** in the device's ⓘ details) for a ScreenConnect-style
  timeline of that device: a **Device online** row, one row per computer that
  connected with a bar per session (colored by type; striped while active;
  hover for times, duration and IP), orange **file transfer** markers on
  that computer's row (↑ files sent from the device, ↓ files sent to it —
  via the file transfer window or clipboard paste; hover for the count,
  size, folder and file names), and **Device events** (registered,
  label/group changed, deleted, restored). Rows are named after the dashboard
  user when known (see Connected by above). Choose **24h / 7d / 30d** and step
  **Earlier / Later**. Totals at the top (sessions, time connected, computers,
  file transfers, % online) and a chronological event list below; clicking a bar highlights
  its start/end in the list. Online history before Oct 12, 2026 has gaps
  (shown hatched) because "came online" wasn't recorded until then.

### Timeline (fleet-wide)

**Timeline** in the sidebar shows remote sessions across every device at once.

- **By device** — one row per device that had sessions in the window, busiest
  first. Hover a bar for the type, duration, who connected and from which IP;
  click a device name for its own connection timeline.
- **By user** — one row per person (dashboard user, else the RustDesk name;
  sessions with nothing reported are under **Unknown**). Bars show which
  device they were on; click a person to see By device filtered to them.
- **Window** — the **24h / 7d / 30d** chips, or pick **From** and **To** dates
  in the calendar and click **Show** (whole days, Pacific time, up to a year).
- **Filters** — client group and person; they keep the chosen window.
- Totals at the top (sessions, time connected, devices, people) and the
  session list below; clicking a bar highlights its row and vice versa.
- **Email deliveries** lists every notification email the server sent (up to
  the 200 most recent): time, event, subject, recipients, and sent/failed —
  with the error shown for failures. Only visible to roles with the
  manage-notifications capability.

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
- **Event triggers** — toggle which events send email: device registered,
  came online, went offline, deleted; installer built, deleted, downloaded via
  link; user created or deleted; new server or client version; server memory or
  disk high. New events start switched on. Each toggle **saves as soon as you
  flip it** (a "Saved" note appears; if the save fails the toggle flips back).
- **When device alerts fire**: the dashboard checks every 30 seconds.
  - **New device registered** fires on the next check after a device
    registers with the server, whether or not it has a client group yet.
    Assigning it a group later doesn't send another.
  - **Came online** and **went offline** fire when a known device changes
    state. A brand-new device only gets the registration email.
  - The first check after the dashboard restarts only records the current
    state, so a restart never floods you with alerts.
  - On a large fleet, "came online" can be frequent (every reboot or network
    blip), so switch it off if it's noisy.
- **Server health alerts** — set the **memory** and **disk** thresholds; you're
  emailed when usage crosses them (debounced, so you're not spammed). These
  thresholds are the markers shown on the Server Health tiles.
- **Delivery log** — moved to **Logs → Email deliveries** (see §10).

### Email templates

**Admin → Email templates** (needs *Manage notifications & alerts*) controls
what every email says and how it looks. That covers the alerts above, the
new-user welcome, the passkey reminder, and the download-link email sent to
clients.

- **Design**: upload a logo (PNG/JPG/GIF; it's sent inside the email, so it
  shows without the recipient allowing images), and set the brand name, header,
  button and page colors, font and footer. Team and client emails have separate
  brand names and footers. Client emails have no header bar unless you give
  them a brand name or logo.
- **Each email**: edit the subject, heading, message, closing text and button
  text. Click a placeholder such as `{label}` or `{group_name}` to insert it.
  A blank line starts a new paragraph, `**text**` is bold, and a paragraph
  whose placeholders are all empty is skipped. You can also hide the details
  table, or **Reset to default**.
- The preview updates as you type. **Send test to me** emails the saved
  version, with sample data, to your own address.

---

## 13. My account

**My Account** (every user):

- Update your display name and email.
- Change your password.
- Register or remove **passkeys** (Touch ID, Windows Hello, a security key)
  for faster, stronger sign-in, and choose whether password sign-in stays
  enabled.

**Theme**: the switch at the bottom of the sidebar picks **Light**, **Dark**,
or **Auto** (follows your computer's light/dark setting). The choice is saved
in this browser, so set it once on each browser or device you use.

---

## 14. Support access

Your provider's support staff can sign in to your dashboard to help you, but
only when you allow it and only for as long as you choose. Admins manage this
under **Admin → Support access**.

- **Grant access:** pick how long (4 hours, 1, 3, 7, 30 or 90 days, 6 or 12
  months, or until a date and time up to 12 months away), add an optional
  note such as "ticket 1234, VPN issue", and click **Grant access**. Your
  admins get an email.
- **Extend or shorten it:** change the period or note on the same page and
  click **Save changes**. There is only ever one grant.
- **Revoke it:** **Revoke now** ends access at once, including any support
  session in progress.
- **Passwords:** support never sees or changes your client groups'
  unattended-access passwords unless you tick **Allow support to see
  unattended-access passwords** on the grant.

What support can do: anything an administrator can, for at most 4 hours per
session and never past your grant: devices, client groups, installers,
download links, reports, notifications and email templates. What support
can't do: manage users or roles, change support access, use the data export
page, or (without your say-so) see passwords. While they're signed in they
see a banner, and you can see them on the Support access page.

Everything support does is in **Logs**, marked `support:<their email>`, and
your admins are emailed when a support session starts. The Support access
page lists every grant and session.

## 15. Your own domain

Your dashboard and your clients' download links can live on a domain you
own, such as `rds.yourcompany.com`, as well as on the address your provider
gave you (`yourcompany.rds.example.com`). That original address always keeps
working, whatever happens to your own domain. Devices never use your own
domain: installers keep talking to your relay, so changing or losing a domain
can't cut off a single device. Admins manage this under **Admin → Domains**.

### Adding a domain

1. **Admin → Domains → Add a domain.** Type the hostname (for example
   `rds.yourcompany.com`) and click **Add domain**. You can have up to 3.
2. The page shows the record to create at your DNS host, with copy buttons.
   The recommended one is a **CNAME**:

   | Type | Name | Value |
   |---|---|---|
   | CNAME | `rds` (the part before your domain) | your original address, e.g. `yourcompany.rds.example.com` |

3. Wait. We check the record right away and then every 5 minutes for 7 days.
   When it's right, we get a certificate for the domain automatically (a few
   minutes), and the domain shows **Active**. The admins get an email.
4. Optional: **Make primary** so emails, invitations, download links and
   your client guides use your own domain.

**Can't use a CNAME?** That happens on a bare domain (`yourcompany.com`
itself) and at some DNS hosts. Open "Can't use a CNAME?" on the Domains page
and create instead:

| Type | Name | Value |
|---|---|---|
| A | `rds` | the server address shown on the page |
| TXT | `_rdf-verify.rds` | the code shown on the page (starts with `rdf-verify-`) |

Remove any AAAA (IPv6) record for the same name unless the page shows an IPv6
address to use.

### Where to create the record

- **Cloudflare:** DNS → Records → **Add record**. Type `CNAME`, Name `rds`,
  Target your original address. Set **Proxy status** to **DNS only** (the
  grey cloud). With the orange cloud, Cloudflare answers instead of us and
  the check fails with a message about a CDN. On a bare domain Cloudflare
  turns the CNAME into A records, so add the TXT record too.
- **GoDaddy:** My Products → your domain → **DNS** → **Add New Record**.
  Type `CNAME`, Name `rds`, Value your original address, TTL 1 hour.
  GoDaddy doesn't allow a CNAME on the bare domain (`@`): use the A + TXT
  records there.
- **Amazon Route 53:** Hosted zones → your domain → **Create record**.
  Record name `rds`, Record type `CNAME`, Value your original address,
  TTL 300, Simple routing. For the bare domain, use A + TXT (an Alias record
  can't point at us).
- **Microsoft 365** (if Microsoft hosts your DNS): Microsoft 365 admin
  center → Settings → **Domains** → your domain → **DNS records** → **Add
  record**. Type `CNAME`, Host name `rds`, Points to address your original
  address, TTL 1 hour.
- **Azure DNS:** DNS zones → your zone → **+ Record set**. Name `rds`,
  Type `CNAME`, Alias your original address, TTL 1 hour.

If your domain has **CAA** records (they say which certificate authorities
may issue for it), one of them must allow `letsencrypt.org`. The page says so
if one doesn't.

### What each status means

| Status | Meaning |
|---|---|
| Checking DNS… | We're looking at the record now. |
| Waiting for DNS | The record isn't there or isn't right yet. The message says what we found. We check every 5 minutes for 7 days; **Check now** checks at once. |
| Getting a certificate… | The record is right; we're getting the certificate. If that fails we retry at most once an hour, and stop after 5 tries. |
| Active | Your domain works, with its own certificate (renewed automatically). |
| DNS broken | It worked, but the record no longer points at us (we check daily). It keeps working for 7 days in case you're moving it. If it isn't fixed by then, we stop serving it and your links go back to your original address. You get an email both times. |
| Failed | We gave up (7 days without the record, 5 failed certificate tries, or broken for 7 days). Fix the record and press **Check now** to start again. |

**Remove** takes the domain off at once and revokes its certificate. Any
link already sent on that domain (download links, invitations) stops
working. If it was primary, your original address becomes primary again.

### Passkeys on your own domain

Your passkeys belong to your original address, so they keep working whatever
happens to your own domain. Current browsers (Chrome and Edge 128 or later,
Safari 18 or later, Firefox 152 or later) also accept them on your own domain
directly. In an older browser, choosing your passkey on your own domain takes
you to your original address to sign in, then brings you straight back
signed in. Passwords work on your own domain as usual. To add a passkey in
an older browser, add it on your original address (My Account there); it then
works on both.

## 16. Billing

If you signed up on our website, your subscription is billed through Stripe
and admins manage it under **Admin → Billing**. (Accounts your provider set up
by hand show a simple page instead: your provider bills you directly.)

- **What you see:** your plan and its prices, the status (free trial, active,
  payment failed…), when the trial ends or the next invoice's date and
  amount, how many technicians and devices you have against your plan's
  limits, and your past invoices (each opens Stripe's invoice page).
- **Manage billing** opens Stripe's secure billing portal: change your card,
  download invoices, update your billing address or tax ID, or cancel. We
  never see your card details.
- **Change plan** switches to another plan at once; any difference is
  prorated on your next invoice.
- **What counts:** technicians are your users (invitations not yet accepted,
  and your provider's support staff, don't count); devices are the devices
  on your Devices page. Going over a plan's limits shows a notice; nothing is
  blocked.

**The trial.** 14 days, with a card on file; it converts to a paid
subscription on its own when it ends, and we email you 3 days before. One
trial per card: a card that has had a trial starts the next subscription
right away.

**If a payment fails** you'll see a notice and get an email; update your
card under Manage billing. After 7 days unpaid the account is suspended:
remote support and installer downloads stop, but admins can still sign in
and pay under Admin → Billing, and the account reopens as soon as the
payment goes through. Nothing is deleted.

**If there's no card on file** (for example you removed it), you'll see a
notice: without a card the subscription ends when the trial does, or fails
at the next renewal.

**Cancelling** (in the billing portal) takes effect at the end of the period
you've paid for; until then everything works and a notice shows the date.
After that the account is cancelled: remote support stops, and for 30 days
admins can sign in to download their data from the export page, and
**Resubscribe** there to bring everything back as it was (same relay, keys
and devices; a card is required). After 30 days the data is deleted for good.

## Quick task index

| I want to… | Go to |
|---|---|
| Onboard a new machine | Client Groups → group → build/share installer |
| Connect to a device | Devices → Connect |
| Remove a duplicate/old device | Devices → Edit/Delete |
| Group devices by customer | Client Groups → create group, set device groups |
| Push auto-update / lock settings | Client Groups → group → Client Settings |
| Give a colleague access | Admin → Users → add user + role |
| Use my own domain for the dashboard and links | Admin → Domains → Add a domain |
| Change card, see invoices, cancel | Admin → Billing → Manage billing |
| Limit what a role can do | Admin → Roles & permissions |
| Set up off-site backups | Admin → Backup & Restore |
| Restore the server | Admin → Backup & Restore → Restore |
| Get alerted on low disk/memory | Admin → Notifications → health thresholds |
| See who connected to what | Logs → Remote sessions |
| See a device's connection history over time | Logs → Remote sessions → click the device, or Devices → ⓘ → Connection timeline |
| See who connected to which devices over a period | Timeline → By user, pick dates |
| Export the device list | Admin → Export device inventory (CSV) |
| Find Windows 10 / stale / low-spec machines | Dynamic Groups → New (pick a template) |
| Give a newly registered device a group and label | Devices → Recently registered → Assign to Group |
| Edit a device without leaving its client | Client Groups → group → Edit |
| Brand a client's installer (name/icon) | Client Groups → group → Branding |
| Remove the SmartScreen warning | Admin → Installer signing |
| Change what alert emails say / add a logo | Admin → Email templates |
| Turn dark mode on/off | Sidebar footer → Light / Dark / Auto |
| Prepare recommendations for a client | Admin → Reports → Client group health / OS support (filter to the client) |
