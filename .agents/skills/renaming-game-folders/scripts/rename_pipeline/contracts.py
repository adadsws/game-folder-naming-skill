"""四阶段 JSON 契约共用的稳定标识与摘要函数。"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
from typing import Any
import unicodedata

from .engine_labels import UNKNOWN_ENGINE_LABEL, normalize_engine_label
from .local_contracts import (
    EMPTY_LANGUAGE_TITLES,
    expand_compact_local_decision,
    expand_compact_series_decision,
    validated_language_titles,
    validated_short_search_keywords,
)
from .remote_contracts import (
    SOURCE_PRIORITY,
    _choose_joint_developer_candidate,
    expand_compact_remote_decision,
)
from .text_normalization import (
    sanitize_search_query,
    search_query_is_from_basename,
)


ANALYSIS_SCHEMA_VERSION = 1


def source_shortcut_metadata_tokens(entry: dict[str, Any]) -> tuple[str, str]:
    """返回快捷方式与搜索轮共用的来源发行年份、年龄标记。"""
    raw_year = entry.get("release_year")
    year = str(raw_year) if raw_year is not None else ""
    audience = entry.get("audience_classification")
    age = (
        "18+" if audience == "adult" else
        "全年龄" if audience == "all_ages" else
        ""
    )
    return year, age


def _normalized_title_fragment(value: str) -> str:
    """仅用于验证 Skill 的关键词仍属于同一叶子标题，不产生新关键词。"""
    searchable = sanitize_search_query(value)
    return "".join(unicodedata.normalize("NFKC", searchable).casefold().split())


def _root_translation_credit_is_literal(raw_name: str, value: str) -> bool:
    """只接受游戏根 basename 中完整、独立的翻译署名字面片段。"""
    return bool(
        value
        and re.search(r"(?:汉化|漢化|翻译|翻譯)(?:组|組)?$", value)
        and re.search(r"(?<!\w)" + re.escape(value) + r"(?!\w)", raw_name)
    )


def _claim_ordered_title_spans(
    raw_name: str,
    title: str,
    occupied: list[tuple[int, int]],
) -> list[tuple[int, int]] | None:
    """定位 AI 标题的本地字面片段；只校验证据，不判断语言。"""
    chunks = [value for value in re.split(r"\s+", title.strip()) if value]
    if not chunks:
        return None
    claimed: list[tuple[int, int]] = []
    cursor = 0
    for chunk in chunks:
        start = raw_name.find(chunk, cursor)
        while start >= 0:
            end = start + len(chunk)
            if not any(
                start < occupied_end and end > occupied_start
                for occupied_start, occupied_end in (*occupied, *claimed)
            ):
                claimed.append((start, end))
                cursor = end
                break
            start = raw_name.find(chunk, start + 1)
        else:
            return None
    return claimed


def _matches_candidate_output(raw_value: Any, output_value: Any) -> bool:
    """远程输出必须逐字来自候选，禁止繁简转换或其他改写。"""
    return raw_value == output_value


def _highest_priority_source(
    candidates: list[dict[str, Any]],
    predicate: Any,
) -> str | None:
    sources = {
        str(candidate.get("source") or "")
        for candidate in candidates if predicate(candidate)
    }
    return min(
        sources,
        key=lambda source: (SOURCE_PRIORITY.get(source, 99), source),
    ) if sources else None


def canonical_sha256(value: Any) -> str:
    """对 JSON 可序列化值计算与键顺序无关的 SHA-256。"""
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def stable_candidate_key(source: Any, source_id: Any, url: Any) -> str:
    """由本轮候选规范身份生成供 AI 决策引用的稳定键。"""
    identity = {
        "source": str(source or ""),
        "source_id": str(source_id or ""),
        "url": str(url or ""),
    }
    if not all(identity.values()):
        raise ValueError("候选缺少 source/source_id/url，无法生成 candidate_key")
    return "candidate-" + canonical_sha256(identity)[:20]


def _verify_embedded_fingerprint(
    document: dict[str, Any],
    field: str,
    label: str,
) -> None:
    body = {key: value for key, value in document.items() if key != field}
    if document.get(field) != canonical_sha256(body):
        raise ValueError(f"{label}自身摘要不匹配")


def stable_game_id(root: Path, game_root: Path) -> str:
    """由扫描根内相对路径生成稳定且不泄漏绝对路径的游戏 ID。"""
    relative = game_root.resolve().relative_to(root.resolve()).as_posix()
    digest = hashlib.sha256(relative.casefold().encode("utf-8")).hexdigest()
    return f"game-{digest[:16]}"


# 用户人工补充只覆盖显式字段，并始终绑定当前 analysis 指纹。
def validate_user_supplements(
    analysis: dict[str, Any],
    document: dict[str, Any],
) -> dict[str, Any]:
    """校验并规范化当前批次的用户人工补充输入。"""
    if document.get("schema_version") != 1:
        raise ValueError("user supplements schema_version 必须为 1")
    if document.get("mode") != "manual_supplement":
        raise ValueError("user supplements mode 必须为 manual_supplement")
    if document.get("analysis_fingerprint") != analysis.get("input_fingerprint"):
        raise ValueError("user supplements 与 analysis 指纹不一致")

    raw_name_by_game_id = {
        str(item.get("game_id") or ""): str(item.get("raw_name") or "")
        for item in analysis.get("games", [])
    }
    valid_game_ids = set(raw_name_by_game_id)
    normalized_games: list[dict[str, Any]] = []
    seen_games: set[str] = set()
    raw_games = document.get("games")
    if not isinstance(raw_games, list):
        raise ValueError("user supplements games 必须为数组")
    for raw in raw_games:
        if not isinstance(raw, dict):
            raise ValueError("user supplement game 必须为对象")
        unknown = set(raw) - {
            "game_id", "developer_evidence", "title_evidence",
            "title_exclusions", "translation_credit", "english_fallback",
            "english_path_override",
        }
        if unknown:
            raise ValueError(
                "user supplement game 包含未知字段："
                + "、".join(sorted(unknown))
            )
        game_id = str(raw.get("game_id") or "")
        if game_id not in valid_game_ids:
            raise ValueError(f"user supplement game_id 不存在：{game_id}")
        if game_id in seen_games:
            raise ValueError(f"user supplement game_id 重复：{game_id}")
        seen_games.add(game_id)
        normalized: dict[str, Any] = {"game_id": game_id}

        developer = raw.get("developer_evidence")
        if developer is not None:
            if not isinstance(developer, dict) or set(developer) != {
                "value", "reason",
            }:
                raise ValueError(
                    f"developer_evidence 必须只含 value/reason：{game_id}"
                )
            value = str(developer.get("value") or "").strip()
            reason = str(developer.get("reason") or "").strip()
            if not value or not reason:
                raise ValueError(
                    f"developer_evidence value/reason 不能为空：{game_id}"
                )
            normalized["developer_evidence"] = {
                "value": value, "reason": reason,
            }

        title_evidence = raw.get("title_evidence")
        if title_evidence is not None:
            if (
                not isinstance(title_evidence, dict)
                or not title_evidence
                or set(title_evidence) - {"jp", "zh", "en"}
            ):
                raise ValueError(f"title_evidence 语言键无效：{game_id}")
            normalized_titles: dict[str, dict[str, Any]] = {}
            for language in ("jp", "zh", "en"):
                evidence = title_evidence.get(language)
                if evidence is None:
                    continue
                if not isinstance(evidence, dict) or set(evidence) not in (
                    {"value", "search_keyword", "reason"},
                    {"value", "search_keyword", "reason",
                     "allow_non_basename_search"},
                ):
                    raise ValueError(
                        f"title_evidence.{language} 字段无效：{game_id}"
                    )
                value = str(evidence.get("value") or "").strip()
                supplied_keyword = evidence.get("search_keyword")
                keyword = (
                    str(supplied_keyword).strip()
                    if supplied_keyword is not None else None
                )
                reason = str(evidence.get("reason") or "").strip()
                exceptional_search = evidence.get("allow_non_basename_search")
                if exceptional_search is not None and exceptional_search is not True:
                    raise ValueError(
                        f"title_evidence.{language} 搜索例外必须为 true：{game_id}"
                    )
                if not value or not reason or keyword == "":
                    raise ValueError(
                        f"title_evidence.{language} 值和理由不能为空；"
                        "无原名词时搜索关键词须为 null："
                        f"{game_id}"
                    )
                if keyword is None and search_query_is_from_basename(
                    sanitize_search_query(value), raw_name_by_game_id[game_id],
                ):
                    raise ValueError(
                        f"title_evidence.{language} 原名已有标题须给出关键词："
                        f"{game_id}"
                    )
                if keyword is not None and (
                    sanitize_search_query(keyword) != keyword
                    or (
                        not exceptional_search
                        and not search_query_is_from_basename(
                            keyword, raw_name_by_game_id[game_id]
                        )
                    )
                ):
                    raise ValueError(
                        f"title_evidence.{language} 搜索关键词必须来自原文件夹名称："
                        f"{game_id}"
                    )
                if keyword is not None and _normalized_title_fragment(keyword) not in (
                    _normalized_title_fragment(value)
                ):
                    raise ValueError(
                        f"title_evidence.{language} 搜索关键词必须来自标题："
                        f"{game_id}"
                    )
                if exceptional_search and (
                    keyword is None
                    or keyword != sanitize_search_query(value)
                ):
                    raise ValueError(
                        f"title_evidence.{language} 例外搜索词必须是完整标题："
                        f"{game_id}"
                    )
                normalized_titles[language] = {
                    "value": value,
                    "search_keyword": keyword,
                    "reason": reason,
                    **({"allow_non_basename_search": True}
                       if exceptional_search else {}),
                }
            normalized["title_evidence"] = normalized_titles

        title_exclusions = raw.get("title_exclusions")
        if title_exclusions is not None:
            if (
                not isinstance(title_exclusions, dict)
                or not title_exclusions
                or set(title_exclusions) - {"jp", "zh", "en"}
            ):
                raise ValueError(f"title_exclusions 语言键无效：{game_id}")
            normalized_exclusions: dict[str, str] = {}
            for language, reason in title_exclusions.items():
                value = str(reason or "").strip()
                if not value:
                    raise ValueError(
                        f"title_exclusions.{language} 必须说明排除理由：{game_id}"
                    )
                if language in (normalized.get("title_evidence") or {}):
                    raise ValueError(
                        f"同一语种不能同时补充和排除标题：{game_id} {language}"
                    )
                normalized_exclusions[language] = value
            normalized["title_exclusions"] = normalized_exclusions

        translation_credit = raw.get("translation_credit")
        if translation_credit is not None:
            if (
                not isinstance(translation_credit, dict)
                or set(translation_credit) != {"value", "type", "reason"}
            ):
                raise ValueError(
                    f"translation_credit 必须只含 value/type/reason：{game_id}"
                )
            credit_value = str(translation_credit.get("value") or "").strip()
            credit_type = translation_credit.get("type")
            credit_reason = str(translation_credit.get("reason") or "").strip()
            if (
                not credit_value
                or credit_type not in {"group", "person"}
                or not credit_reason
            ):
                raise ValueError(
                    f"translation_credit 值、类型和理由无效：{game_id}"
                )
            normalized["translation_credit"] = {
                "value": credit_value,
                "type": credit_type,
                "reason": credit_reason,
            }

        english_fallback = raw.get("english_fallback")
        if english_fallback is not None:
            if (
                not isinstance(english_fallback, dict)
                or set(english_fallback) != {"developer", "en_title", "reason"}
            ):
                raise ValueError(
                    f"english_fallback 必须只含 developer/en_title/reason：{game_id}"
                )
            fallback_reason = str(english_fallback.get("reason") or "").strip()
            normalized_fallback = {
                key: str(english_fallback.get(key) or "").strip()
                for key in ("developer", "en_title")
            }
            if (
                not fallback_reason
                or not any(normalized_fallback.values())
                or any(value and not value.isascii() for value in normalized_fallback.values())
            ):
                raise ValueError(
                    f"english_fallback 必须含理由及至少一个非空 ASCII 回退值：{game_id}"
                )
            normalized["english_fallback"] = {
                **normalized_fallback,
                "reason": fallback_reason,
            }

        path_override = raw.get("english_path_override")
        if path_override is not None:
            if (
                not isinstance(path_override, dict)
                or set(path_override) != {"allow_non_ascii", "reason"}
                or path_override.get("allow_non_ascii") is not True
            ):
                raise ValueError(
                    f"english_path_override 必须显式含 allow_non_ascii=true/reason：{game_id}"
                )
            override_reason = str(path_override.get("reason") or "").strip()
            if not override_reason:
                raise ValueError(
                    f"english_path_override 必须说明理由：{game_id}"
                )
            normalized["english_path_override"] = {
                "allow_non_ascii": True,
                "reason": override_reason,
            }
        if len(normalized) == 1:
            raise ValueError(f"user supplement game 没有补充字段：{game_id}")
        normalized_games.append(normalized)
    normalized_games.sort(key=lambda item: item["game_id"])

    raw_seeds = document.get("candidate_seeds")
    if not isinstance(raw_seeds, list):
        raise ValueError("user supplements candidate_seeds 必须为数组")
    normalized_seeds: list[dict[str, Any]] = []
    for raw in raw_seeds:
        if not isinstance(raw, dict):
            raise ValueError("user supplement candidate seed 必须为对象")
        unknown = set(raw) - {
            "game_id", "source", "source_id", "url", "provided_url",
            "release_title_evidence", "reason",
        }
        if unknown:
            raise ValueError(
                "user supplement candidate seed 包含未知字段："
                + "、".join(sorted(unknown))
            )
        game_id = str(raw.get("game_id") or "")
        if game_id not in valid_game_ids:
            raise ValueError(f"user supplement candidate game_id 不存在：{game_id}")
        item = {
            "game_id": game_id,
            "source": str(raw.get("source") or "").strip(),
            "source_id": str(raw.get("source_id") or "").strip(),
            "url": str(raw.get("url") or "").strip(),
            "reason": str(raw.get("reason") or "").strip(),
        }
        provided_url = str(raw.get("provided_url") or "").strip()
        if provided_url:
            item["provided_url"] = provided_url
        release_titles = raw.get("release_title_evidence")
        if release_titles is not None:
            if (
                item["source"] != "VNDB"
                or not provided_url
                or not re.fullmatch(r"https://vndb\.org/r\d+/?", provided_url)
                or not isinstance(release_titles, dict)
                or not release_titles
                or set(release_titles) - {"jp", "zh", "en"}
            ):
                raise ValueError(
                    "release_title_evidence 只可用于带 VNDB release 链接的种子"
                )
            normalized_titles: dict[str, dict[str, str]] = {}
            for language, evidence in release_titles.items():
                if not isinstance(evidence, dict) or set(evidence) != {
                    "value", "reason",
                }:
                    raise ValueError(
                        f"release_title_evidence.{language} 字段无效：{game_id}"
                    )
                value = str(evidence.get("value") or "").strip()
                title_reason = str(evidence.get("reason") or "").strip()
                if not value or not title_reason:
                    raise ValueError(
                        f"release_title_evidence.{language} 值和理由不能为空：{game_id}"
                    )
                normalized_titles[language] = {
                    "value": value, "reason": title_reason,
                }
            item["release_title_evidence"] = normalized_titles
        normalized_seeds.append(item)
    normalized_seeds.sort(
        key=lambda item: (item["game_id"], item["source"], item["source_id"])
    )

    unresolved = {
        str(item.get("candidate_id") or "")
        for item in analysis.get("hierarchy_unresolved_candidates") or []
    }
    raw_non_games = document.get("non_game_candidates")
    if not isinstance(raw_non_games, list):
        raise ValueError("user supplements non_game_candidates 必须为数组")
    normalized_non_games: list[dict[str, str]] = []
    seen_candidates: set[str] = set()
    for raw in raw_non_games:
        if not isinstance(raw, dict) or set(raw) != {"candidate_id", "reason"}:
            raise ValueError(
                "non_game_candidate 必须只含 candidate_id/reason"
            )
        candidate_id = str(raw.get("candidate_id") or "")
        reason = str(raw.get("reason") or "").strip()
        if candidate_id not in unresolved:
            raise ValueError(f"未决启动候选不存在：{candidate_id}")
        if candidate_id in seen_candidates:
            raise ValueError(f"非游戏候选重复：{candidate_id}")
        if not reason:
            raise ValueError(f"非游戏候选必须说明理由：{candidate_id}")
        seen_candidates.add(candidate_id)
        normalized_non_games.append({
            "candidate_id": candidate_id,
            "reason": reason,
        })
    normalized_non_games.sort(key=lambda item: item["candidate_id"])
    return {
        "schema_version": 1,
        "mode": "manual_supplement",
        "analysis_fingerprint": analysis["input_fingerprint"],
        "games": normalized_games,
        "candidate_seeds": normalized_seeds,
        "non_game_candidates": normalized_non_games,
    }


def _apply_user_supplements_to_decisions(
    decisions: dict[str, Any],
    supplements: dict[str, Any],
) -> dict[str, Any]:
    """把已校验的人工证据覆盖到 Skill 决策，不推导未给字段。"""
    updated = copy.deepcopy(decisions)
    by_id = {
        str(item.get("game_id") or ""): item
        for item in updated.get("games", [])
    }
    for supplement in supplements["games"]:
        game_id = supplement["game_id"]
        if game_id not in by_id:
            raise ValueError(f"本地决策缺少人工补充 game_id：{game_id}")
        item = by_id[game_id]
        developer = supplement.get("developer_evidence")
        if developer:
            item["developer_evidence"] = {
                "value": developer["value"],
                "evidence": ["用户人工补充：" + developer["reason"]],
            }
        title_evidence = supplement.get("title_evidence") or {}
        title_exclusions = supplement.get("title_exclusions") or {}
        if title_exclusions:
            titles = item.get("titles")
            if not isinstance(titles, dict):
                raise ValueError(f"本地决策 titles 缺失：{game_id}")
            keywords = titles.get("search_keywords")
            segments_by_language = titles.get("local_segments_by_language")
            if not isinstance(keywords, dict) or not isinstance(
                segments_by_language, dict
            ):
                raise ValueError(f"本地决策缺少语言分槽：{game_id}")
            for language, reason in title_exclusions.items():
                titles[language] = None
                keywords[language] = None
                segments_by_language[language] = []
                short_keywords = titles.get("short_search_keywords")
                if isinstance(short_keywords, dict):
                    short_keywords[language] = None
            titles["local_segments"] = [
                segment
                for language in ("jp", "zh", "en")
                for segment in segments_by_language.get(language, [])
            ]
            item["titles"] = titles
        if title_evidence:
            titles = item.get("titles")
            if not isinstance(titles, dict):
                raise ValueError(f"本地决策 titles 缺失：{game_id}")
            if "local_field_decisions" not in item:
                keywords = titles.get("search_keywords")
                if not isinstance(keywords, dict):
                    raise ValueError(f"本地决策 search_keywords 缺失：{game_id}")
                for language, evidence in title_evidence.items():
                    titles[language] = evidence["value"]
                    keywords[language] = evidence["search_keyword"]
            exceptions = {
                language: evidence["search_keyword"]
                for language, evidence in title_evidence.items()
                if evidence.get("allow_non_basename_search")
            }
            if exceptions:
                item["authorized_search_query_exceptions"] = exceptions
        translation_credit = supplement.get("translation_credit")
        if translation_credit:
            item["translation_credit"] = {
                "value": translation_credit["value"],
                "type": translation_credit["type"],
                "evidence": ["用户人工补充：" + translation_credit["reason"]],
            }
        english_fallback = supplement.get("english_fallback")
        if english_fallback:
            item["english_fallback"] = {
                key: english_fallback[key]
                for key in ("developer", "en_title")
                if english_fallback[key]
            }
        path_override = supplement.get("english_path_override")
        if path_override:
            item["english_path_override"] = {"allow_non_ascii": True}
        reasons = [
            value["reason"] for value in title_evidence.values()
        ]
        reasons.extend(
            f"用户人工排除{language}标题：{reason}"
            for language, reason in title_exclusions.items()
        )
        if developer:
            reasons.insert(0, developer["reason"])
        if translation_credit:
            reasons.append(translation_credit["reason"])
        if english_fallback:
            reasons.append(english_fallback["reason"])
        if path_override:
            reasons.append(path_override["reason"])
        item["reason"] = "；".join(
            [str(item.get("reason") or "").strip(), *reasons]
        ).strip("；")
    return updated


def compile_local_semantics(
    analysis: dict[str, Any],
    decisions: dict[str, Any],
    user_supplements: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把 Skill 紧凑判断与脚本证据字段编译为完整本地语义。"""
    normalized_supplements = None
    if user_supplements is not None:
        normalized_supplements = validate_user_supplements(
            analysis, user_supplements,
        )
        decisions = _apply_user_supplements_to_decisions(
            decisions, normalized_supplements,
        )
    if decisions.get("schema_version") != ANALYSIS_SCHEMA_VERSION:
        raise ValueError("本地决策 schema_version 不受支持")
    decision_schema = decisions.get("decision_schema_version", 1)
    if decision_schema not in {1, 2, 3}:
        raise ValueError("本地 decision_schema_version 不受支持")
    evidence_by_id = {
        str(item["game_id"]): item for item in analysis.get("games", [])
    }
    supplement_by_id = {
        str(item["game_id"]): item
        for item in (
            (normalized_supplements or {}).get("games", [])
        )
    }
    series_id_by_relative = {
        str(item.get("relative_path") or "").casefold(): str(item["series_id"])
        for item in analysis.get("series", [])
    }
    decision_series_by_id = {
        str(item.get("series_id") or ""): item
        for item in decisions.get("series", [])
        if isinstance(item, dict)
    }
    seen: set[str] = set()
    games: list[dict[str, Any]] = []
    for decision in decisions.get("games", []):
        game_id = str(decision.get("game_id") or "")
        if game_id in seen:
            raise ValueError(f"重复 game_id：{game_id}")
        if game_id not in evidence_by_id:
            raise ValueError(f"未知 game_id：{game_id}")
        seen.add(game_id)
        evidence = evidence_by_id[game_id]
        if decision.get("auxiliary_directories"):
            raise ValueError(f"新批次不规划 CG/存档附属目录改名：{game_id}")
        decision_credit = decision.get("translation_credit")
        if decision_credit and not (
            supplement_by_id.get(game_id) or {}
        ).get("translation_credit"):
            if (
                not isinstance(decision_credit, dict)
                or not _root_translation_credit_is_literal(
                    str(evidence.get("raw_name") or ""),
                    str(decision_credit.get("value") or ""),
                )
            ):
                raise ValueError(
                    f"汉化署名必须来自游戏根 basename 或用户人工补充：{game_id}"
                )
        item = copy.deepcopy(decision)
        if decision_schema in {2, 3}:
            item = expand_compact_local_decision(
                evidence,
                item,
                decision_schema=decision_schema,
                explicit_title_evidence=(
                    supplement_by_id.get(game_id, {}).get("title_evidence") or {}
                ),
            )
        if decision_schema >= 3:
            if "series_id" in decision or "series_common_titles" in decision:
                raise ValueError(
                    "series_id 与 series_common_titles 由编译器按系列决策生成"
                )
            series_id = series_id_by_relative.get(
                str(evidence.get("series_relative_path") or "").casefold()
            )
            item["series_id"] = series_id
            if series_id is None:
                item["series_common_titles"] = copy.deepcopy(
                    EMPTY_LANGUAGE_TITLES
                )
            else:
                series_decision = decision_series_by_id.get(series_id)
                if series_decision is None:
                    raise ValueError(f"紧凑本地决策缺少 series_id：{series_id}")
                item["series_common_titles"] = validated_language_titles(
                    series_decision.get("series_common_titles"),
                    label=f"series_common_titles：{series_id}",
                )
        item["source_path"] = evidence["source_path"]
        item["input_fingerprint"] = evidence["directory_fingerprint"]
        item["raw_name"] = evidence["raw_name"]
        games.append(item)
    game_order = {
        str(item["game_id"]): index
        for index, item in enumerate(analysis.get("games", []))
    }
    games.sort(key=lambda item: game_order[str(item["game_id"])])
    series_evidence_by_id = {
        str(item["series_id"]): item for item in analysis.get("series", [])
    }
    seen_series: set[str] = set()
    series: list[dict[str, Any]] = []
    for decision in decisions.get("series", []):
        series_id = str(decision.get("series_id") or "")
        if series_id in seen_series:
            raise ValueError(f"重复 series_id：{series_id}")
        if series_id not in series_evidence_by_id:
            raise ValueError(f"未知 series_id：{series_id}")
        seen_series.add(series_id)
        evidence = series_evidence_by_id[series_id]
        item = copy.deepcopy(decision)
        if decision_schema in {2, 3}:
            item = expand_compact_series_decision(evidence, item)
        item["source_path"] = evidence["source_path"]
        item["input_fingerprint"] = evidence["directory_fingerprint"]
        series.append(item)
    series_order = {
        str(item["series_id"]): index
        for index, item in enumerate(analysis.get("series", []))
    }
    series.sort(key=lambda item: series_order[str(item["series_id"])])
    compiled = {
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "analysis_fingerprint": analysis["input_fingerprint"],
        "games": games,
        "series": series,
    }
    if normalized_supplements is not None:
        compiled["user_supplements"] = normalized_supplements
        compiled["user_supplements_fingerprint"] = canonical_sha256(
            normalized_supplements
        )
        compiled["confirmed_non_game_candidates"] = list(
            normalized_supplements["non_game_candidates"]
        )
    validate_local_semantics(analysis, compiled)
    return compiled


