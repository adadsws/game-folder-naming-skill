"""给 Skill 的短索引；完整分析和搜索证据始终保留为事实来源。"""

from __future__ import annotations

from typing import Any

from .contracts import canonical_sha256


def local_review_packet(analysis: dict[str, Any]) -> dict[str, Any]:
    games = []
    for game in analysis.get("games", []):
        text_sources = []
        for source in game.get("local_text_sources", []):
            raw = str(source.get("text") or "")
            text_sources.append({
                "relative_path": source.get("relative_path"),
                "excerpt": raw[:1600],
                "omitted_characters": max(0, len(raw) - 1600),
            })
        names = game.get("local_name_evidence") or []
        games.append({
            "game_id": game.get("game_id"),
            "relative_path": game.get("relative_path"),
            "raw_name": game.get("raw_name"),
            "ancestor_names": game.get("ancestor_names"),
            "series_relative_path": game.get("series_relative_path"),
            "primary_exe": game.get("primary_exe"),
            "executable_candidate_ids": game.get("executable_candidate_ids"),
            "engine": game.get("engine"),
            "engine_evidence": game.get("engine_evidence"),
            "source_ids": game.get("source_ids"),
            "local_fields": game.get("local_fields"),
            "direct_children": game.get("direct_children"),
            "local_name_evidence": names[:60],
            "omitted_name_count": max(0, len(names) - 60),
            "local_text_sources": text_sources,
        })
    body = {
        "schema_version": 1,
        "analysis_fingerprint": analysis.get("input_fingerprint"),
        "full_evidence_file": "analysis-input.json",
        "games": games,
        "series": analysis.get("series", []),
        "hierarchy_unresolved_candidates": analysis.get(
            "hierarchy_unresolved_candidates", []),
    }
    return {**body, "packet_fingerprint": canonical_sha256(body)}


def remote_review_packet(search: dict[str, Any]) -> dict[str, Any]:
    games = []
    for game in search.get("games", []):
        sources = []
        for source in game.get("sources", []):
            candidates: dict[str, dict[str, Any]] = {}
            for candidate in source.get("candidates", []):
                key = candidate.get("candidate_key")
                if key:
                    candidates[key] = candidate
            rounds = []
            for round_item in source.get("rounds", []):
                keys = []
                for candidate in round_item.get("candidates", []):
                    key = candidate.get("candidate_key")
                    if key:
                        candidates.setdefault(key, candidate)
                        keys.append(key)
                rounds.append({
                    key: value for key, value in round_item.items()
                    if key != "candidates"
                } | {"candidate_keys": keys})
            sources.append({
                "source": source.get("source"),
                "rounds": rounds,
                "candidates": list(candidates.values()),
                "fallback_attempts": [
                    {key: value for key, value in attempt.items()
                     if key != "candidates"}
                    for attempt in source.get("fallback_attempts", [])
                ],
            })
        games.append({
            "game_id": game.get("game_id"),
            "review_state": game.get("review_state"),
            "sources": sources,
        })
    body = {
        "schema_version": 1,
        "analysis_fingerprint": search.get("analysis_fingerprint"),
        "local_semantics_fingerprint": search.get("local_semantics_fingerprint"),
        "search_fingerprint": search.get("search_fingerprint"),
        "full_evidence_file": "search-evidence.json",
        "games": games,
    }
    return {**body, "packet_fingerprint": canonical_sha256(body)}
