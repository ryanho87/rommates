# ROMmates

ROMmates is a private, self-hosted web interface for cleaning a canonical ROM library and giving each handheld its own game roster. It deploys selected games into per-device ES-DE folders, while Syncthing continues to handle transport to each device.

## Screenshots

### Collection overview

![ROMmates collection overview showing platform totals, cleanup alerts, and device sync status](docs/screenshots/overview.jpg)

### Library and device assignment

![ROMmates library with duplicate status, bundle details, and inline device assignment](docs/screenshots/library-device-assignment.jpg)

### Grouped duplicate review

![ROMmates exact duplicate group with keeper selection and save impact](docs/screenshots/duplicate-review.jpg)

### Device reconciliation

![ROMmates device page showing a pending multi-file game deployment](docs/screenshots/device-sync.jpg)

The MVP supports:

- At-a-glance collection dashboard with cleanup, device, save, artwork, and job health
- Indexed search across platform folders, backed by SQLite
- Compact filtering by platform and duplicate status
- SHA-256 exact duplicate detection
- Normalized-filename possible duplicate review
- Bundle-aware `.cue`, `.gdi`, and `.m3u` descriptor scanning, including unquoted GDI track filenames with spaces
- Fast metadata-indexed folder bundles for directory-based platforms such as PS3 and Wii U
- Vita title-ID bundles combining `app`, `patch`, `addcont`, and `license` trees into one game
- Native `.3ds`, `.cci`, `.nsp`, `.xci`, and `.vpk` discovery
- Switch base-game indexing that excludes update title IDs and support trees such as updates, DLC, cheats, and mods
- Safe bundle rename with descriptor reference rewriting
- Reviewable naming suggestions from XML DAT catalogs and conservative filename cleanup
- Confidence filters, collision checks, editable proposals, and bulk bundle renaming
- Recoverable deletion, restoration, and explicit permanent deletion
- Bulk permanent deletion from Trash with one review and confirmation
- Resumable, chunked browser uploads on a dedicated Transfers screen
- Authenticated per-game downloads, with streamed ZIP output for multi-file bundles
- Owner-scoped device packages that stream every selected ROM as one ES-DE-ready ZIP
- Cross-page duplicate keeper review with one confirmed batch move to recoverable Trash
- Per-device desired game selections and previewed reconciliation
- One-time device cloning plus owner-scoped device groups with shared rosters and full create, rename, membership, and delete controls
- Per-user device ownership with member-scoped onboarding, library assignments, and apply jobs
- Actual device-directory inventory shown in device views and on each Library game row
- Per-game device assignment directly from the Library screen
- Inline device status tags showing synced, pending-add, and pending-remove assignments
- Automatic cleanup of `._*` and `.DS_Store` inside device ROM folders during apply
- Background jobs for scans, copies, renames, delete, restore, and purge operations
- Safe stop controls for scans and device deployment jobs
- Automatic infinite loading across library, device, duplicate, naming, save, snapshot, and job-issue results
- Detailed job reports with timing, structured results, and scroll-loaded unreadable-file paths
- Deduplicated multi-emulator save snapshots with scheduling, retention, historical downloads, and guarded full-state restore
- Read-only RetroArch save-to-ROM matching, orphan detection, and rename impact warnings
- Snapshot-backed deletion of confirmed orphan save groups
- ScreenScraper matching with ratings, within-platform ranking, and locally cached artwork
- Best-effort Discord webhook notifications for uploads, save conflicts, failures, and optional completion events
- Bearer-token protection for the private API
- Browser/OS light and dark themes

## Expected directories

The server layout should look like:

```text
Emulation/
├── roms/
│   ├── gba/
│   ├── psx/
│   └── snes/
├── devices/
│   ├── retroid-pocket/
│   │   └── roms/
│   └── steam-deck/
│       └── roms/
└── .rommates-trash/
```

Every direct child of `devices` containing a `roms` directory is discovered during a library
scan. Administrators can also use **Devices > Add device** to create and register that folder
immediately from a phone, without waiting for a scan.

## Deploy with Docker Compose

1. Copy this project to the Ubuntu server.
2. Create the deployment environment file:

   ```bash
   cp .env.example .env
   openssl rand -hex 32
   ```

   Put the generated value in `ROMMATES_ACCESS_TOKEN` and set `EMULATION_ROOT` to the absolute host directory containing `roms`, `devices`, and the shared `saves` vault. ROMmates refuses to start when the token or Emulation root is missing.

