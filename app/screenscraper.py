from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path
from typing import Callable

from .config import Settings
from .db import Database
from .library import JobCancelled, LibraryError


API_ROOT = "https://api.screenscraper.fr/api2"
MAX_MEDIA_BYTES = 20 * 1024 * 1024
ASSET_CHOICES = {
    "cover": ("box-2D", "box-2D-hd", "mixrbv2", "mixrbv1"),
    "screenshot": ("ss", "sstitle"),
    "logo": ("wheel-hd", "wheel"),
}
PLATFORM_ALIASES = {
    "gb": ("game boy",), "gbc": ("game boy color",),
    "gba": ("game boy advance",), "nds": ("nintendo ds",),
    "nes": ("nintendo entertainment system", "nes"),
    "snes": ("super nintendo", "super famicom"),
    "n64": ("nintendo 64",), "gamecube": ("nintendo gamecube", "gamecube"),
    "gc": ("nintendo gamecube", "gamecube"), "wii": ("nintendo wii", "wii"),
    "megadrive": ("megadrive", "mega drive", "genesis"),
    "genesis": ("megadrive", "mega drive", "genesis"),
    "mastersystem": ("master system",), "sms": ("master system",),
    "gamegear": ("game gear",), "gg": ("game gear",),
    "dreamcast": ("dreamcast",), "saturn": ("saturn",),
    "psx": ("playstation",), "ps1": ("playstation",),
    "ps2": ("playstation 2",), "ps3": ("playstation 3",),
    "psp": ("playstation portable", "psp"),
}


