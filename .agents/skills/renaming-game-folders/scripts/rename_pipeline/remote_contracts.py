"""远程紧凑决策编译：按候选 key 展开身份、固定优先级字段与年龄汇总。"""

from __future__ import annotations

import copy
import re
import unicodedata
from typing import Any, Callable

SOURCE_PRIORITY = {"VNDB": 0, "DLsite": 1, "Steam": 2}
_CANDIDATE_COUNT_CLAIM = re.compile(
    r"(?:\d+|[零一二两三四五六七八九十]+)\s*(?:个|项|条)\s*候选"
)


def _validate_reason_against_current_sources(
    reason: Any, present_sources: set[str], *, label: str,
) -> None:
    """只核对可确定的事实；候选取舍仍由 Skill 判断。"""
    value = str(reason or "")
    if _CANDIDATE_COUNT_CLAIM.search(value):
        raise ValueError(f"{label} 不得在理由中陈述候选数量；使用 candidate_decisions")
    for source in SOURCE_PRIORITY:
        if source not in present_sources and re.search(
            rf"(?<![A-Za-z]){source}(?![A-Za-z])", value, re.IGNORECASE,
        ):
            raise ValueError(f"{label} 提及本轮未返回候选的来源：{source}")


def _candidate_universe(
    search_game: dict[str, Any],
    candidate_key_factory: Callable[[Any, Any, Any], str],
) -> dict[str, dict[str, Any]]:
    universe: dict[str, dict[str, Any]] = {}
    for source in search_game.get("sources", []):
        source_name = str(source.get("source") or "")
        groups = [source.get("candidates", [])]
        groups.extend(
            round_item.get("candidates", [])
            for round_item in source.get("rounds", [])
        )
        for candidates in groups:
            for candidate in candidates:
                expected_key = candidate_key_factory(
                    candidate.get("source") or source_name,
                    candidate.get("source_id"), candidate.get("url"),
                )
                supplied_key = str(candidate.get("candidate_key") or "")
                if supplied_key and supplied_key != expected_key:
                    raise ValueError(
                        f"candidate_key 与候选身份不匹配：{supplied_key}"
                    )
                key = supplied_key or expected_key
                existing = universe.get(key)
                if existing is not None:
                    existing_identity = (
                        existing.get("source"), existing.get("source_id"),
                        existing.get("url"),
                    )
                    current_identity = (
                        candidate.get("source") or source_name,
                        candidate.get("source_id"), candidate.get("url"),
                    )
                    if existing_identity != current_identity:
                        raise ValueError(f"candidate_key 身份冲突：{key}")
                    continue
                normalized = copy.deepcopy(candidate)
                normalized["source"] = normalized.get("source") or source_name
                normalized["candidate_key"] = key
                universe[key] = normalized
    return universe


def _candidate_identity(candidate: dict[str, Any]) -> dict[str, Any]:
    identity = {
        "source": candidate["source"],
        "source_id": candidate["source_id"],
        "url": candidate["url"],
    }
    if candidate.get("release") is not None:
        identity["release"] = copy.deepcopy(candidate["release"])
    elif candidate.get("source") == "VNDB":
        release_pages = candidate.get("release_pages") or []
        if len(release_pages) == 1:
            page = release_pages[0]
            identity["release"] = {
                "source_id": page["source_id"], "url": page["url"],
            }
    return identity


def _choose_priority_candidate(
    candidates: list[dict[str, Any]], field: str, *, label: str,
) -> dict[str, Any] | None:
    available = [item for item in candidates if item.get(field)]
    if not available:
        return None
    best_source = min(
        {str(item["source"]) for item in available},
        key=lambda value: (SOURCE_PRIORITY.get(value, 99), value),
    )
    best = [item for item in available if item["source"] == best_source]
    values = {str(item[field]) for item in best}
    if len(values) != 1:
        raise ValueError(f"同一最高优先级来源存在多个 {label} 值")
    return min(best, key=lambda item: str(item["candidate_key"]))