3. Create the state and trash directories as the same Linux user that owns the ROM library:

   ```bash
   mkdir -p data data/save-snapshots /srv/Emulation/.rommates-trash
   ```

4. Set `PUID` and `PGID` in `.env` to the account that owns the Emulation directory. Find them with:

   ```bash
   id -u
   id -g
   ```

5. Build and start:

   ```bash
   docker compose up -d --build
   ```

6. Open `http://SERVER-IP:8080`, enter `ROMMATES_ACCESS_TOKEN`, and let the startup scan finish. Open **Users** to create named accounts.

The token protects the application from unauthenticated and cross-site API requests. It is still intended for a trusted private network. Bind `ROMMATES_BIND=127.0.0.1` when placing it behind a reverse proxy.

The application refuses to start without a token, so a misconfigured launch fails loudly instead of exposing the API. If an authenticated reverse proxy already guards the app, set `ROMMATES_ALLOW_ANONYMOUS=true` to opt out deliberately.

### Accounts and roles

`ROMMATES_ACCESS_TOKEN` remains the bootstrap administrator credential. Use it to create
named accounts, recover access, and manage roles. Named sessions use an HttpOnly,
SameSite cookie and expire after 30 days. Passwords require at least 12 characters and
are salted and hashed with scrypt. Administrators assign a temporary password when they
create or reset an account; the user must replace it before accessing ROMmates. Signed-in
users can change their own password from the account controls, which invalidates their
other sessions.

Roles are independent grants and can be combined on one account. For example, give a
friend both **Contributor** and **Member** to let them submit reviewed uploads while also
managing only the devices assigned to them.

- **Viewer:** browse the library, inspect cached artwork and rankings, and create a
  short-lived, single-use ROM download.
- **Contributor:** Viewer access plus resumable uploads. Completed uploads remain in the
  isolated staging directory until an administrator approves them.
- **Member:** Viewer access plus onboarding and management of devices they own. Members can
  select ROMs, apply their device changes, and inspect or stop only their own apply jobs.
- **Admin:** complete access to scans, cleanup, naming, devices, saves, jobs,
  notifications, upload review, and user management.

Disabled accounts and administrator password resets invalidate their active sessions. Cloudflare Access
can remain the outer identity gate, but ROMmates roles are the authorization layer inside
the service. Do not set `ROMMATES_ALLOW_ANONYMOUS=true` when you want per-user roles;
anonymous proxy access is intentionally treated as administrator access for backward
compatibility.

### Native iOS access

Keep the browser and administrator UI behind Cloudflare Access. For the native app,
create a separate HTTPS hostname such as `api.rommates.example.com` and set that exact
hostname in `ROMMATES_MOBILE_PUBLIC_HOSTS`. On requests received through that hostname,
ROMmates exposes only the native-client route allowlist, accepts only server-issued
mobile bearer sessions, rejects browser sessions and the bootstrap administrator token,
and does not honor anonymous proxy mode. Normal role and device-ownership checks still
apply to every allowed route.

Do not put `ROMMATES_ACCESS_TOKEN` or a Cloudflare service token in the distributed app.
The iOS client signs in through `/api/v1/mobile/session` and stores its opaque session in
Keychain. When `ROMMATES_MOBILE_PUBLIC_HOSTS` is empty, no hostname is treated as the
restricted public mobile surface. See the [iOS server launch checklist](docs/IOS_SERVER_LAUNCH_CHECKLIST.md)
before publishing a hostname.

The save vault is read from `${EMULATION_ROOT}/saves` through the existing `/emulation`
mount. The account selected by `PUID` and `PGID` needs read/write access to both the
Emulation directory and `ROMMATES_DATA_ROOT`. Snapshot blobs are stored beneath that data
root in `save-snapshots`.

## Shared save vault and snapshots

ROMmates treats `Emulation/saves` as the live Syncthing-backed source and stores immutable,
content-addressed versions under `/data/save-snapshots`. The expected layout is one
top-level directory per standalone emulator plus `retroarch/<core>`. Unchanged files share
a single SHA-256 blob, so frequent snapshots only consume space for changed save data.

