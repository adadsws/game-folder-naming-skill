"""可审计的成功远程查询缓存；查询决定和来源预检仍由调用方负责。"""

from __future__ import annotations

import copy
import json
import os
import threading
import time
import unicodedata
from dataclasses import fields
from pathlib import Path
from typing import Any, Callable

from .contracts import canonical_sha256


CACHE_TTL_SECONDS = 365 * 24 * 60 * 60


def _previous_cache_version(source: str, identifiers: Any) -> int:
    """选择旧解析器最后一次使用的键版本，只读取尚在有效期的成功结果。"""
    if source == "VNDB" and isinstance(identifiers, dict) and identifiers.get("trusted_vndb_release"):
        return 2
    if source == "DLsite" and isinstance(identifiers, dict) and identifiers.get("dlsite"):
        return 3
    return 2 if source == "Steam" else 1


def _strict_title_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _all_cached_candidates_exact(titles: list[str], candidates: list[Any]) -> bool:
    """旧过滤策略对精确标题不依赖制作方，可安全复用其正面缓存。"""
    keys = {_strict_title_key(title) for title in titles if title}
    return bool(keys and candidates) and all(
        any(
            _strict_title_key(str(getattr(candidate, field, "") or "")) in keys
            for field in ("title", "jp_title", "zh_title", "en_title")
        ) for candidate in candidates
    )


class RemoteSearchCache:
    def __init__(self, path: Path, *,
                 clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self.clock = clock
        self.lock = threading.RLock()
        self.key_locks: dict[str, threading.Lock] = {}
        self.entries: dict[str, dict[str, Any]] = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if (isinstance(data, dict) and data.get("schema_version") == 1
                        and isinstance(data.get("entries"), dict)):
                    self.entries = data["entries"]
            except (OSError, ValueError, TypeError):
                pass  # 损坏缓存不可阻塞正式回源。

    def key(self, source: str, method: str, titles: list[str],
            identifiers: Any, developer: str | None,
            max_results: int | None) -> str:
        return canonical_sha256(self.request(
            source, method, titles, identifiers, developer, max_results,
        ))

    def request(self, source: str, method: str, titles: list[str],
                identifiers: Any, developer: str | None,
                max_results: int | None) -> dict[str, Any]:
        return {
            # 标题候选不再按本地制作方过滤；旧缓存保存的是已过滤结果。
            "cache_key_version": 4,
            "source": source, "method": method, "titles": titles,
            "identifiers": identifiers, "developer": developer,
            "max_results": max_results,
        }

    def get(self, key: str) -> tuple[list[dict[str, Any]], float] | None:
        with self.lock:
            item = self.entries.get(key)
            if not isinstance(item, dict):
                return None
            fetched_at = item.get("fetched_at")
            values = item.get("candidates")
            request = item.get("request")
            now = self.clock()
            if (not isinstance(request, dict)
                    or canonical_sha256(request) != key
                    or not isinstance(fetched_at, (int, float))
                    or fetched_at > now
                    or now - fetched_at >= CACHE_TTL_SECONDS
                    or not isinstance(values, list) or not values
                    or canonical_sha256(values) != item.get("sha256")):
                return None
            return copy.deepcopy(values), fetched_at

    def key_lock(self, key: str) -> threading.Lock:
        with self.lock:
            return self.key_locks.setdefault(key, threading.Lock())

    def put(self, key: str, request: dict[str, Any],
            candidates: list[dict[str, Any]]) -> float:
        fetched_at = self.clock()
        if candidates:
            with self.lock:
                self.entries[key] = {
                    "request": copy.deepcopy(request),
                    "fetched_at": fetched_at,
                    "candidates": copy.deepcopy(candidates),
                    "sha256": canonical_sha256(candidates),
                }
        return fetched_at

    def flush(self) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(self.path.name + ".tmp")
            temporary.write_text(json.dumps({
                "schema_version": 1, "entries": self.entries,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, self.path)


class CachedSource:
    def __init__(self, source: Any, cache: RemoteSearchCache,
                 serialize: Callable[[Any], dict[str, Any]], entry_type: type) -> None:
        self.source = source
        self.name = source.name
        self.cache = cache
        self.serialize = serialize
        self.entry_type = entry_type
        self.local = threading.local()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.source, name)

    def cache_provenance(self) -> dict[str, Any]:
        return dict(getattr(self.local, "provenance", {}))

    def _lookup(self, method: str, titles: list[str], identifiers: Any,
                developer: str | None) -> list[Any]:
        key = self.cache.key(self.name, method, titles, identifiers, developer,
                             getattr(self.source, "max_results", None))
        with self.cache.key_lock(key):
            return self._lookup_locked(key, method, titles, identifiers, developer)

    def _lookup_locked(self, key: str, method: str, titles: list[str],
                       identifiers: Any, developer: str | None) -> list[Any]:
        def restored_cache(cache_key: str) -> tuple[list[Any], float] | None:
            cached = self.cache.get(cache_key)
            if cached is None:
                return None
            values, fetched_at = cached
            try:
                names = {item.name for item in fields(self.entry_type)}
                sequence_fields = {
                    "languages", "remote_dlc", "remote_tags", "related_editions",
                    "audience_evidence", "release_year_evidence", "release_pages",
                }
                restored = [self.entry_type(**{
                    field: tuple(value) if field in sequence_fields else value
                    for field, value in item.items() if field in names
                }) for item in values]
            except (TypeError, ValueError, AttributeError):
                return None  # 旧结构或手工损坏的缓存回源重建。
            return restored, fetched_at

        old_request = self.cache.request(
            self.name, method, titles, identifiers, developer,
            getattr(self.source, "max_results", None),
        )
        old_request["cache_key_version"] = _previous_cache_version(
            self.name, identifiers,
        )
        old_key = canonical_sha256(old_request)
        old_cached = restored_cache(old_key)
        current_cached = restored_cache(key)
        old_values = old_cached[0] if old_cached else []
        if current_cached is not None:
            self.local.provenance = {
                "status": "hit", "fetched_at": current_cached[1], "cache_key": key,
            }
            return self._merge_candidates(current_cached[0], old_values)
        if old_cached and _all_cached_candidates_exact(titles, old_values):
            self.local.provenance = {
                "status": "legacy_hit", "fetched_at": old_cached[1],
                "cache_key": old_key,
            }
            return old_values
        self.local.provenance = {"status": "miss", "cache_key": key}
        results = getattr(self.source, method)(titles, identifiers, developer)
        if results:
            request = self.cache.request(
                self.name, method, titles, identifiers, developer,
                getattr(self.source, "max_results", None),
            )
            fetched_at = self.cache.put(
                key, request, [self.serialize(item) for item in results],
            )
            self.local.provenance["fetched_at"] = fetched_at
        return self._merge_candidates(results, old_values)

    @staticmethod
    def _merge_candidates(fresh: list[Any], old: list[Any]) -> list[Any]:
        combined: list[Any] = []
        seen: set[tuple[str, str]] = set()
        for item in [*fresh, *old]:
            identity = (
                str(getattr(item, "source_id", "") or ""),
                str(getattr(item, "url", "") or ""),
            )
            if identity in seen:
                continue
            seen.add(identity)
            combined.append(item)
        return combined

    def search(self, titles: list[str], identifiers: Any,
               developer: str | None) -> list[Any]:
        return self._lookup("search", titles, identifiers, developer)

    def search_discovered(self, titles: list[str], identifiers: Any,
                          developer: str | None) -> list[Any]:
        return self._lookup("search_discovered", titles, identifiers, developer)