def _normalized(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _walk(value):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _localized(items: object, preferred=("us", "wor", "eu", "ss", "en", "fr")) -> str:
    if isinstance(items, str):
        return items
    if not isinstance(items, list):
        return ""
    values = [item for item in items if isinstance(item, dict)]
    for region in preferred:
        for item in values:
            if str(item.get("region") or item.get("langue") or "").casefold() == region:
                return str(item.get("text") or item.get("nom") or "")
    return str((values[0].get("text") or values[0].get("nom") or "")) if values else ""


class ScreenScraperService:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db

    @property
    def configured(self) -> bool:
        return bool(self.settings.screenscraper_dev_id and self.settings.screenscraper_dev_password)

    def status(self) -> dict[str, object]:
        with self.db.connect() as connection:
            counts = connection.execute(
                "SELECT COUNT(DISTINCT game_id) AS games,COUNT(*) AS assets FROM game_assets"
            ).fetchone()
        return {
            "configured": self.configured,
            "media_root": str(self.settings.media_root),
            "games_with_artwork": counts["games"],
            "assets": counts["assets"],
            "system_overrides": self.settings.screenscraper_system_map or {},
        }

    def _params(self, **extra) -> dict[str, object]:
        params: dict[str, object] = {
            "devid": self.settings.screenscraper_dev_id,
            "devpassword": self.settings.screenscraper_dev_password,
            "softname": self.settings.screenscraper_softname,
            "output": "json",
        }
        if self.settings.screenscraper_user:
            params["ssid"] = self.settings.screenscraper_user
        if self.settings.screenscraper_password:
            params["sspassword"] = self.settings.screenscraper_password
        params.update({key: value for key, value in extra.items() if value not in (None, "")})
        return params

    def _request_json(self, endpoint: str, **params) -> dict:
        url = f"{API_ROOT}/{endpoint}?{urllib.parse.urlencode(self._params(**params))}"
        request = urllib.request.Request(url, headers={"User-Agent": "ROMmates/0.1"})
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                body = response.read(8 * 1024 * 1024)
        except urllib.error.HTTPError as exc:
            raise LibraryError(
                f"ScreenScraper rejected the request ({exc.code}); check the credentials and account quota"
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise LibraryError(f"ScreenScraper could not be reached: {exc}") from exc
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise LibraryError("ScreenScraper returned an invalid response") from exc

    def _systems(self) -> dict[str, int]:
        result = dict(self.settings.screenscraper_system_map or {})
        data = self._request_json("systemesListe.php")
        for node in _walk(data):
            if not isinstance(node, dict) or not str(node.get("id", "")).isdigit():
                continue
            names: list[str] = []
            for key in ("noms", "nom", "nomcourt", "shortname"):
                value = node.get(key)
                if isinstance(value, str):
                    names.append(value)
                elif isinstance(value, list):
                    names.extend(
                        str(item.get("text") or item.get("nom") or "")
                        for item in value if isinstance(item, dict)
                    )
            for name in names:
                if _normalized(name):
                    result.setdefault(_normalized(name), int(node["id"]))
        return result

    def _system_id(self, platform: str, systems: dict[str, int]) -> int | None:
        key = platform.casefold()
        if key in systems:
            return systems[key]
        candidates = (platform, *PLATFORM_ALIASES.get(key, ()))
        for candidate in candidates:
            normalized = _normalized(candidate)
            if normalized in systems:
                return systems[normalized]
        return None

    def _fingerprint(
        self,
        game: dict,
        files: list[dict],
        cancel_check: Callable[[], None],
        byte_progress: Callable[[int], None] | None = None,
    ) -> dict[str, str]:
        with self.db.connect() as connection:
            cached = connection.execute(
                "SELECT * FROM game_fingerprints WHERE game_id=? AND bundle_hash=?",
                (game["id"], game["bundle_hash"]),
            ).fetchone()
        if cached:
            return dict(cached)
        if len(files) != 1:
            return {}
        path = self.settings.library_root / files[0]["relpath"]
        crc = 0
        md5 = hashlib.md5(usedforsecurity=False)
        sha1 = hashlib.sha1(usedforsecurity=False)
        completed = 0
        with path.open("rb") as handle:
            while chunk := handle.read(4 * 1024 * 1024):
                cancel_check()
                crc = zlib.crc32(chunk, crc)
                md5.update(chunk)
                sha1.update(chunk)
                completed += len(chunk)
                if byte_progress:
                    byte_progress(completed)
        result = {"crc32": f"{crc & 0xffffffff:08X}", "md5": md5.hexdigest(), "sha1": sha1.hexdigest()}
        with self.db.write() as connection:
            connection.execute(
                "INSERT INTO game_fingerprints(game_id,bundle_hash,crc32,md5,sha1) VALUES(?,?,?,?,?) "
                "ON CONFLICT(game_id) DO UPDATE SET bundle_hash=excluded.bundle_hash,crc32=excluded.crc32,"
                "md5=excluded.md5,sha1=excluded.sha1,updated_at=CURRENT_TIMESTAMP",
                (game["id"], game["bundle_hash"], result["crc32"], result["md5"], result["sha1"]),
            )
        return result

    @staticmethod
    def _game_node(data: dict) -> dict | None:
        response = data.get("response", data)
        game = response.get("jeu") if isinstance(response, dict) else None
        if isinstance(game, dict):
            return game
        games = response.get("jeux") if isinstance(response, dict) else None
        if isinstance(games, dict):
            games = games.get("jeu") or games.get("jeux")
        if isinstance(games, list):
            return next((item for item in games if isinstance(item, dict)), None)
        return None

    def _match(
        self, game: dict, files: list[dict], system_id: int, cancel_check, byte_progress=None
    ) -> tuple[dict | None, str]:
        fingerprints = self._fingerprint(game, files, cancel_check, byte_progress)
        rom_name = Path(game["primary_relpath"]).name
        if fingerprints:
            data = self._request_json(
                "jeuInfos.php", systemeid=system_id, romtype="rom", romnom=rom_name,
                romtaille=game["size"], crc=fingerprints["crc32"],
                md5=fingerprints["md5"], sha1=fingerprints["sha1"],
            )
            matched = self._game_node(data)
            if matched:
                return matched, "hash"
        data = self._request_json("jeuRecherche.php", systemeid=system_id, recherche=game["display_name"])
        response = data.get("response", data)
        games = response.get("jeux", []) if isinstance(response, dict) else []
        if isinstance(games, dict):
            games = games.get("jeu") or games.get("jeux") or []
        if not isinstance(games, list):
            games = []
        target = _normalized(game["display_name"])
        exact = []
        for candidate in games:
            if not isinstance(candidate, dict):
                continue
            title = _localized(candidate.get("noms") or candidate.get("nom"))
            if _normalized(title) == target:
                exact.append(candidate)
        return (exact[0], "name") if len(exact) == 1 else (None, "ambiguous" if exact else "none")

    @staticmethod
    def _media(game_node: dict) -> list[dict]:
        medias = game_node.get("medias", [])
        if isinstance(medias, dict):
            medias = medias.get("media") or []
        return [item for item in medias if isinstance(item, dict)] if isinstance(medias, list) else []

    def _download_asset(self, game_id: int, kind: str, media: dict, overwrite: bool) -> bool:
        url = str(media.get("url") or "")
        if not url.startswith("https://"):
            return False
        with self.db.connect() as connection:
            existing = connection.execute(
                "SELECT id FROM game_assets WHERE game_id=? AND kind=?", (game_id, kind)
            ).fetchone()
        if existing and not overwrite:
            return False
        request = urllib.request.Request(url, headers={"User-Agent": "ROMmates/0.1"})
        with urllib.request.urlopen(request, timeout=60) as response:
            content_type = response.headers.get_content_type()
            if not content_type.startswith("image/"):
                return False
            data = response.read(MAX_MEDIA_BYTES + 1)
        if len(data) > MAX_MEDIA_BYTES:
            raise LibraryError(f"ScreenScraper {kind} exceeded the 20 MB safety limit")
        extension = {"image/png": ".png", "image/webp": ".webp"}.get(content_type, ".jpg")
        directory = self.settings.media_root / str(game_id)
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / f"{kind}{extension}"
        temporary = directory / f".{kind}.rommates-download"
        temporary.write_bytes(data)
        os.replace(temporary, destination)
        relpath = destination.relative_to(self.settings.media_root).as_posix()
        with self.db.write() as connection:
            previous = connection.execute(
                "SELECT local_relpath FROM game_assets WHERE game_id=? AND kind=?", (game_id, kind)
            ).fetchone()
            connection.execute(
                "INSERT INTO game_assets(game_id,source,kind,media_type,local_relpath,content_type,size,sha256) "
                "VALUES(?,'screenscraper',?,?,?,?,?,?) ON CONFLICT(game_id,kind) DO UPDATE SET "
                "source=excluded.source,media_type=excluded.media_type,local_relpath=excluded.local_relpath,"
                "content_type=excluded.content_type,size=excluded.size,sha256=excluded.sha256,updated_at=CURRENT_TIMESTAMP",
                (game_id, kind, str(media.get("type") or ""), relpath, content_type, len(data), hashlib.sha256(data).hexdigest()),
            )
        if previous and previous["local_relpath"] != relpath:
            old = self.settings.media_root / previous["local_relpath"]
            old.unlink(missing_ok=True)
        return True

    def scrape(self, game_ids: list[int], missing_only: bool = True, *, progress_callback, cancel_check) -> dict[str, object]:
        if not self.configured:
            raise LibraryError("ScreenScraper is not configured. Add its developer credentials to Compose.")
        self.settings.media_root.mkdir(parents=True, exist_ok=True)
        systems = self._systems()
        matched = downloaded = skipped = 0
        issues: list[str] = []
        for index, game_id in enumerate(dict.fromkeys(game_ids)):
            cancel_check()
            with self.db.connect() as connection:
                row = connection.execute("SELECT * FROM games WHERE id=?", (game_id,)).fetchone()
                files = connection.execute("SELECT * FROM game_files WHERE game_id=? ORDER BY relpath", (game_id,)).fetchall()
                existing = connection.execute("SELECT COUNT(*) AS count FROM game_assets WHERE game_id=?", (game_id,)).fetchone()["count"]
            if not row:
                skipped += 1
                issues.append(f"Game {game_id}: no longer exists")
                continue
            game = dict(row)
            if missing_only and existing >= len(ASSET_CHOICES):
                skipped += 1
                continue
            progress_callback(int(index * 100 / max(len(game_ids), 1)), f"Scraping {index + 1} of {len(game_ids)} · {game['display_name']}")
            system_id = self._system_id(game["platform"], systems)
            if system_id is None:
                skipped += 1
                issues.append(f"{game['primary_relpath']}: platform '{game['platform']}' could not be mapped to ScreenScraper")
                continue
            def report_bytes(completed: int) -> None:
                fraction = min(completed / max(game["size"], 1), 1.0)
                progress = int((index + fraction * 0.8) * 100 / max(len(game_ids), 1))
                progress_callback(
                    progress,
                    f"Fingerprinting {index + 1} of {len(game_ids)} · "
                    f"{completed / (1024 ** 2):.1f} MB of {game['size'] / (1024 ** 2):.1f} MB",
                )

            node, method = self._match(
                game, [dict(item) for item in files], system_id, cancel_check, report_bytes
            )
            if not node:
                skipped += 1
                issues.append(f"{game['primary_relpath']}: no unambiguous ScreenScraper match ({method})")
                continue
            source_game_id = str(node.get("id") or node.get("gameid") or "")
            if not source_game_id:
                skipped += 1
                issues.append(f"{game['primary_relpath']}: ScreenScraper match had no game ID")
                continue
            matched += 1
            title = _localized(node.get("noms") or node.get("nom"))
            description = _localized(node.get("synopsis"))
            with self.db.write() as connection:
                connection.execute(
                    "INSERT INTO game_metadata(game_id,source,source_game_id,source_system_id,match_method,title,description,raw_json) "
                    "VALUES(?,'screenscraper',?,?,?,?,?,?) ON CONFLICT(game_id) DO UPDATE SET "
                    "source_game_id=excluded.source_game_id,source_system_id=excluded.source_system_id,"
                    "match_method=excluded.match_method,title=excluded.title,description=excluded.description,"
                    "raw_json=excluded.raw_json,updated_at=CURRENT_TIMESTAMP",
                    (game_id, source_game_id, system_id, method, title, description, json.dumps(node, ensure_ascii=False)),
                )
            medias = self._media(node)
            for kind, choices in ASSET_CHOICES.items():
                candidates = [media for choice in choices for media in medias if str(media.get("type")) == choice]
                candidates.sort(key=lambda item: (str(item.get("region") or "") not in {"us", "wor", "eu", "ss"},))
                if candidates and self._download_asset(game_id, kind, candidates[0], not missing_only):
                    downloaded += 1
        self.db.activity("artwork", f"Matched {matched} games and downloaded {downloaded} visual assets")
        return {"requested": len(game_ids), "matched": matched, "downloaded": downloaded, "skipped": skipped, "issues": issues}

    def detail(self, game_id: int) -> dict[str, object]:
        with self.db.connect() as connection:
            metadata = connection.execute("SELECT * FROM game_metadata WHERE game_id=?", (game_id,)).fetchone()
            assets = connection.execute("SELECT * FROM game_assets WHERE game_id=? ORDER BY kind", (game_id,)).fetchall()
        metadata_payload = dict(metadata) if metadata else None
        if metadata_payload:
            metadata_payload.pop("raw_json", None)
        return {"metadata": metadata_payload, "assets": [dict(row) for row in assets]}

    def asset_path(self, asset_id: int) -> tuple[Path, str]:
        with self.db.connect() as connection:
            asset = connection.execute("SELECT * FROM game_assets WHERE id=?", (asset_id,)).fetchone()
        if not asset:
            raise LibraryError("Artwork was not found")
        path = (self.settings.media_root / asset["local_relpath"]).resolve()
        root = self.settings.media_root.resolve()
        if root not in path.parents or not path.is_file():
            raise LibraryError("Artwork file is missing")
        return path, asset["content_type"]
