"""本地紧凑决策编译：只展开引用、固定形状和可唯一定位的文本事实。"""

from __future__ import annotations

import copy
from typing import Any

from .engine_labels import UNKNOWN_ENGINE_LABEL, normalize_engine_label
from .text_normalization import (
    sanitize_search_query,
    search_query_is_from_basename,
)


EMPTY_LANGUAGE_TITLES = {"jp": None, "zh": None, "en": None}


def validated_short_search_keywords(
    value: Any, primary: dict[str, Any], raw_name: str,
) -> dict[str, str | None]:
    """短词只能由 Skill 选定，且逐字来自对应原名标题关键词。"""
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - {"jp", "zh", "en"}:
        raise ValueError("short_search_keywords 只能包含 jp/zh/en")
    result: dict[str, str | None] = {}
    for language in ("jp", "zh", "en"):
        keyword = value.get(language)
        if keyword is None:
            result[language] = None
            continue
        if not isinstance(keyword, str) or not keyword.strip():
            raise ValueError(f"short_search_keywords.{language} 必须为非空文本或 null")
        cleaned = sanitize_search_query(keyword)
        full = str(primary.get(language) or "")
        if (
            cleaned != keyword
            or not full
            or len(keyword) >= len(full)
            or keyword.casefold() not in full.casefold()
            or not search_query_is_from_basename(keyword, raw_name)
        ):
            raise ValueError(
                f"short_search_keywords.{language} 必须是原名主查询的严格短片段"
            )
        result[language] = keyword
    return result


def validated_language_titles(value: Any, *, label: str) -> dict[str, Any]:
    """校验 AI 的三语共同标题判断，不在脚本中推断语言或正文。"""
    if not isinstance(value, dict) or set(value) != {"jp", "zh", "en"}:
        raise ValueError(f"{label} 必须完整包含 jp/zh/en")
    for language, title in value.items():
        if title is not None and (
            not isinstance(title, str) or not title.strip()
        ):
            raise ValueError(f"{label}.{language} 必须为非空字符串或 null")
    return copy.deepcopy(value)


def _candidate_local_fields(evidence: dict[str, Any]) -> dict[str, Any]:
    local_fields = evidence.get("local_fields") or {}
    return {
        "engine": normalize_engine_label(evidence.get("engine")),
        "version": local_fields.get("version"),
        "dlc": bool(local_fields.get("dlc")),
        "dlc_version": local_fields.get("dlc_version"),
        "dlc_range": local_fields.get("dlc_range"),
        "version_tags": list(local_fields.get("version_tags") or []),
        "positive_tag": local_fields.get("positive_tag"),
        "negative_tags": list(local_fields.get("negative_tags") or []),
    }


def derive_compact_title_segments(
    evidence: dict[str, Any], titles: dict[str, Any],
) -> dict[str, Any]:
    """只在 raw basename 中逐字且唯一时派生标题片段；歧义仍交回 AI。"""
    result = copy.deepcopy(titles)
    if "local_segments" in result:
        raise ValueError(
            "decision_schema_version=3 的 local_segments 由编译器生成"
        )
    raw_name = str(evidence.get("raw_name") or "")
    explicit = result.get("local_segments_by_language")
    if explicit is not None:
        if not isinstance(explicit, dict) or set(explicit) != {"jp", "zh", "en"}:
            raise ValueError("local_segments_by_language 必须完整包含 jp/zh/en")
        segments: list[str] = []
        for language in ("jp", "zh", "en"):
            values = explicit[language]
            if not isinstance(values, list) or any(
                not isinstance(value, str) or not value or value not in raw_name
                for value in values
            ):
                raise ValueError(
                    f"local_segments_by_language.{language} 必须逐字来自 raw_name"
                )
            segments.extend(values)
        result["local_segments"] = segments
        return result

    occupied: list[tuple[int, int]] = []
    derived = {"jp": [], "zh": [], "en": []}
    for language in ("jp", "zh", "en"):
        title = result.get(language)
        if not title:
            continue
        if not isinstance(title, str) or raw_name.count(title) != 1:
            raise ValueError(
                f"titles.{language} 无法从 raw_name 唯一派生片段，"
                "请显式填写 local_segments_by_language"
            )
        start = raw_name.index(title)
        end = start + len(title)
        if any(start < right and end > left for left, right in occupied):
            raise ValueError(
                "多个语言标题在 raw_name 中重叠，"
                "请显式填写 local_segments_by_language"
            )
        occupied.append((start, end))
        derived[language].append(title)
    result["local_segments_by_language"] = derived
    result["local_segments"] = [
        segment
        for language in ("jp", "zh", "en")
        for segment in derived[language]
    ]
    return result


