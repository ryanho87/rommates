# ROM Manager

ROM Manager is a private, self-hosted web interface for cleaning a canonical ROM library and deploying selected games into per-device ES-DE folders. Syncthing continues to handle transport to each handheld.

The MVP supports:

- Indexed search across platform folders, backed by SQLite
- Compact filtering by platform and duplicate status
- SHA-256 exact duplicate detection
- Normalized-filename possible duplicate review
- Bundle-aware `.cue`/`.bin` and `.m3u` scanning
- Safe bundle rename with `.cue` and `.m3u` reference rewriting
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
└── .rommanager-trash/
```

Every direct child of `devices` containing a `roms` directory is discovered as a device during a library scan.

## Deploy with Docker Compose

1. Copy this project to the Ubuntu server.
2. Create the deployment environment file:

   ```bash
   cp .env.example .env
   openssl rand -hex 32
   ```

   Put the generated value in `ROM_ACCESS_TOKEN` and set `EMULATION_ROOT` to the absolute host directory containing `roms` and `devices`. Compose refuses to start when either value is missing.

3. Create the state and trash directories as the same Linux user that owns the ROM library:

   ```bash
   mkdir -p data /srv/Emulation/.rommanager-trash
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

6. Open `http://SERVER-IP:8080`, enter `ROM_ACCESS_TOKEN`, and let the startup scan finish.

The token protects the application from unauthenticated and cross-site API requests. It is still intended for a trusted private network. Bind `ROM_MANAGER_BIND=127.0.0.1` when placing it behind a reverse proxy.

## Syncthing ignores

Add these patterns to the Syncthing folder ignore list as another layer of protection:

```text
._*
.DS_Store
```

## How device reconciliation works

Selections represent the desired managed set for a device. Applying changes:

1. Copies missing or changed files from `/emulation/roms` to `/emulation/devices/{device}/roms`.
2. Removes previously managed files whose games were unselected.
3. Leaves unrelated, unmanaged files alone.
4. Removes Finder metadata from the target device ROM tree.

On the first run, existing files in a device directory are considered unmanaged and will not be deleted. Select matching games in the UI to bring them under management.

You can build the desired set in either direction:

- **Library:** Find a game, select its device count, and choose every device that should include it.
- **Devices:** Choose one device and select or unselect many games, then review and apply its pending changes.

Library assignments update the desired selections only. Open Devices and apply a target when you want ROM Manager to change its filesystem.

## Rename and delete safety

- Renaming a descriptor bundle renames its primary file and companion files whose names share the primary stem. It then rewrites references in `.cue` and `.m3u` files.
- Companion files with unrelated names remain unchanged but stay part of the bundle.
- Deleting atomically moves the entire indexed bundle into `.rommanager-trash` and records its original paths. Library, device, and trash directories share one Emulation mount so the operation cannot degrade into an interruptible copy-and-delete.
- Deleting a canonical game also moves copies previously deployed by ROM Manager into the same recoverable trash bundle. Restore puts both canonical and deployed copies back.
- Permanent deletion is available only from the Trash screen.

Keep normal filesystem backups. Recoverable trash protects against UI mistakes, not disk failure or manual filesystem changes.

## Configuration

| Variable | Default | Purpose |
|---|---:|---|
| `ROM_LIBRARY_ROOT` | `/emulation/roms` in Compose | Canonical platform-folder library |
| `ROM_DEVICES_ROOT` | `/emulation/devices` in Compose | Parent of device directories |
| `ROM_TRASH_ROOT` | `/emulation/.rommanager-trash` in Compose | Recoverable deleted bundles |
| `ROM_DATABASE_PATH` | `/data/rommanager.db` | SQLite catalog and selections |
| `ROM_SCAN_ON_START` | `true` | Start a background scan at boot |
| `ROM_REQUIRE_EXISTING_ROOTS` | `true` in Compose | Fail startup instead of silently creating missing mounts |
| `ROM_ACCESS_TOKEN` | required by Compose | Token entered in the browser and sent as a Bearer credential |
| `ROM_EXTENSIONS` | built-in list | Optional comma-separated extension override |

## Local development

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
ROM_LIBRARY_ROOT=/tmp/roms \
ROM_DEVICES_ROOT=/tmp/devices \
ROM_TRASH_ROOT=/tmp/rom-trash \
ROM_DATABASE_PATH=/tmp/rommanager.db \
ROM_ACCESS_TOKEN=development-token-123456 \
.venv/bin/uvicorn app.main:app --reload --port 8080
```

Run the filesystem and API tests with:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

## Updating the NUC with Git

After the repository has a remote, clone it on the NUC once. For later updates:

```bash
git pull --ff-only
docker compose up -d --build
```

Keep `.env`, `data/`, and the ROM library outside Git. Database migrations run automatically when the updated container starts.
