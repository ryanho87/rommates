"""Private save sources alongside the unchanged legacy save service."""
from __future__ import annotations

import threading
import uuid
from dataclasses import replace
from pathlib import Path

from .config import Settings
from .db import Database
from .library import LibraryError
from .saves import SaveSnapshotService


class SaveVaultService:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self._lock = threading.RLock()
        self._services: dict[int, SaveSnapshotService] = {}

    @property
    def root(self) -> Path:
        # A sibling of the existing shared saves tree, never a child of it.
        return self.settings.devices_root.parent / "save-vaults"

    def list(self, owner_user_id: int | None = None) -> list[dict]:
        with self.db.connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT v.*,u.display_name AS owner_name FROM save_vaults v "
                "JOIN users u ON u.id=v.owner_user_id "
                "WHERE (? IS NULL OR v.owner_user_id=?) ORDER BY u.display_name,v.id",
                (owner_user_id, owner_user_id),
            )]

    def get(self, vault_id: int) -> dict:
        with self.db.connect() as connection:
            row = connection.execute("SELECT * FROM save_vaults WHERE id=?", (vault_id,)).fetchone()
        if not row:
            raise LibraryError("Save vault was not found")
        return dict(row)

    def create(self, owner_user_id: int) -> dict:
        with self._lock:
            with self.db.write() as connection:
                connection.execute(
                    "INSERT OR IGNORE INTO save_vaults(storage_key,owner_user_id) VALUES(?,?)",
                    (f"vault-{uuid.uuid4()}", owner_user_id),
                )
                vault = dict(connection.execute(
                    "SELECT * FROM save_vaults WHERE owner_user_id=?", (owner_user_id,)
                ).fetchone())
            self.service(vault["id"], create=True)
            return vault

    def service(self, vault_id: int, *, create: bool = False) -> SaveSnapshotService:
        with self._lock:
            vault = self.get(vault_id)
            key = vault["storage_key"]
            try:
                valid_key = f"vault-{uuid.UUID(key.removeprefix('vault-'))}"
            except (ValueError, AttributeError):
                raise LibraryError("Invalid save vault storage identity") from None
            if key != valid_key:
                raise LibraryError("Invalid save vault storage identity")
            source = self.root / key
            legacy = self.settings.saves_root.resolve()
            resolved = source.resolve()
            if self.root.is_symlink() or source.is_symlink() or (
                resolved == legacy or legacy in resolved.parents or resolved in legacy.parents
            ):
                raise LibraryError("Private save vaults must be separate from the legacy save source")
            if create:
                source.mkdir(parents=True, exist_ok=True)
                (source / "retroarch").mkdir(exist_ok=True)
            service = self._services.get(vault_id)
            if service is None:
                # Separate blob directories keep retention and in-flight captures
                # independent of legacy snapshots and other users' vaults.
                private_settings = replace(
                    self.settings,
                    saves_root=source,
                    snapshots_root=self.settings.snapshots_root / "vaults" / key,
                )
                service = SaveSnapshotService(private_settings, self.db, vault_id)
                service.initialize()
                self._services[vault_id] = service
            return service

    def devices(self, vault_id: int) -> list[dict]:
        with self.db.connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT d.id,d.name,d.syncthing_device_id,vd.connected_at "
                "FROM devices d JOIN save_vaults v ON d.owner_user_id=v.owner_user_id "
                "LEFT JOIN save_vault_devices vd ON vd.device_id=d.id AND vd.vault_id=v.id "
                "WHERE v.id=? ORDER BY d.name COLLATE NOCASE", (vault_id,),
            )]
