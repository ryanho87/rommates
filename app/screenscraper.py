from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections import deque
from datetime import date, datetime, time as datetime_time, timedelta
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from .config import Settings
from .db import Database
from .library import JobCancelled, LibraryError
from .ratings import screenscraper_rating, screenscraper_top_staff


API_ROOT = "https://api.screenscraper.fr/api2"
MAX_MEDIA_BYTES = 20 * 1024 * 1024
SYSTEM_LIST_TTL_SECONDS = 24 * 60 * 60
SCREENSCRAPER_TIMEZONE = ZoneInfo("Europe/Paris")
QUOTA_FIELDS = {
    "maxthreads": "max_threads",
    "maxdownloadspeed": "max_download_speed",
    "requeststoday": "requests_today",
    "requestskotoday": "failed_requests_today",
    "maxrequestsperdmin": "max_requests_per_minute",
    "maxrequestspermin": "max_requests_per_minute",
    "maxrequestsperminute": "max_requests_per_minute",
    "maxrequestsperday": "max_requests_per_day",
    "maxrequestskoperday": "max_failed_requests_per_day",
}
ASSET_CHOICES = {
    "cover": ("box-2D", "box-2D-hd", "mixrbv2", "mixrbv1"),
    "screenshot": ("ss", "sstitle"),
    "logo": ("wheel-hd", "wheel"),
}


class ScreenScraperDailyQuota(LibraryError):
    """The account quota is exhausted until ScreenScraper's next day."""


class ScreenScraperRateLimit(LibraryError):
    def __init__(self, retry_after: float = 30):
        super().__init__("ScreenScraper's request or concurrency limit was reached")
        self.retry_after = max(1.0, min(float(retry_after), 300.0))


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


def _system_names(value: object) -> list[str]:
    """Return aliases from both legacy and current ScreenScraper name shapes."""
    if isinstance(value, str):
        return [name.strip() for name in value.split(",") if name.strip()]
    if isinstance(value, list):
        names: list[str] = []
        for item in value:
            if isinstance(item, dict):
                names.extend(_system_names(item.get("text") or item.get("nom")))
            else:
                names.extend(_system_names(item))
        return names
    if isinstance(value, dict):
        names: list[str] = []
        for key, item in value.items():
            if str(key).casefold().startswith("nom"):
                names.extend(_system_names(item))
        return names
    return []