If you are moving from the old RetroArch WebDAV arrangement, remove that separate mount
from ROMmates and point every device's Syncthing save folder at the shared host directory
`${EMULATION_ROOT}/saves`. Configure RetroArch to write beneath `saves/retroarch` and each
standalone emulator beneath its own directory. Compose already mounts this location at
`/emulation/saves`; no additional ROMmates environment variable is required. Let Syncthing
finish before taking the first snapshot, and keep `/data/save-snapshots` on persistent
storage. Configure the save folder as **Send & Receive** on the NUC and every handheld so
changes can flow in either direction and Syncthing can surface concurrent edits as conflict
files for ROMmates to review. Close the relevant emulator before resolving a conflict or
restoring a snapshot, then let Syncthing settle before reopening it.

The **Saves** screen provides:

- Searchable files grouped by emulator, core, and file type
- A Save matching review queue for filename-based RetroArch and standalone emulator saves
- Syncthing conflict detection with device attribution, content comparison, and resolution history
- Recoverable orphan cleanup that forces a full safety snapshot before deletion
- Manual snapshots with optional notes
- Scheduled snapshots and tiered recent, daily, weekly, and monthly retention
- Pinned snapshots that retention never removes
- Complete change previews against the live save tree
- Historical file downloads that do not alter the live vault
- Full-tree restore with a mandatory pre-restore safety snapshot

Identifier-based layouts such as Dolphin memory cards and Ryujinx title-ID directories are
inventoried and snapshotted, but are not guessed into ROM filename matches. Close every
emulator on every device and allow Syncthing to finish before restoring. The
restore job verifies that live files still match the preview, stages and hashes every
historical file, creates a safety snapshot, and rolls the live directory back if the job
fails or is stopped during mutation.

When Syncthing preserves concurrent edits as `.sync-conflict-*` files, the **Conflicts**
tab presents the current and alternate hashes side by side. ROMmates never chooses a
winner automatically. Resolving a conflict creates a forced safety snapshot containing
both branches, revalidates both reviewed hashes, applies the selected version atomically,
and records the decision. This makes simultaneous play recoverable, but it cannot merge
two emulator save files into one combined progression.

## Syncthing ignores

Add these patterns to the Syncthing folder ignore list as another layer of protection:

```text
._*
.DS_Store
*.rommates-copy
*.rommanager-copy
*.rommates-link
```

## Syncthing device presence

ROMmates can show the live connection state of every device configured on the NUC's
Syncthing instance. Status is cached, and the API key is never exposed to the browser.
After a successful device apply, ROMmates also asks Syncthing to rescan the matching device
folder. It recognizes a dedicated device ROM folder or a parent `Emulation`/`devices` share;
if no folder matches or Syncthing is unavailable, the completed deployment remains successful
and the job result explains why the rescan was skipped. Device owners can also enter their
handheld's Syncthing device ID from the Devices page. ROMmates then adds the remote device,
creates or reuses the device ROM folder, shares it, and requests a scan. Folder paths are derived
from the owned ROMmates device; browser clients cannot provide arbitrary server paths.

Create or copy an API key from **Syncthing > Actions > Settings > General > API Key**, then
set these values in ROMmates' `.env`:

```dotenv
ROMMATES_SYNCTHING_URL=http://syncthing:8384
ROMMATES_SYNCTHING_API_KEY=replace-with-your-syncthing-api-key
# Optional when Syncthing uses a different container mount than ROMmates:
ROMMATES_SYNCTHING_DEVICES_ROOT=/media/Emulation/devices
```

The URL is resolved inside the ROMmates container. If Syncthing is defined in a different
Compose project, attach both services to a shared Docker network so the `syncthing` hostname
resolves, or use another URL that is reachable from the ROMmates container. The API key is
passed to the container by `compose.yaml`; recreating the container is required after changing
it. The Overview dashboard then reports online/offline state, connection type, address, client
version, paused devices, and the last recorded connection time.

## How device reconciliation works

Selections represent the desired managed set for a device. Each device can use independent
copies or prefer hardlinks. Applying changes:

1. Deploys missing or changed files from `/emulation/roms` to `/emulation/devices/{device}/roms`.
   Hardlink-preferred devices use a zero-additional-storage hardlink when the source and
   target are on the same underlying filesystem, with a safe copy fallback.
2. Removes previously managed files whose games were unselected.
3. Leaves unrelated, unmanaged files alone.
4. Removes Finder metadata and interrupted ROMmates temp files from the target device ROM tree.

