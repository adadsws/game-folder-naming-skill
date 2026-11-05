"""审核短索引与远程查询缓存的行为案例。"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path


SCRIPT_DIRECTORY = (Path(__file__).resolve().parents[1] / ".agents" / "skills"
                    / "renaming-game-folders" / "scripts")
sys.path.insert(0, str(SCRIPT_DIRECTORY))

from rename_pipeline.remote_cache import (
    CACHE_TTL_SECONDS, CachedSource, RemoteSearchCache,
)
from rename_pipeline.review_packets import local_review_packet, remote_review_packet
from rename_pipeline.evidence import _search_one_source
from rename_pipeline.remote_contracts import expand_compact_remote_decision


@dataclass
class ExampleEntry:
    title: str
    source_id: str


class ExampleSource:
    name = "VNDB"
    max_results = 12

    def __init__(self) -> None:
        self.calls = 0
        self.result = [ExampleEntry("标题", "v1")]

    def search(self, titles, identifiers, developer):
        self.calls += 1
        return self.result

    def search_discovered(self, titles, identifiers, developer):
        self.calls += 1
        return self.result


class RemoteCacheTests(unittest.TestCase):
    def test_exact_legacy_candidate_reused_after_filter_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = RemoteSearchCache(Path(tmp) / "cache.json")
            source = ExampleSource()
            request = cache.request("VNDB", "search", ["标题"], {}, "作者", 12)
            request["cache_key_version"] = 1
            key = cache.key("VNDB", "search", ["标题"], {}, "作者", 12)
            self.assertNotEqual(key, cache.key("VNDB", "search", ["别题"], {}, "作者", 12))
            from rename_pipeline.contracts import canonical_sha256
            cache.put(canonical_sha256(request), request, [vars(source.result[0])])
            wrapped = CachedSource(source, cache, vars, ExampleEntry)
            self.assertEqual(wrapped.search(["标题"], {}, "作者"), source.result)
            self.assertEqual(source.calls, 0)
            self.assertEqual(wrapped.cache_provenance()["status"], "legacy_hit")

    def test_partial_legacy_candidate_joins_fresh_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = RemoteSearchCache(Path(tmp) / "cache.json")
            source = ExampleSource()
            source.result = [ExampleEntry("游戏续作", "v2")]
            request = cache.request("VNDB", "search", ["游戏"], {}, "作者", 12)
            request["cache_key_version"] = 1
            from rename_pipeline.contracts import canonical_sha256
            cache.put(canonical_sha256(request), request, [
                vars(ExampleEntry("游戏本篇", "v1")),
            ])
            wrapped = CachedSource(source, cache, vars, ExampleEntry)
            expected = ["v2", "v1"]
            self.assertEqual(
                [item.source_id for item in wrapped.search(["游戏"], {}, "作者")],
                expected,
            )
            self.assertEqual(source.calls, 1)
            self.assertEqual(
                [item.source_id for item in wrapped.search(["游戏"], {}, "作者")],
                expected,
            )
            self.assertEqual(source.calls, 1)

    def test_persistent_hit_exact_key_until_one_year_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            now = [1000.0]
            path = Path(tmp) / "cache.json"
            source = ExampleSource()
            cache = RemoteSearchCache(path, clock=lambda: now[0])
            wrapped = CachedSource(source, cache, vars, ExampleEntry)
            ids = {"vndb": ["v1"]}
            self.assertEqual(wrapped.search(["标题"], ids, "作者")[0].title, "标题")
            self.assertEqual(wrapped.cache_provenance()["status"], "miss")
            cache.flush()
            reloaded = RemoteSearchCache(path, clock=lambda: now[0])
            wrapped = CachedSource(source, reloaded, vars, ExampleEntry)
            self.assertEqual(wrapped.search(["标题"], ids, "作者")[0].source_id, "v1")
            self.assertEqual(source.calls, 1)
            self.assertEqual(wrapped.cache_provenance()["fetched_at"], 1000.0)
            self.assertEqual(wrapped.cache_provenance()["status"], "hit")
            wrapped.search(["标题"], ids, "另一个作者")
            self.assertEqual(source.calls, 2)
            now[0] = 1000.0 + CACHE_TTL_SECONDS - 1
            wrapped.search(["标题"], ids, "作者")
            self.assertEqual(source.calls, 2)
            self.assertEqual(wrapped.cache_provenance()["status"], "hit")
            now[0] = 1000.0 + CACHE_TTL_SECONDS
            wrapped.search(["标题"], ids, "作者")
            self.assertEqual(source.calls, 3)
            self.assertEqual(wrapped.cache_provenance()["status"], "miss")

    def test_empty_error_and_corruption_fall_back_to_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            source = ExampleSource()
            source.result = []
            cache = RemoteSearchCache(path)
            wrapped = CachedSource(source, cache, vars, ExampleEntry)
            wrapped.search(["空"], {}, None)
            wrapped.search(["空"], {}, None)
            self.assertEqual(source.calls, 2)
            source.result = [ExampleEntry("标题", "v1")]
            wrapped.search(["标题"], {}, None)
            cache.flush()
            data = json.loads(path.read_text(encoding="utf-8"))
            next(iter(data["entries"].values()))["sha256"] = "bad"
            path.write_text(json.dumps(data), encoding="utf-8")
            reloaded = CachedSource(source, RemoteSearchCache(path), vars, ExampleEntry)
            reloaded.search(["标题"], {}, None)
            self.assertEqual(source.calls, 4)

    def test_discovery_method_has_separate_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = ExampleSource()
            wrapped = CachedSource(source, RemoteSearchCache(Path(tmp) / "cache.json"),
                                   vars, ExampleEntry)
            wrapped.search(["标题"], {}, None)
            wrapped.search_discovered(["标题"], {}, None)
            self.assertEqual(source.calls, 2)

    def test_round_evidence_records_hit_and_original_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            now = [1000.0]
            cache = RemoteSearchCache(Path(tmp) / "cache.json", clock=lambda: now[0])
            source = ExampleSource()
            wrapped = CachedSource(source, cache, vars, ExampleEntry)
            local = {
                "raw_name": "标题",
                "source_ids": {"vndb": [], "dlsite": [], "steam": []},
            }
            semantics = {"titles": {"search_keywords": {
                "jp": "标题", "zh": None, "en": None,
            }}, "developer_evidence": {"value": "作者"}}
            first = _search_one_source(local, semantics, wrapped, vars)
            second = _search_one_source(local, semantics, wrapped, vars)
            self.assertEqual(first["rounds"][0]["cache"]["status"], "miss")
            self.assertEqual(second["rounds"][0]["cache"]["status"], "hit")
            self.assertEqual(second["rounds"][0]["cache"]["fetched_at"], 1000.0)
            self.assertEqual(source.calls, 1)

    def test_parallel_identical_queries_only_call_source_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = ExampleSource()
            original_search = source.search

            def slow_search(titles, identifiers, developer):
                time.sleep(0.03)
                return original_search(titles, identifiers, developer)

            source.search = slow_search
            wrapped = CachedSource(source, RemoteSearchCache(Path(tmp) / "cache.json"),
                                   vars, ExampleEntry)
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(
                    lambda _: wrapped.search(["标题"], {}, None), range(2)
                ))
            self.assertEqual(source.calls, 1)
            self.assertEqual(len(results), 2)


class ReviewPacketTests(unittest.TestCase):
    def test_local_packet_truncates_with_explicit_full_evidence_pointer(self) -> None:
        analysis = {
            "input_fingerprint": "fingerprint",
            "games": [{
                "game_id": "g", "raw_name": "标题", "relative_path": "标题",
                "local_name_evidence": [str(n) for n in range(100)],
                "local_text_sources": [{"relative_path": "readme.txt",
                                        "text": "a" * 2000}],
                "local_text_evidence": ["a" * 2000],
            }],
        }
        packet = local_review_packet(analysis)
        game = packet["games"][0]
        self.assertEqual(game["omitted_name_count"], 40)
        self.assertEqual(game["local_text_sources"][0]["omitted_characters"], 400)
        self.assertEqual(packet["full_evidence_file"], "analysis-input.json")
        self.assertLess(len(json.dumps(packet)), len(json.dumps(analysis)))

    def test_remote_packet_deduplicates_candidates_but_keeps_round_links(self) -> None:
        candidate = {"candidate_key": "VNDB:v1", "title": "标题", "url": "https://example.test/v1"}
        search = {
            "search_fingerprint": "search", "analysis_fingerprint": "analysis",
            "local_semantics_fingerprint": "local",
            "games": [{"game_id": "g", "sources": [{
                "source": "VNDB", "candidates": [candidate],
                "rounds": [
                    {"number": 1, "candidates": [candidate]},
                    {"number": 2, "candidates": [candidate]},
                ],
            }]}],
        }
        report = remote_review_packet(search)["games"][0]["sources"][0]
        self.assertEqual(len(report["candidates"]), 1)
        self.assertEqual([item["candidate_keys"] for item in report["rounds"]],
                         [["VNDB:v1"], ["VNDB:v1"]])
        self.assertTrue(all("candidates" not in item for item in report["rounds"]))


class RemoteReasonAuditTests(unittest.TestCase):
    def test_rejects_stale_source_and_candidate_count_claims(self) -> None:
        candidate = {
            "source": "DLsite", "source_id": "RJ100",
            "url": "https://example.test/RJ100", "title": "作品",
            "jp_title": "作品", "developer": "Circle",
        }
        search_game = {"sources": [{
            "source": "DLsite", "candidates": [candidate], "rounds": [],
        }]}
        base_decision = {
            "candidate_decisions": [{
                "candidate_key": "key-1", "disposition": "adopted",
                "reason": "DLsite 详情与作品一致",
            }],
            "title_assignments": [{
                "candidate_key": "key-1", "field": "jp_title", "language": "jp",
            }],
            "reason": "DLsite 日文标题与本地一致",
        }
        factory = lambda source, source_id, url: "key-1"

        stale_source = json.loads(json.dumps(base_decision))
        stale_source["reason"] = "Steam 标题与本地一致"
        with self.assertRaisesRegex(ValueError, "本轮未返回候选的来源：Steam"):
            expand_compact_remote_decision(
                {}, search_game, stale_source,
                candidate_key_factory=factory,
            )

        stale_count = json.loads(json.dumps(base_decision))
        stale_count["reason"] = "三个候选均为同一作品"
        with self.assertRaisesRegex(ValueError, "不得在理由中陈述候选数量"):
            expand_compact_remote_decision(
                {}, search_game, stale_count,
                candidate_key_factory=factory,
            )

        accepted = expand_compact_remote_decision(
            {}, search_game, base_decision,
            candidate_key_factory=factory,
        )
        self.assertEqual(accepted["selected_candidates"][0]["source"], "DLsite")


if __name__ == "__main__":
    unittest.main()