class ScreenScraperService:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        # ScreenScraper assigns concurrency per account. A single request stream is
        # valid for every account tier and prevents two queued jobs from overlapping.
        self._scrape_lock = threading.Lock()
        self._rate_lock = threading.Lock()
        self._request_times: deque[float] = deque()
        self._quota: dict[str, int] = {}
        self._quota_day = self._quota_date()
        self._systems_cache: dict[str, int] | None = None
        self._systems_cached_at = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.settings.screenscraper_dev_id and self.settings.screenscraper_dev_password)

    @staticmethod
    def _quota_date() -> date:
        return datetime.now(SCREENSCRAPER_TIMEZONE).date()

    @staticmethod
    def _seconds_until_quota_reset() -> float:
        now = datetime.now(SCREENSCRAPER_TIMEZONE)
        reset = datetime.combine(now.date() + timedelta(days=1), datetime_time.min, SCREENSCRAPER_TIMEZONE)
        return max((reset - now).total_seconds() + 30, 30)

    def status(self) -> dict[str, object]:
        with self.db.connect() as connection:
            counts = connection.execute(
                "SELECT COUNT(DISTINCT game_id) AS games,COUNT(*) AS assets FROM game_assets"
            ).fetchone()
        with self._rate_lock:
            quota = dict(self._quota)
        return {
            "configured": self.configured,
            "media_root": str(self.settings.media_root),
            "games_with_artwork": counts["games"],
            "assets": counts["assets"],
            "system_overrides": self.settings.screenscraper_system_map or {},
            "quota": quota,
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

    @staticmethod
    def _as_nonnegative_int(value: object) -> int | None:
        try:
            result = int(str(value).strip())
        except (TypeError, ValueError):
            return None
        return result if result >= 0 else None

    def _update_quota(self, data: object) -> None:
        updates: dict[str, int] = {}
        for node in _walk(data):
            if not isinstance(node, dict):
                continue
            for raw_key, value in node.items():
                canonical = QUOTA_FIELDS.get(str(raw_key).casefold())
                parsed = self._as_nonnegative_int(value)
                if canonical and parsed is not None:
                    updates[canonical] = parsed
        if not updates:
            return
        with self._rate_lock:
            for key, value in updates.items():
                # Our local counter includes requests made since the last server
                # response, so never move a running count backwards.
                if key in {"requests_today", "failed_requests_today"}:
                    self._quota[key] = max(self._quota.get(key, 0), value)
                else:
                    self._quota[key] = value

    def _before_request(self, cancel_check=None, *, may_miss: bool = False) -> None:
        while True:
            if cancel_check:
                cancel_check()
            now = time.monotonic()
            with self._rate_lock:
                quota_date = self._quota_date()
                if quota_date != self._quota_day:
                    self._quota_day = quota_date
                    self._quota.pop("requests_today", None)
                    self._quota.pop("failed_requests_today", None)
                    self._request_times.clear()
                while self._request_times and self._request_times[0] <= now - 60:
                    self._request_times.popleft()
                requests_today = self._quota.get("requests_today")
                daily_limit = self._quota.get("max_requests_per_day")
                failed_today = self._quota.get("failed_requests_today")
                failed_limit = self._quota.get("max_failed_requests_per_day")
                if daily_limit and requests_today is not None and requests_today >= daily_limit:
                    raise ScreenScraperDailyQuota(
                        "ScreenScraper's daily request quota is exhausted; retry after its quota resets"
                    )
                if may_miss and failed_limit and failed_today is not None and failed_today >= failed_limit:
                    raise ScreenScraperDailyQuota(
                        "ScreenScraper's daily unmatched-ROM quota is exhausted; retry after its quota resets"
                    )
                minute_limit = self._quota.get("max_requests_per_minute")
                if not minute_limit or len(self._request_times) < minute_limit:
                    self._request_times.append(now)
                    if requests_today is not None:
                        self._quota["requests_today"] = requests_today + 1
                    return
                wait_for = max(self._request_times[0] + 60 - now, 0.05)
            # Keep quota waits cancellable so a job can still be stopped promptly.
            time.sleep(min(wait_for, 0.5))

    @staticmethod
    def _http_error(code: int) -> str:
        return {
            401: "ScreenScraper is temporarily overloaded or unavailable to this account tier",
            403: "ScreenScraper rejected the developer credentials",
            423: "ScreenScraper's API is temporarily closed",
            426: "ScreenScraper has blocked this scraper; stop requests and contact ScreenScraper",
            429: "ScreenScraper's request or concurrency limit was reached; wait before retrying",
            430: "ScreenScraper's daily request quota is exhausted",
            431: "ScreenScraper's daily unmatched-ROM quota is exhausted",
        }.get(code, f"ScreenScraper rejected the request ({code})")

    @staticmethod
    def _retry_after(error: urllib.error.HTTPError) -> float:
        try:
            return float(error.headers.get("Retry-After", "30"))
        except (AttributeError, TypeError, ValueError):
            return 30

    def _request_json(self, endpoint: str, *, cancel_check=None, **params) -> dict:
        may_miss = endpoint in {"jeuInfos.php", "jeuRecherche.php"}
        self._before_request(cancel_check, may_miss=may_miss)
        url = f"{API_ROOT}/{endpoint}?{urllib.parse.urlencode(self._params(**params))}"
        request = urllib.request.Request(url, headers={"User-Agent": "ROMmates/0.1"})
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                body = response.read(8 * 1024 * 1024)
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and may_miss:
                with self._rate_lock:
                    if "failed_requests_today" in self._quota:
                        self._quota["failed_requests_today"] += 1
                return {}
            if exc.code == 429:
                raise ScreenScraperRateLimit(self._retry_after(exc)) from exc
            if exc.code in {430, 431}:
                raise ScreenScraperDailyQuota(self._http_error(exc.code)) from exc
            raise LibraryError(self._http_error(exc.code)) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise LibraryError(f"ScreenScraper could not be reached: {exc}") from exc
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise LibraryError("ScreenScraper returned an invalid response") from exc
        self._update_quota(data)
        return data

    def _systems(self, cancel_check=None) -> dict[str, int]:
        now = time.monotonic()
        if self._systems_cache is not None and now - self._systems_cached_at < SYSTEM_LIST_TTL_SECONDS:
            return dict(self._systems_cache)
        result = dict(self.settings.screenscraper_system_map or {})
        data = self._request_json("systemesListe.php", cancel_check=cancel_check)
        for node in _walk(data):
            if not isinstance(node, dict) or not str(node.get("id", "")).isdigit():
                continue
            names: list[str] = []
            for key in ("noms", "nom", "nomcourt", "shortname"):
                names.extend(_system_names(node.get(key)))
            for name in names:
                if _normalized(name):
                    result.setdefault(_normalized(name), int(node["id"]))
        self._systems_cache = dict(result)
        self._systems_cached_at = now
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
        # A blank catalog SHA marks a deliberately deferred large-file hash. Do not
        # turn a fast library scan into the same multi-terabyte read during artwork
        # scraping; use ScreenScraper's exact-name fallback for that title.
        if len(files) != 1 or not files[0].get("sha256"):
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
                "jeuInfos.php", cancel_check=cancel_check,
                systemeid=system_id, romtype="rom", romnom=rom_name,
                romtaille=game["size"], crc=fingerprints["crc32"],
                md5=fingerprints["md5"], sha1=fingerprints["sha1"],
            )
            matched = self._game_node(data)
            if matched:
                return matched, "hash"
        data = self._request_json(
            "jeuRecherche.php", cancel_check=cancel_check,
            systemeid=system_id, recherche=game["display_name"]
        )
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

    def _read_media(self, response, cancel_check=None) -> bytes:
        chunks: list[bytes] = []
        completed = 0
        started = time.monotonic()
        while True:
            if cancel_check:
                cancel_check()
            chunk = response.read(min(64 * 1024, MAX_MEDIA_BYTES + 1 - completed))
            if not chunk:
                break
            chunks.append(chunk)
            completed += len(chunk)
            if completed > MAX_MEDIA_BYTES:
                break
            with self._rate_lock:
                speed_kbps = self._quota.get("max_download_speed", 0)
            if speed_kbps:
                target_elapsed = completed / (speed_kbps * 1024)
                while time.monotonic() - started < target_elapsed:
                    if cancel_check:
                        cancel_check()
                    time.sleep(min(target_elapsed - (time.monotonic() - started), 0.25))
        return b"".join(chunks)

    def _download_asset(
        self, game_id: int, kind: str, media: dict, overwrite: bool, cancel_check=None
    ) -> bool:
        url = str(media.get("url") or "")
        if not url.startswith("https://"):
            return False
        with self.db.connect() as connection:
            existing = connection.execute(
                "SELECT id FROM game_assets WHERE game_id=? AND kind=?", (game_id, kind)
            ).fetchone()
        if existing and not overwrite:
            return False
        self._before_request(cancel_check)
        request = urllib.request.Request(url, headers={"User-Agent": "ROMmates/0.1"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                content_type = response.headers.get_content_type()
                if not content_type.startswith("image/"):
                    return False
                data = self._read_media(response, cancel_check)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise ScreenScraperRateLimit(self._retry_after(exc)) from exc
            if exc.code in {430, 431}:
                raise ScreenScraperDailyQuota(self._http_error(exc.code)) from exc
            raise LibraryError(self._http_error(exc.code)) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise LibraryError(f"ScreenScraper media could not be reached: {exc}") from exc
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

    def scrape(
        self,
        game_ids: list[int],
        missing_only: bool = True,
        download_media: bool = True,
        asset_kinds: tuple[str, ...] | None = None,
        *,
        progress_callback,
        cancel_check,
    ) -> dict[str, object]:
        while not self._scrape_lock.acquire(timeout=0.25):
            cancel_check()
            progress_callback(0, "Waiting for the active ScreenScraper job")
        try:
            return self._scrape_locked(
                game_ids,
                missing_only,
                download_media,
                asset_kinds,
                progress_callback=progress_callback,
                cancel_check=cancel_check,
                record_activity=True,
            )
        finally:
            self._scrape_lock.release()

    def _scrape_locked(
        self,
        game_ids: list[int],
        missing_only: bool = True,
        download_media: bool = True,
        asset_kinds: tuple[str, ...] | None = None,
        *,
        progress_callback,
        cancel_check,
        record_activity: bool = True,
    ) -> dict[str, object]:
        if not self.configured:
            raise LibraryError("ScreenScraper is not configured. Add its developer credentials to Compose.")
        self.settings.media_root.mkdir(parents=True, exist_ok=True)
        systems = self._systems(cancel_check)
        requested_assets = tuple(asset_kinds or ASSET_CHOICES)
        if any(kind not in ASSET_CHOICES for kind in requested_assets):
            raise LibraryError("Unsupported ScreenScraper artwork type")
        matched = downloaded = skipped = 0
        issues: list[str] = []
        for index, game_id in enumerate(dict.fromkeys(game_ids)):
            cancel_check()
            with self.db.connect() as connection:
                row = connection.execute("SELECT * FROM games WHERE id=?", (game_id,)).fetchone()
                files = connection.execute("SELECT * FROM game_files WHERE game_id=? ORDER BY relpath", (game_id,)).fetchall()
                existing = {
                    item["kind"] for item in connection.execute(
                        "SELECT kind FROM game_assets WHERE game_id=?", (game_id,)
                    )
                }
                metadata = connection.execute(
                    "SELECT rating FROM game_metadata WHERE game_id=?", (game_id,)
                ).fetchone()
            if not row:
                skipped += 1
                issues.append(f"Game {game_id}: no longer exists")
                continue
            game = dict(row)
            has_rating = metadata is not None and metadata["rating"] is not None
            if missing_only and has_rating and (
                not download_media or all(kind in existing for kind in requested_assets)
            ):
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
            rating = screenscraper_rating(node)
            top_staff = int(screenscraper_top_staff(node))
            with self.db.write() as connection:
                connection.execute(
                    "INSERT INTO game_metadata(game_id,source,source_game_id,source_system_id,match_method,title,description,rating,top_staff,raw_json) "
                    "VALUES(?,'screenscraper',?,?,?,?,?,?,?,?) ON CONFLICT(game_id) DO UPDATE SET "
                    "source_game_id=excluded.source_game_id,source_system_id=excluded.source_system_id,"
                    "match_method=excluded.match_method,title=excluded.title,description=excluded.description,"
                    "rating=excluded.rating,top_staff=excluded.top_staff,"
                    "raw_json=excluded.raw_json,updated_at=CURRENT_TIMESTAMP",
                    (
                        game_id, source_game_id, system_id, method, title, description,
                        rating, top_staff, json.dumps(node, ensure_ascii=False),
                    ),
                )
            if download_media:
                medias = self._media(node)
                for kind in requested_assets:
                    choices = ASSET_CHOICES[kind]
                    candidates = [media for choice in choices for media in medias if str(media.get("type")) == choice]
                    candidates.sort(key=lambda item: (str(item.get("region") or "") not in {"us", "wor", "eu", "ss"},))
                    if candidates and self._download_asset(
                        game_id, kind, candidates[0], not missing_only, cancel_check
                    ):
                        downloaded += 1
        action = "artwork" if download_media else "ratings"
        detail = f"Matched {matched} games"
        if download_media:
            detail += f" and downloaded {downloaded} visual assets"
        if record_activity:
            self.db.activity(action, detail)
        return {"requested": len(game_ids), "matched": matched, "downloaded": downloaded, "skipped": skipped, "issues": issues}

    def create_bulk_run(self, asset_mode: str) -> tuple[dict[str, object], bool]:
        if asset_mode not in {"cover", "full"}:
            raise LibraryError("Artwork mode must be cover or full")
        with self.db.write() as connection:
            active = connection.execute(
                "SELECT * FROM artwork_bulk_runs WHERE status IN ('queued','running','paused') "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if active:
                return dict(active), False
            if asset_mode == "cover":
                missing_sql = (
                    "NOT EXISTS(SELECT 1 FROM game_assets ga WHERE ga.game_id=g.id AND ga.kind='cover')"
                )
            else:
                missing_sql = " OR ".join(
                    f"NOT EXISTS(SELECT 1 FROM game_assets ga WHERE ga.game_id=g.id AND ga.kind='{kind}')"
                    for kind in ASSET_CHOICES
                )
            game_ids = [
                row["id"] for row in connection.execute(
                    f"SELECT g.id FROM games g WHERE {missing_sql} ORDER BY g.id"
                )
            ]
            status = "queued" if game_ids else "complete"
            connection.execute(
                "INSERT INTO artwork_bulk_runs(asset_mode,status,total_games,completed_at) "
                "VALUES(?,?,?,CASE WHEN ?='complete' THEN CURRENT_TIMESTAMP END)",
                (asset_mode, status, len(game_ids), status),
            )
            run_id = connection.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            connection.executemany(
                "INSERT INTO artwork_bulk_items(run_id,game_id) VALUES(?,?)",
                ((run_id, game_id) for game_id in game_ids),
            )
            run = connection.execute("SELECT * FROM artwork_bulk_runs WHERE id=?", (run_id,)).fetchone()
        return dict(run), True

    def attach_bulk_job(self, run_id: int, job_id: int) -> None:
        with self.db.write() as connection:
            connection.execute(
                "UPDATE artwork_bulk_runs SET job_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (job_id, run_id),
            )

    def resumable_bulk_runs(self) -> list[int]:
        with self.db.connect() as connection:
            return [
                row["id"] for row in connection.execute(
                    "SELECT id FROM artwork_bulk_runs WHERE status IN ('queued','running','paused') ORDER BY id"
                )
            ]

    def bulk_status(self) -> dict[str, object]:
        with self.db.connect() as connection:
            coverage = dict(connection.execute(
                "SELECT COUNT(*) AS games,"
                "SUM(CASE WHEN EXISTS(SELECT 1 FROM game_assets ga WHERE ga.game_id=g.id AND ga.kind='cover') THEN 1 ELSE 0 END) AS covers,"
                "SUM(CASE WHEN EXISTS(SELECT 1 FROM game_assets ga WHERE ga.game_id=g.id AND ga.kind='cover') "
                "AND EXISTS(SELECT 1 FROM game_assets ga WHERE ga.game_id=g.id AND ga.kind='screenshot') "
                "AND EXISTS(SELECT 1 FROM game_assets ga WHERE ga.game_id=g.id AND ga.kind='logo') THEN 1 ELSE 0 END) AS full "
                "FROM games g"
            ).fetchone())
            latest = connection.execute(
                "SELECT * FROM artwork_bulk_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        games = int(coverage.get("games") or 0)
        covers = int(coverage.get("covers") or 0)
        full = int(coverage.get("full") or 0)
        return {
            "configured": self.configured,
            "games": games,
            "covers": covers,
            "full": full,
            "missing_covers": max(0, games - covers),
            "missing_full": max(0, games - full),
            "run": dict(latest) if latest else None,
            "quota": self.status()["quota"],
        }

    @staticmethod
    def _wait_cancellable(seconds: float, cancel_check, progress_callback, progress: int, detail: str) -> None:
        deadline = time.monotonic() + max(seconds, 0)
        progress_callback(progress, detail, "paused")
        while time.monotonic() < deadline:
            cancel_check()
            time.sleep(min(5.0, max(deadline - time.monotonic(), 0.05)))

    def scrape_bulk(self, run_id: int, *, progress_callback, cancel_check) -> dict[str, object]:
        while not self._scrape_lock.acquire(timeout=0.25):
            cancel_check()
            progress_callback(0, "Waiting for the active ScreenScraper job")
        try:
            with self.db.write() as connection:
                run = connection.execute("SELECT * FROM artwork_bulk_runs WHERE id=?", (run_id,)).fetchone()
                if not run:
                    raise LibraryError("Bulk artwork run was not found")
                connection.execute(
                    "UPDATE artwork_bulk_runs SET status='running',last_error='',updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (run_id,),
                )
            asset_kinds = ("cover",) if run["asset_mode"] == "cover" else tuple(ASSET_CHOICES)
            while True:
                cancel_check()
                with self.db.connect() as connection:
                    run = connection.execute("SELECT * FROM artwork_bulk_runs WHERE id=?", (run_id,)).fetchone()
                    item = connection.execute(
                        "SELECT game_id FROM artwork_bulk_items WHERE run_id=? AND status='pending' "
                        "ORDER BY game_id LIMIT 1",
                        (run_id,),
                    ).fetchone()
                if not item:
                    break
                total = max(int(run["total_games"]), 1)
                processed = int(run["processed_games"])
                progress = int(processed * 100 / total)
                progress_callback(progress, f"Preparing artwork {processed + 1} of {run['total_games']}", "running")
                try:
                    result = self._scrape_locked(
                        [item["game_id"]],
                        True,
                        True,
                        asset_kinds,
                        progress_callback=lambda local_progress, detail: progress_callback(
                            int((processed + local_progress / 100) * 100 / total), detail, "running"
                        ),
                        cancel_check=cancel_check,
                        record_activity=False,
                    )
                except ScreenScraperRateLimit as exc:
                    detail = f"ScreenScraper asked ROMmates to slow down; retrying in {int(exc.retry_after)} seconds"
                    with self.db.write() as connection:
                        connection.execute(
                            "UPDATE artwork_bulk_runs SET status='paused',last_error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                            (detail, run_id),
                        )
                    self._wait_cancellable(exc.retry_after, cancel_check, progress_callback, progress, detail)
                    continue
                except ScreenScraperDailyQuota:
                    wait_for = self._seconds_until_quota_reset()
                    reset = datetime.now(SCREENSCRAPER_TIMEZONE) + timedelta(seconds=wait_for)
                    detail = f"Daily ScreenScraper quota reached; resumes after {reset.strftime('%b %d at %H:%M %Z')}"
                    with self.db.write() as connection:
                        connection.execute(
                            "UPDATE artwork_bulk_runs SET status='paused',last_error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                            (detail, run_id),
                        )
                    self._wait_cancellable(wait_for, cancel_check, progress_callback, progress, detail)
                    continue
                issue = "; ".join(result.get("issues", []))
                item_status = "complete" if result.get("matched") else "skipped"
                with self.db.write() as connection:
                    connection.execute(
                        "UPDATE artwork_bulk_items SET status=?,issue=? WHERE run_id=? AND game_id=?",
                        (item_status, issue, run_id, item["game_id"]),
                    )
                    connection.execute(
                        "UPDATE artwork_bulk_runs SET status='running',processed_games=processed_games+1,"
                        "matched_games=matched_games+?,downloaded_assets=downloaded_assets+?,"
                        "skipped_games=skipped_games+?,last_error='',updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (
                            int(result.get("matched") or 0),
                            int(result.get("downloaded") or 0),
                            int(result.get("skipped") or 0),
                            run_id,
                        ),
                    )
            with self.db.write() as connection:
                connection.execute(
                    "UPDATE artwork_bulk_runs SET status='complete',processed_games=total_games,"
                    "last_error='',updated_at=CURRENT_TIMESTAMP,completed_at=CURRENT_TIMESTAMP WHERE id=?",
                    (run_id,),
                )
                completed = dict(connection.execute(
                    "SELECT * FROM artwork_bulk_runs WHERE id=?", (run_id,)
                ).fetchone())
                issues = [
                    row["issue"] for row in connection.execute(
                        "SELECT issue FROM artwork_bulk_items WHERE run_id=? AND issue<>'' ORDER BY game_id",
                        (run_id,),
                    )
                ]
            self.db.activity(
                "artwork",
                f"Bulk artwork run matched {completed['matched_games']} games and downloaded "
                f"{completed['downloaded_assets']} assets",
            )
            return {
                "run_id": run_id,
                "requested": completed["total_games"],
                "matched": completed["matched_games"],
                "downloaded": completed["downloaded_assets"],
                "skipped": completed["skipped_games"],
                "asset_mode": completed["asset_mode"],
                "issues": issues,
            }
        except JobCancelled:
            with self.db.write() as connection:
                connection.execute(
                    "UPDATE artwork_bulk_runs SET status='cancelled',updated_at=CURRENT_TIMESTAMP,"
                    "completed_at=CURRENT_TIMESTAMP WHERE id=?",
                    (run_id,),
                )
            raise
        except Exception as exc:
            with self.db.write() as connection:
                connection.execute(
                    "UPDATE artwork_bulk_runs SET status='failed',last_error=?,updated_at=CURRENT_TIMESTAMP,"
                    "completed_at=CURRENT_TIMESTAMP WHERE id=?",
                    (str(exc), run_id),
                )
            raise
        finally:
            self._scrape_lock.release()

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