Missing system directories are created automatically. ROMmates translates unambiguous
human-readable library folders to ES-DE's canonical, case-sensitive paths (for example,
`Nintendo Game Boy` to `gb` and `PlayStation` to `psx`) and preserves every nested bundle
path below them. Unknown or custom platform folders are preserved instead of guessed.

Each deployment is recorded as it lands, so an apply that fails or is interrupted partway leaves every file it already wrote under management. That record is an ownership boundary—not a cached claim that a file is a copy or hardlink. ROMmates derives the storage relationship from the source and destination inodes whenever it renders the device preview. Re-running the apply finishes the job, and unselecting a game still removes what was deployed.

Opening a device reconciles its actual ROM directory and publishes that inventory to the
database. Library and duplicate pages reuse the persisted inventory instead of recursively
walking every device directory during navigation. Use **Refresh** on the Devices page after
making files outside ROMmates when you need those external changes reflected immediately.

Within `roms/switch`, packages in `update`, `updates`, `dlc`, `cheats`, `mods`, or
`firmware` trees are deliberately excluded from the game catalog. Root-level packages
whose Nintendo title ID ends in `800`, plus packages explicitly named `Update` or `UPD`,
are also treated as support content. Base `.nsp` and `.xci` packages remain selectable.

Folder-based games are indexed from their file paths, sizes, and timestamps without reading
every byte during a normal scan. This keeps large PS3, Wii U, and unpacked Vita libraries
responsive. Because that structural fingerprint is not proof of identical content, folder
bundles do not appear as exact SHA-256 duplicates; ROMmates presents same-title folders in
the possible-duplicate review instead. Single-file and descriptor-based games retain full
content hashing and exact duplicate detection when their files are no larger than the
configured scan threshold. New or changed files above that threshold are indexed from
their path, size, and timestamp instead. A previously cached full hash remains valid and is
reused without rereading the file. This keeps large ISO, NSP, XCI, and similar additions
from blocking the catalog while preserving exact duplicate detection for smaller ROMs.

Unpacked Vita libraries use the platform layout directly. ROMmates recognizes both the
`vita` and ES-DE `psvita` platform names, treats each title ID beneath `app` as the primary
game, and includes files carrying that title ID beneath `patch`, `addcont`, and `license`.
Orphan support trees without a matching `app` title are ignored rather than exposed as
thousands of individual ROMs.

Changing a device from **Independent copies** to **Hardlinks preferred** makes the next
apply atomically replace eligible managed copies with hardlinks. The filename Syncthing
sees does not change. Removing the device entry only unlinks that entry; the canonical ROM
remains. ROMmates treats ROM content as immutable because permissions, timestamps, and
in-place writes are shared by every hardlink to the same inode.

For mergerfs pools, hardlinks require the device/platform directory to exist on the same
underlying branch as the canonical ROM. Configure the mergerfs mount with `func.mkdir=all`
and mirror existing `/devices/{device}/roms/{platform}` directories across the branches
once before converting. ROMmates falls back to an independent copy when the kernel returns
`EXDEV` or otherwise rejects a hardlink, so enabling the preference is safe before every
directory has been prepared.

Device apply jobs can be stopped from the header or Jobs screen. ROMmates checks for
cancellation between copy chunks, removes the current temporary partial file, and leaves
previously completed copies recorded for the next apply. Short atomic operations such as
rename, trash, restore, and purge are not interruptible once they begin.

Open **Jobs → Report** to inspect a run's status, duration, result counts, and every
unreadable file with its library-relative path and error reason. Scans created before
this feature may expose only the first 50 paths retained by that older version; the
report labels that limitation, and the next cached scan records the complete list.

## ScreenScraper metadata and artwork

ROMmates can match games with ScreenScraper and cache a cover, screenshot, and logo under
`/data/media`. Artwork belongs to the logical game bundle, so multi-track discs and
folder-based games receive one media set. Scrapes run as cancellable background jobs;
single-file ROMs with a completed catalog hash use CRC32, MD5, SHA-1, and size before
falling back to an unambiguous exact-title match. Large files deliberately deferred by the
scanner go directly to the title fallback instead of triggering a surprise multi-gigabyte
read. Fingerprints and downloaded assets are reused by later jobs.