def expand_compact_local_decision(
    evidence: dict[str, Any],
    decision: dict[str, Any],
    *,
    decision_schema: int = 2,
    explicit_title_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """根据 AI 的采用开关复制候选，并派生规范查询词和固定结构。"""
    selectors = decision.pop("local_field_decisions", None)
    candidates = _candidate_local_fields(evidence)
    if not isinstance(selectors, dict) or set(selectors) != set(candidates):
        raise ValueError("local_field_decisions 必须完整覆盖本地候选字段")
    reviewed: dict[str, Any] = {}
    for field in (
        "engine", "version", "dlc", "dlc_version", "dlc_range",
        "positive_tag",
    ):
        selected = selectors.get(field)
        if not isinstance(selected, bool):
            raise ValueError(f"local_field_decisions.{field} 必须为布尔值")
        if field == "engine":
            reviewed[field] = candidates[field] if selected else UNKNOWN_ENGINE_LABEL
        elif field == "dlc":
            reviewed[field] = bool(candidates[field]) and selected
        else:
            reviewed[field] = candidates[field] if selected else None
    for field in ("version_tags", "negative_tags"):
        selected_values = selectors.get(field)
        if not isinstance(selected_values, list):
            raise ValueError(f"local_field_decisions.{field} 必须为数组")
        if len(selected_values) != len(set(selected_values)) or any(
            value not in candidates[field] for value in selected_values
        ):
            raise ValueError(
                f"local_field_decisions.{field} 必须是当前候选的无重复子集"
            )
        reviewed[field] = list(selected_values)
    if not reviewed["dlc"]:
        reviewed["dlc_version"] = None
        reviewed["dlc_range"] = None
    decision["reviewed_local_fields"] = reviewed

    titles = decision.get("titles")
    if not isinstance(titles, dict):
        raise ValueError("紧凑本地决策缺少 titles")
    if decision_schema >= 3:
        titles = derive_compact_title_segments(evidence, titles)
    explicit_title_evidence = explicit_title_evidence or {}
    for language, supplied in explicit_title_evidence.items():
        titles[language] = supplied["value"]
    titles["search_keywords"] = {}
    for language in ("jp", "zh", "en"):
        explicit = explicit_title_evidence.get(language) or {}
        title_query = sanitize_search_query(str(titles.get(language) or ""))
        titles["search_keywords"][language] = (
            explicit.get("search_keyword")
            if explicit else (
                title_query if search_query_is_from_basename(
                    title_query, str(evidence.get("raw_name") or "")
                ) else None
            )
        )
    titles["short_search_keywords"] = validated_short_search_keywords(
        titles.get("short_search_keywords"), titles["search_keywords"],
        str(evidence.get("raw_name") or ""),
    )
    decision["titles"] = titles
    if decision_schema >= 3:
        decision.setdefault("subtitle_boundary", None)
        decision.setdefault("auxiliary_directories", [])
    return decision


def expand_compact_series_decision(
    evidence: dict[str, Any], decision: dict[str, Any],
) -> dict[str, Any]:
    """把合集表面候选的采用选择编译为完整复核对象。"""
    if "series_common_titles" in decision:
        decision["series_common_titles"] = validated_language_titles(
            decision["series_common_titles"],
            label=f"series_common_titles：{decision.get('series_id')}",
        )
    selectors = decision.pop("surface_field_decisions", None)
    if selectors is None:
        return decision
    candidates = evidence.get("surface_local_fields") or {}
    if not isinstance(selectors, dict) or set(selectors) != {
        "positive_tag", "negative_tags",
    }:
        raise ValueError("surface_field_decisions 字段不完整")
    if not isinstance(selectors["positive_tag"], bool):
        raise ValueError("surface_field_decisions.positive_tag 必须为布尔值")
    negative_tags = selectors["negative_tags"]
    candidate_negative = list(candidates.get("negative_tags") or [])
    if (
        not isinstance(negative_tags, list)
        or len(negative_tags) != len(set(negative_tags))
        or any(value not in candidate_negative for value in negative_tags)
    ):
        raise ValueError("surface_field_decisions.negative_tags 必须是候选子集")
    decision["reviewed_surface_fields"] = {
        "positive_tag": (
            candidates.get("positive_tag")
            if selectors["positive_tag"] else None
        ),
        "negative_tags": list(negative_tags),
    }
    return decision
