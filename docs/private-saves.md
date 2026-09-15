# Private saves

Members can open **Saves → Enable private saves** to create an empty personal
save vault. The vault uses an immutable UUID, independent of usernames and
device names. Each user currently has one private vault, shared only between
that user's explicitly connected devices.

## Existing saves stay in place

The existing `ROMMATES_SAVES_ROOT`, snapshot history, schedule, and Syncthing
share continue to work as before. Administrators see **Existing shared saves**
by default and can select private vaults for support. Deployment does not create
personal vaults or move or share existing files. Migrating the existing source
is a separate future operation.

## Connect a handheld

1. Set up the handheld's Syncthing connection from **Devices**.
2. Open **Saves**, enable private saves, and expand **Connected devices**.
3. Choose **Connect saves** for a device you own.
4. On that handheld, accept the **Private saves** folder and choose its
   `Emulation/saves` directory. Use **Send & Receive**.
5. Point RetroArch at `Emulation/saves/retroarch`. Use per-emulator directories
   underneath `Emulation/saves` for standalone emulators.

ROMmates creates a dedicated Syncthing folder for the vault, set to **Send &
Receive** on the server. Managed ROM folders still use **Send Only**. The
handheld must be online to receive the invitation; accepting it remains a
handheld action. "Share configured" means server setup succeeded, not that the
handheld has accepted the invitation or finished syncing.

**Disconnect** removes the save share for that handheld, preserving existing
files and snapshots. It cannot erase saves already downloaded to a handheld.
Disconnect before changing a device's owner or Syncthing identity. Devices
with a configured private save connection remain registered if their ROM
directory disappears, so the connection can still be managed.

## Storage and protection

- Live private saves: a sibling of `ROMMATES_DEVICES_ROOT`, under
  `save-vaults/vault-<uuid>/` (normally `/emulation/save-vaults/vault-<uuid>`).
- Private snapshot blobs and staging: `ROMMATES_SNAPSHOTS_ROOT/vaults/vault-<uuid>/`.
- The database records vault ownership, connections, settings, snapshots,
  conflict resolutions, and job ownership. Existing records remain legacy
  records with no private vault assigned.
- Syncthing paths use `ROMMATES_SYNCTHING_DEVICES_ROOT` when configured, otherwise
  the same mount mapping inferred for device ROM shares. Both containers need
  access to the shared Emulation directory, including its new `save-vaults`
  sibling. No additional mount is needed when Emulation is already mounted.
- An overlapping Syncthing parent share is rejected to prevent accidentally
  distributing private saves to its existing peers.
- Automatic snapshots and retention run independently for each vault. Conflicts
  and completed save operations appear in the owner's in-app notifications.
- Every save API resolves an authorized vault before reading files or snapshot
  records. Members cannot use another user's snapshot IDs to download, restore,
  compare, or pin files. Administrators can inspect all vaults.

## API

- `GET /api/save-vaults`: list accessible private vaults.
- `POST /api/save-vaults`: idempotently enable the signed-in user's private vault.
- `POST /api/save-vaults/{vault_id}/devices/{device_id}`: configure its save share.
- `DELETE /api/save-vaults/{vault_id}/devices/{device_id}`: disconnect its save share.
- Existing `/api/saves` endpoints accept `?vault_id=<id>`. Without it, members
  use their own vault and administrators use the existing shared source.

The authenticated native API permits these routes with the same ownership
checks. The native app's Saves tab exposes opt-in, device connections, current
files, snapshots, and conflict recovery. It always sends an explicit private vault
ID, including when signed in with a restricted administrator session. Native save
settings and individual snapshot-file downloads remain available through the web
interface rather than the app.
Sharing a vault between different users and migrating the legacy source are
not enabled in this release.