Matched games also cache ScreenScraper's community score on its documented 0–20 scale
and Staff Pick flag. Library and device views show the score and its rank among rated
games on the same platform. Choose a platform, then use **Fetch missing ratings** to run
a metadata-only job for every unrated game in that set; this does not download artwork.
The sort control supports best or lowest rating, title, and file size. Unrated games
always remain at the end of rating sorts.

For platform-wide coverage, configure a RAWG API key and open **Top 100 coverage** after
selecting a platform. ROMmates caches RAWG's highest Metacritic-ranked games, then marks
each title as owned, a possible filename match, or missing. Possible matches are shown
for review and never silently counted as owned. RAWG requires attribution, so every
ranking panel includes active links back to RAWG and the individual game pages.

ScreenScraper requires developer credentials issued for the application. Put these in
the same `.env` file as `EMULATION_ROOT`, then recreate the container:

```env
ROMMATES_SCREENSCRAPER_DEV_ID=your-developer-id
ROMMATES_SCREENSCRAPER_DEV_PASSWORD=your-developer-password
ROMMATES_SCREENSCRAPER_SOFTNAME=ROMmates
ROMMATES_SCREENSCRAPER_USER=your-optional-user-name
ROMMATES_SCREENSCRAPER_PASSWORD=your-optional-user-password
RAWG_API_KEY=your-rawg-api-key
```

ROMmates downloads ScreenScraper's current system list and maps common folder names such
as `gba`, `gbc`, `megadrive`, and `ps3`. Add numeric overrides for custom folder names as
a JSON object:

```env
ROMMATES_SCREENSCRAPER_SYSTEM_MAP={"my-gba-folder":12,"custom-system":123}
```

ROMmates follows ScreenScraper's account-specific API limits. Every request includes the
configured developer credentials and software name, and only one ScreenScraper request
stream can run at a time. Limits returned by the API are enforced for requests per minute,
requests per day, unmatched ROMs per day, and media download speed. The system catalog is
cached for 24 hours, while ROM fingerprints and artwork remain cached until their source
changes or you explicitly refresh them. Closure, credential, and blocked-client responses
stop the job with a specific error instead of being retried aggressively.

Open **Artwork** to queue missing media for the entire library, one or more platforms, or
a hand-picked set of ROMs. Covers-only mode minimizes API use and gets useful library art
in place first; full mode also requests a screenshot and logo. The page shows the active
run, quota use, live progress, stop and report controls, and the results of past runs.

Artwork queues are persisted in SQLite, skip locally cached assets, pause when
ScreenScraper reports a rate or daily quota limit, and resume automatically after the
allowed wait. An interrupted container resumes the same queue on startup. Stop cancels the
remaining queue without removing completed assets. Ambiguous name matches and unmapped
platforms are skipped and listed in the job report rather than guessed. Library artwork
tiles display cached media and link unmatched ROMs into the scoped Artwork workflow.

On the first run, existing files in a device directory are considered unmanaged and will not be deleted. Select matching games in the UI to bring them under management.

You can build the desired set in either direction:

- **Library:** Find a game, select its device count, and choose every device that should include it.
- **Devices:** Choose one device and select or unselect many games, then review and apply its pending changes.

For a new handheld, **Devices > Add device** creates
`/emulation/devices/{device}/roms`. Choose automatic Syncthing delivery or manual ZIP downloads
during onboarding. Syncthing devices require an administrator to add and share the host folder;
ROMmates deliberately does not edit Syncthing's cluster configuration. Download-only devices
can select games immediately and use **Download ROM package** to stream one ZIP containing the
complete selected bundles under their ES-DE platform paths. The archive is generated without
creating another persistent copy on the server.

Devices created by a Member are owned by that account automatically. Existing device folders
remain administrator-only after upgrade until an administrator chooses an owner on the Devices
page. Ownership is enforced by the API, not just hidden in the browser.

New and existing devices can copy the desired roster from another accessible device as a one-time
operation. Devices can also be combined into a named, owner-specific group. A group appears once
in the target picker, owns one shared desired roster, and hides its individual members from the
top-level list. Members see and manage only groups they own; administrators can see every group.
All devices in a group must have the same owner. From **Devices**, use **Create group** to choose a
name, source roster, and members; group settings allow renaming, adding or removing devices, and
deleting the group. Deleting a group preserves its devices and selected games. Adding or removing
a game updates the complete group; **Review and apply group** queues a separate filesystem
reconciliation and Syncthing rescan for every member. Removing a member preserves its current
desired games and makes future changes independent.