def _validate_reviewed_candidate_fields(
    reviewed: Any,
    candidates: dict[str, Any],
    *,
    label: str,
) -> None:
    """AI 可否决脚本候选，但不得凭空增加本地字段。"""
    if (
        isinstance(reviewed, dict)
        and "version_tags" not in reviewed
        and not candidates.get("version_tags")
    ):
        reviewed = {**reviewed, "version_tags": []}
    if not isinstance(reviewed, dict) or set(reviewed) != set(candidates):
        raise ValueError(f"{label} 必须完整记录 AI 复核的本地字段")
    expected_engine = normalize_engine_label(candidates.get("engine"))
    reviewed_engine = normalize_engine_label(reviewed.get("engine"))
    if reviewed_engine not in {expected_engine, UNKNOWN_ENGINE_LABEL}:
        raise ValueError(f"{label}.engine 必须来自当前证据候选或降级为未知")
    for field in ("version", "dlc_version", "dlc_range", "positive_tag"):
        candidate = candidates.get(field)
        if reviewed.get(field) not in ({candidate, None} if candidate is not None else {None}):
            raise ValueError(f"{label}.{field} 不是当前证据候选")
    version_tags = reviewed.get("version_tags")
    candidate_version_tags = list(candidates.get("version_tags") or [])
    if (
        not isinstance(version_tags, list)
        or len(version_tags) != len(set(version_tags))
        or any(tag not in candidate_version_tags for tag in version_tags)
    ):
        raise ValueError(f"{label}.version_tags 必须是当前证据候选的子集")
    candidate_dlc = bool(candidates.get("dlc"))
    if not isinstance(reviewed.get("dlc"), bool) or (
        reviewed.get("dlc") and not candidate_dlc
    ):
        raise ValueError(f"{label}.dlc 不是当前证据候选")
    if not reviewed.get("dlc") and (
        reviewed.get("dlc_version") is not None
        or reviewed.get("dlc_range") is not None
    ):
        raise ValueError(f"{label} 否决 DLC 时不得保留 DLC 版本或范围")
    negative_tags = reviewed.get("negative_tags")
    candidate_negative = list(candidates.get("negative_tags") or [])
    if (
        not isinstance(negative_tags, list)
        or len(negative_tags) != len(set(negative_tags))
        or any(tag not in candidate_negative for tag in negative_tags)
    ):
        raise ValueError(f"{label}.negative_tags 必须是当前证据候选的子集")
    if "负面标签" in negative_tags:
        raise ValueError(f"禁止使用泛化负面标签占位：{label}")