def _choose_joint_developer_candidate(
    adopted: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """只在候选已共同采用后，优先非拉丁字形制作方。"""
    candidates = [item for item in adopted if item.get("developer")]
    for source in {str(item["source"]) for item in candidates}:
        values = {
            str(item["developer"]) for item in candidates
            if str(item["source"]) == source
        }
        if len(values) > 1:
            raise ValueError("同一最高优先级来源存在多个 developer 值")
    if len(candidates) > 1 and len({str(x["developer"]) for x in candidates}) > 1:
        non_english = [
            item for item in candidates
            if any(
                char.isalpha() and "LATIN" not in unicodedata.name(char, "")
                for char in str(item["developer"])
            )
        ]
        if non_english:
            candidates = non_english
    return _choose_priority_candidate(candidates, "developer", label="developer")


def expand_compact_remote_decision(
    local: dict[str, Any],
    search_game: dict[str, Any],
    decision: dict[str, Any],
    *,
    candidate_key_factory: Callable[[Any, Any, Any], str],
) -> dict[str, Any]:
    """把候选/语言判断编译为固定优先级的完整远程语义。"""
    universe = _candidate_universe(search_game, candidate_key_factory)
    present_sources = {str(item["source"]) for item in universe.values()}
    _validate_reason_against_current_sources(
        decision.get("reason"), present_sources, label="远程决策",
    )
    raw_candidate_decisions = decision.pop("candidate_decisions", None)
    if not isinstance(raw_candidate_decisions, list):
        raise ValueError("紧凑远程决策缺少 candidate_decisions")
    dispositions: dict[str, str] = {}
    nonadopted_candidate_reasons: list[dict[str, str]] = []
    for item in raw_candidate_decisions:
        if not isinstance(item, dict) or set(item) - {
            "candidate_key", "disposition", "reason",
        }:
            raise ValueError("candidate_decisions 项格式无效")
        key = str(item.get("candidate_key") or "")
        disposition = str(item.get("disposition") or "")
        if key not in universe:
            raise ValueError(f"候选 key 不存在于本轮搜索证据：{key}")
        if key in dispositions:
            raise ValueError(f"候选 key 重复判断：{key}")
        if disposition not in {"adopted", "reference", "rejected"}:
            raise ValueError(f"候选 disposition 无效：{key}")
        _validate_reason_against_current_sources(
            item.get("reason"), present_sources,
            label=f"候选判断 {key}",
        )
        dispositions[key] = disposition
        if disposition != "adopted":
            nonadopted_candidate_reasons.append({
                "candidate_key": key,
                "source": str(universe[key]["source"]),
                "disposition": disposition,
                "reason": str(item["reason"]).strip(),
            })
    missing = set(universe) - set(dispositions)
    if missing:
        raise ValueError("紧凑远程决策未覆盖候选：" + ", ".join(sorted(missing)))

    requested_preview = decision.pop("preview_candidate_keys", {})
    if not isinstance(requested_preview, dict):
        raise ValueError("preview_candidate_keys 必须为来源到候选 key 的映射")
    preview_keys: dict[str, str] = {}
    for source in sorted({str(item["source"]) for item in universe.values()}):
        source_keys = [
            key for key, candidate in universe.items()
            if str(candidate["source"]) == source
        ]
        chosen = str(requested_preview.get(source) or "")
        if not chosen and len(source_keys) > 1:
            raise ValueError(f"同站多候选缺少最佳展示判断：{source}")
        if not chosen:
            chosen = source_keys[0]
        if chosen not in source_keys:
            raise ValueError(f"最佳展示候选不属于本来源：{source} {chosen}")
        best_rank = min(
            {"adopted": 0, "reference": 1, "rejected": 2}[dispositions[key]]
            for key in source_keys
        )
        if {"adopted": 0, "reference": 1, "rejected": 2}[dispositions[chosen]] != best_rank:
            raise ValueError(f"最佳展示候选未采用更可信的候选状态：{source} {chosen}")
        preview_keys[source] = chosen
    if set(requested_preview) - set(preview_keys):
        raise ValueError("preview_candidate_keys 含无候选来源")
    decision["preview_candidate_keys"] = preview_keys
    decision["preview_candidate_dispositions"] = {
        source: dispositions[key] for source, key in preview_keys.items()
    }

    candidate_order = lambda item: (
        SOURCE_PRIORITY.get(str(item["source"]), 99),
        str(item["source"]), str(item["candidate_key"]),
    )
    adopted = sorted(
        [universe[key] for key, value in dispositions.items() if value == "adopted"],
        key=candidate_order,
    )
    explicit_audiences = {
        str(item.get("audience_classification"))
        for item in adopted
        if item.get("audience_evidence")
        and item.get("audience_classification") in {"adult", "all_ages"}
    }
    if explicit_audiences == {"adult", "all_ages"}:
        raise ValueError("非全年龄候选已采用时不得同时采用全年龄候选")
    references = sorted(
        [universe[key] for key, value in dispositions.items() if value == "reference"],
        key=candidate_order,
    )
    decision["selected_candidates"] = [
        _candidate_identity(item) for item in adopted
    ]
    decision["reference_candidates"] = [
        _candidate_identity(item) for item in references
    ]

    source_disposition: dict[str, str] = {}
    for source in sorted({str(item["source"]) for item in universe.values()}):
        states = {
            dispositions[key] for key, item in universe.items()
            if str(item["source"]) == source
        }
        if "adopted" in states:
            source_disposition[source] = "adopted"
        elif states == {"rejected"}:
            source_disposition[source] = "rejected"
    decision["source_disposition"] = source_disposition
    decision["nonadopted_candidate_reasons"] = nonadopted_candidate_reasons

    assignments = decision.pop("title_assignments", None)
    if not isinstance(assignments, list):
        raise ValueError("紧凑远程决策缺少 title_assignments")
    title_options: dict[str, list[dict[str, Any]]] = {
        "jp": [], "zh": [], "en": [],
    }
    assigned_fields: set[tuple[str, str]] = set()
    assigned_values: dict[str, str] = {}
    for assignment in assignments:
        if not isinstance(assignment, dict) or set(assignment) != {
            "candidate_key", "field", "language",
        }:
            raise ValueError("title_assignments 项格式无效")
        key = str(assignment["candidate_key"])
        field = str(assignment["field"])
        language = str(assignment["language"])
        if key not in universe or dispositions.get(key) != "adopted":
            raise ValueError(f"标题只能引用 adopted 候选：{key}")
        if field not in {"title", "jp_title", "zh_title", "en_title"}:
            raise ValueError(f"标题候选字段无效：{field}")
        if language not in title_options:
            raise ValueError(f"标题语言槽无效：{language}")
        candidate = universe[key]
        value = candidate.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"标题候选字段为空：{key} {field}")
        identity = (key, field)
        if identity in assigned_fields:
            raise ValueError(f"候选标题字段重复归槽：{key} {field}")
        assigned_fields.add(identity)
        previous_language = assigned_values.get(value)
        if previous_language is not None and previous_language != language:
            raise ValueError("同一远程标题不得重复归入不同语言槽")
        assigned_values[value] = language
        title_options[language].append({**candidate, "_assigned_value": value})

    returned_titles = {"jp": None, "zh": None, "en": None}
    field_sources: dict[str, str] = {}
    for language, options in title_options.items():
        if not options:
            continue
        best_source = min(
            {str(item["source"]) for item in options},
            key=lambda value: (SOURCE_PRIORITY.get(value, 99), value),
        )
        best = [item for item in options if item["source"] == best_source]
        values = {str(item["_assigned_value"]) for item in best}
        if len(values) != 1:
            raise ValueError(
                f"同一最高优先级来源的 {language} 标题无法唯一决定"
            )
        returned_titles[language] = next(iter(values))
        field_sources[language] = best_source
    decision["returned_titles"] = returned_titles

    if decision.pop("developer_aliases_confirmed", False):
        raise ValueError(
            "developer_aliases_confirmed 已废弃；同作候选按来源优先级合并"
        )
    developer_candidate = _choose_joint_developer_candidate(adopted)
    decision["developer"] = (
        developer_candidate.get("developer") if developer_candidate else None
    )
    if developer_candidate:
        field_sources["developer"] = str(developer_candidate["source"])

    engine_candidate = _choose_priority_candidate(adopted, "engine", label="engine")
    decision["engine"] = (
        {"value": engine_candidate["engine"], "source": engine_candidate["source"]}
        if engine_candidate else None
    )
    if engine_candidate:
        field_sources["engine"] = str(engine_candidate["source"])
    decision["field_sources"] = field_sources

    audience_candidates = [
        item for item in adopted
        if item.get("audience_classification") and item.get("audience_evidence")
    ]
    audience = (
        "adult" if any(
            item["audience_classification"] == "adult"
            for item in audience_candidates
        ) else "all_ages" if audience_candidates else None
    )
    decision["audience_classification"] = audience
    if audience:
        audience_source = _choose_priority_candidate(
            [
                item for item in audience_candidates
                if item["audience_classification"] == audience
            ],
            "audience_classification", label="audience_classification",
        )
        decision["audience_source"] = _candidate_identity(audience_source)
    return decision