Library assignments update the desired selections only. Open Devices and apply a target when you want ROMmates to change its filesystem.

## MCP server

ROMmates exposes an authenticated Streamable HTTP MCP endpoint at `/mcp/`. When the UI is
available at `https://rommates.example.com`, configure an MCP host to connect to:

```text
https://rommates.example.com/mcp/
```

Send the existing access token as an HTTP header:

```text
Authorization: Bearer YOUR_ROMMATES_ACCESS_TOKEN
```

The endpoint uses the same container, port, Traefik router, and token as the browser API;
it does not require another Compose service or published port. Keep the token in the MCP
host's secret or environment configuration rather than placing it in the URL or committing
it to a client configuration file.

The initial tool surface can search and inspect games, list duplicate groups, inspect
devices, preview device changes, read job reports and live scan telemetry, start safe scans,
change device selections, apply reviewed device plans, and stop cancellable jobs. It does
not expose shell execution, caller-provided filesystem paths, ROM deletion, trash purging,
save restoration, or uploads.

Applying a device plan is a two-step operation. `preview_device_changes` returns a
`preview_token`; `apply_device_changes` requires that exact token and revalidates it when
the serialized filesystem worker starts. If selections, library files, deployments, device
inventory, or deployment mode changed while the job waited, it fails without applying the
stale plan.

All MCP filesystem operations use the normal job queue and return a `job_id`. MCP-requested
mutations are also recorded in Activity. `ROMMATES_ALLOW_ANONYMOUS=true` disables token
checks for MCP as well as the browser API, so use it only when the reverse proxy protects
the complete host.

## Scan safety

A scan reconciles the catalog with the filesystem, and removing a game also removes the
device selections and deployment records that depend on it. Because an unmounted or
still-mounting library volume is indistinguishable from an emptied one, a scan refuses to
continue when it would delete more than `ROMMATES_SCAN_PRUNE_LIMIT` (default 50%) of the
catalog. The job fails with an explanation, nothing changes, and the UI offers a
confirmation if you really did remove those files.

Device folders are pruned the same way: a device whose directory is gone is removed, but
a devices root that reports no devices at all is treated as unavailable rather than empty.

Files that cannot be read — including symlinks, which are never indexed — are counted and
named in the scan job detail instead of being dropped silently.

Disc descriptors (`.cue`, `.gdi`, and `.m3u`) are indexed with their referenced tracks as
one game. Dreamcast and PSX game folders claim every nested disc component, preventing
shared tracks or incomplete descriptor rows from becoming standalone games. Platforms
listed in `ROMMATES_FOLDER_BUNDLE_PLATFORMS` are indexed as one game per immediate child
folder, including every nested file; `ps3` and `wiiu` are enabled by default. Cartridge
collection folders remain file-based. A corrective scan automatically merges legacy
component rows into their logical bundle while preserving device selections and managed
deployment history.

Running scans can be stopped from the header or Jobs screen. Cancellation is cooperative:
ROMmates finishes the current filesystem checkpoint, preserves completed hash-cache batches,
and does not commit a partially reconciled catalog. A later scan resumes from those cached
hashes.

## Rename and delete safety

- Renaming a descriptor bundle renames its primary file and companion files whose names share the primary stem. It then rewrites references in `.cue` and `.m3u` files.
- Companion files with unrelated names remain unchanged but stay part of the bundle.
- Deleting atomically moves the entire indexed bundle into `.rommates-trash` and records its original paths. Library, device, and trash directories share one Emulation mount so the operation cannot degrade into an interruptible copy-and-delete.
- Deleting a canonical game also moves copies previously deployed by ROMmates into the same recoverable trash bundle. Restore puts both canonical and deployed copies back.
- Permanent deletion is available only from the Trash screen.
- Trash supports selecting any number of bundles and permanently deleting them in one background job.

Keep normal filesystem backups. Recoverable trash protects against UI mistakes, not disk failure or manual filesystem changes.

## Browser transfers

Open **Transfers** to upload a single ROM, a related set of files, or a complete game
folder. Data is written to a staging directory in bounded chunks, can resume after an
interruption when the same files are selected again, and is moved into the library only
after every declared byte arrives. Existing library paths are never overwritten. Folder
uploads follow the same descriptor and folder-bundle rules as the scanner, and archives
are stored as ROM files rather than extracted on the server.