def validate_local_series_semantics(
    analysis: dict[str, Any],
    semantics: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """校验 AI 对合集表面标签候选的复核，返回 series_id 映射。"""
    root = Path(str(analysis["root"])).resolve()
    evidence_by_id = {
        str(item["series_id"]): item for item in analysis.get("series", [])
    }
    result: dict[str, dict[str, Any]] = {}
    for item in semantics.get("series", []):
        series_id = str(item.get("series_id") or "")
        if series_id in result:
            raise ValueError(f"重复 series_id：{series_id}")
        if series_id not in evidence_by_id:
            raise ValueError(f"未知 series_id：{series_id}")
        evidence = evidence_by_id[series_id]
        expected_source = Path(str(evidence["source_path"])).resolve()
        supplied_source = Path(str(item.get("source_path") or "")).resolve()
        try:
            supplied_source.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"合集源路径超出扫描根：{supplied_source}") from exc
        if supplied_source != expected_source:
            raise ValueError(f"合集源路径与分析证据不匹配：{series_id}")
        if item.get("input_fingerprint") != evidence.get("directory_fingerprint"):
            raise ValueError(f"合集输入指纹不匹配：{series_id}")
        if item.get("review_state") not in {"accepted", "needs_user_review"}:
            raise ValueError(f"合集 review_state 无效：{series_id}")
        confidence = item.get("confidence")
        if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise ValueError(f"合集 confidence 必须位于 0 到 1：{series_id}")
        candidates = evidence.get("surface_local_fields") or {}
        expected_candidates = {
            "positive_tag": candidates.get("positive_tag"),
            "negative_tags": list(candidates.get("negative_tags") or []),
        }
        reviewed = item.get("reviewed_surface_fields")
        if not isinstance(reviewed, dict) or set(reviewed) != {
            "positive_tag", "negative_tags",
        }:
            raise ValueError(
                f"reviewed_surface_fields 只允许 positive_tag/negative_tags：{series_id}"
            )
        if reviewed.get("positive_tag") not in {
            expected_candidates["positive_tag"], None,
        }:
            raise ValueError(
                f"reviewed_surface_fields.positive_tag 不是当前候选：{series_id}"
            )
        negative_tags = reviewed.get("negative_tags")
        candidate_negative = expected_candidates["negative_tags"]
        if (
            not isinstance(negative_tags, list)
            or len(negative_tags) != len(set(negative_tags))
            or any(tag not in candidate_negative for tag in negative_tags)
        ):
            raise ValueError(
                f"reviewed_surface_fields.negative_tags 必须是候选子集：{series_id}"
            )
        if "负面标签" in negative_tags:
            raise ValueError(f"禁止使用泛化负面标签占位：{series_id}")
        if "series_common_titles" in item:
            validated_language_titles(
                item["series_common_titles"],
                label=f"series_common_titles：{series_id}",
            )
        result[series_id] = item
    missing = set(evidence_by_id) - set(result)
    if missing:
        raise ValueError(f"Skill 结果缺少 series_id：{', '.join(sorted(missing))}")
    return result


