from __future__ import annotations

import os
import threading
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from .config import Settings
from .db import Database
from .library import LibraryError


THUMBNAIL_CACHE_VERSION = "v1"
THUMBNAIL_MAX_SIZE = (160, 160)
THUMBNAIL_REFRESH_SECONDS = 300


class ArtworkThumbnailCache:
    """Persistent, asynchronously hydrated thumbnails for library-sized views."""

    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self.root = settings.media_root / ".thumbnails"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._status_lock = threading.Lock()
        self._status: dict[str, object] = {
            "state": "idle",
            "total": 0,
            "ready": 0,
            "generated": 0,
            "failed": 0,
        }

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="rommates-artwork-cache",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def status(self) -> dict[str, object]:
        with self._status_lock:
            return dict(self._status)

    def _set_status(self, **updates: object) -> None:
        with self._status_lock:
            self._status.update(updates)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.hydrate()
            except Exception as exc:
                self._set_status(state="failed", error=str(exc))
            if self._stop.wait(THUMBNAIL_REFRESH_SECONDS):
                return

    def _assets(self) -> list[dict[str, object]]:
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT id,local_relpath,content_type,sha256 FROM game_assets "
                "WHERE kind='cover' ORDER BY id"
            ).fetchall()
        return [dict(row) for row in rows]

    def _cache_path(self, asset: dict[str, object]) -> Path:
        digest = str(asset["sha256"])[:16]
        return self.root / f"{asset['id']}-{digest}-{THUMBNAIL_CACHE_VERSION}.webp"

    def _source_path(self, asset: dict[str, object]) -> Path:
        root = self.settings.media_root.resolve()
        source = (root / str(asset["local_relpath"])).resolve()
        if root not in source.parents or not source.is_file():
            raise LibraryError("Artwork file is missing")
        return source

    def _generate(self, asset: dict[str, object], destination: Path) -> None:
        source = self._source_path(asset)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(
            f".{destination.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            with Image.open(source) as opened:
                image = ImageOps.exif_transpose(opened)
                image.thumbnail(THUMBNAIL_MAX_SIZE, Image.Resampling.LANCZOS)
                if image.mode not in {"RGB", "RGBA"}:
                    image = image.convert("RGBA" if "transparency" in image.info else "RGB")
                image.save(temporary, format="WEBP", quality=78, method=4)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    def ensure(self, asset_id: int) -> tuple[Path, str]:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT id,local_relpath,content_type,sha256 FROM game_assets "
                "WHERE id=? AND kind='cover'",
                (asset_id,),
            ).fetchone()
        if not row:
            raise LibraryError("Cover artwork was not found")
        asset = dict(row)
        destination = self._cache_path(asset)
        if not destination.is_file():
            try:
                self._generate(asset, destination)
            except (OSError, UnidentifiedImageError):
                # An invalid legacy image should not make the library cover vanish.
                return self._source_path(asset), str(asset["content_type"])
        return destination, "image/webp"

    def hydrate(self) -> dict[str, object]:
        assets = self._assets()
        self.root.mkdir(parents=True, exist_ok=True)
        valid_paths = {self._cache_path(asset) for asset in assets}
        ready = generated = failed = 0
        self._set_status(
            state="running",
            total=len(assets),
            ready=0,
            generated=0,
            failed=0,
            error="",
        )
        for asset in assets:
            if self._stop.is_set():
                self._set_status(state="stopped")
                return self.status()
            destination = self._cache_path(asset)
            if destination.is_file():
                ready += 1
            else:
                try:
                    self._generate(asset, destination)
                    ready += 1
                    generated += 1
                except (OSError, UnidentifiedImageError, LibraryError):
                    failed += 1
            if (ready + failed) % 25 == 0:
                self._set_status(ready=ready, generated=generated, failed=failed)
        for cached in self.root.glob("*.webp"):
            if cached not in valid_paths:
                cached.unlink(missing_ok=True)
        self._set_status(
            state="complete",
            total=len(assets),
            ready=ready,
            generated=generated,
            failed=failed,
        )
        return self.status()