Contributor uploads stop after validation and enter the administrator review queue on the
same page. Approval runs the normal atomic finalize and indexing job. Rejection deletes
the staged bytes and retains the decision in upload history until normal expiry cleanup.

Use **Download** beside a game in Library. The authenticated API creates a short-lived,
opaque, single-use download URL. Single-file games stream directly; multi-file games stream as an
uncompressed ZIP without first creating another full copy on disk. Download URLs expire
quickly and are revalidated against the indexed library before use.

## Naming suggestions

The Naming screen always offers conservative cleanup for obvious pack prefixes such as
`03.` and underscore-separated words. It does not guess capitalization or remove region,
language, revision, and disc metadata.

Import a No-Intro, Redump, or other Logiqx-style XML DAT and assign it to the matching
ROMmates platform for canonical suggestions. Suggestions have four confidence levels:

- **Exact DAT match:** a DAT-provided SHA-256 matches an indexed content file.
- **Strong name match:** the normalized current name uniquely matches one DAT entry for the platform and extension.
- **ScreenScraper title:** cached ScreenScraper metadata provides a localized canonical title.
- **Cleanup only:** only deterministic local formatting rules were applied.

DAT matches take precedence over ScreenScraper titles when both are available.

Many DAT files publish CRC, MD5, or SHA-1 but not SHA-256. Those catalogs still provide
strong filename matches; ROMmates does not label them exact without a verifiable SHA-256.
Suggested names remain editable. Colliding targets are disabled, and every batch is
validated again before any file changes. Applying suggestions uses the same bundle-aware
rename path as the Library screen, preserves device selections, updates descriptor
references, and carries hash-cache records to the new paths.

ROMmates also checks the live RetroArch `saves/` and `states/` trees before showing a
rename. Exact content filenames are matched first, with the core directory used to narrow
platforms when possible. Normalized-name matches are explicitly marked as possible or
ambiguous. Save matching is read-only: a ROM rename never silently renames or deletes save
data. “Select all” on the Naming screen skips games with matched save data by default;
individual affected games can still be selected after reviewing the warning.

The Save matching screen permits deletion only for groups with no ROM candidate. ROMmates
revalidates the match and filesystem metadata in the background, publishes a forced safety
snapshot, then removes the live files. Possible and ambiguous groups cannot be deleted.
Duplicate and ROM trash confirmations list matching save filenames that would be orphaned.

## Configuration