def compile_remote_semantics(
    analysis: dict[str, Any],
    local_semantics: dict[str, Any],
    search_evidence: dict[str, Any],
    decisions: dict[str, Any],
) -> dict[str, Any]:
    """把 Skill 候选判断编译为完整远程语义并补齐本轮摘要。"""
    if decisions.get("schema_version") != ANALYSIS_SCHEMA_VERSION:
        raise ValueError("远程决策 schema_version 不受支持")
    decision_schema = decisions.get("decision_schema_version", 1)
    if decision_schema not in {1, 2, 3}:
        raise ValueError("远程 decision_schema_version 不受支持")
    local_by_id = {
        str(item["game_id"]): item for item in local_semantics.get("games", [])
    }
    search_by_id = {
        str(item["game_id"]): item for item in search_evidence.get("games", [])
    }
    analysis_series_ids = {
        str(item["series_id"]) for item in analysis.get("series", [])
    }
    remote_series_by_id: dict[str, dict[str, Any]] = {}
    if decision_schema >= 3:
        for raw_series in decisions.get("series", []):
            if not isinstance(raw_series, dict):
                raise ValueError("远程系列决策项必须为对象")
            series_id = str(raw_series.get("series_id") or "")
            if series_id not in analysis_series_ids:
                raise ValueError(f"未知远程 series_id：{series_id}")
            if series_id in remote_series_by_id:
                raise ValueError(f"重复远程 series_id：{series_id}")
            common_titles = validated_language_titles(
                raw_series.get("series_common_titles"),
                label=f"remote series_common_titles：{series_id}",
            )
            reason = str(raw_series.get("reason") or "").strip()
            confidence = raw_series.get("confidence")
            if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
                raise ValueError(f"远程系列 confidence 必须位于 0 到 1：{series_id}")
            review_state = raw_series.get("review_state")
            if review_state not in {"accepted", "needs_user_review"}:
                raise ValueError(f"远程系列 review_state 无效：{series_id}")
            if any(common_titles.values()) and (
                not reason or review_state != "accepted"
            ):
                raise ValueError(
                    f"远程系列共同标题必须 accepted 且有理由：{series_id}"
                )
            unknown = set(raw_series) - {
                "series_id", "series_common_titles", "confidence", "reason",
                "review_state",
            }
            if unknown:
                raise ValueError(
                    f"远程系列决策包含未知字段：{series_id} "
                    + "、".join(sorted(unknown))
                )
            remote_series_by_id[series_id] = {
                "series_common_titles": common_titles,
                "series_common_titles_reason": reason or None,
            }
        missing_series = analysis_series_ids - set(remote_series_by_id)
        if missing_series:
            raise ValueError(
                "远程决策缺少 series_id：" + "、".join(sorted(missing_series))
            )
    games: list[dict[str, Any]] = []
    for raw in decisions.get("games", []):
        item = copy.deepcopy(raw)
        game_id = str(item.get("game_id") or "")
        if decision_schema in {2, 3}:
            if game_id not in local_by_id or game_id not in search_by_id:
                raise ValueError(f"未知 game_id：{game_id}")
            item = expand_compact_remote_decision(
                local_by_id[game_id], search_by_id[game_id], item,
                candidate_key_factory=stable_candidate_key,
            )
        if decision_schema >= 3:
            if (
                "series_common_titles" in raw
                or "series_common_titles_reason" in raw
            ):
                raise ValueError(
                    "远程系列共同标题由编译器按 series 决策展开"
                )
            series_id = local_by_id[game_id].get("series_id")
            series_decision = remote_series_by_id.get(str(series_id))
            common_titles = (series_decision or {}).get("series_common_titles")
            if common_titles and any(common_titles.values()):
                item["series_common_titles"] = copy.deepcopy(common_titles)
                item["series_common_titles_reason"] = (
                    series_decision["series_common_titles_reason"]
                )
        games.append(item)
    unlisted = decisions.get("unlisted_game")
    if unlisted is not None:
        if not isinstance(unlisted, dict):
            raise ValueError("unlisted_game 必须为对象")
        explicit_ids = {str(item.get("game_id") or "") for item in games}
        for local in local_semantics.get("games", []):
            game_id = str(local.get("game_id") or "")
            if game_id in explicit_ids:
                continue
            games.append({
                "game_id": game_id,
                "selected_candidates": [],
                "reference_candidates": [],
                "returned_titles": {"jp": None, "zh": None, "en": None},
                "developer": None,
                "engine": None,
                "field_sources": {},
                "source_disposition": {},
                "confidence": unlisted.get("confidence"),
                "reason": unlisted.get("reason"),
                "review_state": "needs_user_review",
            })
    game_order = {
        str(item["game_id"]): index
        for index, item in enumerate(local_semantics.get("games", []))
    }
    games.sort(key=lambda item: game_order.get(str(item.get("game_id")), 10**9))
    compiled = {
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "analysis_fingerprint": analysis["input_fingerprint"],
        "local_semantics_fingerprint": canonical_sha256(local_semantics),
        "search_fingerprint": search_evidence["search_fingerprint"],
        "games": games,
    }
    validate_remote_semantics(
        analysis, local_semantics, search_evidence, compiled
    )
    return compiled


