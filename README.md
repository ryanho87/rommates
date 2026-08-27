# ROMmates

ROMmates is a private, self-hosted web interface for cleaning a canonical ROM library and giving each handheld its own game roster. It deploys selected games into per-device ES-DE folders, while Syncthing continues to handle transport to each device.

The MVP supports:

- Indexed search across platform folders, backed by SQLite
- Compact filtering by platform and duplicate status
- SHA-256 exact duplicate detection
- Normalized-filename possible duplicate review
- Bundle-aware `.cue`/`.bin` and `.m3u` scanning
- Safe bundle rename with `.cue` and `.m3u` reference rewriting
- Reviewable naming suggestions from XML DAT catalogs and conservative filename cleanup
- Confidence filters, collision checks, editable proposals, and bulk bundle renaming
- Recoverable deletion, restoration, and explicit permanent deletion
- Per-device desired game selections and previewed reconciliation
- Per-game device assignment directly from the Library screen
- Inline device status tags showing synced, pending-add, and pending-remove assignments
- Automatic cleanup of `._*` and `.DS_Store` inside device ROM folders during apply
- Background jobs for scans, copies, renames, delete, restore, and purge operations
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

Every direct child of `devices` containing a `roms` directory is discovered as a device during a library scan.

## Deploy with Docker Compose

1. Copy this project to the Ubuntu server.
2. Create the deployment environment file:

   ```bash
   cp .env.example .env
   openssl rand -hex 32
   ```

   Put the generated value in `ROMMATES_ACCESS_TOKEN` and set `EMULATION_ROOT` to the absolute host directory containing `roms` and `devices`. ROMmates refuses to start when either value is missing.

3. Create the state and trash directories as the same Linux user that owns the ROM library:

   ```bash
   mkdir -p data /srv/Emulation/.rommates-trash
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

6. Open `http://SERVER-IP:8080`, enter `ROMMATES_ACCESS_TOKEN`, and let the startup scan finish.

The token protects the application from unauthenticated and cross-site API requests. It is still intended for a trusted private network. Bind `ROMMATES_BIND=127.0.0.1` when placing it behind a reverse proxy.

The application refuses to start without a token, so a misconfigured launch fails loudly instead of exposing the API. If an authenticated reverse proxy already guards the app, set `ROMMATES_ALLOW_ANONYMOUS=true` to opt out deliberately.

## Syncthing ignores

Add these patterns to the Syncthing folder ignore list as another layer of protection:

```text
._*
.DS_Store
*.rommates-copy
*.rommanager-copy
```

## How device reconciliation works

Selections represent the desired managed set for a device. Applying changes:

1. Copies missing or changed files from `/emulation/roms` to `/emulation/devices/{device}/roms`.
2. Removes previously managed files whose games were unselected.
3. Leaves unrelated, unmanaged files alone.
4. Removes Finder metadata and interrupted ROMmates temp files from the target device ROM tree.

Each copy is recorded as it lands, so an apply that fails or is interrupted partway leaves every file it already wrote under management. Re-running the apply finishes the job, and unselecting a game still removes what was copied.

On the first run, existing files in a device directory are considered unmanaged and will not be deleted. Select matching games in the UI to bring them under management.

You can build the desired set in either direction:

- **Library:** Find a game, select its device count, and choose every device that should include it.
- **Devices:** Choose one device and select or unselect many games, then review and apply its pending changes.

Library assignments update the desired selections only. Open Devices and apply a target when you want ROMmates to change its filesystem.

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

## Rename and delete safety

- Renaming a descriptor bundle renames its primary file and companion files whose names share the primary stem. It then rewrites references in `.cue` and `.m3u` files.
- Companion files with unrelated names remain unchanged but stay part of the bundle.
- Deleting atomically moves the entire indexed bundle into `.rommates-trash` and records its original paths. Library, device, and trash directories share one Emulation mount so the operation cannot degrade into an interruptible copy-and-delete.
- Deleting a canonical game also moves copies previously deployed by ROMmates into the same recoverable trash bundle. Restore puts both canonical and deployed copies back.
- Permanent deletion is available only from the Trash screen.

Keep normal filesystem backups. Recoverable trash protects against UI mistakes, not disk failure or manual filesystem changes.

## Naming suggestions

The Naming screen always offers conservative cleanup for obvious pack prefixes such as
`03.` and underscore-separated words. It does not guess capitalization or remove region,
language, revision, and disc metadata.

Import a No-Intro, Redump, or other Logiqx-style XML DAT and assign it to the matching
ROMmates platform for canonical suggestions. Suggestions have three confidence levels:

- **Exact DAT match:** a DAT-provided SHA-256 matches an indexed content file.
- **Strong name match:** the normalized current name uniquely matches one DAT entry for the platform and extension.
- **Cleanup only:** only deterministic local formatting rules were applied.

Many DAT files publish CRC, MD5, or SHA-1 but not SHA-256. Those catalogs still provide
strong filename matches; ROMmates does not label them exact without a verifiable SHA-256.
Suggested names remain editable. Colliding targets are disabled, and every batch is
validated again before any file changes. Applying suggestions uses the same bundle-aware
rename path as the Library screen, preserves device selections, updates descriptor
references, and carries hash-cache records to the new paths.

## Configuration

| Variable | Default | Purpose |
|---|---:|---|
| `ROMMATES_LIBRARY_ROOT` | `/emulation/roms` in Compose | Canonical platform-folder library |
| `ROMMATES_DEVICES_ROOT` | `/emulation/devices` in Compose | Parent of device directories |
| `ROMMATES_TRASH_ROOT` | `/emulation/.rommates-trash` in Compose | Recoverable deleted bundles |
| `ROMMATES_DATABASE_PATH` | `/data/rommates.db` | SQLite catalog and selections |
| `ROMMATES_SCAN_ON_START` | `true` | Start a background scan at boot |
| `ROMMATES_REQUIRE_EXISTING_ROOTS` | `true` in Compose | Fail startup instead of silently creating missing mounts |
| `ROMMATES_ACCESS_TOKEN` | **required** | Token entered in the browser and sent as a Bearer credential. Startup fails if it is missing or under 16 characters |
| `ROMMATES_ALLOW_ANONYMOUS` | `false` | Disables the token check. Only for instances already behind an authenticated reverse proxy |
| `ROMMATES_SCAN_PRUNE_LIMIT` | `0.5` | Largest share of the catalog one scan may delete without confirmation |
| `ROMMATES_EXTENSIONS` | built-in list | Optional comma-separated extension override |

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
