from __future__ import annotations

import json
import re
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from difflib import SequenceMatcher

from .config import Settings
from .db import Database
from .library import LibraryError


RAWG_API = "https://api.rawg.io/api"
RANKING_LIMIT = 100
PLATFORM_SLUGS = {
    "gb": "game-boy",
    "gbc": "game-boy-color",
    "gba": "game-boy-advance",
    "nds": "nintendo-ds",
    "n3ds": "nintendo-3ds",
    "nes": "nes",
    "snes": "snes",
    "n64": "nintendo-64",
    "gc": "gamecube",
    "gamecube": "gamecube",
    "wii": "wii",
    "wiiu": "wii-u",
    "switch": "nintendo-switch",
    "genesis": "genesis",
    "megadrive": "genesis",
    "dreamcast": "dreamcast",
    "saturn": "sega-saturn",
    "psx": "playstation1",
    "ps1": "playstation1",
    "ps2": "playstation2",
    "ps3": "playstation3",
    "psp": "psp",
    "xbox": "xbox-old",
    "xbox360": "xbox360",
}


def ranking_name(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    value = re.sub(r"\s*[\(\[].*?[\)\]]", " ", value.casefold())
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


class RankingService:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db

    @property
    def configured(self) -> bool:
        return bool(self.settings.rawg_api_key)

    def _request(self, endpoint: str, **params) -> dict:
        query = urllib.parse.urlencode({"key": self.settings.rawg_api_key, **params})
        request = urllib.request.Request(
            f"{RAWG_API}/{endpoint}?{query}", headers={"User-Agent": "ROMmates/0.1"}
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403}:
                raise LibraryError("RAWG rejected the configured API key") from exc
            if exc.code == 429:
                raise LibraryError("RAWG request quota was reached; try again later") from exc
            raise LibraryError(f"RAWG returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise LibraryError(f"RAWG could not be reached: {exc}") from exc
        if not isinstance(payload, dict):
            raise LibraryError("RAWG returned an invalid response")
        return payload

    def refresh(self, platform: str, *, progress_callback, cancel_check) -> dict[str, object]:
        if not self.configured:
            raise LibraryError("RAWG is not configured. Add ROMMATES_RAWG_API_KEY to Compose.")
        slug = PLATFORM_SLUGS.get(platform.casefold())
        if not slug:
            raise LibraryError(f"Platform '{platform}' is not mapped to RAWG")
        progress_callback(5, f"Finding {platform} in RAWG")
        cancel_check()
        platforms = self._request("platforms", page_size=100).get("results", [])
        match = next(
            (item for item in platforms if isinstance(item, dict) and item.get("slug") == slug), None
        )
        if not match:
            raise LibraryError(f"RAWG did not return its '{slug}' platform")
        progress_callback(35, f"Fetching the top {RANKING_LIMIT} {platform} games")
        cancel_check()
        payload = self._request(
            "games",
            platforms=match["id"],
            ordering="-metacritic",
            metacritic="1,100",
            page_size=RANKING_LIMIT,
        )
        results = [
            item for item in payload.get("results", []) if isinstance(item, dict)
        ][:RANKING_LIMIT]
        rows = []
        for rank, item in enumerate(results, 1):
            rows.append(
                (
                    platform,
                    rank,
                    str(item.get("id") or ""),
                    str(item.get("slug") or ""),
                    str(item.get("name") or "Untitled game"),
                    item.get("metacritic"),
                    item.get("rating"),
                    int(item.get("ratings_count") or 0),
                    str(item.get("released") or ""),
                )
            )
        cancel_check()
        with self.db.write() as connection:
            connection.execute("DELETE FROM platform_rankings WHERE platform=?", (platform,))
            connection.executemany(
                "INSERT INTO platform_rankings(platform,rank,source,source_game_id,slug,title,score,rating,ratings_count,released) "
                "VALUES(?,?,'rawg',?,?,?,?,?,?,?)",
                rows,
            )
        self.db.activity("ranking", f"Cached {len(rows)} RAWG rankings for {platform}")
        return {"platform": platform, "games": len(rows)}

    def coverage(self, platform: str) -> dict[str, object]:
        with self.db.connect() as connection:
            ranked = [dict(row) for row in connection.execute(
                "SELECT * FROM platform_rankings WHERE platform=? ORDER BY rank", (platform,)
            )]
            games = [dict(row) for row in connection.execute(
                "SELECT id,display_name,primary_relpath FROM games WHERE platform=?", (platform,)
            )]
        normalized = [(game, ranking_name(game["display_name"])) for game in games]
        used: set[int] = set()
        counts = {"owned": 0, "possible": 0, "missing": 0}
        for item in ranked:
            target = ranking_name(item["title"])
            exact = next((game for game, name in normalized if name == target), None)
            possible = None
            if not exact and target:
                candidates = sorted(
                    (
                        (SequenceMatcher(None, target, name).ratio(), game)
                        for game, name in normalized
                        if game["id"] not in used
                    ),
                    key=lambda pair: pair[0],
                    reverse=True,
                )
                if candidates and candidates[0][0] >= 0.86:
                    possible = candidates[0][1]
            matched = exact or possible
            status = "owned" if exact else "possible" if possible else "missing"
            counts[status] += 1
            if matched:
                used.add(matched["id"])
            item["status"] = status
            item["match"] = matched
            item["url"] = f"https://rawg.io/games/{item['slug']}"
        return {
            "platform": platform,
            "configured": self.configured,
            "items": ranked,
            "counts": counts,
        }