def validate_local_semantics(
    analysis: dict[str, Any],
    semantics: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """严格校验 Skill 本地语义，返回按 game_id 建立的只读映射。"""
    _verify_embedded_fingerprint(
        analysis, "input_fingerprint", "分析输入"
    )
    if semantics.get("schema_version") != ANALYSIS_SCHEMA_VERSION:
        raise ValueError("本地语义 schema_version 不受支持")
    if semantics.get("analysis_fingerprint") != analysis.get("input_fingerprint"):
        raise ValueError("分析输入摘要不匹配，Skill 结果已过期")
    supplement_document = semantics.get("user_supplements")
    supplement_games: dict[str, dict[str, Any]] = {}
    if supplement_document is not None:
        normalized_supplements = validate_user_supplements(
            analysis, supplement_document,
        )
        supplement_fingerprint = canonical_sha256(normalized_supplements)
        if semantics.get("user_supplements_fingerprint") != supplement_fingerprint:
            raise ValueError("user supplements 摘要不匹配")
        if supplement_document != normalized_supplements:
            raise ValueError("user supplements 未使用规范化内容")
        expected_non_games = normalized_supplements["non_game_candidates"]
        if semantics.get("confirmed_non_game_candidates") != expected_non_games:
            raise ValueError("confirmed_non_game_candidates 与人工补充不一致")
        supplement_games = {
            item["game_id"]: item for item in normalized_supplements["games"]
        }
    elif any(
        key in semantics for key in (
            "user_supplements_fingerprint", "confirmed_non_game_candidates",
        )
    ):
        raise ValueError("本地语义缺少 user_supplements 正文")
    local_series_by_id = validate_local_series_semantics(analysis, semantics)
    root = Path(str(analysis["root"])).resolve()
    analysis_games = {
        str(item["game_id"]): item for item in analysis.get("games", [])
    }
    result: dict[str, dict[str, Any]] = {}
    for item in semantics.get("games", []):
        game_id = str(item.get("game_id") or "")
        if game_id in result:
            raise ValueError(f"重复 game_id：{game_id}")
        if game_id not in analysis_games:
            raise ValueError(f"未知 game_id：{game_id}")
        evidence = analysis_games[game_id]
        expected_source = Path(str(evidence["source_path"])).resolve()
        supplied_source = Path(str(item.get("source_path") or "")).resolve()
        try:
            supplied_source.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"源路径超出扫描根：{supplied_source}") from exc
        if supplied_source != expected_source:
            raise ValueError(f"源路径与分析证据不匹配：{game_id}")
        if item.get("input_fingerprint") != evidence.get("directory_fingerprint"):
            raise ValueError(f"游戏输入指纹不匹配：{game_id}")
        supplement = supplement_games.get(game_id) or {}
        expected_query_exceptions = {
            language: evidence["search_keyword"]
            for language, evidence in (supplement.get("title_evidence") or {}).items()
            if evidence.get("allow_non_basename_search")
        }
        if (item.get("authorized_search_query_exceptions") or {}) != expected_query_exceptions:
            raise ValueError(f"用户授权搜索例外与补充不一致：{game_id}")
        supplemented_developer = supplement.get("developer_evidence")
        if supplemented_developer:
            developer_evidence = item.get("developer_evidence") or {}
            if developer_evidence.get("value") != supplemented_developer["value"]:
                raise ValueError(f"人工补充 developer 未进入本地语义：{game_id}")
        supplemented_fallback = supplement.get("english_fallback")
        if supplemented_fallback:
            expected_fallback = {
                key: supplemented_fallback[key]
                for key in ("developer", "en_title")
                if supplemented_fallback[key]
            }
            if item.get("english_fallback") != expected_fallback:
                raise ValueError(f"人工补充 english_fallback 未进入本地语义：{game_id}")
        supplemented_path_override = supplement.get("english_path_override")
        if supplemented_path_override:
            if item.get("english_path_override") != {"allow_non_ascii": True}:
                raise ValueError(
                    f"人工补充 english_path_override 未进入本地语义：{game_id}"
                )
        elif item.get("english_path_override") is not None:
            if item.get("english_path_override") != {"allow_non_ascii": True}:
                raise ValueError(f"english_path_override 格式无效：{game_id}")
        review_state = item.get("review_state")
        if review_state not in {"accepted", "needs_user_review"}:
            raise ValueError(f"未知 review_state：{review_state}")
        confidence = item.get("confidence")
        if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise ValueError(f"confidence 必须位于 0 到 1：{game_id}")
        reviewed_local_fields = item.get("reviewed_local_fields")
        expected_local_fields = {
            "engine": normalize_engine_label(evidence.get("engine")),
            "version": (evidence.get("local_fields") or {}).get("version"),
            "dlc": bool((evidence.get("local_fields") or {}).get("dlc")),
            "dlc_version": (evidence.get("local_fields") or {}).get("dlc_version"),
            "dlc_range": (evidence.get("local_fields") or {}).get("dlc_range"),
            "version_tags": list(
                (evidence.get("local_fields") or {}).get("version_tags") or []
            ),
            "positive_tag": (evidence.get("local_fields") or {}).get("positive_tag"),
            "negative_tags": list(
                (evidence.get("local_fields") or {}).get("negative_tags") or []
            ),
        }
        _validate_reviewed_candidate_fields(
            reviewed_local_fields,
            expected_local_fields,
            label=f"reviewed_local_fields：{game_id}",
        )
        english_fallback = item.get("english_fallback")
        if english_fallback is not None:
            if not isinstance(english_fallback, dict):
                raise ValueError(f"english_fallback 必须为对象：{game_id}")
            unknown_fallbacks = set(english_fallback) - {
                "developer", "en_title"
            }
            if unknown_fallbacks:
                raise ValueError(
                    f"english_fallback 包含未知字段：{game_id} "
                    + "、".join(sorted(unknown_fallbacks))
                )
            for field_name, value in english_fallback.items():
                if (
                    not isinstance(value, str)
                    or not value.strip()
                    or not value.isascii()
                ):
                    raise ValueError(
                        f"english_fallback.{field_name} 必须是非空 ASCII 文本："
                        f"{game_id}"
                    )
        titles = item.get("titles")
        if not isinstance(titles, dict):
            raise ValueError(f"titles 必须为对象：{game_id}")
        raw_name = str(evidence.get("raw_name") or "")
        local_segments = titles.get("local_segments")
        if not isinstance(local_segments, list) or any(
            not isinstance(segment, str)
            or not segment
            or segment not in raw_name
            for segment in local_segments
        ):
            raise ValueError(
                f"titles.local_segments 必须逐项原样存在于 raw_name：{game_id}"
            )
        if (
            review_state == "accepted"
            and not local_segments
            and not supplement.get("title_evidence")
        ):
            raise ValueError(
                f"accepted 本地语义必须列出 local_segments：{game_id}"
            )
        segment_languages = titles.get("local_segments_by_language")
        if segment_languages is not None:
            if (
                not isinstance(segment_languages, dict)
                or set(segment_languages) != {"jp", "zh", "en"}
                or any(
                    not isinstance(values, list)
                    or any(not isinstance(value, str) or not value for value in values)
                    for values in segment_languages.values()
                )
            ):
                raise ValueError(
                    f"local_segments_by_language 语言映射无效：{game_id}"
                )
            mapped_segments = [
                segment
                for language in ("jp", "zh", "en")
                for segment in segment_languages[language]
            ]
            if sorted(mapped_segments) != sorted(local_segments):
                raise ValueError(
                    f"local_segments_by_language 必须精确覆盖 local_segments：{game_id}"
                )
        occupied_title_spans: list[tuple[int, int]] = []
        local_title_evidence = "\n".join(
            str(value) for value in (
                list(evidence.get("local_name_evidence") or [])
                + list(evidence.get("local_text_evidence") or [])
                + [
                    record.get("text") or ""
                    for record in evidence.get("local_text_sources") or []
                    if isinstance(record, dict)
                ]
            )
        )
        supplemented_titles = supplement.get("title_evidence") or {}
        if supplemented_titles:
            local_title_evidence += "\n" + "\n".join(
                value["value"] for value in supplemented_titles.values()
            )
        evidence_title_keys: set[str] = set()
        for language in ("jp", "zh", "en"):
            title = titles.get(language)
            supplemented = supplemented_titles.get(language)
            if supplemented and (
                title != supplemented["value"]
                or (titles.get("search_keywords") or {}).get(language)
                != supplemented["search_keyword"]
            ):
                raise ValueError(
                    f"人工补充标题未逐字进入本地语义：{game_id} {language}"
                )
            if not title:
                continue
            if not isinstance(title, str) or not title.strip():
                raise ValueError(f"titles.{language} 格式无效：{game_id}")
            claimed = _claim_ordered_title_spans(
                raw_name, title, occupied_title_spans,
            )
            if claimed is None:
                evidence_key = unicodedata.normalize("NFC", title).casefold()
                if (
                    title in raw_name
                    or title not in local_title_evidence
                    or evidence_key in evidence_title_keys
                ):
                    raise ValueError(
                        f"titles.{language} 必须由 raw_name 中不重叠的有序字面片段，"
                        f"或本地文件名/受限文本中的逐字标题支持：{game_id}"
                    )
                evidence_title_keys.add(evidence_key)
            else:
                occupied_title_spans.extend(claimed)
        keywords = titles.get("search_keywords")
        if not isinstance(keywords, dict):
            raise ValueError(f"search_keywords 必须为对象：{game_id}")
        unknown_languages = set(keywords) - {"jp", "zh", "en"}
        if unknown_languages:
            raise ValueError(
                f"未知语言键：{', '.join(sorted(unknown_languages))}"
            )
        short_keywords = titles.get("short_search_keywords")
        if short_keywords is not None and (
            not isinstance(short_keywords, dict)
            or set(short_keywords) != {"jp", "zh", "en"}
            or short_keywords != validated_short_search_keywords(
                short_keywords, keywords, raw_name,
            )
        ):
            raise ValueError(f"short_search_keywords 结构无效：{game_id}")
        propagated_context = titles.get("propagated_title_context")
        if propagated_context is not None and (
            not isinstance(propagated_context, dict)
            or set(propagated_context) != {"jp", "zh", "en"}
        ):
            raise ValueError(
                f"propagated_title_context 语言键无效：{game_id}"
            )
        if review_state == "accepted":
            existing = 0
            for language in ("jp", "zh", "en"):
                title = titles.get(language)
                keyword = keywords.get(language)
                if title:
                    existing += 1
                    title_from_basename = search_query_is_from_basename(
                        sanitize_search_query(str(title)), raw_name,
                    )
                    if keyword in (None, "") and not title_from_basename:
                        continue
                    if not isinstance(keyword, str) or not keyword.strip():
                        raise ValueError(
                            f"{language} 搜索关键词不能为空：{game_id}"
                        )
                    if sanitize_search_query(keyword) != keyword:
                        raise ValueError(
                            f"{language} 搜索关键词只能包含文字、字母、数字和单个空格："
                            f"{game_id}"
                        )
                    if _normalized_title_fragment(keyword) not in (
                        _normalized_title_fragment(str(title))
                    ):
                        raise ValueError(
                            f"{language} 搜索关键词必须来自叶子标题：{game_id}"
                        )
                    if (
                        not search_query_is_from_basename(keyword, raw_name)
                        and expected_query_exceptions.get(language) != keyword
                    ):
                        raise ValueError(
                            f"{language} 搜索关键词必须来自原文件夹名称：{game_id}"
                        )
                elif keyword not in (None, ""):
                    raise ValueError(
                        f"{language} 搜索关键词没有对应标题：{game_id}"
                    )
            if not existing:
                raise ValueError(f"至少需要一个语言标题：{game_id}")
        series_by_relative = {
            str(entry.get("relative_path") or ""): entry.get("series_id")
            for entry in analysis.get("series", [])
        }
        expected_series_id = series_by_relative.get(
            str(evidence.get("series_relative_path") or "")
        )
        series_id = item.get("series_id")
        if series_id != expected_series_id:
            raise ValueError(
                f"series_id 与实际父系列不匹配：{game_id}"
            )
        common_titles = item.get("series_common_titles")
        if (
            not isinstance(common_titles, dict)
            or set(common_titles) != {"jp", "zh", "en"}
        ):
            raise ValueError(f"series_common_titles 语言键无效：{game_id}")
        for language, value in common_titles.items():
            if value is not None and (
                not isinstance(value, str) or not value.strip()
            ):
                raise ValueError(
                    f"series_common_titles.{language} 必须为非空字符串或 null"
                )
        if series_id is None and any(common_titles.values()):
            raise ValueError(f"非系列游戏不得填写共同标题：{game_id}")
        if propagated_context is not None:
            if series_id is None:
                raise ValueError(
                    f"非系列游戏不得填写 propagated_title_context：{game_id}"
                )
            for language, value in propagated_context.items():
                if value is not None and (
                    not isinstance(value, str) or not value.strip()
                ):
                    raise ValueError(
                        f"propagated_title_context.{language} 必须为非空字符串或 null："
                        f"{game_id}"
                    )
                if value and value != common_titles.get(language):
                    raise ValueError(
                        f"propagated_title_context.{language} 必须等于系列共同标题："
                        f"{game_id}"
                    )
        standalone_dlc_language = item.get("standalone_dlc_title_language")
        if standalone_dlc_language is not None:
            if standalone_dlc_language not in {"jp", "zh", "en"}:
                raise ValueError(
                    f"standalone_dlc_title_language 无效：{game_id}"
                )
            if not bool(reviewed_local_fields.get("dlc")):
                raise ValueError(
                    f"standalone_dlc_title_language 缺少本地 DLC 证据：{game_id}"
                )
        credit = item.get("translation_credit")
        if credit is not None:
            if not isinstance(credit, dict) or set(credit) != {
                "value", "type", "evidence",
            }:
                raise ValueError(f"translation_credit 格式无效：{game_id}")
            if credit.get("type") not in {"group", "person"}:
                raise ValueError(f"translation_credit.type 无效：{game_id}")
            value = str(credit.get("value") or "").strip()
            for source_record in evidence.get("local_text_sources") or []:
                if not isinstance(source_record, dict) or not isinstance(
                    source_record.get("relative_path"), str
                ) or not isinstance(source_record.get("text"), str):
                    raise ValueError(f"local_text_sources 格式无效：{game_id}")
            supplemented_credit = supplement.get("translation_credit")
            if supplemented_credit and (
                value != supplemented_credit["value"]
                or credit.get("type") != supplemented_credit["type"]
                or credit.get("evidence") != [
                    "用户人工补充：" + supplemented_credit["reason"]
                ]
            ):
                raise ValueError(
                    f"人工补充 translation_credit 未进入本地语义：{game_id}"
                )
            if (
                not value
                or not isinstance(credit.get("evidence"), list)
                or (
                    not supplemented_credit and (
                        not _root_translation_credit_is_literal(
                            str(evidence.get("raw_name") or ""), value,
                        )
                        or credit["evidence"] != ["游戏根 basename"]
                    )
                )
            ):
                raise ValueError(
                    f"translation_credit 必须由游戏根 basename 或人工补充支持："
                    f"{game_id}"
                )
        auxiliaries = item.get("auxiliary_directories") or []
        if auxiliaries:
            raise ValueError(f"新批次不规划 CG/存档附属目录改名：{game_id}")
        result[game_id] = item
    missing = set(analysis_games) - set(result)
    if missing:
        raise ValueError(f"Skill 结果缺少 game_id：{', '.join(sorted(missing))}")
    for game_id, item in result.items():
        series_id = item.get("series_id")
        if not series_id:
            continue
        series_common = (local_series_by_id.get(str(series_id)) or {}).get(
            "series_common_titles"
        )
        if series_common is not None and item.get("series_common_titles") != series_common:
            raise ValueError(
                f"成员 series_common_titles 未由系列决策一致展开：{game_id}"
            )
    return result


