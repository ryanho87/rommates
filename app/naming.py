from __future__ import annotations

import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

from .db import Database
from .library import LibraryError, LibraryService, cleanup_name, normalize_name


MAX_DAT_BYTES = 64 * 1024 * 1024
MAX_DAT_ENTRIES = 500_000


class NamingService:
    def __init__(
        self, db: Database, library_root: Path, library: LibraryService | None = None,
        save_service=None,
    ):
        self.db = db
        self.library_root = library_root
        self.library = library
        self.save_service = save_service

    def import_dat(self, source_name: str, platform: str, content: str) -> dict[str, object]:
        source_name = Path(source_name.strip()).name
        platform = platform.strip()
        if not source_name or not platform or "/" in platform or "\\" in platform:
            raise LibraryError("Choose a platform and a valid DAT filename")
        encoded_size = len(content.encode("utf-8"))
        if encoded_size > MAX_DAT_BYTES:
            raise LibraryError("DAT file is larger than the 64 MB import limit")
        upper_prefix = content[:4096].upper()
        if "<!DOCTYPE" in upper_prefix or "<!ENTITY" in upper_prefix:
            raise LibraryError("DAT files containing DTD or entity declarations are not supported")
        try:
            root = ET.fromstring(content)
        except ET.ParseError as exc:
            raise LibraryError(f"DAT XML could not be parsed: {exc}") from exc

        entries: list[tuple[str, str, str, int | None, str]] = []
        for element in root.iter():
            if element.tag.rsplit("}", 1)[-1].lower() != "rom":
                continue
            raw_name = (element.get("name") or "").replace("\\", "/")
            canonical_name = Path(raw_name).name.strip()
            extension = Path(canonical_name).suffix.lower()
            if not canonical_name or not extension:
                continue
            try:
                size = int(element.get("size", ""))
            except ValueError:
                size = None
            sha256 = (element.get("sha256") or "").strip().lower()
            if len(sha256) != 64 or any(character not in "0123456789abcdef" for character in sha256):
                sha256 = ""
            entries.append((canonical_name, extension, normalize_name(canonical_name), size, sha256))
            if len(entries) > MAX_DAT_ENTRIES:
                raise LibraryError("DAT file contains more than 500,000 ROM entries")
        if not entries:
            raise LibraryError("No ROM entries were found in this DAT file")

        with self.db.write() as connection:
            connection.execute(
                "INSERT INTO naming_catalogs(name,platform,entry_count) VALUES(?,?,?)",
                (source_name, platform, len(entries)),
            )
            catalog_id = connection.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            connection.executemany(
                "INSERT INTO naming_entries(catalog_id,canonical_name,extension,normalized_name,size,sha256) "
                "VALUES(?,?,?,?,?,?)",
                ((catalog_id, *entry) for entry in entries),
            )
        return {"catalog_id": catalog_id, "name": source_name, "platform": platform, "entries": len(entries)}

    def catalogs(self) -> list[dict[str, object]]:
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM naming_catalogs ORDER BY platform COLLATE NOCASE,name COLLATE NOCASE"
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_catalog(self, catalog_id: int) -> None:
        with self.db.write() as connection:
            cursor = connection.execute("DELETE FROM naming_catalogs WHERE id=?", (catalog_id,))
            if not cursor.rowcount:
                raise LibraryError("Naming catalog was not found")

    def suggestions(
        self,
        search: str = "",
        platform: str = "",
        confidence: str = "all",
        save_impact: str = "all",
        limit: int = 200,
        offset: int = 0,
    ) -> dict[str, object]:
        with self.db.connect() as connection:
            entries = connection.execute(
                "SELECT e.*,c.name AS source,c.platform FROM naming_entries e "
                "JOIN naming_catalogs c ON c.id=e.catalog_id"
            ).fetchall()
            where = "WHERE g.platform=?" if platform else ""
            params = (platform,) if platform else ()
            games = connection.execute(
                "SELECT g.*,COUNT(gf.relpath) AS file_count,"
                "GROUP_CONCAT(CASE WHEN gf.kind='content' THEN gf.sha256 END) AS content_hashes "
                "FROM games g LEFT JOIN game_files gf ON gf.game_id=g.id "
                f"{where} GROUP BY g.id ORDER BY g.display_name COLLATE NOCASE",
                params,
            ).fetchall()

        exact_map: dict[tuple[str, str, str], list[object]] = defaultdict(list)
        name_map: dict[tuple[str, str, str], list[object]] = defaultdict(list)
        for entry in entries:
            key_base = (entry["platform"].casefold(), entry["extension"].lower())
            if entry["sha256"]:
                exact_map[(*key_base, entry["sha256"])].append(entry)
            if entry["normalized_name"]:
                name_map[(*key_base, entry["normalized_name"])].append(entry)

        results: list[dict[str, object]] = []
        for game in games:
            game_platform = game["platform"].casefold()
            extension = game["extension"].lower()
            matched = None
            matched_confidence = ""
            hashes = (game["content_hashes"] or "").split(",")
            exact_entries = {
                entry["canonical_name"]: entry
                for digest in hashes
                for entry in exact_map.get((game_platform, extension, digest), [])
            }
            if len(exact_entries) == 1:
                matched = next(iter(exact_entries.values()))
                matched_confidence = "exact"
            else:
                candidates = {
                    entry["canonical_name"]: entry
                    for entry in name_map.get(
                        (game_platform, extension, normalize_name(Path(game["primary_relpath"]).name)),
                        [],
                    )
                }
                if len(candidates) == 1:
                    matched = next(iter(candidates.values()))
                    matched_confidence = "strong"

            if matched is not None:
                suggested_name = Path(matched["canonical_name"]).stem
                source = matched["source"]
            else:
                suggested_name = cleanup_name(game["display_name"])
                source = "Filename cleanup"
                matched_confidence = "cleanup"
            if not suggested_name or suggested_name == game["display_name"]:
                continue
            if confidence != "all" and matched_confidence != confidence:
                continue
            if search.strip() and search.casefold() not in game["display_name"].casefold() and search.casefold() not in suggested_name.casefold():
                continue
            primary = self.library_root / game["primary_relpath"]
            target = primary.with_name(suggested_name + primary.suffix)
            results.append(
                {
                    "game_id": game["id"],
                    "platform": game["platform"],
                    "current_name": game["display_name"],
                    "suggested_name": suggested_name,
                    "primary_relpath": game["primary_relpath"],
                    "confidence": matched_confidence,
                    "source": source,
                    "collision": target.exists() and target != primary,
                    "_file_count": game["file_count"],
                }
            )
        impacts = self.save_service.save_impacts([item["game_id"] for item in results]) if self.save_service else {}
        for item in results:
            item["save_impact"] = impacts.get(
                item["game_id"],
                {"status": "none", "groups": 0, "files": 0, "save_files": 0, "state_files": 0, "paths": [], "content_names": []},
            )
        if save_impact == "has_saves":
            results = [item for item in results if item["save_impact"]["status"] != "none"]
        elif save_impact == "no_saves":
            results = [item for item in results if item["save_impact"]["status"] == "none"]
        elif save_impact == "review":
            results = [item for item in results if item["save_impact"]["status"] in {"possible", "ambiguous"}]
        rank = {"exact": 0, "strong": 1, "cleanup": 2}
        results.sort(key=lambda item: (rank[item["confidence"]], item["platform"].casefold(), item["current_name"].casefold()))
        page = results[offset : offset + limit]
        if self.library:
            for item in page:
                if item["collision"] or item["_file_count"] <= 1:
                    item.pop("_file_count", None)
                    continue
                try:
                    self.library.preview_rename(item["game_id"], item["suggested_name"])
                except LibraryError as exc:
                    item["collision"] = True
                    item["collision_detail"] = str(exc)
                item.pop("_file_count", None)
        else:
            for item in page:
                item.pop("_file_count", None)
        return {"items": page, "total": len(results), "limit": limit, "offset": offset}