| Variable | Default | Purpose |
|---|---:|---|
| `ROMMATES_LIBRARY_ROOT` | `/emulation/roms` in Compose | Canonical platform-folder library |
| `ROMMATES_DEVICES_ROOT` | `/emulation/devices` in Compose | Parent of device directories |
| `ROMMATES_TRASH_ROOT` | `/emulation/.rommates-trash` in Compose | Recoverable deleted bundles |
| `ROMMATES_UPLOAD_ROOT` | `/emulation/.rommates-uploads` in Compose | Staging area for incomplete browser uploads; must be outside the ROM library |
| `ROMMATES_UPLOAD_MAX_BYTES` | `137438953472` (128 GiB) | Maximum declared size of one upload session |
| `ROMMATES_UPLOAD_CHUNK_BYTES` | `8388608` (8 MiB) | Maximum body size of each resumable upload request |
| `ROMMATES_UPLOAD_EXPIRY_HOURS` | `24` | Age after which abandoned staged uploads are removed |
| `ROMMATES_DOWNLOAD_TICKET_SECONDS` | `300` | Lifetime of an opaque game download URL |
| `ROMMATES_DATABASE_PATH` | `/data/rommates.db` | SQLite catalog and selections |
| `ROMMATES_SCAN_ON_START` | `true` | Start a background scan at boot |
| `ROMMATES_REQUIRE_EXISTING_ROOTS` | `true` in Compose | Fail startup instead of silently creating missing mounts |
| `ROMMATES_ACCESS_TOKEN` | **required** | Bearer credential used by the browser API and `/mcp/`. Startup fails if it is missing or under 16 characters |
| `ROMMATES_ALLOW_ANONYMOUS` | `false` | Disables the token check. Only for instances already behind an authenticated reverse proxy |
| `ROMMATES_MOBILE_PUBLIC_HOSTS` | empty | Optional comma-separated hostnames restricted to the native-app API allowlist and mobile bearer sessions |
| `ROMMATES_SCAN_PRUNE_LIMIT` | `0.5` | Largest share of the catalog one scan may delete without confirmation |
| `ROMMATES_EXTENSIONS` | built-in list | Optional comma-separated extension override |
| `ROMMATES_FOLDER_BUNDLE_PLATFORMS` | `ps3,wiiu` | Comma-separated platforms where each immediate child directory is one game bundle |
| `ROMMATES_HASH_MAX_BYTES` | `536870912` (512 MiB) | Largest new or changed file SHA-256 hashes during a normal scan. Larger files use a structural fingerprint; `0` hashes every file |
| `ROMMATES_RAWG_API_KEY` / `RAWG_API_KEY` | empty | Optional RAWG key for cached per-platform Top 100 Metacritic coverage; the prefixed name takes precedence |
| `ROMMATES_SYNCTHING_URL` | empty | Optional base URL for the NUC Syncthing API as reached from the ROMmates container |
| `ROMMATES_SYNCTHING_API_KEY` | empty | Syncthing API key used by the backend for status, device/folder share setup, and rescans; never exposed to browsers |
| `ROMMATES_SYNCTHING_DEVICES_ROOT` | inferred | Optional device root in Syncthing's filesystem namespace, such as `/media/Emulation/devices`, when its container mount differs from ROMmates |
| `ROMMATES_SYNCTHING_TIMEOUT_SECONDS` | `2` | Maximum wait for a Syncthing status request, from 0.25 to 10 seconds |
| `ROMMATES_SYNCTHING_CACHE_SECONDS` | `10` | How long ROMmates reuses device presence results before querying Syncthing again |
| `ROMMATES_DISCORD_WEBHOOK_URL` | empty | Optional Discord channel webhook used for outbound notifications; treat it as a secret |
| `ROMMATES_DISCORD_TIMEOUT_SECONDS` | `5` | Maximum wait for one Discord delivery attempt, from 1 to 20 seconds |
| `ROMMATES_PUBLIC_URL` | empty | Public ROMmates origin included in notification links, such as `https://rommates.example.com` |

## Discord notifications

Create an incoming webhook in the Discord channel that should receive ROMmates alerts,
put its URL in `ROMMATES_DISCORD_WEBHOOK_URL`, and recreate the container. Then open
**Notifications** to send a test and choose events. Uploads, save conflicts, and job
failures are enabled by default; scans, device changes, save operations, and trash
changes are opt-in.

Delivery is outbound-only: ROMmates does not install a Discord bot or accept commands
from Discord. Messages are queued on a background worker, retried up to three times,
and recorded in the Notifications page. A Discord outage cannot fail the ROM, save, or
device operation that produced the alert. Mentions are disabled on webhook payloads.

## Local development

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
ROMMATES_LIBRARY_ROOT=/tmp/roms \
ROMMATES_DEVICES_ROOT=/tmp/devices \
ROMMATES_TRASH_ROOT=/tmp/rommates-trash \
ROMMATES_DATABASE_PATH=/tmp/rommates.db \
ROMMATES_ACCESS_TOKEN=development-token-123456 \
.venv/bin/uvicorn app.main:app --reload --port 8080
```

Run the filesystem and API tests with:

```bash
.venv/bin/python -m pytest
```

## Updating the NUC with Git

After the repository has a remote, clone it on the NUC once. For later updates:

```bash
git pull --ff-only
docker compose up -d --build
```

Keep `.env`, `data/`, and the ROM library outside Git. Database migrations run automatically when the updated container starts.

## Upgrading from ROM Manager

Existing deployments upgrade in place. ROMmates accepts the previous `ROM_*` environment variables, migrates the default `rommanager.db` database and `.rommanager-trash` directory, cleans up legacy copy-temp files, and moves the saved browser token to the new key. New configuration should use the `ROMMATES_*` names above.

For the rename release, update the remote and remove the old Compose service once:

```bash
git remote set-url origin https://github.com/ryanho87/rommates.git
git pull --ff-only
docker compose -p rommanager down --remove-orphans
docker compose up -d --build
```

Later releases only need the normal `git pull` and `docker compose up` commands.