def unresolved_candidates_for_preview(
    analysis: dict[str, Any],
    local_semantics: dict[str, Any],
) -> list[dict[str, Any]]:
    """保留仍未判断的启动候选，排除用户已确认的非游戏工具。"""
    confirmed = {
        str(item.get("candidate_id") or "")
        for item in local_semantics.get("confirmed_non_game_candidates") or []
    }
    return [
        item for item in analysis.get("hierarchy_unresolved_candidates") or []
        if str(item.get("candidate_id") or "") not in confirmed
    ]


def validate_remote_semantics(
    analysis: dict[str, Any],
    local_semantics: dict[str, Any],
    search_evidence: dict[str, Any],
    remote_semantics: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """验证 Skill 远程判断只引用本轮实际检索到的候选。"""
    _verify_embedded_fingerprint(
        analysis, "input_fingerprint", "分析输入"
    )
    _verify_embedded_fingerprint(
        search_evidence, "search_fingerprint", "搜索证据"
    )
    if remote_semantics.get("schema_version") != ANALYSIS_SCHEMA_VERSION:
        raise ValueError("远程语义 schema_version 不受支持")
    if remote_semantics.get("analysis_fingerprint") != analysis.get(
        "input_fingerprint"
    ):
        raise ValueError("分析输入摘要不匹配，远程 Skill 结果已过期")
    if search_evidence.get("analysis_fingerprint") != analysis.get(
        "input_fingerprint"
    ):
        raise ValueError("搜索证据引用的分析输入摘要不匹配")
    expected_local = canonical_sha256(local_semantics)
    if (
        search_evidence.get("local_semantics_fingerprint") != expected_local
        or remote_semantics.get("local_semantics_fingerprint") != expected_local
    ):
        raise ValueError("本地语义摘要不匹配")
    if remote_semantics.get("search_fingerprint") != search_evidence.get(
        "search_fingerprint"
    ):
        raise ValueError("搜索证据摘要不匹配，远程 Skill 结果已过期")
    local_by_id = validate_local_semantics(analysis, local_semantics)
    analysis_by_id = {
        str(item.get("game_id")): item for item in analysis.get("games", [])
    }
    search_by_id = {
        str(item.get("game_id")): item
        for item in search_evidence.get("games", [])
    }
    result: dict[str, dict[str, Any]] = {}
    for item in remote_semantics.get("games", []):
        game_id = str(item.get("game_id") or "")
        if game_id in result:
            raise ValueError(f"重复 game_id：{game_id}")
        if game_id not in local_by_id or game_id not in search_by_id:
            raise ValueError(f"未知 game_id：{game_id}")
        review_state = item.get("review_state")
        if review_state not in {"accepted", "needs_user_review"}:
            raise ValueError(f"未知 review_state：{review_state}")
        confidence = item.get("confidence")
        if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise ValueError(f"confidence 必须位于 0 到 1：{game_id}")
        candidate_universe: dict[
            tuple[str, str, str], dict[str, Any]
        ] = {}
        for source in search_by_id[game_id].get("sources", []):
            for candidate in source.get("candidates", []):
                candidate_universe[(
                    str(candidate.get("source") or source.get("source") or ""),
                    str(candidate.get("source_id") or ""),
                    str(candidate.get("url") or ""),
                )] = candidate
            for round_item in source.get("rounds", []):
                for candidate in round_item.get("candidates", []):
                    candidate_universe[(
                        str(candidate.get("source") or source.get("source") or ""),
                        str(candidate.get("source_id") or ""),
                        str(candidate.get("url") or ""),
                    )] = candidate
        selected: set[tuple[str, str, str]] = set()
        for candidate in item.get("selected_candidates", []):
            identity = (
                str(candidate.get("source") or ""),
                str(candidate.get("source_id") or ""),
                str(candidate.get("url") or ""),
            )
            if identity in selected:
                raise ValueError(f"重复采用候选：{identity[0]} {identity[1]}")
            if identity not in candidate_universe:
                raise ValueError(
                    f"采用了未检索候选：{identity[0]} {identity[1]} {identity[2]}"
                )
            release = candidate.get("release")
            if release is not None:
                if (
                    identity[0] != "VNDB"
                    or not isinstance(release, dict)
                    or set(release) != {"source_id", "url"}
                ):
                    raise ValueError(f"VNDB release 指向格式无效：{game_id}")
                release_id = str(release.get("source_id") or "")
                release_url = str(release.get("url") or "")
                if (
                    not re.fullmatch(r"r\d+", release_id)
                    or release_url != f"https://vndb.org/{release_id}"
                ):
                    raise ValueError(f"VNDB release 指向不规范：{game_id}")
                seeded_releases = {
                    str(seed.get("provided_url") or "")
                    for seed in (local_semantics.get("user_supplements") or {}).get(
                        "candidate_seeds", []
                    )
                    if str(seed.get("game_id") or "") == game_id
                    and str(seed.get("source") or "") == "VNDB"
                    and str(seed.get("source_id") or "") == identity[1]
                }
                if release_url not in seeded_releases:
                    raise ValueError(
                        f"VNDB release 指向必须来自本轮用户候选种子：{game_id}"
                    )
            selected.add(identity)
        selected_sources = {identity[0] for identity in selected}
        selected_candidates = [
            candidate_universe[identity] for identity in selected
        ]
        selected_with_developer = [
            candidate for candidate in selected_candidates
            if candidate.get("developer")
        ]
        local_developer = str(
            (local_by_id[game_id].get("developer_evidence") or {}).get("value")
            or ""
        ).strip()
        audience = item.get("audience_classification")
        audience_source = item.get("audience_source")
        candidates_with_audience = [
            candidate for candidate in selected_candidates
            if candidate.get("audience_classification")
            and candidate.get("audience_evidence")
        ]
        if {
            str(candidate.get("audience_classification"))
            for candidate in candidates_with_audience
        } >= {"adult", "all_ages"}:
            raise ValueError(
                f"非全年龄候选已采用时不得同时采用全年龄候选：{game_id}"
            )
        strictest_audience = (
            "adult"
            if any(item.get("audience_classification") == "adult"
                   for item in candidates_with_audience)
            else "all_ages"
            if any(item.get("audience_classification") == "all_ages"
                   for item in candidates_with_audience)
            else None
        )
        if audience is not None:
            if audience not in {"all_ages", "adult"}:
                raise ValueError(f"未知年龄分类：{game_id}")
            if not isinstance(audience_source, dict):
                raise ValueError(f"年龄分类缺少来源：{game_id}")
            identity = (
                str(audience_source.get("source") or ""),
                str(audience_source.get("source_id") or ""),
                str(audience_source.get("url") or ""),
            )
            candidate = candidate_universe.get(identity)
            if candidate not in selected_candidates:
                raise ValueError(f"年龄分类来源不是采用候选：{game_id}")
            if not candidate.get("audience_evidence"):
                raise ValueError(f"年龄分类来源缺少原始证据：{game_id}")
            if candidate.get("audience_classification") != audience:
                raise ValueError(f"年龄分类来源不支持合并结论：{game_id}")
            if strictest_audience != audience:
                raise ValueError(f"合并年龄未采用最高限制：{game_id}")
        elif audience_source is not None:
            raise ValueError(f"空年龄分类不得声明来源：{game_id}")
        elif strictest_audience is not None:
            raise ValueError(f"采用候选有年龄证据但未给出合并分类：{game_id}")
        references: set[tuple[str, str, str]] = set()
        for candidate in item.get("reference_candidates", []):
            identity = (
                str(candidate.get("source") or ""),
                str(candidate.get("source_id") or ""),
                str(candidate.get("url") or ""),
            )
            if identity in references:
                raise ValueError(f"重复参考候选：{identity[0]} {identity[1]}")
            if identity not in candidate_universe:
                raise ValueError(
                    f"参考了未检索候选：{identity[0]} {identity[1]} {identity[2]}"
                )
            references.add(identity)
        overlap = selected & references
        if overlap:
            source, source_id, _url = sorted(overlap)[0]
            raise ValueError(
                f"同一候选不得同时采用和参考：{source} {source_id}"
            )
        candidate_sources = {identity[0] for identity in candidate_universe}
        reference_sources = {identity[0] for identity in references}
        field_sources = item.get("field_sources") or {}
        if not isinstance(field_sources, dict):
            raise ValueError(f"field_sources 必须为对象：{game_id}")
        unknown_fields = set(field_sources) - {
            "developer", "engine", "jp", "zh", "en",
        }
        if unknown_fields:
            raise ValueError(
                "field_sources 含未知字段：" + ", ".join(sorted(unknown_fields))
            )
        unknown_field_sources = {
            str(source) for source in field_sources.values()
            if str(source) not in selected_sources
        }
        if unknown_field_sources:
            raise ValueError(
                "字段来自未采用来源：" + ", ".join(sorted(unknown_field_sources))
            )
        dispositions = item.get("source_disposition") or {}
        if not isinstance(dispositions, dict) or any(
            value not in {"adopted", "rejected"}
            for value in dispositions.values()
        ):
            raise ValueError(f"source_disposition 含未知状态：{game_id}")
        if any(dispositions.get(source) != "adopted" for source in selected_sources):
            raise ValueError(f"采用候选的 source_disposition 不是 adopted：{game_id}")
        unknown_disposition_sources = set(dispositions) - candidate_sources
        if unknown_disposition_sources:
            raise ValueError(
                "source_disposition 引用了无候选来源："
                + "、".join(sorted(unknown_disposition_sources))
                + f" {game_id}"
            )
        adopted_without_candidate = {
            source for source, state in dispositions.items()
            if state == "adopted" and source not in selected_sources
        }
        if adopted_without_candidate:
            raise ValueError(
                "adopted 来源缺少采用候选："
                + "、".join(sorted(adopted_without_candidate))
                + f" {game_id}"
            )
        candidate_keys_by_source = {
            str(candidate.get("candidate_key") or ""):
            str(candidate.get("source") or "")
            for candidate in candidate_universe.values()
        }
        preview_keys = item.get("preview_candidate_keys") or {}
        preview_states = item.get("preview_candidate_dispositions") or {}
        if not isinstance(preview_keys, dict) or not isinstance(preview_states, dict):
            raise ValueError(f"最佳展示候选结构无效：{game_id}")
        if set(preview_keys) != set(preview_states) or set(preview_keys) - candidate_sources:
            raise ValueError(f"最佳展示候选来源不一致：{game_id}")
        for source, key in preview_keys.items():
            if candidate_keys_by_source.get(str(key)) != source:
                raise ValueError(f"最佳展示候选 key 不属于来源：{game_id} {source}")
            if preview_states[source] not in {"adopted", "reference", "rejected"}:
                raise ValueError(f"最佳展示候选状态无效：{game_id} {source}")
        rationale_keys: set[str] = set()
        for rationale in item.get("nonadopted_candidate_reasons") or []:
            if not isinstance(rationale, dict) or set(rationale) != {
                "candidate_key", "source", "disposition", "reason",
            }:
                raise ValueError(f"未采用理由结构无效：{game_id}")
            key = str(rationale["candidate_key"])
            source = str(rationale["source"])
            if (
                not key or key in rationale_keys
                or candidate_keys_by_source.get(key) != source
                or rationale["disposition"] not in {"reference", "rejected"}
                or not isinstance(rationale["reason"], str)
                or not rationale["reason"].strip()
            ):
                raise ValueError(f"未采用理由未绑定真实候选：{game_id}")
            rationale_keys.add(key)
        adopted_identities = {
            (str(candidate.get("source") or ""),
             str(candidate.get("source_id") or ""),
             str(candidate.get("url") or ""))
            for candidate in item.get("selected_candidates") or []
        }
        reference_identities = {
            (str(candidate.get("source") or ""),
             str(candidate.get("source_id") or ""),
             str(candidate.get("url") or ""))
            for candidate in item.get("reference_candidates") or []
        }
        candidate_by_key = {
            str(candidate.get("candidate_key") or ""): candidate
            for candidate in candidate_universe.values()
        }
        for source, key in preview_keys.items():
            candidate = candidate_by_key[str(key)]
            identity = (
                str(candidate.get("source") or ""),
                str(candidate.get("source_id") or ""),
                str(candidate.get("url") or ""),
            )
            state = preview_states[source]
            if (
                (state == "adopted" and identity not in adopted_identities)
                or (state == "reference" and (
                    identity not in reference_identities or key not in rationale_keys
                ))
                or (state == "rejected" and key not in rationale_keys)
            ):
                raise ValueError(f"最佳展示候选状态与逐项审核不符：{game_id} {source}")
        undecided_sources = candidate_sources - set(dispositions) - reference_sources
        if undecided_sources:
            raise ValueError(
                "候选来源未完成处置："
                + "、".join(sorted(undecided_sources))
                + f" {game_id}"
            )
        titles = item.get("returned_titles") or {}
        if (
            not isinstance(titles, dict)
            or set(titles) != {"jp", "zh", "en"}
        ):
            raise ValueError(f"returned_titles 语言键无效：{game_id}")
        excluded_languages = {
            str(language)
            for supplement in (
                (local_semantics.get("user_supplements") or {}).get("games", [])
            )
            if str(supplement.get("game_id") or "") == game_id
            for language in (supplement.get("title_exclusions") or {})
        }
        for language in sorted(excluded_languages):
            if titles.get(language):
                raise ValueError(
                    f"用户已排除 {language} 标题，远程语义不得回填：{game_id}"
                )
        remote_common_titles = item.get("series_common_titles")
        remote_common_reason = item.get("series_common_titles_reason")
        if remote_common_titles is not None:
            if (
                not local_by_id[game_id].get("series_id")
                or not isinstance(remote_common_titles, dict)
                or set(remote_common_titles) != {"jp", "zh", "en"}
                or not isinstance(remote_common_reason, str)
                or not remote_common_reason.strip()
            ):
                raise ValueError(f"远程系列共同标题格式无效：{game_id}")
            if not any(remote_common_titles.values()):
                raise ValueError(f"远程系列共同标题不得全空：{game_id}")
            for language, common_value in remote_common_titles.items():
                if common_value is None:
                    continue
                if not isinstance(common_value, str) or not common_value.strip():
                    raise ValueError(
                        f"series_common_titles.{language} 必须为非空字符串或 null："
                        f"{game_id}"
                    )
                returned_value = titles.get(language)
                if (
                    not returned_value
                    or _normalized_title_fragment(common_value)
                    not in _normalized_title_fragment(str(returned_value))
                ):
                    raise ValueError(
                        f"series_common_titles.{language} 必须来自成员远程标题："
                        f"{game_id}"
                    )
        elif remote_common_reason is not None:
            raise ValueError(f"空远程系列共同标题不得提供理由：{game_id}")
        propagated_context = item.get("propagated_title_context")
        if propagated_context is not None:
            if (
                not isinstance(propagated_context, dict)
                or set(propagated_context) != {"jp", "zh", "en"}
            ):
                raise ValueError(
                    f"propagated_title_context 语言键无效：{game_id}"
                )
            local_item = local_by_id[game_id]
            common_titles = local_item.get("series_common_titles") or {}
            if not local_item.get("series_id"):
                raise ValueError(
                    f"非系列游戏不得填写 propagated_title_context：{game_id}"
                )
            for language, value in propagated_context.items():
                if value is not None and (
                    not isinstance(value, str) or not value.strip()
                ):
                    raise ValueError(
                        f"propagated_title_context.{language} 必须为非空字符串或 null："
                        f"{game_id}"
                    )
                if value and value != common_titles.get(language):
                    raise ValueError(
                        f"propagated_title_context.{language} 必须等于系列共同标题："
                        f"{game_id}"
                    )
        standalone_dlc_language = item.get("standalone_dlc_title_language")
        if standalone_dlc_language is not None:
            if standalone_dlc_language not in {"jp", "zh", "en"}:
                raise ValueError(
                    f"standalone_dlc_title_language 无效：{game_id}"
                )
            if not bool(
                (local_by_id[game_id].get("reviewed_local_fields") or {}).get("dlc")
            ):
                raise ValueError(
                    f"standalone_dlc_title_language 缺少本地 DLC 证据：{game_id}"
                )
            if not titles.get(standalone_dlc_language):
                raise ValueError(
                    f"standalone_dlc_title_language 对应远程标题为空：{game_id}"
                )
        for field_name in ("jp", "zh", "en"):
            value = titles.get(field_name)
            source = field_sources.get(field_name)
            if value is None:
                if source is not None:
                    raise ValueError(
                        f"空字段不得声明来源：{field_name} {game_id}"
                    )
                continue
            candidate_field = {
                "jp": "jp_title", "zh": "zh_title", "en": "en_title",
            }[field_name]
            title_fields = ("title", "jp_title", "zh_title", "en_title")
            if not source or not any(
                candidate.get("source") == source
                and any(
                    _matches_candidate_output(candidate.get(name), value)
                    for name in title_fields
                )
                for candidate in selected_candidates
            ):
                raise ValueError(
                    f"远程字段与候选原始值不匹配：{field_name} {game_id}"
                )
            highest_source = _highest_priority_source(
                selected_candidates,
                lambda candidate: bool(candidate.get(candidate_field)) or any(
                    _matches_candidate_output(candidate.get(name), value)
                    for name in title_fields
                ),
            )
            if source != highest_source:
                raise ValueError(
                    f"远程字段未使用最高优先级来源：{field_name} {game_id}"
                )
        developer = item.get("developer")
        developer_source = field_sources.get("developer")
        if developer is None:
            if developer_source is not None:
                raise ValueError(f"空 developer 不得声明来源：{game_id}")
        elif not developer_source or not any(
            candidate.get("source") == developer_source
            and _matches_candidate_output(candidate.get("developer"), developer)
            for candidate in selected_candidates
        ):
            raise ValueError(
                f"远程字段与候选原始值不匹配：developer {game_id}"
            )
        else:
            expected_developer = _choose_joint_developer_candidate(
                selected_with_developer
            )
            if (developer_source != expected_developer["source"]
                    or developer != expected_developer["developer"]):
                raise ValueError(
                    "developer 未遵循共同采用后的文字与来源优先级：" + game_id
                )
        engine = item.get("engine")
        engine_source = field_sources.get("engine")
        if engine is None:
            if engine_source is not None:
                raise ValueError(f"空 engine 不得声明来源：{game_id}")
        elif (
            not isinstance(engine, dict)
            or not engine.get("value")
            or engine.get("source") != engine_source
            or not any(
                candidate.get("source") == engine_source
                and _matches_candidate_output(
                    candidate.get("engine"), engine.get("value")
                )
                for candidate in selected_candidates
            )
        ):
            raise ValueError(
                f"远程字段与候选原始值不匹配：engine {game_id}"
            )
        elif engine_source != _highest_priority_source(
            selected_candidates,
            lambda candidate: bool(candidate.get("engine")),
        ):
            raise ValueError(f"engine 未使用最高优先级来源：{game_id}")
        if review_state == "accepted":
            if not selected:
                raise ValueError(f"accepted 条目至少采用一个候选：{game_id}")
            if not item.get("developer") and not local_developer:
                raise ValueError(f"accepted 条目缺少 developer：{game_id}")
            if not any(titles.get(language) for language in ("jp", "zh", "en")):
                raise ValueError(f"accepted 条目至少需要一个数据库返回标题：{game_id}")
            if audience is None:
                raise ValueError(f"accepted 条目必须选择全年龄或成人：{game_id}")
        result[game_id] = item
    missing = set(local_by_id) - set(result)
    if missing:
        raise ValueError(f"远程 Skill 结果缺少 game_id：{', '.join(sorted(missing))}")
    for series in analysis.get("series", []):
        child_ids = [str(value) for value in series.get("child_game_ids", [])]
        common_values = [result[game_id].get("series_common_titles") for game_id in child_ids]
        if any(value is not None for value in common_values):
            if any(value is None for value in common_values) or any(
                value != common_values[0] for value in common_values[1:]
            ):
                raise ValueError(
                    f"远程系列共同标题必须完整覆盖成员且逐字一致：{series['series_id']}"
                )
    return result
