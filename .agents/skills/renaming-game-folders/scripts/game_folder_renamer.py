#!/usr/bin/env python3
# 游戏目录规范化工具：预览清单与批准哈希一致后才执行文件系统变更。
from __future__ import annotations

import argparse
import copy
import difflib
import hashlib
import html
import json
import os
import re
import shutil
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


# 直接执行脚本与测试按文件加载时，都从相邻包导入职责模块。
SCRIPT_DIRECTORY = Path(__file__).resolve().parent
if str(SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIRECTORY))

from rename_pipeline import (
    CatalogUpdateStats,
    append_catalog_conflicts_to_decisions,
    attach_search_audit,
    begin_search_audit,
    build_combined_approval_manifest,
    build_updated_combined_approval_manifest,
    build_scan_evidence,
    canonical_sha256,
    classify_query_outcome,
    complete_preview_audit,
    compile_local_semantics,
    compile_remote_semantics,
    compile_hierarchy_semantics,
    directory_fingerprint,
    developer_identity_key,
    fail_search_audit,
    finish_search_audit,
    group_source_shortcuts,
    make_analysis_document,
    make_search_document,
    retry_search_network_errors,
    manifest_approval_hash,
    PREVIEW_CONTRACT_VERSION,
    UNKNOWN_ENGINE_LABEL,
    literal_original_name_lines,
    name_preview_lines,
    normalize_title_delimiters,
    normalize_engine_label,
    is_unknown_engine,
    original_name_lines,
    order_and_number_preview_entries,
    plan_series_layout,
    series_developer_from_children,
    series_developer_only_basename,
    render_semantic_preview_html,
    render_combined_semantic_preview_html,
    render_semantic_preview_markdown,
    round_preview_lines,
    search_reports_for_preview,
    sanitize_search_query,
    search_query_is_from_basename,
    sanitize_windows_component_text,
    stable_candidate_key,
    stable_game_id,
    strip_search_replacement_symbols,
    target_basename_fields,
    target_name_lines,
    series_target_basename_fields,
    series_target_name_lines,
    update_success_catalog,
    unresolved_candidates_for_preview,
    validate_user_supplements,
    write_folder_structure_preview,
    validate_local_semantics,
    validate_local_series_semantics,
    validate_remote_semantics,
    validate_search_audit,
    validate_combined_approval_manifest,
    validate_preview_contract_version,
)
from rename_pipeline.contracts import source_shortcut_metadata_tokens
from rename_pipeline.remote_cache import CachedSource, RemoteSearchCache
from rename_pipeline.review_packets import local_review_packet, remote_review_packet
from rename_pipeline.text_normalization import title_identity_key


VNDB_API = "https://api.vndb.org/kana"
DLSITE_BASE = "https://www.dlsite.com"
STEAM_STORE = "https://store.steampowered.com"
POSITIVE_PRIORITY = {"去码": 1, "步兵": 2, "官中": 3, "官中 去码": 4,
                     "官中步兵": 5, "官中步兵 去码": 6, "步兵 去码": 4}
NEGATIVE_ORDER = ("ENG", "生肉", "机翻", "ai汉化", "全年龄")
TRANSLATION_STATUS_TAGS = frozenset({"eng", "生肉", "机翻", "ai汉化"})
UNKNOWN_TRANSLATION_GROUPS = frozenset({"未知汉化组", "汉化组未知", "Unknown_CH"})
QUERY_FAILURE_CONTAINER = "~查询失败"
SERIES_HUB_CONTAINER = "~合集"


def series_hub_target_parent(root: Path, source: Path) -> Path:
    """已归集的多游戏父层内保留系列层级；其他系列进入根合集目录。"""
    hub = root / SERIES_HUB_CONTAINER
    try:
        relative = source.relative_to(hub)
    except ValueError:
        return hub
    return source.parent if len(relative.parts) > 1 else hub
ENGLISH_UNKNOWN_ENGINE_LABEL = "ENGINE_UNKNOWN"
# 全英文路径只对规范本地标签使用固定短别名，不自由翻译自定义标签。
ENGLISH_LOCAL_TAG_ALIASES = {
    "未知汉化组": "Unknown_CH",
    "汉化组未知": "Unknown_CH",
    "官中": "CH",
    "步兵": "Uncen",
    "官中步兵": "CH_Uncen",
    "去码": "Decen",
    "官中 去码": "CH Decen",
    "步兵 去码": "Uncen Decen",
    "官中步兵 去码": "CH_Uncen Decen",
    "生肉": "Raw",
    "机翻": "Machine_CH",
    "ai汉化": "ai_CH",
    "全年龄": "All_Ages",
    "本地全年龄": "Local_All_Ages",
    "VNDB全年龄": "VNDB_All_Ages",
    "DLsite全年龄": "DLsite_All_Ages",
    "Steam全年龄": "Steam_All_Ages",
}
REQUIRED_FIELDS = (
    ("engine", "开发引擎"),
    ("developer", "developer"),
)
TITLE_FIELDS = (
    ("jp_title", "日文名"),
    ("zh_title", "中文名"),
    ("en_title", "英文名"),
)
OPTIONAL_REPORT_FIELDS = (*TITLE_FIELDS, ("version", "版本号"))
IGNORED_EXE_PARTS = (
    "unitycrashhandler", "unins", "uninstall", "crashpad", "reporter", "vc_redist"
)
SUPPORT_DIRECTORY_NAMES = {
    "dat", "data", "engine", "mono", "monobleedingedge", "plugins",
    "resources", "save", "saves", "www", "存档",
}


def _direct_auxiliary_directory_facts(game_root: Path) -> list[dict[str, str]]:
    """只导出直属候选事实；Skill 决定是否真是独立存档或 CG 资料目录。"""
    excluded = {name.casefold() for name in SUPPORT_DIRECTORY_NAMES}
    facts: list[dict[str, str]] = []
    try:
        children = sorted(game_root.iterdir(), key=lambda path: path.name.casefold())
    except OSError:
        return facts
    for child in children:
        if not child.is_dir() or child.name.casefold() in excluded:
            continue
        lowered = child.name.casefold()
        kind = (
            "archive" if any(token in lowered for token in ("存档", "存檔", "archive"))
            else "cg" if "cg" in lowered or "gallery" in lowered else None
        )
        if kind:
            facts.append({
                "relative_path": child.name,
                "kind_hint": kind,
                "directory_fingerprint": directory_fingerprint(child),
            })
    return facts


def auxiliary_target_name(source_name: str, kind: str) -> str:
    """为直属资料目录加规范前缀，同时保留其原 basename。"""
    if kind not in {"archive", "cg"}:
        raise ValueError(f"未知附属目录类型：{kind}")
    prefix = "@存档" if kind == "archive" else "@CG"
    normalized_source = str(source_name).strip()
    if (
        normalized_source.casefold() == prefix.casefold()
        or normalized_source.casefold().startswith(prefix.casefold() + " ")
    ):
        return normalized_source
    retained = (
        normalized_source[1:] if normalized_source.startswith("@")
        else normalized_source
    ).strip()
    return prefix if not retained else f"{prefix} {retained}"


class ApprovalError(RuntimeError):
    """批准码与预览清单不一致。"""


class PreflightError(RuntimeError):
    """执行前状态验证失败。"""


@dataclass
class LocalMetadata:
    engine: str | None = None
    developer: str | None = None
    jp_title: str | None = None
    zh_title: str | None = None
    en_title: str | None = None
    version: str | None = None
    dlc: bool = False
    dlc_version: str | None = None
    dlc_range: str | None = None
    version_tags: tuple[str, ...] = ()
    translation_group: str | None = None
    positive_tag: str | None = None
    negative_tags: tuple[str, ...] = ()
    evidence: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class RemoteEntry:
    title: str
    url: str
    version: str | None = None
    positive_tag: str | None = None
    engine: str | None = None
    developer: str | None = None
    jp_title: str | None = None
    zh_title: str | None = None
    en_title: str | None = None
    source: str = ""
    source_id: str | None = None
    languages: tuple[str, ...] = ()
    remote_dlc: tuple[str, ...] = ()
    remote_tags: tuple[str, ...] = ()
    related_editions: tuple[str, ...] = ()
    audience_classification: str | None = None
    audience_evidence: tuple[str, ...] = ()
    release_year: int | None = None
    release_year_evidence: tuple[str, ...] = ()
    maximum_release_age: int | None = None
    release_pages: tuple[dict[str, Any], ...] = ()


def extract_search_keyword(title: str) -> str:
    """保留主标题；从第一个确认的副标题边界或末尾括号说明起删除。"""
    value = strip_search_replacement_symbols(title)
    value = re.sub(r"\s+[（(][^()（）]+[)）]\s*$", "", value).strip()
    boundaries = [
        match.start() for pattern in (
            r"[～〜~]", r"[：:]", r"\s+(?:-|—)\s+",
        ) if (match := re.search(pattern, value))
    ]
    if boundaries:
        value = value[:min(boundaries)].strip()
    return strip_search_replacement_symbols(value)


def normalize_subtitle_delimiters(title: str | None) -> str | None:
    """预计名称统一半角波浪线；不修改证据字段中的原文。"""
    return normalize_title_delimiters(title)


def normalize_metadata(metadata: LocalMetadata) -> LocalMetadata:
    """规范副标题分隔符和本地年龄来源，其余语义字段保留原字形。"""
    negative_tags = tuple(
        "本地全年龄" if tag == "全年龄" else tag
        for tag in metadata.negative_tags
    )
    normalized = replace(
        metadata,
        engine=normalize_engine_label(metadata.engine),
        jp_title=normalize_subtitle_delimiters(metadata.jp_title),
        zh_title=normalize_subtitle_delimiters(metadata.zh_title),
        en_title=normalize_subtitle_delimiters(metadata.en_title),
        negative_tags=negative_tags,
    )
    return suppress_unknown_group_for_ai_translation(normalized)


def tag_remote_all_ages(
    metadata: LocalMetadata, remote_semantics: Mapping[str, Any],
) -> LocalMetadata:
    """仅由已编译的年龄来源，为已采用全年龄版本生成可追溯标签。"""
    if remote_semantics.get("audience_classification") != "all_ages":
        return metadata
    audience_source = remote_semantics.get("audience_source") or {}
    source = audience_source.get("source")
    if source not in {"VNDB", "DLsite", "Steam"}:
        raise ValueError("全年龄证据缺少已采用的规范来源")
    negative_tags = tuple(
        tag for tag in metadata.negative_tags
        if tag not in {"全年龄", "本地全年龄"}
    )
    return replace(
        metadata, negative_tags=(*negative_tags, f"{source}全年龄"),
    )


def suppress_unknown_group_for_ai_translation(
    metadata: LocalMetadata,
) -> LocalMetadata:
    """明确翻译状态已存在时，不再保留未知汉化组占位。"""
    translation_statuses = {
        str(tag).lstrip("#").casefold() for tag in metadata.negative_tags
    } & TRANSLATION_STATUS_TAGS
    group = str(metadata.translation_group or "").lstrip("%")
    if group in UNKNOWN_TRANSLATION_GROUPS and translation_statuses:
        return replace(metadata, translation_group=None)
    return metadata


# 英文路径目标投影：只删除非 ASCII 字段并消费 Skill 已给出的英文补全。
def project_english_path_metadata(
    metadata: LocalMetadata,
    fallback: dict[str, Any] | None = None,
) -> tuple[LocalMetadata, list[str]]:
    """生成纯 ASCII 目标元数据；本函数绝不自行翻译。"""
    fallback = fallback or {}
    allowed_fallbacks = {"developer", "en_title"}
    unknown = set(fallback) - allowed_fallbacks
    if unknown:
        raise ValueError(
            "english_fallback 包含未知字段：" + "、".join(sorted(unknown))
        )
    for field_name, value in fallback.items():
        if not isinstance(value, str) or not value.strip() or not value.isascii():
            raise ValueError(f"english_fallback.{field_name} 必须是非空 ASCII 文本")

    removed: list[str] = []

    def ascii_value(field_name: str, value: str | None) -> str | None:
        if value and not value.isascii():
            removed.append(field_name)
            return None
        return value

    translated_group = ENGLISH_LOCAL_TAG_ALIASES.get(
        metadata.translation_group or ""
    )
    translated_positive = ENGLISH_LOCAL_TAG_ALIASES.get(
        metadata.positive_tag or ""
    )
    translated_negative = tuple(
        alias
        for tag in metadata.negative_tags
        if (
            alias := (
                tag if tag.isascii()
                else ENGLISH_LOCAL_TAG_ALIASES.get(tag)
            )
        )
    )
    projected_version_tags = tuple(
        value for value in metadata.version_tags if value.isascii()
    )
    unknown_group_suppressed = (
        translated_group == "Unknown_CH" and "ai_CH" in translated_negative
    )
    projected = replace(
        metadata,
        engine=(
            ENGLISH_UNKNOWN_ENGINE_LABEL
            if is_unknown_engine(metadata.engine)
            else ascii_value("engine", metadata.engine)
        ),
        developer=ascii_value("developer", metadata.developer),
        jp_title=ascii_value("jp_title", metadata.jp_title),
        zh_title=ascii_value("zh_title", metadata.zh_title),
        en_title=ascii_value("en_title", metadata.en_title),
        version=ascii_value("version", metadata.version),
        version_tags=projected_version_tags,
        translation_group=(
            metadata.translation_group
            if metadata.translation_group and metadata.translation_group.isascii()
            else translated_group
        ),
        positive_tag=(
            metadata.positive_tag
            if metadata.positive_tag and metadata.positive_tag.isascii()
            else translated_positive
        ),
        negative_tags=translated_negative,
    )
    projected = suppress_unknown_group_for_ai_translation(projected)
    if (
        metadata.translation_group
        and not projected.translation_group
        and not unknown_group_suppressed
    ):
        removed.append("translation_group")
    if metadata.positive_tag and not projected.positive_tag:
        removed.append("positive_tag")
    if len(translated_negative) != len(metadata.negative_tags):
        removed.append("negative_tags")
    if len(projected_version_tags) != len(metadata.version_tags):
        removed.append("version_tags")
    if not projected.developer and fallback.get("developer"):
        fallback_developer = f"{fallback['developer'].strip()} #eng"
        if "#uncheck" in str(metadata.developer or ""):
            fallback_developer += " #uncheck"
        projected = replace(
            projected, developer=fallback_developer,
        )
    if not any((projected.jp_title, projected.zh_title, projected.en_title)):
        if fallback.get("en_title"):
            projected = replace(projected, en_title=fallback["en_title"].strip())
    return projected, removed


@dataclass
class RemoteReferences:
    highest_version: RemoteEntry | None = None
    best_positive_tag: RemoteEntry | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class GameDetection:
    root: Path
    engine: str
    primary_exe: Path
    evidence: tuple[str, ...]


@dataclass
class SeriesContainer:
    """包含至少两个独立游戏的命名外层目录。"""
    root: Path
    children: list[GameDetection]


@dataclass
class PlanEntry:
    status: str
    source: Path
    target: Path | None
    marker_name: str | None
    metadata: LocalMetadata
    local_evidence: list[str]
    marker_metadata: LocalMetadata | None = None
    remote_highest: dict[str, Any] | None = None
    remote_best_tag: dict[str, Any] | None = None
    remote_other: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    source_fingerprint: str | None = None
    local_metadata: LocalMetadata | None = None
    selected_source: str | None = None
    selected_sources: list[str] = field(default_factory=list)
    field_sources: dict[str, str] = field(default_factory=dict)
    remote_searches: list[dict[str, Any]] = field(default_factory=list)
    source_shortcuts: list[dict[str, str]] = field(default_factory=list)
    developer_conflict: dict[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["source"] = str(self.source)
        data["target"] = str(self.target) if self.target else None
        return data


# 本地目录解析：规范格式精确解析，旧名称只保守提取。
def positive_tag_from_text(text: str) -> str | None:
    lowered = text.lower()
    has_official_chinese = bool(re.search(r"官中|官方中文", text, re.I))
    has_uncensored = bool(
        re.search(r"步兵|无码|無修|\buncen(?:sored)?\b", lowered, re.I)
    )
    has_demosaic = bool(
        re.search(r"去码|去碼|去马赛克|去馬賽克|demosaic", text, re.I)
    )
    return merge_positive_tags(
        "官中" if has_official_chinese else None,
        "步兵" if has_uncensored else None,
        "去码" if has_demosaic else None,
    )


def positive_tag_from_local_evidence(text: str) -> str | None:
    """只从明确描述游戏版本/标签的本地文本提取正面标签。

    普通 readme、广告或资源目录可能提到“无码动漫”等其他内容；这些泛用词不能
    证明当前游戏本身是步兵版。目录 basename 仍由 ``positive_tag_from_text``
    解析，这里只收紧递归读取的文本证据。
    """
    selected: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        direct = re.fullmatch(
            r"(?i)(?:标签|標籤|tag)?\s*[:：=]?\s*%?\s*"
            r"(官中步兵|官中|官方中文|步兵|去[码碼]|去[马馬]赛克|"
            r"uncen(?:sored)?|demosaic)\s*(?:版|版本)?\s*",
            line,
        )
        contextual = re.search(
            r"(?i)(?:本作|本游戏|本遊戲|游戏|遊戲|版本|补丁|補丁|标签|標籤)"
            r".{0,16}(?:官中步兵|官中|官方中文|步兵|无码|無修|"
            r"uncen(?:sored)?|去[码碼]|去[马馬]赛克|demosaic)",
            line,
        ) or re.search(
            r"(?i)(?:官中步兵|官中|官方中文|步兵|无码|無修|"
            r"uncen(?:sored)?|去[码碼]|去[马馬]赛克|demosaic)"
            r".{0,16}(?:版本|版|补丁|補丁|游戏|遊戲|本作|标签|標籤)",
            line,
        )
        if not (direct or contextual):
            continue
        candidate = positive_tag_from_text(line)
        selected = merge_positive_tags(selected, candidate)
    return selected


def _positive_tag_components(tag: str | None) -> set[str]:
    """把复合正面标签拆成可做交集的语义原子。"""
    components: set[str] = set()
    for value in (tag or "").split():
        if value == "官中步兵":
            components.update({"官中", "步兵"})
        elif value in {"官中", "步兵", "去码"}:
            components.add(value)
    return components


def merge_positive_tags(*tags: str | None) -> str | None:
    """把成员自身与合集表面标签做语义并集，再压回单一规范标签。"""
    merged: set[str] = set()
    for tag in tags:
        merged.update(_positive_tag_components(tag))
    result = []
    if "官中" in merged and "步兵" in merged:
        result.append("官中步兵")
    else:
        result.extend(tag for tag in ("官中", "步兵") if tag in merged)
    if "去码" in merged:
        result.append("去码")
    return " ".join(result) or None


def merge_negative_tags(*groups: Sequence[str]) -> tuple[str, ...]:
    """合并本地负面标签并保持规范顺序，不制造未知标签。"""
    values = list(dict.fromkeys(tag for group in groups for tag in group))
    return tuple(tag for tag in NEGATIVE_ORDER if tag in values) + tuple(
        tag for tag in values if tag not in NEGATIVE_ORDER
    )


def common_child_tags(
    child_fields: Sequence[dict[str, Any]],
) -> tuple[str | None, tuple[str, ...]]:
    """只汇总每个成员已审核标签的精确共同部分。"""
    if not child_fields:
        return None, ()
    positive_sets = [
        _positive_tag_components(fields.get("positive_tag"))
        for fields in child_fields
    ]
    common_positive = set.intersection(*positive_sets) if positive_sets else set()
    positive = merge_positive_tags(*sorted(common_positive))
    negative_sets = [
        set(fields.get("negative_tags") or ()) for fields in child_fields
    ]
    common_negative = set.intersection(*negative_sets) if negative_sets else set()
    return positive, merge_negative_tags(tuple(common_negative))


def common_series_titles(
    child_locals: Sequence[dict[str, Any]],
    child_remotes: Sequence[dict[str, Any]],
    *,
    allow_local_fallback: bool = True,
) -> dict[str, str | None]:
    """取成员共同标题；全员搜索成功时禁止用本地标题回填。"""
    result: dict[str, str | None] = {}
    for language in ("jp", "zh", "en"):
        remote_values = [
            str((item.get("series_common_titles") or {}).get(language) or "").strip()
            for item in child_remotes
        ]
        if any(remote_values):
            values = remote_values
        elif allow_local_fallback:
            values = [
                str((item.get("series_common_titles") or {}).get(language) or "").strip()
                for item in child_locals
            ]
        else:
            values = []
        nonempty = {value for value in values if value}
        result[language] = next(iter(nonempty)) if len(nonempty) == 1 else None
    return result


def _series_title_stem(metadata: LocalMetadata) -> str | None:
    title = metadata.zh_title or metadata.jp_title or metadata.en_title
    if not title:
        return None
    keyword = extract_search_keyword(title)
    return re.sub(r"(?:\s*[-_]?\s*(?:\d+|[IVX]+))\s*$", "", keyword, flags=re.I).strip()


def compose_series_name(
    original_name: str, children: Sequence[LocalMetadata]
) -> str | None:
    """合集不查远程；共同标题后固定追加“合集”及可选显式范围。"""
    surface = parse_local_metadata(original_name)
    child_developers = {
        _match_key(item.developer): item.developer
        for item in children if item.developer
    }
    if len(child_developers) > 1:
        return None
    if (
        surface.developer and child_developers
        and _match_key(surface.developer) not in child_developers
    ):
        return None
    developer = surface.developer or next(iter(child_developers.values()), None)
    if not developer:
        return None
    body = re.sub(r"^\s*\[[^\]]+\]\s*", "", original_name).strip()
    body = re.split(
        r"(?i)\s+(?:v(?:er(?:sion)?)?\.?\s*[0-9]|\+?\s*DLC\b|[%#])",
        body, maxsplit=1,
    )[0].strip()
    body = re.sub(
        r"(?:\s*%?\s*(?:官中步兵|官方中文|官中|步兵|去[码碼]|去马赛克|去馬賽克))\s*$",
        "", body, flags=re.I,
    ).strip()
    body = re.sub(
        r"\s+[A-Za-z0-9\u3400-\u9fffぁ-ゖァ-ヺー_-]{2,}"
        r"(?:汉化组|漢化組|翻译组|翻譯組)\s*$",
        "", body, flags=re.I,
    ).strip()
    range_match = re.search(
        r"(?i)(?:\d+|[IVX]+)\s*[~～〜]\s*(?:\d+|[IVX]+)\s*$", body,
    )
    if range_match:
        range_value = range_match.group(0).replace("～", "~").replace("〜", "~")
        range_value = range_value.replace(" ", "")
        title = re.sub(r"\s*(?:合集|系列)?\s*$", "", body[:range_match.start()]).strip()
        title = f"{title} 合集{range_value}"
    else:
        title = re.sub(r"\s*(?:合集|系列)\s*$", "", body).strip()
        if not title:
            return None
        title = f"{title} 合集"
    return f"[{developer}] {title}"


def negative_tags_from_text(text: str) -> tuple[str, ...]:
    explicit = re.findall(r"#([^\s#%\]\[()（）【】]+)", text)
    untagged = re.sub(r"#([^\s#%\]\[()（）【】]+)", " ", text)
    legacy = tuple(
        tag for tag in NEGATIVE_ORDER
        if (
            (tag == "ENG" and re.search(r"(?:^|[%\s])ENG(?:$|\s)", untagged, re.I))
            or (tag == "生肉" and re.search(r"(?:^|[%\s])生肉(?:$|\s)", untagged))
            or (tag == "机翻" and re.search(r"机翻|機翻", untagged))
            or (tag == "ai汉化" and re.search(r"AI\s*(?:汉化|漢化|翻译|翻譯)", untagged, re.I))
            or (tag == "全年龄" and re.search(r"全年[龄齢]", untagged))
        )
    )
    canonical_explicit = {
        "eng": "ENG", "生肉": "生肉", "机翻": "机翻", "機翻": "机翻",
        "ai汉化": "ai汉化", "ai漢化": "ai汉化",
        "全年龄": "全年龄", "全年齢": "全年龄",
    }
    return merge_negative_tags(
        legacy,
        tuple(canonical_explicit.get(tag.casefold(), tag) for tag in explicit),
    )


def extract_version(text: str) -> str | None:
    match = re.search(
        r"(?i)(?<!@)(?:v(?:er(?:sion)?)?\.?)[\s._-]*"
        r"([0-9]+(?:[._-][0-9A-Za-z]+)*)",
        text,
    )
    if match:
        return match.group(1).replace("_", ".")
    # Bare dotted build numbers are common immediately before local metadata.
    # At least one separator is required so sequel numbers such as "Game 2"
    # remain part of the title.
    bare_match = re.search(
        r"(?i)(?<![A-Za-z0-9])"
        r"([0-9]+(?:[._-][0-9A-Za-z]+)+)"
        r"(?=\s*(?:\+?\s*DLC(?:\d+(?:\s*[-~]\s*\d+)?)?\b|"
        r"官中步兵|官方中文|官中|步兵|去[码碼]|去马赛克|去馬賽克|"
        r"[A-Za-z0-9\u3400-\u9fffぁ-ゖァ-ヺー_-]{2,}(?:汉化组|漢化組|汉化|漢化)|$))",
        text,
    )
    return bare_match.group(1).replace("_", ".") if bare_match else None


def _contains_kana(text: str) -> bool:
    return bool(re.search(r"[ぁ-ゖァ-ヺー]", text))


def _contains_han(text: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", text))


def _looks_english(text: str) -> bool:
    return bool(re.search(r"[A-Za-z]", text)) and not _contains_kana(text) and not _contains_han(text)


def _clean_old_title_text(text: str) -> str:
    text = re.sub(
        r"(?i)\b(?:v(?:er(?:sion)?)?\.?)\s*[0-9][0-9A-Za-z._-]*", " ", text
    )
    text = re.sub(r"(?i)\+?\s*DLC\b", " ", text)
    text = re.sub(
        r"(?:官中步兵|官中|官方中文|步兵|去码|去碼|生肉|机翻|機翻|"
        r"AI\s*(?:汉化|漢化|翻译|翻譯)|全年[龄齢]|(?<![A-Za-z])ENG(?![A-Za-z])|"
        r"体验版|體驗版)", " ", text,
        flags=re.I,
    )
    text = re.sub(r"[#%][^\s#%]+", " ", text)
    return re.sub(r"\s+", " ", text).strip(" -_[]")


def _parse_loose_titles(text: str) -> tuple[str | None, str | None, str | None]:
    tokens = _clean_old_title_text(text).split()
    if not tokens:
        return None, None, None
    kana_indexes = [index for index, token in enumerate(tokens) if _contains_kana(token)]
    if kana_indexes:
        boundary = kana_indexes[0]
        zh_segment = " ".join(tokens[:boundary]).strip()
        jp_segment = " ".join(tokens[boundary:]).strip()
        zh_colon = re.fullmatch(r"(.+?)[：:]\s*(.+)", zh_segment)
        jp_colon = re.fullmatch(r"(.+?)[：:]\s*(.+)", jp_segment)
        if (
            zh_colon
            and jp_colon
            and zh_colon.group(2).casefold() == jp_colon.group(2).casefold()
            and _contains_han(zh_colon.group(1))
            and _contains_kana(jp_colon.group(1))
            and _looks_english(zh_colon.group(2))
        ):
            return (
                jp_colon.group(1).strip(),
                zh_colon.group(1).strip(),
                zh_colon.group(2).strip(),
            )
    english_tokens: list[str] = []
    while tokens and _looks_english(tokens[-1]):
        english_tokens.insert(0, tokens.pop())
    en_title = " ".join(english_tokens).strip() or None
    remaining = " ".join(tokens).strip()
    jp_title = None
    zh_title = None
    if _contains_kana(remaining):
        parts = remaining.split()
        indexes = [index for index, part in enumerate(parts) if _contains_kana(part)]
        start = indexes[0]
        jp_title = " ".join(parts[start:]).strip() or None
        zh_candidate = " ".join(parts[:start]).strip()
        if zh_candidate and _contains_han(zh_candidate) and not _contains_kana(zh_candidate):
            zh_title = zh_candidate
    elif remaining and _contains_han(remaining):
        zh_title = remaining
    return jp_title, zh_title, en_title


def parse_local_metadata(
    folder_name: str,
    evidence_texts: Sequence[str] = (),
    ancestor_names: Sequence[str] = (),
    series_positive_tag: str | None = None,
    series_negative_tags: Sequence[str] = (),
    tag_evidence_texts: Sequence[str] | None = None,
) -> LocalMetadata:
    developer_match = re.match(r"^\[([^\]]+)\]\s*", folder_name)
    developer = developer_match.group(1).strip() if developer_match else None
    if developer is None:
        for ancestor_name in ancestor_names:
            ancestor_match = re.match(r"^\[([^\]]+)\]\s*", ancestor_name)
            if ancestor_match:
                developer = ancestor_match.group(1).strip()
                break
    remainder = folder_name[developer_match.end():] if developer_match else folder_name
    facts = extract_local_facts(
        folder_name,
        evidence_texts,
        series_positive_tag=series_positive_tag,
        series_negative_tags=series_negative_tags,
        tag_evidence_texts=tag_evidence_texts,
    )

    title_area = remainder
    version_match = re.search(
        r"(?i)(?:^|\s)(?:v(?:er(?:sion)?)?\.?)\s*[0-9]", title_area
    )
    if version_match:
        title_area = title_area[:version_match.start()].rstrip()
    title_area = re.split(r"\s+[%#]", title_area, maxsplit=1)[0].strip()
    title_area = re.sub(
        r"(?i)\s*(?:\+\s*)?\bDLC\b\s*$", "", title_area
    ).strip()
    for version_tag in facts.version_tags:
        pattern = re.escape(version_tag)
        if version_tag.startswith("+"):
            pattern = pattern.replace(r"\~", r"\s*(?:-|~|～|〜)\s*")
        else:
            pattern = rf"\$?{pattern}"
        title_area = re.sub(pattern, "", title_area, flags=re.I).strip()
    parts = re.split(r"\s*=\s*", title_area)
    if len(parts) >= 3:
        jp_title, zh_title, en_title = (part.strip() or None for part in parts[:3])
    else:
        jp_title, zh_title, en_title = _parse_loose_titles(title_area)
    return replace(
        facts,
        developer=developer,
        jp_title=jp_title,
        zh_title=zh_title,
        en_title=en_title,
    )


def extract_local_facts(
    folder_name: str,
    evidence_texts: Sequence[str] = (),
    series_positive_tag: str | None = None,
    series_negative_tags: Sequence[str] = (),
    tag_evidence_texts: Sequence[str] | None = None,
) -> LocalMetadata:
    """只提取可重复本地事实，不判断 developer、语言或标题。"""
    remainder = re.sub(r"^\s*\[[^\]]+\]\s*", "", folder_name).strip()
    tag_evidence = (
        evidence_texts if tag_evidence_texts is None else tag_evidence_texts
    )
    local_text_positive = merge_positive_tags(*(
        positive_tag_from_local_evidence(value) for value in tag_evidence
    ))
    version = extract_version(folder_name)
    version_evidence = "本地目录名"
    # Enumerated bundles such as DLC1-5 are DLC evidence too.  The Unicode
    # letter guard still rejects ordinary words that merely start with DLC.
    dlc = bool(re.search(
        r"(?i)(?<![^\W_])(?:\+\s*)?DLC(?=$|[\W_]|v|\d)", folder_name
    ))
    dlc_evidence = "本地目录名"
    dlc_version_match = re.search(
        r"(?i)(?<![A-Za-z])(?:\+\s*)?DLC\s*"
        r"(?:v(?:er(?:sion)?)?\.?)\s*"
        r"([0-9]+(?:[._-][0-9A-Za-z]+)*)",
        folder_name,
    )
    dlc_version = (
        dlc_version_match.group(1).replace("_", ".")
        if dlc_version_match else None
    )
    dlc_range_match = re.search(
        r"(?i)(?<![A-Za-z])(?:\+\s*)?DLC\s*"
        r"((?:\d+|[IVX]+)\s*(?:-|~|～|〜)\s*(?:\d+|[IVX]+))",
        folder_name,
    )
    dlc_range = (
        re.sub(r"\s*(?:-|~|～|〜)\s*", "~", dlc_range_match.group(1))
        if dlc_range_match else None
    )
    version_tags: list[str] = []
    # 版本附加标签只从目录名中的明确字面证据产生。重制/重置类标签
    # 保留原字形；+AP01-05 等通用附加包范围统一为 +AP01~05。
    for match in re.finditer(
        r"(?i)(?:重置版|重制版|重製版|リメイク版|remake(?:\s+edition)?)",
        folder_name,
    ):
        value = match.group(0)
        if value not in version_tags:
            version_tags.append(value)
    for match in re.finditer(
        r"(?i)(?<![A-Za-z0-9])Alpha\s*\d+(?:\.\d+)*(?![A-Za-z0-9])|"
        r"体验版|體驗版|DL版|Steam版|完全版",
        folder_name,
    ):
        value = match.group(0)
        if value not in version_tags:
            version_tags.append(value)
    for match in re.finditer(
        r"(?i)(?<![0-9A-Za-z])\+\s*([A-Za-z]{1,8})\s*"
        r"((?:\d+|[IVX]+)\s*(?:-|~|～|〜)\s*(?:\d+|[IVX]+))",
        folder_name,
    ):
        label = match.group(1)
        range_value = re.sub(r"\s*(?:-|~|～|〜)\s*", "~", match.group(2))
        value = f"+{label}{range_value}"
        if value.casefold() not in {item.casefold() for item in version_tags}:
            version_tags.append(value)
    if version is None:
        for evidence_text in evidence_texts:
            explicit_version = re.search(
                r"(?im)^\s*(?:game\s*version|游戏版本|遊戲版本)\s*[:：=]\s*"
                r"v?([0-9]+(?:[._-][0-9A-Za-z]+)*)\s*$",
                evidence_text,
            )
            if explicit_version:
                version = explicit_version.group(1).replace("_", ".")
                version_evidence = "本地说明文本：显式版本标签"
                break
    if not dlc:
        for evidence_text in evidence_texts:
            if re.search(
                r"(?im)^\s*DLC\s*[:：=]\s*(?:included|yes|true|包含|有)\s*$",
                evidence_text,
            ):
                dlc = True
                dlc_evidence = "本地说明文本：显式 DLC 标签"
                break
    local_positive = merge_positive_tags(
        positive_tag_from_text(remainder), local_text_positive
    )
    positive = merge_positive_tags(local_positive, series_positive_tag)
    positive_evidence = (
        "本地目录名" if positive_tag_from_text(remainder) else "本地说明文本：明确标签语境"
    )
    if series_positive_tag and not local_positive:
        positive_evidence = "合集表面"
    elif series_positive_tag and local_positive:
        positive_evidence = "本地目录名/文件与合集表面语义并集"
    tag_text = "\n".join((remainder, *tag_evidence))
    negative = merge_negative_tags(
        negative_tags_from_text(tag_text), series_negative_tags
    )
    percent_tokens = re.findall(r"%([^\s#%\]\[()（）【】]+)", folder_name)
    translation_group = next(
        (token for token in percent_tokens if token not in POSITIVE_PRIORITY), None
    )
    if translation_group is None:
        group_match = re.search(
            r"([A-Za-z0-9\u3400-\u9fffぁ-ゖァ-ヺー_-]{2,}"
            r"(?:汉化组|漢化組|翻译组|翻譯組))",
            tag_text,
        )
        if group_match:
            translation_group = group_match.group(1)

    evidence: dict[str, list[str]] = {}
    if version:
        evidence["version"] = [version_evidence]
    if dlc:
        evidence["dlc"] = [dlc_evidence]
    if dlc_version:
        evidence["dlc_version"] = ["本地目录名"]
    if dlc_range:
        evidence["dlc_range"] = ["本地目录名"]
    if version_tags:
        evidence["version_tags"] = ["本地目录名"]
    if positive:
        evidence["positive_tag"] = [positive_evidence]
    if translation_group:
        evidence["translation_group"] = [
            "本地目录名" if translation_group in percent_tokens else "本地说明文本"
        ]
    if negative:
        evidence["negative_tags"] = [
            f"{'本地目录名' if tag in negative_tags_from_text(remainder) else '本地说明文本'}：#{tag}"
            for tag in negative
        ]
        for tag in series_negative_tags:
            if tag in negative:
                evidence["negative_tags"].append(f"合集表面：#{tag}")
    return LocalMetadata(
        version=version, dlc=dlc, dlc_version=dlc_version,
        dlc_range=dlc_range, version_tags=tuple(version_tags),
        translation_group=translation_group,
        positive_tag=positive, negative_tags=negative, evidence=evidence,
    )


def apply_translation_group_rule(
    metadata: LocalMetadata,
) -> tuple[LocalMetadata, list[str]]:
    original_group = metadata.translation_group
    metadata = suppress_unknown_group_for_ai_translation(metadata)
    translation_statuses = {
        str(tag).lstrip("#").casefold() for tag in metadata.negative_tags
    } & TRANSLATION_STATUS_TAGS
    if translation_statuses:
        notes = (
            ["检测到明确负面/翻译状态标签，省略通用未知标签"]
            if original_group and not metadata.translation_group else []
        )
        return metadata, notes
    if "官中" in _positive_tag_components(metadata.positive_tag) or metadata.translation_group:
        return metadata, []
    return replace(metadata, translation_group="未知汉化组"), [
        "本地未发现官中、汉化组或翻译状态证据，补入%未知汉化组"
    ]


def apply_standalone_dlc_title(
    metadata: LocalMetadata, language: str | None,
) -> LocalMetadata:
    """把独立 DLC 身份写入指定语言标题，避免末尾元数据重复 +DLC。"""
    if not language:
        return metadata
    field_name = f"{language}_title"
    title = getattr(metadata, field_name, None)
    if not title:
        return metadata
    if re.search(r"(?i)(?<![A-Za-z])DLC(?![A-Za-z])", title):
        return metadata
    return replace(metadata, **{field_name: f"{title} DLC"})


def merge_metadata(local: LocalMetadata, remote: RemoteEntry) -> LocalMetadata:
    # 远程只补 developer 与三语标题；引擎、版本和标签始终保持本地来源。
    return replace(
        local,
        developer=local.developer or remote.developer,
        jp_title=local.jp_title or remote.jp_title,
        zh_title=local.zh_title or remote.zh_title,
        en_title=local.en_title or remote.en_title,
    )


def merge_remote_consensus(
    local: LocalMetadata, remote_entries: Sequence[RemoteEntry]
) -> tuple[LocalMetadata, list[str]]:
    """官方商店优先，VNDB 补缺；任一现有语种标题匹配即可确认同游戏。"""
    merged = local
    notes: list[str] = []
    local_titles = [
        value for value in (local.jp_title, local.zh_title, local.en_title) if value
    ]
    for entry in remote_entries:
        remote_titles = [
            value for value in (
                entry.jp_title, entry.zh_title, entry.en_title, entry.title
            ) if value
        ]
        if local_titles and not any(
            _title_matches(local_title, remote_title)
            for local_title in local_titles
            for remote_title in remote_titles
        ):
            notes.append(f"{entry.source} 标题无任一匹配：{entry.title}")

    strict_fields = (("developer", "developer"),)
    title_fields = (
        ("jp_title", "日文名"),
        ("zh_title", "中文名"),
        ("en_title", "英文名"),
    )
    for field_name, label in (*strict_fields, *title_fields):
        local_value = getattr(merged, field_name)
        if field_name.endswith("_title") and local_value:
            continue
        valued_entries = [
            (entry, str(value).strip())
            for entry in remote_entries
            if (value := getattr(entry, field_name))
        ]
        official = [
            (entry, value) for entry, value in valued_entries
            if entry.source in {"DLsite", "Steam"}
        ]
        selected = official or valued_entries
        values = [value for _, value in selected]
        if not values:
            continue
        normalized: dict[str, str] = {}
        for value in values:
            key = _match_key(value)
            normalized.setdefault(str(key).casefold(), value)
        if len(normalized) > 1:
            if official:
                notes.append(
                    f"官方来源 {label} 冲突：" + " / ".join(normalized.values())
                )
                continue
            else:
                sources = {entry.source for entry, _ in selected}
                prefix = next(iter(sources)) if len(sources) == 1 else "远程来源"
            notes.append(f"{prefix} {label}候选冲突：" + " / ".join(normalized.values()))
            continue
        remote_value = next(iter(normalized.values()))
        if field_name.endswith("_title") and not local_value:
            existing_title_keys = {
                _match_key(str(getattr(merged, other_field)))
                for other_field, _ in TITLE_FIELDS
                if other_field != field_name and getattr(merged, other_field)
            }
            if _match_key(remote_value) in existing_title_keys:
                continue
        if local_value:
            local_key = _match_key(str(local_value))
            remote_key = _match_key(remote_value)
            if str(local_key).casefold() != str(remote_key).casefold():
                notes.append(
                    f"{label} 冲突：本地 {local_value}，远程 {remote_value}"
                )
                if field_name == "developer":
                    merged = replace(merged, developer=remote_value)
        else:
            merged = replace(merged, **{field_name: remote_value})
    return merged, notes


# 引擎识别与递归边界：只检查当前层和启动器紧邻的一层引擎目录。
def _direct_engine_evidence(directory: Path) -> tuple[str | None, list[str]]:
    try:
        names = {item.name.lower(): item for item in directory.iterdir()}
    except OSError:
        return None, []
    if "unityplayer.dll" in names or "gameassembly.dll" in names:
        evidence = [
            names[marker].name for marker in ("unityplayer.dll", "gameassembly.dll")
            if marker in names
        ]
        return "UNITY", evidence
    for item in names.values():
        if item.is_dir() and item.name.lower().endswith("_data"):
            if (item / "globalgamemanagers").exists() or (item / "Managed").exists():
                return "UNITY", [f"{item.name}/"]
    if "data.win" in names:
        return "GAMEMAKER", [names["data.win"].name]
    if "engine" in names and names["engine"].is_dir():
        return "UNREAL", ["Engine/"]
    if "www" in names and (names["www"] / "data").is_dir():
        return "RPGMAKER", ["www/data/"]
    suffixes = {item.suffix.lower() for item in names.values() if item.is_file()}
    if suffixes & {".rvdata", ".rvdata2", ".rgss3a"}:
        return "RPGMAKER", ["RPG Maker 数据文件"]
    if ".rpa" in suffixes:
        return "RENPY", ["Ren'Py .rpa"]
    if ".xp3" in suffixes:
        return "KIRIKIRI", ["KiriKiri .xp3"]
    if ".wolf" in suffixes:
        return "WOLF", ["Wolf RPG .wolf"]
    if ".swf" in suffixes:
        return "FLASH", ["Flash .swf"]
    return None, []


def _choose_primary_exe(directory: Path) -> Path | None:
    try:
        exes = [
            item for item in directory.iterdir()
            if item.is_file()
            and item.suffix.lower() == ".exe"
            and not any(part in item.name.lower() for part in IGNORED_EXE_PARTS)
        ]
    except OSError:
        return None
    if not exes:
        return None
    non_launcher = [item for item in exes if item.stem.lower() != "launcher"]
    return sorted(non_launcher or exes, key=lambda item: (len(item.name), item.name.lower()))[0]


def _choose_primary_swf(directory: Path) -> Path | None:
    """Flash 合集中的作品可能只有 swf，没有独立 exe。"""
    try:
        values = [
            item for item in directory.iterdir()
            if item.is_file() and item.suffix.lower() == ".swf"
        ]
    except OSError:
        return None
    return sorted(values, key=lambda item: (len(item.name), item.name.lower()))[0] if values else None


def _is_support_directory(directory: Path) -> bool:
    """排除资源、存档和备份树，避免把内部媒体误认成独立游戏。"""
    name = directory.name.strip().casefold()
    return (
        name in SUPPORT_DIRECTORY_NAMES
        or name.endswith("_data")
        or "backup" in name
        or "备份" in name
    )


def detect_game(directory: Path) -> GameDetection | None:
    directory = directory.resolve()
    engine, evidence = _direct_engine_evidence(directory)
    exe = _choose_primary_exe(directory)
    primary = exe or (_choose_primary_swf(directory) if engine == "FLASH" else None)
    if engine and primary:
        return GameDetection(directory, engine, primary.resolve(), tuple(evidence))
    launcher = directory / "Launcher.exe"
    if launcher.is_file():
        try:
            children = [item for item in directory.iterdir() if item.is_dir()]
        except OSError:
            children = []
        for child in children:
            child_engine, child_evidence = _direct_engine_evidence(child)
            child_exe = _choose_primary_exe(child)
            if child_engine and child_exe:
                return GameDetection(
                    directory, child_engine, child_exe.resolve(),
                    tuple(f"{child.name}/{value}" for value in child_evidence),
                )
    return None


def _detect_named_surface_wrapper(directory: Path) -> GameDetection | None:
    """外层已有 developer/标题且仅含一个直属游戏时，以外层作为游戏根。"""
    metadata = parse_local_metadata(directory.name)
    if not metadata.developer or not any(
        getattr(metadata, field_name) for field_name, _ in TITLE_FIELDS
    ):
        return None
    try:
        children = [item for item in directory.iterdir() if item.is_dir()]
    except OSError:
        return None
    detections = [value for child in children if (value := detect_game(child))]
    if len(detections) != 1:
        return None
    child = detections[0]
    return GameDetection(
        directory.resolve(), child.engine, child.primary_exe,
        tuple(
            f"{child.root.name}/{value}" for value in child.evidence
        ),
    )


def _detect_unknown_game(directory: Path) -> GameDetection | None:
    """仅以主 exe、目录 developer/标题和非多游戏容器识别未知引擎根。"""
    exe = _choose_primary_exe(directory)
    if not exe:
        return None
    metadata = parse_local_metadata(directory.name)
    if not metadata.developer or not any(
        getattr(metadata, field_name) for field_name, _ in TITLE_FIELDS
    ):
        return None
    try:
        children = [item for item in directory.iterdir() if item.is_dir()]
    except OSError:
        children = []
    child_games = 0
    for child in children:
        child_metadata = parse_local_metadata(child.name)
        if detect_game(child) or (
            _choose_primary_exe(child)
            and child_metadata.developer
            and any(getattr(child_metadata, field_name) for field_name, _ in TITLE_FIELDS)
        ):
            child_games += 1
            if child_games >= 2:
                return None
    return GameDetection(
        directory.resolve(), UNKNOWN_ENGINE_LABEL, exe.resolve(),
        (f"主程序：{exe.name}",)
    )


def discover_games(
    root: Path, include_paths: Sequence[Path] | None = None
) -> list[GameDetection]:
    root = root.resolve()
    found: list[GameDetection] = []

    def visit(directory: Path, container_context: bool = False) -> None:
        try:
            children = sorted(
                (
                    item for item in directory.iterdir()
                    if item.is_dir() and not item.is_symlink()
                ),
                key=lambda item: item.name.casefold(),
            )
        except OSError:
            children = []
        # 多个直属可运行游戏优先证明外层是容器，即使外层残留启动器或引擎文件。
        candidate_count = 0
        for child in children:
            if _is_support_directory(child):
                continue
            if detect_game(child) is not None:
                candidate_count += 1
                continue
            try:
                grandchildren = [
                    item for item in child.iterdir()
                    if item.is_dir() and not item.is_symlink()
                ]
            except OSError:
                grandchildren = []
            candidate_count += sum(
                detect_game(grandchild) is not None
                for grandchild in grandchildren
            )
        if candidate_count >= 2:
            for child in children:
                if _is_support_directory(child):
                    continue
                visit(child, container_context=True)
            return
        detection = detect_game(directory)
        if detection:
            found.append(detection)
            return
        surface_wrapper = _detect_named_surface_wrapper(directory)
        if surface_wrapper:
            found.append(surface_wrapper)
            return
        unknown = _detect_unknown_game(directory)
        if unknown:
            found.append(unknown)
            return
        if container_context:
            primary = _choose_primary_exe(directory) or _choose_primary_swf(directory)
            if primary:
                found.append(GameDetection(
                    directory.resolve(),
                    "FLASH"
                    if primary.suffix.lower() == ".swf"
                    else UNKNOWN_ENGINE_LABEL,
                    primary.resolve(),
                    (f"合集子游戏启动文件：{primary.name}",),
                ))
                return
        for child in children:
            if _is_support_directory(child):
                continue
            visit(child, container_context=container_context)

    starts: list[Path] = []
    if include_paths:
        for relative in include_paths:
            if relative.is_absolute():
                raise ValueError(f"include-path 必须相对扫描根目录：{relative}")
            selected = (root / relative).resolve()
            try:
                selected.relative_to(root)
            except ValueError as exc:
                raise ValueError(
                    f"include-path 不得离开扫描根目录：{relative}"
                ) from exc
            if not selected.is_dir():
                raise ValueError(f"include-path 不是现有目录：{relative}")
            starts.append(selected)
    else:
        starts.append(root)
    for start in starts:
        visit(start)
    unique = {item.root: item for item in found}
    return [unique[path] for path in sorted(unique, key=lambda value: str(value).casefold())]


def discover_series_containers(
    root: Path, detections: Sequence[GameDetection]
) -> list[SeriesContainer]:
    """找出最近的命名共同父目录；合集本身不作为远程查询对象。"""
    def is_descriptive_container(directory: Path) -> bool:
        """只判定容器是否有描述性名称，不推断 developer 或标题语言。"""
        name = directory.name.strip()
        if name.startswith("~"):
            return False
        normalized = re.sub(r"[\W_]+", "", name, flags=re.UNICODE).casefold()
        if normalized in {"game", "games", "group", "bundle", "合集", "系列"}:
            return False
        if re.fullmatch(r"(?i)(?:#?\d+|part\s*\d+)", name):
            return False
        return sum(character.isalpha() for character in name) >= 2

    root = root.resolve()
    grouped: dict[Path, list[GameDetection]] = {}
    for detection in detections:
        current = detection.root.parent.resolve()
        while current != root and _inside(current, root):
            grouped.setdefault(current, []).append(detection)
            current = current.parent
    candidates: list[SeriesContainer] = []
    for directory, children in grouped.items():
        unique = {item.root: item for item in children}
        if len(unique) < 2:
            continue
        if not is_descriptive_container(directory):
            continue
        # 只选最靠近游戏的容器，避免同一批子游戏被多层重复规划。
        if any(
            other != directory
            and directory in other.parents
            and len({item.root for item in other_children}) >= 2
            and is_descriptive_container(other)
            for other, other_children in grouped.items()
        ):
            continue
        candidates.append(SeriesContainer(
            directory, sorted(unique.values(), key=lambda item: str(item.root).casefold())
        ))
    return sorted(candidates, key=lambda item: str(item.root).casefold())


def is_english_exe_path(
    game_root: Path,
    primary_exe: Path,
    scan_root: Path | None = None,
) -> bool:
    anchor = scan_root or game_root.parent
    try:
        relative = primary_exe.resolve().relative_to(anchor.resolve())
    except ValueError:
        return False
    parts = relative.parts
    illegal = set('<>:"/\\|?*')
    return (
        all(
            part.isascii()
            and part
            and all(32 <= ord(character) <= 126 and character not in illegal for character in part)
            for part in parts
        )
        and any(re.search(r"[A-Za-z]", part) for part in parts)
    )


# 多来源元数据：优先使用本地显式 ID，其次才按现有日中英标题查询。
def extract_source_ids(
    folder_name: str, evidence_texts: Sequence[str],
    *, file_names: Sequence[str] = (),
) -> dict[str, list[str]]:
    combined = "\n".join((folder_name, *file_names, *evidence_texts))
    # 文件名中的来源标记和文本中的明确 ID 字段是本地身份；
    # readme/credit 中引用的其他 DLsite 商品 URL 不代表当前游戏。
    dlsite = re.findall(
        r"(?i)\b(?:RJ|VJ|BJ|EJ)\d{6,8}\b",
        "\n".join((folder_name, *file_names)),
    )
    for evidence in evidence_texts:
        dlsite.extend(re.findall(
            r"(?i)(?<![A-Za-z0-9_])dlsite\s*(?:work\s*|product\s*)?"
            r"(?:id)?\s*[:=]\s*((?:RJ|VJ|BJ|EJ)\d{6,8})\b",
            evidence,
        ))
    # 账户 steamid 不是游戏 AppID；数字结束边界也禁止截取 SteamID64 前缀。
    steam = re.findall(
        r"(?i)(?<![A-Za-z0-9_])"
        r"(?:store\.steampowered\.com/app/|steam[\s_-]*appid\s*[:=]\s*)"
        r"(\d{3,10})(?![A-Za-z0-9])",
        combined,
    )
    vndb = re.findall(
        r"(?i)(?:vndb(?:\.org/|\s*[:=]\s*))(?P<id>v\d+)\b", combined
    )
    # “@v1234” is a common local VNDB identity marker. It remains untrusted
    # evidence: title search is still required without a fingerprint-bound seed.
    vndb.extend(re.findall(r"(?i)(?<![A-Za-z0-9])@(?P<id>v\d+)\b", combined))

    def unique(values: Sequence[str], upper: bool = False) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            normalized = value.upper() if upper else value.lower()
            if normalized not in seen:
                seen.add(normalized)
                result.append(normalized)
        return result

    return {
        "dlsite": unique(dlsite, upper=True),
        "steam": unique(steam),
        "vndb": unique(vndb),
    }


def _language_title_fields(
    title: str, language: str
) -> dict[str, str | None]:
    result = {"jp_title": None, "zh_title": None, "en_title": None}
    lowered = language.lower()
    if (
        lowered.startswith(("ja", "japanese"))
        and (_contains_kana(title) or _contains_han(title))
    ):
        result["jp_title"] = title
    elif (
        lowered.startswith(("zh", "schinese", "tchinese"))
        and _contains_han(title)
        and not _contains_kana(title)
    ):
        result["zh_title"] = title
    elif lowered.startswith(("en", "english")) and _looks_english(title):
        result["en_title"] = title
    else:
        result.update(_remote_titles(title))
    return result


def _has_standalone_dlc_identity(value: str) -> bool:
    """只把独立的 DLC 标记视为作品身份，避免普通单词片段误判。"""
    return bool(re.search(r"(?<![A-Za-z0-9])DLC(?![A-Za-z0-9])", value, re.I))


def _title_match_score(first: str, second: str) -> int:
    if _has_standalone_dlc_identity(first) != _has_standalone_dlc_identity(second):
        return 0
    first_key = _match_key(first)
    second_key = _match_key(second)
    if not first_key or not second_key:
        return 0
    if first_key == second_key:
        return 100_000 + len(first_key)
    if first_key in second_key or second_key in first_key:
        shorter, longer = sorted((len(first_key), len(second_key)))
        return 10_000 + shorter * 100 - (longer - shorter)
    return 0


def best_title_matches(
    titles: Sequence[str], entries: Sequence[RemoteEntry],
    developer: str | None = None,
) -> list[RemoteEntry]:
    """保留标题匹配候选供 Skill 审核；本地制作方不筛选搜索结果。"""
    scored: list[tuple[int, RemoteEntry]] = []
    for entry in entries:
        candidates = (
            entry.title, entry.jp_title, entry.zh_title, entry.en_title
        )
        score = max(
            (
                _title_match_score(title, candidate)
                for title in titles
                for candidate in candidates
                if candidate
            ),
            default=0,
        )
        if score:
            scored.append((score, entry))
    exact = [entry for score, entry in scored if score >= 100_000]
    if exact:
        return exact
    return [entry for _, entry in sorted(scored, key=lambda pair: -pair[0])]


def parse_vndb_response(payload: dict[str, Any]) -> list[RemoteEntry]:
    entries: list[RemoteEntry] = []
    for raw in payload.get("results", []):
        source_id = str(raw.get("id") or "").strip()
        if not source_id:
            continue
        fields = {"jp_title": None, "zh_title": None, "en_title": None}
        languages: list[str] = []
        for item in raw.get("titles") or []:
            title = str(item.get("title") or "").strip()
            language = str(item.get("lang") or "")
            if not title:
                continue
            languages.append(language)
            mapped = _language_title_fields(title, language)
            for key, value in mapped.items():
                if value and not fields[key]:
                    fields[key] = value
        alternate = str(raw.get("alttitle") or "").strip()
        if alternate and not fields["jp_title"] and _contains_kana(alternate):
            fields["jp_title"] = alternate
        primary = str(raw.get("title") or alternate or source_id).strip()
        if primary and not any(fields.values()):
            fields.update(_remote_titles(primary))
        developers = raw.get("developers") or []
        developer = None
        if developers:
            developer = str(
                developers[0].get("original")
                or developers[0].get("name")
                or ""
            ).strip() or None
        tags = tuple(
            str(item.get("name") or "").strip()
            for item in (raw.get("tags") or [])
            if str(item.get("name") or "").strip()
        )
        entries.append(RemoteEntry(
            title=primary,
            url=f"https://vndb.org/{source_id}",
            source="VNDB",
            source_id=source_id,
            developer=developer,
            languages=tuple(dict.fromkeys(languages)),
            remote_tags=tags,
            **fields,
        ))
    return entries


def parse_vndb_release_audience(
    payload: dict[str, Any],
) -> tuple[str | None, tuple[str, ...]]:
    """兼容入口：从 VNDB release.minage 中取最高年龄分类。"""
    metadata = parse_vndb_release_metadata(payload)
    return metadata[2], metadata[3]


def parse_vndb_release_metadata(
    payload: dict[str, Any],
) -> tuple[int | None, int | None, str | None, tuple[str, ...], tuple[str, ...]]:
    """从全部已获取的 Release 中取最早年份和最高 minage。"""
    ages: list[int] = []
    dated_releases: list[tuple[int, str]] = []
    age_evidence: list[str] = []
    for raw in payload.get("results") or []:
        if not isinstance(raw, dict):
            continue
        release_id = str(raw.get("id") or "release")
        released = str(raw.get("released") or "").strip()
        year_match = re.match(r"^(\d{4})(?:-\d{2}(?:-\d{2})?)?$", released)
        if year_match:
            dated_releases.append((int(year_match.group(1)), release_id))
        age = raw.get("minage")
        if not isinstance(age, int) or not 0 <= age <= 18:
            continue
        ages.append(age)
        age_evidence.append(f"VNDB {release_id} minage={age}")
    if not ages:
        classification = None
        max_age = None
    else:
        max_age = max(ages)
        classification = "adult" if max_age >= 18 else "all_ages"
    earliest_year = min((year for year, _ in dated_releases), default=None)
    if earliest_year is not None:
        first_ids = sorted(
            release_id for year, release_id in dated_releases
            if year == earliest_year
        )
        year_evidence = (
            f"VNDB earliest Release year={earliest_year} ({', '.join(first_ids)})",
        )
    else:
        year_evidence = ()
    return earliest_year, max_age, classification, tuple(age_evidence), year_evidence


def release_year_from_text(value: Any) -> int | None:
    """从来源明确返回的日期/日期文本提取年份，不推算未知值。"""
    match = re.search(r"(?<!\d)(\d{4})(?!\d)", str(value or "").strip())
    return int(match.group(1)) if match else None


def parse_dlsite_response(
    payload: Any, language: str = "japanese"
) -> list[RemoteEntry]:
    raw_entries = payload if isinstance(payload, list) else []
    entries: list[RemoteEntry] = []
    for raw in raw_entries:
        source_id = str(
            raw.get("workno") or raw.get("product_id") or raw.get("base_product_id") or ""
        ).upper()
        title = str(raw.get("work_name") or raw.get("product_name") or "").strip()
        if not source_id or not title:
            continue
        genres: list[str] = []
        for item in raw.get("genres") or raw.get("genres_replaced") or []:
            value = item.get("name") if isinstance(item, dict) else item
            if value:
                genres.append(str(value).strip())
        editions = raw.get("language_editions") or []
        languages = tuple(
            str(item.get("lang") or item.get("language") or "").strip()
            for item in editions
            if isinstance(item, dict)
            and str(item.get("lang") or item.get("language") or "").strip()
        )
        age_value = str(raw.get("age_category_string") or "").strip().casefold()
        numeric_age = raw.get("age_category")
        normalized_age = (
            0 if age_value in {"general", "all_ages", "all-ages"}
            else 15 if age_value == "r15"
            else 18 if age_value in {"r18", "adult", "adl"}
            else {1: 0, 2: 15, 3: 18}.get(numeric_age)
        )
        audience = None
        audience_evidence: tuple[str, ...] = ()
        if normalized_age is not None:
            audience = "adult" if normalized_age >= 18 else "all_ages"
        if audience:
            evidence_value = age_value or str(numeric_age)
            audience_evidence = (f"DLsite age_category={evidence_value}",)
        entries.append(RemoteEntry(
            title=title,
            url=f"{DLSITE_BASE}/maniax/work/=/product_id/{source_id}.html",
            source="DLsite",
            source_id=source_id,
            developer=str(raw.get("maker_name") or "").strip() or None,
            languages=languages,
            remote_tags=tuple(dict.fromkeys(genres)),
            audience_classification=audience,
            audience_evidence=audience_evidence,
            maximum_release_age=normalized_age,
            release_year=release_year_from_text(raw.get("regist_date")),
            release_year_evidence=(
                (f"DLsite regist_date={raw.get('regist_date')}",)
                if release_year_from_text(raw.get("regist_date")) is not None else ()
            ),
            **_language_title_fields(title, language),
        ))
    return entries


def parse_dlsite_ajax_response(
    payload: Any, source_id: str
) -> list[RemoteEntry]:
    if not isinstance(payload, dict):
        return []
    raw = payload.get(source_id) or payload.get(source_id.upper())
    if not isinstance(raw, dict):
        return []
    title = str(raw.get("work_name") or "").strip()
    if not title:
        return []
    translation = raw.get("translation_info") or {}
    languages = tuple(
        value for value in (str(translation.get("lang") or "").strip(),) if value
    )
    related = tuple(
        str(value) for value in (translation.get("child_worknos") or []) if value
    )
    numeric_age = raw.get("age_category")
    normalized_age = {1: 0, 2: 15, 3: 18}.get(numeric_age)
    audience = (
        "adult" if normalized_age is not None and normalized_age >= 18
        else "all_ages" if normalized_age is not None
        else None
    )
    audience_evidence = (
        (f"DLsite age_category={numeric_age}",) if audience else ()
    )
    return [RemoteEntry(
        title=title,
        url=f"{DLSITE_BASE}/maniax/work/=/product_id/{source_id}.html",
        source="DLsite",
        source_id=source_id,
        developer=str(raw.get("maker_name") or "").strip() or None,
        languages=languages,
        related_editions=related,
        audience_classification=audience,
        audience_evidence=audience_evidence,
        maximum_release_age=normalized_age,
        release_year=release_year_from_text(raw.get("regist_date")),
        release_year_evidence=(
            (f"DLsite regist_date={raw.get('regist_date')}",)
            if release_year_from_text(raw.get("regist_date")) is not None else ()
        ),
        **_language_title_fields(title, "japanese"),
    )]


def parse_dlsite_detail_html(
    page: str, source_id: str, language: str,
) -> RemoteEntry | None:
    """仅从作品详情页的明确字段回读 JSON 接口未返回的作品。"""
    class DetailParser(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.product_id_seen = False
            self.title_parts: list[str] = []
            self.in_title = False
            self.brand_depth = 0
            self.brand: str | None = None

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            values = dict(attrs)
            if tag == "template" and values.get("data-product-id") == source_id:
                self.product_id_seen = True
            if tag == "h1" and values.get("id") == "work_name":
                self.in_title = True
            if tag == "div":
                if self.brand_depth:
                    self.brand_depth += 1
                elif values.get("itemprop") == "brand":
                    self.brand_depth = 1
            if (tag == "meta" and self.brand_depth
                    and values.get("itemprop") == "name"):
                self.brand = str(values.get("content") or "").strip() or None

        def handle_endtag(self, tag: str) -> None:
            if tag == "h1":
                self.in_title = False
            if tag == "div" and self.brand_depth:
                self.brand_depth -= 1

        def handle_data(self, data: str) -> None:
            if self.in_title:
                self.title_parts.append(data)

    parser = DetailParser()
    parser.feed(page)
    title = html.unescape("".join(parser.title_parts)).strip()
    if not parser.product_id_seen or not title:
        return None
    date_match = re.search(r'"regist_date"\s*:\s*"((?:\\.|[^"])*)"', page)
    date_text = (
        json.loads('"' + date_match.group(1) + '"')
        if date_match else None
    )
    year = release_year_from_text(date_text)
    return RemoteEntry(
        title=title,
        url=f"{DLSITE_BASE}/maniax/work/=/product_id/{source_id}.html",
        source="DLsite",
        source_id=source_id,
        developer=parser.brand,
        release_year=year,
        release_year_evidence=(
            (f"DLsite HTML regist_date={date_text}",) if year else ()
        ),
        **_language_title_fields(title, language),
    )


def parse_dlsite_suggest_response(payload: Any) -> list[str]:
    """仅接受 DLsite suggest 的作品数组与合法作品编号。"""
    if not isinstance(payload, dict) or not isinstance(payload.get("work"), list):
        return []
    identifiers: list[str] = []
    seen: set[str] = set()
    for raw in payload["work"]:
        if not isinstance(raw, dict):
            continue
        source_id = str(raw.get("workno") or "").upper().strip()
        if not re.fullmatch(r"(?:RJ|VJ|BJ|EJ)\d{6,8}", source_id):
            continue
        if source_id not in seen:
            seen.add(source_id)
            identifiers.append(source_id)
    return identifiers


def parse_dlsite_search_response(payload: Any) -> list[str]:
    """从 DLsite FSR JSON 及其中的 HTML 片段提取合法作品编号。"""
    identifiers: list[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        source_id = str(value or "").upper().strip()
        if (
            re.fullmatch(r"(?:RJ|VJ|BJ|EJ)\d{6,8}", source_id)
            and source_id not in seen
        ):
            seen.add(source_id)
            identifiers.append(source_id)

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key in ("workno", "work_no", "product_id", "work_id"):
                if key in value:
                    add(value[key])
            for nested in value.values():
                visit(nested)
        elif isinstance(value, (list, tuple)):
            for nested in value:
                visit(nested)
        elif isinstance(value, str):
            for match in re.findall(
                r"(?<![A-Z0-9])(?:RJ|VJ|BJ|EJ)\d{6,8}(?!\d)",
                value.upper(),
            ):
                add(match)

    visit(payload)
    return identifiers


def parse_steam_storesearch_response(payload: Any) -> list[str]:
    """仅接受 Steam storesearch 返回的 app 类型数字 AppID。"""
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        return []
    identifiers: list[str] = []
    seen: set[str] = set()
    for raw in payload["items"]:
        if not isinstance(raw, dict):
            continue
        if str(raw.get("type") or "app").casefold() != "app":
            continue
        app_id = str(raw.get("id") or "").strip()
        if app_id.isdigit() and app_id not in seen:
            seen.add(app_id)
            identifiers.append(app_id)
    return identifiers


def parse_steam_community_search_response(payload: Any) -> list[str]:
    """解析 Steam Community SearchApps 的无密钥应用建议。"""
    if not isinstance(payload, list):
        return []
    identifiers: list[str] = []
    seen: set[str] = set()
    for raw in payload:
        if not isinstance(raw, dict):
            continue
        app_id = str(raw.get("appid") or "").strip()
        if app_id.isdigit() and app_id not in seen:
            seen.add(app_id)
            identifiers.append(app_id)
    return identifiers


def parse_steam_response(
    payload: dict[str, Any], app_id: str, language: str
) -> list[RemoteEntry]:
    wrapper = payload.get(str(app_id)) or {}
    if not wrapper.get("success") or not isinstance(wrapper.get("data"), dict):
        return []
    raw = wrapper["data"]
    title = str(raw.get("name") or "").strip()
    if not title:
        return []
    developers = raw.get("developers") or []
    required_age = raw.get("required_age")
    descriptor_container = raw.get("content_descriptors") or {}
    descriptor_values = (
        descriptor_container.get("ids")
        if isinstance(descriptor_container, dict)
        else []
    ) or []
    descriptor_ids = tuple(sorted({
        int(value) for value in descriptor_values
        if isinstance(value, (int, str)) and str(value).strip().isdigit()
    }))
    audience = None
    audience_evidence: tuple[str, ...] = ()
    if set(descriptor_ids) & {1, 2, 3, 4, 5}:
        audience = "adult"
        maximum_age = 18
        audience_evidence = (
            "Steam content_descriptors=" + ",".join(map(str, descriptor_ids)),
        )
    elif isinstance(required_age, (int, str)) and str(required_age).strip().isdigit():
        age = int(str(required_age).strip())
        audience = "adult" if age >= 18 else "all_ages"
        maximum_age = age
        audience_evidence = (f"Steam required_age={age}",)
    else:
        maximum_age = None
    return [RemoteEntry(
        title=title,
        url=f"{STEAM_STORE}/app/{app_id}/",
        source="Steam",
        source_id=str(app_id),
        developer=str(developers[0]).strip() if developers else None,
        remote_dlc=tuple(str(value) for value in (raw.get("dlc") or [])),
        audience_classification=audience,
        audience_evidence=audience_evidence,
        maximum_release_age=maximum_age,
        release_year=release_year_from_text(
            (raw.get("release_date") or {}).get("date")
            if isinstance(raw.get("release_date"), dict)
            else raw.get("release_date")
        ),
        release_year_evidence=(
            (f"Steam release_date={(raw.get('release_date') or {}).get('date')}",)
            if isinstance(raw.get("release_date"), dict)
            and release_year_from_text((raw.get("release_date") or {}).get("date")) is not None
            else ()
        ),
        **_language_title_fields(title, language),
    )]


class _HttpClient:
    def __init__(
        self, timeout: float = 4.0, max_results: int = 8,
        max_attempts: int = 2, source_budget: float = 45.0,
        max_parallel_searches: int = 4,
    ) -> None:
        self.timeout = timeout
        self.max_results = max_results
        self.max_attempts = max(1, max_attempts)
        self.source_budget = max(1.0, source_budget)
        self._budget_state = threading.local()
        self._search_slots = threading.BoundedSemaphore(
            max(1, max_parallel_searches)
        )

    def begin_search_budget(self) -> None:
        """限制单游戏单来源总耗时，避免失效站点拖住整项。"""
        self._search_slots.acquire()
        self._budget_state.deadline = time.monotonic() + self.source_budget

    def end_search_budget(self) -> None:
        self._budget_state.deadline = None
        self._search_slots.release()

    def _remaining_budget(self) -> float | None:
        deadline = getattr(self._budget_state, "deadline", None)
        if deadline is None:
            return None
        return deadline - time.monotonic()

    def _request(
        self, url: str, data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        payload = None
        request_headers = {
            "User-Agent": "CodexGameFolderRenamer/2.0",
            "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
        }
        if data is not None:
            payload = json.dumps(data).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        request = urllib.request.Request(
            url, data=payload, headers=request_headers,
            method="POST" if payload is not None else "GET",
        )
        for attempt in range(self.max_attempts):
            remaining = self._remaining_budget()
            if remaining is not None and remaining <= 0:
                raise TimeoutError("来源搜索超过单游戏时间预算")
            request_timeout = self.timeout
            if remaining is not None:
                request_timeout = min(request_timeout, max(0.1, remaining))
            try:
                with urllib.request.urlopen(
                    request, timeout=request_timeout
                ) as response:
                    return response.read()
            except urllib.error.HTTPError as exc:
                if (
                    exc.code not in {429, 500, 502, 503, 504}
                    or attempt == self.max_attempts - 1
                ):
                    raise
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                if attempt == self.max_attempts - 1:
                    raise
            delay = 0.5 * (2 ** attempt)
            remaining = self._remaining_budget()
            if remaining is not None and remaining <= delay:
                raise TimeoutError("来源搜索超过单游戏时间预算")
            time.sleep(delay)
        raise RuntimeError("HTTP 重试流程异常结束")

    def _json(
        self, url: str, data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        return json.loads(self._request(url, data=data, headers=headers).decode(
            "utf-8", errors="replace"
        ))

    def _text(
        self, url: str, headers: dict[str, str] | None = None
    ) -> str:
        return self._request(url, headers=headers).decode("utf-8", errors="replace")


class VNDBClient(_HttpClient):
    name = "VNDB"

    def __init__(self, timeout: float = 4.0, max_results: int = 8) -> None:
        super().__init__(timeout=timeout, max_results=max_results)
        self._audience_cache: dict[
            str, tuple[
                int | None, int | None, str | None,
                tuple[str, ...], tuple[str, ...],
            ]
        ] = {}

    def probe_connectivity(self) -> None:
        """用正式标题查询端点验证传输、JSON 与最低响应结构。"""
        payload = self._json(f"{VNDB_API}/vn", data={
            "filters": ["search", "=", "connectivity probe"],
            "fields": "id",
            "results": 1,
        })
        if not isinstance(payload, dict) or not isinstance(
            payload.get("results"), list
        ):
            raise ValueError("VNDB API 响应结构无效")

    def _with_audience(self, entry: RemoteEntry) -> RemoteEntry:
        if len(entry.release_pages) == 1:
            release_year, maximum_age, audience, age_evidence, year_evidence = (
                parse_vndb_release_metadata({"results": [entry.release_pages[0]]})
            )
            return replace(
                entry,
                audience_classification=audience,
                audience_evidence=age_evidence,
                release_year=release_year,
                release_year_evidence=year_evidence,
                maximum_release_age=maximum_age,
            )
        source_id = str(entry.source_id or "")
        if source_id not in self._audience_cache:
            releases: list[dict[str, Any]] = []
            page = 1
            while True:
                payload = self._json(f"{VNDB_API}/release", data={
                    "filters": ["vn", "=", ["id", "=", source_id]],
                    "fields": "id,minage,released",
                    "results": 100,
                    "page": page,
                })
                if not isinstance(payload, dict) or not isinstance(
                    payload.get("results"), list
                ) or not isinstance(payload.get("more"), bool):
                    raise ValueError(f"VNDB Release 响应结构无效：{source_id}")
                releases.extend(payload["results"])
                if not payload.get("more"):
                    break
                if not payload["results"]:
                    raise ValueError(f"VNDB Release 分页没有前进：{source_id}")
                page += 1
            self._audience_cache[source_id] = parse_vndb_release_metadata({
                "results": releases,
            })
        release_year, maximum_age, audience, age_evidence, year_evidence = (
            self._audience_cache[source_id]
        )
        return replace(
            entry,
            audience_classification=audience,
            audience_evidence=age_evidence,
            release_year=release_year,
            release_year_evidence=year_evidence,
            maximum_release_age=maximum_age,
        )

    def _with_seeded_release_variant(
        self,
        entry: RemoteEntry,
        release_ids: Sequence[str],
        release_titles: dict[str, dict[str, str]] | None = None,
    ) -> RemoteEntry:
        """用户指定的 VNDB release 可为同一 VN 的分篇提供身份标题。"""
        if not release_ids:
            return entry
        variants: list[str] = []
        release_pages: list[dict[str, Any]] = []
        supplied_titles: dict[str, set[str]] = {"jp": set(), "zh": set(), "en": set()}
        for release_id in dict.fromkeys(release_ids):
            payload = self._json(f"{VNDB_API}/release", data={
                "filters": ["id", "=", release_id],
                "fields": (
                    "id,title,alttitle,languages.lang,languages.title,"
                    "languages.main,minage,released,producers.name,producers.original,"
                    "producers.developer,vns.id,vns.rtype"
                ),
                "results": 1,
            })
            results = payload.get("results") if isinstance(payload, dict) else None
            if not isinstance(results, list) or len(results) != 1:
                raise ValueError(f"VNDB release 返回结构无效：{release_id}")
            raw = results[0]
            linked_ids = {
                str(item.get("id") or "") for item in raw.get("vns") or []
                if isinstance(item, dict)
            }
            if str(entry.source_id or "") not in linked_ids:
                raise ValueError(
                    f"VNDB release 与候选 VN 不关联："
                    f"{release_id} -> {entry.source_id}"
                )
            title = str(raw.get("title") or "").strip()
            if not title:
                raise ValueError(f"VNDB release 缺少标题：{release_id}")
            variants.append(title)
            language_values: dict[str, set[str]] = {
                "jp_title": set(), "zh_title": set(), "en_title": set(),
            }
            simplified_chinese_titles: set[str] = set()
            raw_languages = raw.get("languages")
            if not isinstance(raw_languages, list):
                raise ValueError(f"VNDB release 语言字段无效：{release_id}")
            main_language = next((
                str(item.get("lang") or "").casefold()
                for item in raw_languages
                if isinstance(item, dict) and item.get("main")
            ), "")
            for language_entry in raw_languages:
                if not isinstance(language_entry, dict):
                    continue
                language = str(language_entry.get("lang") or "").casefold()
                field_name = (
                    "jp_title" if language == "ja"
                    else "zh_title" if language.startswith("zh")
                    else "en_title" if language == "en"
                    else None
                )
                if not field_name:
                    continue
                value = str(language_entry.get("title") or "").strip()
                if not value and language == main_language:
                    value = title
                if value:
                    language_values[field_name].add(value)
                    if language in {"zh-hans", "zh-cn", "zh-sg"}:
                        simplified_chinese_titles.add(value)
            if len(simplified_chinese_titles) == 1:
                language_values["zh_title"] = simplified_chinese_titles
            raw_producers = raw.get("producers")
            if not isinstance(raw_producers, list):
                raise ValueError(f"VNDB release 制作方字段无效：{release_id}")
            release_developers = [
                str(producer.get("original") or producer.get("name") or "").strip()
                for producer in raw_producers
                if isinstance(producer, dict) and producer.get("developer")
                and str(producer.get("original") or producer.get("name") or "").strip()
            ]
            alttitle = str(raw.get("alttitle") or "").strip()
            if not language_values["jp_title"]:
                fallback_jp = _remote_titles(alttitle).get("jp_title")
                if fallback_jp:
                    language_values["jp_title"].add(fallback_jp)
            if not language_values["en_title"]:
                fallback_en = _remote_titles(title).get("en_title")
                if fallback_en:
                    language_values["en_title"].add(fallback_en)
            release_page: dict[str, Any] = {
                "source": "VNDB",
                "id": release_id,
                "source_id": release_id,
                "url": f"https://vndb.org/{release_id}",
                "title": title,
                "alttitle": alttitle or None,
                "languages": copy.deepcopy(raw_languages),
                "producers": copy.deepcopy(raw_producers),
                "vns": copy.deepcopy(raw.get("vns") or []),
                "minage": raw.get("minage"),
                "released": raw.get("released"),
                "developer": " / ".join(dict.fromkeys(release_developers)) or None,
            }
            for field_name, values in language_values.items():
                release_page[field_name] = (
                    next(iter(values)) if len(values) == 1 else None
                )
            release_pages.append(release_page)
            for language, value in (release_titles or {}).get(release_id, {}).items():
                if language in supplied_titles and value:
                    supplied_titles[language].add(value)
        if len(set(variants)) != 1:
            raise ValueError("VNDB release 种子存在多个冲突的分篇标题")
        for language, values in supplied_titles.items():
            if len(values) > 1:
                raise ValueError(f"VNDB release 种子存在冲突的{language}分篇标题")
        variant = variants[0]
        release_fields = release_pages[0] if len(release_pages) == 1 else None
        return replace(
            entry,
            title=variant,
            developer=(
                release_fields.get("developer") if release_fields else entry.developer
            ),
            jp_title=next(
                iter(supplied_titles["jp"]),
                release_fields.get("jp_title") if release_fields else entry.jp_title,
            ),
            zh_title=next(
                iter(supplied_titles["zh"]),
                release_fields.get("zh_title") if release_fields else entry.zh_title,
            ),
            en_title=next(
                iter(supplied_titles["en"]),
                release_fields.get("en_title") if release_fields else variant,
            ),
            release_pages=tuple(release_pages),
        )

    def search(
        self, titles: Sequence[str], identifiers: dict[str, list[str]],
        developer: str | None = None,
    ) -> list[RemoteEntry]:
        trusted_ids = {
            value.casefold() for value in identifiers.get("trusted_vndb", [])
        }
        direct_ids = [
            value for value in identifiers.get("vndb", [])
            if value.casefold() in trusted_ids
        ]
        direct = bool(direct_ids)
        filters: list[list[Any]] = []
        filters.extend(["id", "=", value] for value in direct_ids)
        if not filters:
            filters.extend(
                ["search", "=", query]
                for title in titles
                if (query := sanitize_search_query(title))
            )
        results: list[RemoteEntry] = []
        seen: set[str] = set()
        for item_filter in filters:
            payload = self._json(f"{VNDB_API}/vn", data={
                "filters": item_filter,
                "fields": (
                    "id,title,alttitle,titles.title,titles.lang,titles.main,"
                    "developers.name,developers.original,tags.name"
                ),
                "results": self.max_results,
            })
            for entry in parse_vndb_response(payload):
                if entry.source_id in seen:
                    continue
                trusted = bool(
                    entry.source_id
                    and entry.source_id.casefold() in trusted_ids
                )
                if trusted or not titles or any(
                    _title_matches(title, candidate)
                    for title in titles
                    for candidate in (
                        entry.title, entry.jp_title, entry.zh_title, entry.en_title
                    )
                    if candidate
                ):
                    seen.add(entry.source_id or entry.url)
                    results.append(entry)
        matched = results if direct else best_title_matches(
            titles, results, developer
        )
        release_ids = list(identifiers.get("trusted_vndb_release", []))
        release_titles = identifiers.get("trusted_vndb_release_titles", {})
        return [
            self._with_audience(
                self._with_seeded_release_variant(
                    entry, release_ids, release_titles,
                )
            )
            for entry in matched
        ]

    def search_discovered(
        self, titles: Sequence[str], source_ids: Sequence[str],
        developer: str | None = None,
    ) -> list[RemoteEntry]:
        results: list[RemoteEntry] = []
        for source_id in list(dict.fromkeys(source_ids))[:self.max_results]:
            payload = self._json(f"{VNDB_API}/vn", data={
                "filters": ["id", "=", source_id],
                "fields": (
                    "id,title,alttitle,titles.title,titles.lang,titles.main,"
                    "developers.name,developers.original,tags.name"
                ),
                "results": 1,
            })
            for entry in parse_vndb_response(payload):
                if any(
                    _title_matches(title, candidate)
                    for title in titles
                    for candidate in (
                        entry.title, entry.jp_title, entry.zh_title, entry.en_title
                    )
                    if candidate
                ):
                    results.append(entry)
        return [
            self._with_audience(entry)
            for entry in best_title_matches(titles, results, developer)
        ]


def _fetch_candidate_details(
    source_ids: Sequence[str],
    fetch: Callable[[str], RemoteEntry | None],
    *,
    max_workers: int = 4,
) -> list[tuple[str, RemoteEntry | None]]:
    """Fetch independent candidate details concurrently, preserving ID order."""
    ordered_ids = list(source_ids)
    if not ordered_ids:
        return []
    with ThreadPoolExecutor(
        max_workers=min(max_workers, len(ordered_ids))
    ) as executor:
        entries = list(executor.map(fetch, ordered_ids))
    return list(zip(ordered_ids, entries))


class DLsiteClient(_HttpClient):
    name = "DLsite"

    def probe_connectivity(self) -> None:
        """用官网当前的轻量 suggest 端点验证传输、JSON 与最低响应结构。"""
        query = urllib.parse.urlencode({
            "term": "connectivity probe", "site": "maniax",
            "time": "0",
        })
        payload = self._json(
            f"{DLSITE_BASE}/api/products/maniax/fetchSuggest?{query}",
            headers={
                "Cookie": "adultchecked=1; locale=ja_JP",
                "Referer": f"{DLSITE_BASE}/maniax/",
            },
        )
        if not isinstance(payload, dict) or not isinstance(
            payload.get("work"), list
        ):
            raise ValueError("DLsite suggest 响应结构无效")

    def _search_ids(self, title: str) -> list[str]:
        title = sanitize_search_query(title)
        if not title:
            return []
        query = urllib.parse.urlencode({
            "term": title, "site": "maniax", "time": "0",
        })
        payload = self._json(
            f"{DLSITE_BASE}/api/products/maniax/fetchSuggest?{query}",
            headers={
                "Cookie": "adultchecked=1; locale=ja_JP",
                "Referer": f"{DLSITE_BASE}/maniax/",
            },
        )
        return parse_dlsite_suggest_response(payload)[:self.max_results]

    def _fetch_id(self, source_id: str) -> RemoteEntry | None:
        merged: RemoteEntry | None = None
        for locale, language in (
            ("ja_JP", "japanese"), ("zh_CN", "schinese"), ("en_US", "english")
        ):
            url = (
                f"{DLSITE_BASE}/maniax/api/=/product.json?workno={source_id}"
                f"&locale={locale}"
            )
            try:
                payload = self._json(url, headers={
                    "Cookie": f"adultchecked=1; locale={locale}"
                })
            except Exception:
                continue
            parsed = parse_dlsite_response(payload, language)
            if not parsed:
                continue
            current = parsed[0]
            if merged is None:
                merged = current
            else:
                merged = replace(
                    merged,
                    jp_title=merged.jp_title or current.jp_title,
                    zh_title=merged.zh_title or current.zh_title,
                    en_title=merged.en_title or current.en_title,
                    languages=tuple(dict.fromkeys(
                        (*merged.languages, *current.languages)
                    )),
                    audience_classification=(
                        "adult" if "adult" in {
                            merged.audience_classification,
                            current.audience_classification,
                        }
                        else merged.audience_classification
                        or current.audience_classification
                    ),
                    audience_evidence=tuple(dict.fromkeys(
                        (*merged.audience_evidence, *current.audience_evidence)
                    )),
                    release_year=(
                        min(value for value in (merged.release_year, current.release_year)
                            if value is not None)
                        if merged.release_year is not None or current.release_year is not None
                        else None
                    ),
                    release_year_evidence=tuple(dict.fromkeys(
                        (*merged.release_year_evidence, *current.release_year_evidence)
                    )),
                    maximum_release_age=max(
                        (value for value in (
                            merged.maximum_release_age, current.maximum_release_age
                        ) if value is not None),
                        default=None,
                    ),
                )
        if merged is not None:
            return merged
        ajax_url = (
            f"{DLSITE_BASE}/maniax/product/info/ajax?product_id={source_id}"
        )
        try:
            ajax_payload = self._json(ajax_url, headers={
                "Cookie": "adultchecked=1; locale=ja_JP"
            })
        except (urllib.error.HTTPError, urllib.error.URLError, ValueError):
            ajax_payload = None
        fallback = parse_dlsite_ajax_response(ajax_payload, source_id)
        if fallback:
            return fallback[0]
        for locale, language in (
            ("ja_JP", "japanese"), ("zh_CN", "schinese"), ("en_US", "english")
        ):
            detail_url = (
                f"{DLSITE_BASE}/maniax/work/=/product_id/{source_id}.html"
                f"?locale={locale}"
            )
            try:
                page = self._text(detail_url, headers={
                    "Cookie": f"adultchecked=1; locale={locale}"
                })
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
                continue
            current = parse_dlsite_detail_html(page, source_id, language)
            if current is None:
                continue
            if merged is None:
                merged = current
            else:
                merged = replace(
                    merged,
                    jp_title=merged.jp_title or current.jp_title,
                    zh_title=merged.zh_title or current.zh_title,
                    en_title=merged.en_title or current.en_title,
                    developer=merged.developer or current.developer,
                    release_year=merged.release_year or current.release_year,
                    release_year_evidence=tuple(dict.fromkeys((
                        *merged.release_year_evidence,
                        *current.release_year_evidence,
                    ))),
                )
        return merged

    def search(
        self, titles: Sequence[str], identifiers: dict[str, list[str]],
        developer: str | None = None,
    ) -> list[RemoteEntry]:
        direct = bool(identifiers.get("dlsite"))
        trusted_ids = {
            value.casefold() for value in identifiers.get("trusted_dlsite", [])
        }
        ids = list(identifiers.get("dlsite", []))
        if not ids:
            for title in titles:
                ids.extend(self._search_ids(title))
        results: list[RemoteEntry] = []
        candidate_limit = self.max_results if direct else self.max_results * 2
        ordered_ids = list(dict.fromkeys(ids))[:candidate_limit]
        for source_id, entry in _fetch_candidate_details(
            ordered_ids, self._fetch_id
        ):
            if entry and (
                direct
                or source_id.casefold() in trusted_ids
                or
                not titles or any(
                    _title_matches(title, candidate)
                    for title in titles
                    for candidate in (
                        entry.title, entry.jp_title, entry.zh_title, entry.en_title
                    )
                    if candidate
                )
            ):
                results.append(entry)
        # 本地 ID 先回源；若只指向 DLC，继续按原名查本体并保留 DLC 供审核。
        direct_dlc_mismatch = bool(
            direct and results and titles
            and all(
                _has_standalone_dlc_identity(entry.title)
                != _has_standalone_dlc_identity(title)
                for entry in results for title in titles
            )
        )
        if direct and (not results or direct_dlc_mismatch):
            discovered_ids: list[str] = []
            for title in titles:
                discovered_ids.extend(self._search_ids(title))
            discovered_ids = [
                source_id for source_id in dict.fromkeys(discovered_ids)
                if source_id not in ordered_ids
            ][:self.max_results * 2]
            for _source_id, entry in _fetch_candidate_details(
                discovered_ids, self._fetch_id
            ):
                if entry and any(
                    _title_matches(title, candidate)
                    for title in titles
                    for candidate in (
                        entry.title, entry.jp_title,
                        entry.zh_title, entry.en_title,
                    )
                    if candidate
                ):
                    results.append(entry)
            if not direct_dlc_mismatch:
                direct = False
        if direct_dlc_mismatch:
            # 原名是本体时，本体先进入轮次预览；DLC 仍保留供逐项审核。
            results.sort(key=lambda entry: not any(
                _has_standalone_dlc_identity(entry.title)
                == _has_standalone_dlc_identity(title)
                for title in titles
            ))
        matched = results if direct else best_title_matches(
            titles, results, developer
        )
        return matched[:self.max_results]

    def search_discovered(
        self, titles: Sequence[str], source_ids: Sequence[str],
        developer: str | None = None,
    ) -> list[RemoteEntry]:
        results = [
            entry for _, entry in _fetch_candidate_details(
                list(dict.fromkeys(source_ids))[:self.max_results], self._fetch_id
            )
            if entry and any(
                _title_matches(title, candidate)
                for title in titles
                for candidate in (
                    entry.title, entry.jp_title, entry.zh_title, entry.en_title
                )
                if candidate
            )
        ]
        return best_title_matches(titles, results, developer)[:self.max_results]


class SteamClient(_HttpClient):
    name = "Steam"

    def __init__(self, timeout: float = 4.0, max_results: int = 8) -> None:
        super().__init__(
            timeout=timeout, max_results=max_results,
            max_parallel_searches=1,
        )
        self._detail_cache: dict[str, RemoteEntry] = {}

    def probe_connectivity(self) -> None:
        """用正式 Community 发现端点验证传输、JSON 与最低响应结构。"""
        payload = self._json(
            "https://steamcommunity.com/actions/SearchApps/connectivity%20probe"
        )
        if not isinstance(payload, list):
            raise ValueError("Steam SearchApps 响应结构无效")

    def _search_ids(self, title: str) -> list[str]:
        title = sanitize_search_query(title)
        if not title:
            return []
        encoded_title = urllib.parse.quote(title, safe="")
        payload = self._json(
            f"https://steamcommunity.com/actions/SearchApps/{encoded_title}"
        )
        return parse_steam_community_search_response(payload)[:self.max_results]

    def _search_chinese_store_ids(self, title: str) -> list[str]:
        """Community 未提供精确中文标题时，顺序查 Steam 官方商店入口。"""
        query = urllib.parse.urlencode({
            "term": sanitize_search_query(title), "l": "schinese", "cc": "us",
        })
        payload = self._json(f"{STEAM_STORE}/api/storesearch/?{query}")
        if not isinstance(payload, dict) or not isinstance(
            payload.get("items"), list
        ):
            raise ValueError("Steam storesearch 响应结构无效")
        return parse_steam_storesearch_response(payload)[:self.max_results * 2]

    def _fetch_id(self, app_id: str) -> RemoteEntry | None:
        cached = self._detail_cache.get(app_id)
        if cached is not None:
            return cached
        merged: RemoteEntry | None = None
        for language in ("japanese", "schinese", "english"):
            url = (
                f"{STEAM_STORE}/api/appdetails?appids={app_id}"
                f"&l={language}&cc=us"
            )
            parsed = parse_steam_response(self._json(url), app_id, language)
            if not parsed:
                continue
            current = parsed[0]
            if merged is None:
                merged = current
            else:
                merged = replace(
                    merged,
                    jp_title=merged.jp_title or current.jp_title,
                    zh_title=merged.zh_title or current.zh_title,
                    en_title=merged.en_title or current.en_title,
                    remote_dlc=tuple(dict.fromkeys(
                        (*merged.remote_dlc, *current.remote_dlc)
                    )),
                    audience_classification=(
                        "adult" if "adult" in {
                            merged.audience_classification,
                            current.audience_classification,
                        }
                        else merged.audience_classification
                        or current.audience_classification
                    ),
                    audience_evidence=tuple(dict.fromkeys(
                        (*merged.audience_evidence, *current.audience_evidence)
                    )),
                    release_year=(
                        min(value for value in (merged.release_year, current.release_year)
                            if value is not None)
                        if merged.release_year is not None or current.release_year is not None
                        else None
                    ),
                    release_year_evidence=tuple(dict.fromkeys(
                        (*merged.release_year_evidence, *current.release_year_evidence)
                    )),
                    maximum_release_age=max(
                        (value for value in (
                            merged.maximum_release_age, current.maximum_release_age
                        ) if value is not None),
                        default=None,
                    ),
                )
        if merged is not None:
            self._detail_cache[app_id] = merged
        return merged

    def search(
        self, titles: Sequence[str], identifiers: dict[str, list[str]],
        developer: str | None = None,
    ) -> list[RemoteEntry]:
        direct = bool(identifiers.get("steam"))
        trusted_ids = {
            value.casefold() for value in identifiers.get("trusted_steam", [])
        }
        ids = list(identifiers.get("steam", []))
        if not ids:
            for title in titles:
                ids.extend(self._search_ids(title))
        results: list[RemoteEntry] = []
        candidate_limit = self.max_results if direct else self.max_results * 2
        ordered_ids = list(dict.fromkeys(ids))[:candidate_limit]
        for app_id, entry in _fetch_candidate_details(
            ordered_ids, self._fetch_id, max_workers=2
        ):
            if entry and (
                direct
                or app_id.casefold() in trusted_ids
                or
                not titles or any(
                    _title_matches(title, candidate)
                    for title in titles
                    for candidate in (
                        entry.title, entry.jp_title, entry.zh_title, entry.en_title
                    )
                    if candidate
                )
            ):
                results.append(entry)
        # 查询文字仍来自原 basename；只在中文词缺少精确候选时补官方商店发现。
        chinese_without_exact = [
            title for title in titles
            if _contains_han(title) and not _contains_kana(title)
            and not any(
                _title_match_score(title, candidate) >= 100_000
                for entry in results
                for candidate in (
                    entry.title, entry.jp_title, entry.zh_title, entry.en_title
                )
                if candidate
            )
        ]
        if not direct and chinese_without_exact:
            discovered_ids: list[str] = []
            for title in chinese_without_exact:
                discovered_ids.extend(self._search_chinese_store_ids(title))
            existing_ids = {entry.source_id for entry in results}
            for app_id, entry in _fetch_candidate_details(
                [value for value in dict.fromkeys(discovered_ids)
                 if value not in existing_ids][:self.max_results * 2],
                self._fetch_id, max_workers=2,
            ):
                if entry and any(
                    _title_matches(title, candidate)
                    for title in chinese_without_exact
                    for candidate in (
                        entry.title, entry.jp_title, entry.zh_title, entry.en_title
                    )
                    if candidate
                ):
                    results.append(entry)
        matched = results if direct else best_title_matches(
            titles, results, developer
        )
        return matched[:self.max_results]

    def search_discovered(
        self, titles: Sequence[str], source_ids: Sequence[str],
        developer: str | None = None,
    ) -> list[RemoteEntry]:
        results = [
            entry for _, entry in _fetch_candidate_details(
                list(dict.fromkeys(source_ids))[:self.max_results],
                self._fetch_id,
                max_workers=2,
            )
            if entry and any(
                _title_matches(title, candidate)
                for title in titles
                for candidate in (
                    entry.title, entry.jp_title, entry.zh_title, entry.en_title
                )
                if candidate
            )
        ]
        return best_title_matches(titles, results, developer)[:self.max_results]


LANGUAGE_ROUNDS = (
    (1, "jp", "jp_title"),
    (2, "zh", "zh_title"),
    (3, "en", "en_title"),
)


def _title_map_from_sequence(titles: Sequence[str]) -> dict[str, str | None]:
    """兼容旧调用；正式清单由本地三语字段显式传入映射。"""
    result: dict[str, str | None] = {"jp": None, "zh": None, "en": None}
    for title in titles:
        if _contains_kana(title) and result["jp"] is None:
            result["jp"] = title
        elif _looks_english(title) and result["en"] is None:
            result["en"] = title
        elif _contains_han(title) and result["zh"] is None:
            result["zh"] = title
        else:
            key = next((item for item in ("jp", "zh", "en") if result[item] is None), None)
            if key:
                result[key] = title
    return result


def _report_source_search(
    source: Any,
    title_map: dict[str, str | None],
    identifiers: dict[str, list[str]],
    developer: str | None,
) -> tuple[dict[str, Any], list[str]]:
    """单一来源内按日中英顺序查询，并记录成功、失败、覆盖和缺失轮次。"""
    entries: list[RemoteEntry] = []
    rounds: list[dict[str, Any]] = []
    warnings: list[str] = []
    seen: set[tuple[str, str]] = set()
    source_key = {"VNDB": "vndb", "DLsite": "dlsite", "Steam": "steam"}.get(
        source.name, source.name.lower()
    )
    direct_pending = bool(identifiers.get(source_key))
    for number, language, field_name in LANGUAGE_ROUNDS:
        full_query = title_map.get(language)
        query = extract_search_keyword(full_query) if full_query else None
        covered = next(
            (
                value for entry in entries
                if (value := getattr(entry, field_name)) and query
                and title_identity_key(value) == title_identity_key(query)
            ),
            None,
        )
        if covered:
            rounds.append({
                "number": number, "language": language,
                "query": query,
                "status": "covered", "title": covered, "result": None,
            })
            continue
        if not full_query:
            rounds.append({
                "number": number, "language": language, "query": None,
                "status": "missing", "title": None, "result": None,
            })
            continue
        query_identifiers = identifiers if direct_pending else {
            "vndb": [], "dlsite": [], "steam": [],
        }
        direct_pending = False
        try:
            found = source.search([query], query_identifiers, developer)
        except Exception as exc:
            warnings.append(f"{source.name} 查询失败：{type(exc).__name__}")
            rounds.append({
                "number": number, "language": language, "query": query,
                "status": "failed", "title": query, "result": None,
            })
            continue
        for entry in found:
            key = (entry.source, entry.source_id or entry.url)
            if key not in seen:
                seen.add(key)
                entries.append(entry)
        result = found[0] if found else None
        rounds.append({
            "number": number, "language": language, "query": query,
            "status": "success" if result else "failed",
            "title": query, "result": _remote_dict(result) if result else None,
        })
    if not entries:
        warnings.append(f"{source.name} 未找到可信同游戏条目")
    return {
        "source": source.name, "entries": entries, "rounds": rounds,
    }, list(dict.fromkeys(warnings))


class MetadataAggregator:
    def __init__(self, sources: Sequence[Any]) -> None:
        self.sources = list(sources)
        self.last_reports: list[dict[str, Any]] = []

    def search(
        self, titles: Sequence[str], identifiers: dict[str, list[str]],
        developer: str | None = None,
        title_map: dict[str, str | None] | None = None,
    ) -> tuple[list[RemoteEntry], list[str]]:
        entries: list[RemoteEntry] = []
        warnings: list[str] = []
        seen: set[tuple[str, str]] = set()
        resolved_title_map = title_map or _title_map_from_sequence(titles)
        with ThreadPoolExecutor(max_workers=max(1, len(self.sources))) as executor:
            futures = {
                source: executor.submit(
                    _report_source_search, source, resolved_title_map,
                    identifiers, developer,
                )
                for source in self.sources
            }
            reports: list[dict[str, Any]] = []
            for source in self.sources:
                report, source_warnings = futures[source].result()
                reports.append(report)
                warnings.extend(source_warnings)
                found = report["entries"]
                for entry in found:
                    key = (entry.source, entry.source_id or entry.url)
                    if key not in seen:
                        seen.add(key)
                        entries.append(entry)
        self.last_reports = reports
        return entries, warnings


def select_naming_sources(
    reports: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """所有含可信详情的来源都参加命名合并，保留查询展示顺序。"""
    return [report for report in reports if report.get("entries")], []


def merge_selected_sources(
    local: LocalMetadata,
    reports: Sequence[dict[str, Any]],
) -> tuple[LocalMetadata, dict[str, str], list[str]]:
    """按 VNDB、DLsite、Steam 优先级逐字段合并，远程命名字段覆盖本地。"""
    priority = {"Steam": 1, "DLsite": 2, "VNDB": 3}
    ordered = sorted(
        reports,
        key=lambda report: priority.get(str(report.get("source") or ""), 0),
        reverse=True,
    )
    merged = replace(
        local, developer=None, jp_title=None, zh_title=None, en_title=None,
    )
    field_sources: dict[str, str] = {}
    notes: list[str] = []
    for field_name, label in (
        ("developer", "developer"), ("jp_title", "日文名"),
        ("zh_title", "中文名"), ("en_title", "英文名"),
    ):
        candidates: list[tuple[str, str]] = []
        for report in ordered:
            source = str(report.get("source") or "未知")
            values: dict[str, str] = {}
            for entry in report.get("entries") or []:
                value = getattr(entry, field_name)
                if value:
                    values.setdefault(_match_key(str(value)), str(value).strip())
            if values:
                if len(values) > 1:
                    notes.append(
                        f"{source} {label}候选冲突，按详情顺序采用："
                        + " / ".join(values.values())
                    )
                candidates.append((source, next(iter(values.values()))))
        if not candidates:
            continue
        source, candidate = candidates[0]
        if len({_match_key(value) for _, value in candidates}) > 1:
            notes.append(
                f"{label} 来源值冲突，按 VNDB>DLsite>Steam 采用 {source}："
                + " / ".join(f"{item_source}={value}" for item_source, value in candidates)
            )
        if field_name.endswith("_title"):
            existing = {
                _match_key(str(getattr(merged, other)))
                for other, _ in TITLE_FIELDS if getattr(merged, other)
            }
            if _match_key(candidate) in existing:
                continue
        merged = replace(merged, **{field_name: candidate})
        field_sources[field_name] = source
    return merged, field_sources, notes


def merge_selected_source(
    local: LocalMetadata,
    source: str,
    entries: Sequence[RemoteEntry],
) -> tuple[LocalMetadata, list[str]]:
    """只从被采用来源生成 developer 与三语标题。"""
    notes: list[str] = []
    merged = replace(
        local,
        developer=None,
        jp_title=None,
        zh_title=None,
        en_title=None,
    )
    for field_name, label in (
        ("developer", "developer"),
        ("jp_title", "日文名"),
        ("zh_title", "中文名"),
        ("en_title", "英文名"),
    ):
        values: dict[str, str] = {}
        for entry in entries:
            value = getattr(entry, field_name)
            if value:
                values.setdefault(_match_key(str(value)), str(value).strip())
        if len(values) > 1:
            notes.append(f"{source} {label}候选冲突：" + " / ".join(values.values()))
            continue
        if values:
            candidate = next(iter(values.values()))
            if field_name.endswith("_title"):
                existing = {
                    _match_key(str(getattr(merged, other)))
                    for other, _ in TITLE_FIELDS if getattr(merged, other)
                }
                if _match_key(candidate) in existing:
                    continue
            merged = replace(merged, **{field_name: candidate})
    return merged, notes


def apply_validated_remote_semantics(
    local: LocalMetadata,
    remote_semantic: dict[str, Any],
) -> tuple[LocalMetadata, dict[str, str]]:
    """排他消费 Skill 已确认的远程命名字段，不用本地标题补缺。"""
    titles = remote_semantic.get("returned_titles") or {}
    semantic_values = {
        "developer": remote_semantic.get("developer"),
        "jp_title": titles.get("jp"),
        "zh_title": titles.get("zh"),
        "en_title": titles.get("en"),
    }
    updates: dict[str, Any] = {
        # 可信采用候选存在时，日中英标题成为封闭的远程字段集合。
        # 必须先清空本地槽，再只写回 returned_titles 声明的值。
        "jp_title": None,
        "zh_title": None,
        "en_title": None,
    }
    updates.update({
        field_name: value
        for field_name, value in semantic_values.items()
        if value is not None
    })
    source_keys = {
        "developer": "developer",
        "jp_title": "jp",
        "zh_title": "zh",
        "en_title": "en",
    }
    declared_sources = remote_semantic.get("field_sources") or {}
    field_sources: dict[str, str] = {}
    for field_name, value in updates.items():
        if value is None:
            continue
        source = declared_sources.get(source_keys[field_name])
        if not source:
            raise ValueError(f"远程语义字段缺少来源：{field_name}")
        field_sources[field_name] = str(source)
    return replace(local, **updates), field_sources


def validate_planned_title_provenance(
    metadata: LocalMetadata,
    remote_semantic: dict[str, Any],
    field_sources: dict[str, str],
    english_fallback: dict[str, Any] | None = None,
) -> None:
    """验证最终叶子标题只能来自远程字段，或来自带 #uncheck 的本地回退。"""
    selected = remote_semantic.get("selected_candidates") or []
    if selected:
        fallback_en_title = str(
            (english_fallback or {}).get("en_title") or ""
        ).strip()
        missing_sources = [
            field_name
            for field_name, _label in TITLE_FIELDS
            if (
                getattr(metadata, field_name)
                and not field_sources.get(field_name)
                and not (
                    field_name == "en_title"
                    and fallback_en_title
                    and getattr(metadata, field_name) == fallback_en_title
                )
            )
        ]
        if missing_sources:
            raise ValueError(
                "可信采用条目的最终标题缺少远程字段来源："
                + "、".join(missing_sources)
            )
        return
    if any(getattr(metadata, field_name) for field_name, _label in TITLE_FIELDS):
        developer = str(metadata.developer or "")
        if "#uncheck" not in developer:
            raise ValueError("本地尽力命名保留标题时必须标记 #uncheck")


def _remote_titles(title: str) -> dict[str, str | None]:
    """为仍使用标题回退的来源适配器提取缺失语种。"""
    cleaned = re.sub(r"\[[^\]]+\]", " ", title)
    cleaned = re.sub(
        r"(?i)\b(?:v(?:er(?:sion)?)?\.?)\s*[0-9][0-9A-Za-z._-]*", " ", cleaned
    )
    result: dict[str, str | None] = {
        "jp_title": None, "zh_title": None, "en_title": None
    }
    for segment in (
        re.sub(r"\s+", " ", part).strip(" -") for part in cleaned.split("/")
    ):
        if not segment:
            continue
        if _contains_kana(segment):
            result["jp_title"] = result["jp_title"] or segment
        elif _contains_han(segment):
            result["zh_title"] = result["zh_title"] or segment
        elif _looks_english(segment):
            segment = re.sub(r"^(?:ACT|RPG|SLG|VN|ADV)\s+", "", segment, flags=re.I)
            result["en_title"] = result["en_title"] or segment
    return result


def _match_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    normalized = normalized.replace("λ", "lambda").replace("Λ", "lambda")
    return "".join(
        character.casefold() for character in normalized
        if character.isalnum()
    )


def _title_matches(first: str, second: str) -> bool:
    if _has_standalone_dlc_identity(first) != _has_standalone_dlc_identity(second):
        return False
    first_key = _match_key(first)
    second_key = _match_key(second)
    return bool(
        first_key and second_key
        and (first_key in second_key or second_key in first_key)
    )


def filter_remote_matches(
    queries: str | Sequence[str], entries: Sequence[RemoteEntry]
) -> list[RemoteEntry]:
    values = [queries] if isinstance(queries, str) else list(queries)
    query_keys = [_match_key(value) for value in values if value]
    if not query_keys:
        return []
    return [
        entry for entry in entries
        if any(
            query_key in _match_key(entry.title)
            or _match_key(entry.title) in query_key
            for query_key in query_keys
        )
    ]


def _version_key(version: str | None) -> tuple[int, ...] | None:
    if not version or not re.fullmatch(r"[0-9]+(?:[._-][0-9]+)*", version):
        return None
    return tuple(int(part) for part in re.split(r"[._-]", version))


def select_remote_references(entries: Sequence[RemoteEntry]) -> RemoteReferences:
    warnings: list[str] = []
    comparable = [
        (key, entry) for entry in entries if (key := _version_key(entry.version))
    ]
    highest = max(comparable, key=lambda pair: pair[0])[1] if comparable else None
    if entries and not comparable:
        warnings.append("远程版本无法可靠比较")
    tagged = [entry for entry in entries if entry.positive_tag in POSITIVE_PRIORITY]
    best = max(
        tagged,
        key=lambda entry: (
            POSITIVE_PRIORITY[entry.positive_tag or "去码"],
            _version_key(entry.version) or (),
        ),
    ) if tagged else None
    return RemoteReferences(highest, best, warnings)


# 名称和计划：Windows 非法字符用全角等价字符，避免丢失标题语义。
def sanitize_component(value: str, max_length: int = 230) -> str:
    cleaned = sanitize_windows_component_text(value)
    cleaned = re.sub(r"\s+", " ", cleaned).rstrip(" .")
    if not cleaned:
        raise ValueError("名称为空")
    if len(cleaned) > max_length:
        raise ValueError(f"名称超过 {max_length} 字符")
    reserved = {"CON", "PRN", "AUX", "NUL"}
    reserved.update(f"COM{i}" for i in range(1, 10))
    reserved.update(f"LPT{i}" for i in range(1, 10))
    if cleaned.upper() in reserved:
        raise ValueError("名称是 Windows 保留名")
    return cleaned


def compose_game_name(metadata: LocalMetadata) -> str:
    metadata = normalize_metadata(metadata)
    missing = [
        label for field_name, label in REQUIRED_FIELDS
        if not getattr(metadata, field_name)
    ]
    if missing:
        raise ValueError("缺少必填字段：" + "、".join(missing))
    if not any(getattr(metadata, field_name) for field_name, _ in TITLE_FIELDS):
        raise ValueError("日文名、中文名、英文名至少需要一个")
    fields = target_basename_fields(asdict(metadata))
    return sanitize_component(" ".join(fields))


PREFERRED_TARGET_BASENAME_LENGTH = 100
WINDOWS_SAFE_TARGET_PATH_LENGTH = 240


def fit_target_name(
    metadata: Mapping[str, Any],
    target_parent: Path,
    name_fields: Callable[[Mapping[str, Any]], Sequence[str]],
    marker_name: str | None = None,
) -> tuple[dict[str, Any] | None, str | None, Path | None, list[str], str | None]:
    """按英文、中文语言槽的顺序缩短目标，保留留存标题的完整字面值。"""
    current = dict(metadata)
    stages: list[tuple[dict[str, Any], list[str]]] = [(current, [])]
    steps: list[str] = []
    if current.get("en_title"):
        current = {**current, "en_title": None}
        steps = [*steps, "已去除英文名称"]
        stages.append((current, steps.copy()))
    if current.get("zh_title") and current.get("jp_title"):
        current = {**current, "zh_title": None}
        steps = [*steps, "已去除中文名称"]
        stages.append((current, steps.copy()))

    last_name: str | None = None
    last_target: Path | None = None
    last_error: str | None = None
    for candidate, applied in stages:
        if (
            not any(candidate.get(key) for key in ("jp_title", "zh_title", "en_title"))
            and candidate.get("series_count") in (None, "")
        ):
            continue
        try:
            name = sanitize_component(" ".join(name_fields(candidate)))
        except ValueError as exc:
            last_error = str(exc)
            continue
        target = (target_parent / name).resolve()
        last_name, last_target = name, target
        if (
            len(name) <= PREFERRED_TARGET_BASENAME_LENGTH
            and len(str(target)) <= WINDOWS_SAFE_TARGET_PATH_LENGTH
            and (
                marker_name is None
                or len(str(target / marker_name)) <= WINDOWS_SAFE_TARGET_PATH_LENGTH
            )
        ):
            return candidate, name, target, applied, None

    if last_name is not None and len(last_name) > PREFERRED_TARGET_BASENAME_LENGTH:
        reason = (
            f"预计名称仍超过 {PREFERRED_TARGET_BASENAME_LENGTH} 字符；"
            "必要主标题、制作方或元数据不可继续删除"
        )
    elif last_target is not None:
        reason = (
            f"目标完整路径超过 Windows 安全长度 "
            f"{WINDOWS_SAFE_TARGET_PATH_LENGTH} 字符"
        )
    else:
        reason = last_error or "无法生成预计名称"
    return None, None, None, [], reason


def build_source_shortcuts(
    remote_entries: Sequence[RemoteEntry],
) -> list[dict[str, str]]:
    """为每个可信且唯一的详情 URL 生成一个可审计的 Internet 快捷方式动作。"""
    shortcuts: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    used_names: set[str] = set()
    for entry in remote_entries:
        url = entry.url.strip()
        if (
            not re.match(r"^https?://[^\r\n]+$", url, re.I)
            or url.casefold() in seen_urls
        ):
            continue
        seen_urls.add(url.casefold())
        label = entry.source_id or entry.title or "来源"
        year_token, age_token = source_shortcut_metadata_tokens({
            "release_year": entry.release_year,
            "audience_classification": entry.audience_classification,
        })
        identifiers = [token for token in (year_token, age_token) if token]
        status = "_".join((*identifiers, label))
        stem = sanitize_component(f"[{entry.source}] {status}", max_length=180)
        name = sanitize_component(stem + ".url", max_length=190)
        suffix = 2
        while name.casefold() in used_names:
            name = sanitize_component(f"{stem} ({suffix}).url", max_length=190)
            suffix += 1
        used_names.add(name.casefold())
        shortcuts.append({"name": name, "url": url})
    return shortcuts


def internet_shortcut_text(url: str) -> str:
    return f"[InternetShortcut]\nURL={url}\n"


def _remote_dict(entry: RemoteEntry | None) -> dict[str, Any] | None:
    if not entry:
        return None
    result = {
        "title": entry.title, "version": entry.version,
        "positive_tag": entry.positive_tag, "url": entry.url,
        "source": entry.source, "source_id": entry.source_id,
        "developer": entry.developer, "engine": entry.engine,
        "jp_title": entry.jp_title, "zh_title": entry.zh_title,
        "en_title": entry.en_title,
        "languages": list(entry.languages),
        "remote_dlc": list(entry.remote_dlc),
        "remote_tags": list(entry.remote_tags),
        "related_editions": list(entry.related_editions),
        "audience_classification": entry.audience_classification,
        "audience_evidence": list(entry.audience_evidence),
        "release_year": entry.release_year,
        "release_year_evidence": list(entry.release_year_evidence),
        "maximum_release_age": entry.maximum_release_age,
    }
    if entry.source and entry.source_id and entry.url:
        result["candidate_key"] = stable_candidate_key(
            entry.source, entry.source_id, entry.url,
        )
    if entry.release_pages:
        result["release_pages"] = [
            dict(release_page) for release_page in entry.release_pages
        ]
    return result


def _serialize_search_reports(
    reports: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    serialized: list[dict[str, Any]] = []
    for report in reports:
        serialized.append({
            "source": report.get("source"),
            "entries": [
                value for entry in (report.get("entries") or [])
                if (value := _remote_dict(entry))
            ],
            "rounds": copy.deepcopy(report.get("rounds") or []),
        })
    return serialized


def plan_game(
    scan_root: Path,
    detection: GameDetection,
    metadata: LocalMetadata,
    remote_entries: Sequence[RemoteEntry],
    selected_source: str | None = None,
    selected_sources: Sequence[str] = (),
    remote_searches: Sequence[dict[str, Any]] = (),
    english_fallback: dict[str, Any] | None = None,
    allow_non_ascii_target: bool = False,
    standalone_dlc_title_language: str | None = None,
    resolved_remote_semantics: dict[str, Any] | None = None,
) -> PlanEntry:
    notes: list[str] = []
    local_engine = metadata.engine or (
        detection.engine if not is_unknown_engine(detection.engine) else None
    )
    base = replace(metadata, engine=local_engine)
    field_sources: dict[str, str] = {}
    if resolved_remote_semantics is not None:
        merged, field_sources = apply_validated_remote_semantics(
            base, resolved_remote_semantics
        )
        conflict_notes: list[str] = []
        if not merged.engine:
            merged = replace(merged, engine=UNKNOWN_ENGINE_LABEL)
    elif selected_sources:
        selected_reports = [
            {"source": source, "entries": [
                entry for entry in remote_entries if entry.source == source
            ]}
            for source in selected_sources
        ]
        merged, field_sources, conflict_notes = merge_selected_sources(
            base, selected_reports
        )
        if not merged.engine:
            merged = replace(merged, engine=UNKNOWN_ENGINE_LABEL)
    elif selected_source is None:
        merged, conflict_notes = merge_remote_consensus(base, remote_entries)
    else:
        selected_entries = [
            entry for entry in remote_entries if entry.source == selected_source
        ]
        merged, conflict_notes = merge_selected_source(
            base, selected_source or "未采用来源", selected_entries
        )
        if not merged.engine:
            merged = replace(merged, engine=UNKNOWN_ENGINE_LABEL)
    notes.extend(conflict_notes)
    merged = apply_standalone_dlc_title(
        merged, standalone_dlc_title_language,
    )
    if resolved_remote_semantics is not None:
        merged = tag_remote_all_ages(merged, resolved_remote_semantics)
    merged = normalize_metadata(merged)
    developer_conflict: dict[str, str] | None = None
    original_developer = original_name_lines(
        asdict(base), detection.root
    )[1]
    if (
        original_developer and base.developer and merged.developer
        and developer_identity_key(base.developer)
        != developer_identity_key(merged.developer)
    ):
        remote_source = field_sources.get("developer")
        if not remote_source:
            remote_source = next((
                entry.source for entry in remote_entries
                if entry.developer
                and developer_identity_key(entry.developer)
                == developer_identity_key(merged.developer)
            ), "远程")
        developer_conflict = {
            "local": str(base.developer),
            "remote": str(merged.developer),
            "source": str(remote_source),
        }
    merged, group_notes = apply_translation_group_rule(merged)
    notes.extend(group_notes)
    english_protected = is_english_exe_path(
        detection.root, detection.primary_exe, scan_root
    )
    english_projection_active = english_protected and not allow_non_ascii_target
    marker_metadata = merged if english_projection_active else None
    if english_projection_active:
        try:
            merged, removed_fields = project_english_path_metadata(
                merged, english_fallback
            )
        except ValueError as exc:
            notes.append(str(exc))
            removed_fields = []
        if removed_fields:
            notes.append(
                "英文路径保护：目标名删除非 ASCII 字段："
                + "、".join(removed_fields)
            )
        if merged.engine == ENGLISH_UNKNOWN_ENGINE_LABEL:
            notes.append(
                "英文路径保护：未识别常见引擎，使用 ASCII 占位 ENGINE_UNKNOWN"
            )
    if resolved_remote_semantics is not None:
        validate_planned_title_provenance(
            merged, resolved_remote_semantics, field_sources,
            english_fallback if english_projection_active else None,
        )
    missing = [
        label for field_name, label in REQUIRED_FIELDS
        if not getattr(merged, field_name)
    ]
    title_missing = [
        label for field_name, label in TITLE_FIELDS
        if not getattr(merged, field_name)
    ]
    if len(title_missing) == len(TITLE_FIELDS):
        notes.append("日文名、中文名、英文名至少需要一个")
        missing.append("至少一个标题")
    report_missing = [
        label for field_name, label in OPTIONAL_REPORT_FIELDS
        if not getattr(merged, field_name)
    ]
    if report_missing:
        notes.append("缺少字段：" + "、".join(report_missing))
    refs = select_remote_references(remote_entries)
    notes.extend(refs.warnings)
    if missing:
        notes.append("缺少必填字段：" + "、".join(missing))
    blocking_conflicts = [
        note for note in conflict_notes
        if (
            (
                bool(selected_sources)
                and "developer" in note
                and ("候选冲突" in note or "来源值冲突" in note)
            )
            or (
                not selected_sources
                and not note.startswith("developer 冲突：本地 ")
                and (
                    "候选冲突" in note
                    or note.startswith("官方来源 ")
                    or "标题无任一匹配" in note
                )
            )
        )
    ]
    if blocking_conflicts:
        missing.append("元数据冲突")
    source_shortcuts = build_source_shortcuts(remote_entries)
    common = {
        "metadata": merged,
        "marker_metadata": marker_metadata,
        "local_evidence": [
            *detection.evidence,
            *(
                f"{field_name}: {value}"
                for field_name, values in metadata.evidence.items()
                for value in values
            ),
        ],
        "remote_highest": _remote_dict(refs.highest_version),
        "remote_best_tag": _remote_dict(refs.best_positive_tag),
        "remote_other": [
            value for item in remote_entries if (value := _remote_dict(item))
        ],
        "notes": notes,
        "local_metadata": metadata,
        "selected_source": selected_source or None,
        "selected_sources": list(selected_sources) or (
            [selected_source] if selected_source else []
        ),
        "field_sources": field_sources,
        "remote_searches": list(remote_searches),
        "source_shortcuts": source_shortcuts,
        "developer_conflict": developer_conflict,
        "source_fingerprint": (
            directory_fingerprint(detection.root) if source_shortcuts else None
        ),
    }
    if missing:
        return PlanEntry(
            status="跳过", source=detection.root, target=None, marker_name=None,
            **common,
        )
    parent = detection.root.parent
    target_engine = normalize_engine_label(merged.engine or detection.engine)
    # 旧版把引擎作为容器目录；新版把它编码进同层 basename，并在识别到
    # 精确旧容器时提升一级。其他组织目录（包括以 ~ 开头者）原样保留。
    target_parent = (
        parent.parent
        if parent != scan_root.resolve()
        and parent.name.casefold() == target_engine.casefold()
        else parent
    )
    marker_name = None
    if marker_metadata is not None:
        try:
            marker_name = sanitize_component(
                compose_game_name(marker_metadata) + ".txt"
            )
        except ValueError as exc:
            notes.append(f"英文路径保护前规范名称无法生成 TXT：{exc}")
            return PlanEntry(
                status="跳过", source=detection.root, target=None,
                marker_name=None, **common,
            )
    source = detection.root.resolve()
    fitted, _target_name, target, shortening_steps, fit_error = fit_target_name(
        asdict(merged), target_parent, target_basename_fields, marker_name,
    )
    if fitted is None or target is None:
        notes.append(fit_error or "无法生成预计名称")
        return PlanEntry(
            status="跳过", source=source, target=None, marker_name=None, **common,
        )
    if shortening_steps:
        merged = LocalMetadata(**fitted)
        common["metadata"] = merged
        notes.append("名称缩短：" + "、".join(shortening_steps))
    if target.exists() and target != source:
        notes.append(f"目标已存在：{target}")
        return PlanEntry(
            status="跳过", source=source, target=None, marker_name=None, **common
        )
    existing_marker = source / marker_name if marker_name else None
    if existing_marker and existing_marker.exists():
        notes.append(f"标记 txt 已存在：{existing_marker}")
        return PlanEntry(
            status="跳过", source=source, target=None, marker_name=None, **common
        )
    if english_projection_active:
        notes.append(
            "英文路径保护：使用纯英文目标名，并创建英文投影前完整规范名称的空 txt"
        )
    return PlanEntry(
        status="可执行", source=source, target=target, marker_name=marker_name,
        source_fingerprint=directory_fingerprint(source),
        **{key: value for key, value in common.items() if key != "source_fingerprint"},
    )


def _read_local_text_sources(
    game_root: Path, max_bytes: int = 1_000_000,
) -> list[dict[str, str]]:
    sources: list[dict[str, str]] = []
    keywords = (
        "readme", "说明", "說明", "汉化", "漢化",
        "steam_appid", "vndb", "dlsite",
    )
    try:
        candidates = [
            path for path in game_root.rglob("*")
            if path.is_file()
            and path.stat().st_size <= max_bytes
            and (
                path.suffix.lower() in {".txt", ".md", ".nfo", ".ini"}
                or any(token in path.name.lower() for token in keywords)
            )
        ][:30]
    except OSError:
        return sources
    for path in candidates:
        try:
            payload = path.read_bytes()
        except OSError:
            continue
        for encoding in ("utf-8-sig", "cp932", "gb18030"):
            try:
                decoded = payload.decode(encoding)
                if path.name.casefold() == "steam_appid.txt":
                    decoded = f"Steam AppID={decoded.strip()}"
                sources.append({
                    "relative_path": path.relative_to(game_root).as_posix(),
                    "text": decoded,
                })
                break
            except UnicodeDecodeError:
                continue
    return sources


def _read_local_texts(game_root: Path, max_bytes: int = 1_000_000) -> list[str]:
    """兼容旧字段，同时由带相对路径的事实记录生成文本列表。"""
    return [
        source["text"]
        for source in _read_local_text_sources(game_root, max_bytes=max_bytes)
    ]


def _read_local_name_evidence(game_root: Path, limit: int = 300) -> list[str]:
    """只读取相对路径名称，用于本地汉化组、标签及来源 ID 证据。"""
    try:
        return [
            str(path.relative_to(game_root))
            for path in game_root.rglob("*")
        ][:limit]
    except OSError:
        return []


def build_analysis_input(
    scan_root: Path,
    include_paths: Sequence[Path] | None = None,
) -> dict[str, Any]:
    """导出供 Skill 分析的本地事实，不推断 developer、语言或关键词。"""
    root = scan_root.resolve()
    if not root.is_dir():
        raise ValueError(f"扫描根目录不存在：{root}")
    detections = discover_games(root, include_paths=include_paths)
    containers = discover_series_containers(root, detections)
    parent_by_child = {
        child.root: container.root
        for container in containers
        for child in container.children
    }
    games: list[dict[str, Any]] = []
    for detection in detections:
        relative = detection.root.relative_to(root).as_posix()
        ancestors = [
            path.name
            for path in detection.root.parents
            if path != root and root in path.parents
        ]
        local_names = _read_local_name_evidence(detection.root)
        local_text_sources = _read_local_text_sources(detection.root)
        local_texts = [item["text"] for item in local_text_sources]
        identifiers = extract_source_ids(
            detection.root.name, local_texts, file_names=local_names,
        )
        # 标签只从游戏根 basename 提取；内部文本仍供来源 ID 与版本证据使用。
        local = extract_local_facts(
            detection.root.name, local_texts, tag_evidence_texts=(),
        )
        games.append({
            "game_id": stable_game_id(root, detection.root),
            "relative_path": relative,
            "source_path": str(detection.root),
            "directory_fingerprint": directory_fingerprint(detection.root),
            "raw_name": detection.root.name,
            "ancestor_names": ancestors,
            "direct_children": sorted(
                child.name for child in detection.root.iterdir()
            ),
            "auxiliary_directory_candidates": [],
            "series_relative_path": (
                parent_by_child[detection.root].relative_to(root).as_posix()
                if detection.root in parent_by_child else None
            ),
            "engine": detection.engine,
            "engine_evidence": list(detection.evidence),
            "primary_exe": detection.primary_exe.relative_to(
                detection.root
            ).as_posix(),
            "source_ids": identifiers,
            "local_fields": {
                "version": local.version,
                "dlc": local.dlc,
                "dlc_version": local.dlc_version,
                "dlc_range": local.dlc_range,
                "version_tags": list(local.version_tags),
                "translation_group": None,
                "positive_tag": local.positive_tag,
                "negative_tags": list(local.negative_tags),
            },
            "local_name_evidence": local_names,
            "local_text_evidence": local_texts,
            "local_text_sources": local_text_sources,
        })
    game_id_by_root = {
        detection.root: stable_game_id(root, detection.root)
        for detection in detections
    }
    series = [{
        "series_id": (
            "series-" + hashlib.sha256(
                container.root.relative_to(root).as_posix().casefold().encode("utf-8")
            ).hexdigest()[:16]
        ),
        "relative_path": container.root.relative_to(root).as_posix(),
        "source_path": str(container.root),
        "directory_fingerprint": directory_fingerprint(container.root),
        "raw_name": container.root.name,
        "surface_local_fields": {
            "positive_tag": positive_tag_from_text(container.root.name),
            "negative_tags": list(negative_tags_from_text(container.root.name)),
        },
        "child_game_ids": sorted(
            game_id_by_root[child.root] for child in container.children
        ),
    } for container in containers]
    return make_analysis_document(
        str(root), directory_fingerprint(root), games, series
    )


def build_analysis_input_from_hierarchy(
    scan: dict[str, Any],
    hierarchy: dict[str, Any],
    hierarchy_decisions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """消费已验证层级根，构建供既有本地/远程语义阶段使用的事实文档。"""
    hierarchy_body = {
        key: value for key, value in hierarchy.items()
        if key != "hierarchy_fingerprint"
    }
    if hierarchy.get("hierarchy_fingerprint") != canonical_sha256(hierarchy_body):
        raise ValueError("已编译层级语义自身摘要不匹配")
    if hierarchy.get("scan_fingerprint") != scan.get("scan_fingerprint"):
        raise ValueError("已编译层级语义与 scan-evidence 不匹配")
    root = Path(str(scan["root"])).resolve()
    if directory_fingerprint(root) != scan.get("root_fingerprint"):
        raise ValueError("扫描根在层级编译后发生变化")
    candidate_by_id = {
        str(item["candidate_id"]): item
        for item in scan.get("executable_candidates", [])
    }
    series_containers = [
        item for item in hierarchy.get("containers", [])
        if item.get("type") == "series"
    ]
    series_relative_by_child = {
        str(child).casefold(): str(container["relative_path"])
        for container in series_containers
        for child in container.get("child_roots", [])
    }

    # 主启动器只用于路径保护和执行前校验；补丁树中的 EXE 仍保留为扫描候选，
    # 但在同一游戏另有原始启动器时绝不能被错误选作主入口。
    patch_path_tokens = (
        "patch", "update", "fix", "crack", "trainer", "tool", "utility",
        "补丁", "補丁", "汉化", "漢化", "修复", "修復", "修改器",
    )
    launcher_name_tokens = (
        "start", "startup", "launch", "launcher", "play", "game", "main",
        "启动", "啟動", "开始", "開始",
    )

    def primary_candidate_key(
        candidate: dict[str, Any], source_path: Path,
    ) -> tuple[int, int, int, str]:
        exe_path = (root / Path(str(candidate["exe_relative_path"]))).resolve()
        relative = exe_path.relative_to(source_path)
        parents = [part.casefold() for part in relative.parts[:-1]]
        has_patch_parent = any(
            any(token in part for token in patch_path_tokens) for part in parents
        )
        stem = exe_path.stem.casefold()
        is_named_launcher = any(token in stem for token in launcher_name_tokens)
        return (
            1 if has_patch_parent else 0,
            0 if is_named_launcher else 1,
            len(relative.parts),
            relative.as_posix().casefold(),
        )

    games: list[dict[str, Any]] = []
    for game_root in hierarchy.get("game_roots", []):
        source = Path(str(game_root["source_path"])).resolve()
        candidates = [
            candidate_by_id[str(candidate_id)]
            for candidate_id in game_root.get("candidate_ids", [])
        ]
        primary = min(candidates, key=lambda item: primary_candidate_key(item, source))
        known_engines = {
            normalize_engine_label((item.get("signature") or {}).get("engine"))
            for item in candidates
            if not is_unknown_engine((item.get("signature") or {}).get("engine"))
        }
        engine = (
            next(iter(known_engines))
            if len(known_engines) == 1 else UNKNOWN_ENGINE_LABEL
        )
        engine_markers = sorted({
            str(marker)
            for item in candidates
            for marker in ((item.get("signature") or {}).get("markers") or [])
        })
        local_names = _read_local_name_evidence(source)
        local_text_sources = _read_local_text_sources(source)
        local_texts = [item["text"] for item in local_text_sources]
        identifiers = extract_source_ids(
            source.name, local_texts, file_names=local_names,
        )
        # 系列父子标签在规划阶段传播，叶子本地候选只看自身根 basename。
        local = extract_local_facts(
            source.name, local_texts, tag_evidence_texts=(),
        )
        relative = source.relative_to(root).as_posix()
        primary_exe = (root / Path(str(primary["exe_relative_path"]))).resolve()
        games.append({
            "game_id": str(game_root["game_id"]),
            "relative_path": relative,
            "source_path": str(source),
            "directory_fingerprint": directory_fingerprint(source),
            "raw_name": source.name,
            "ancestor_names": [
                path.name for path in source.parents
                if path != root and root in path.parents
            ],
            "direct_children": sorted(item.name for item in source.iterdir()),
            "auxiliary_directory_candidates": [],
            "series_relative_path": series_relative_by_child.get(relative.casefold()),
            "engine": engine,
            "engine_evidence": engine_markers,
            "primary_exe": primary_exe.relative_to(source).as_posix(),
            "executable_candidate_ids": [
                str(item["candidate_id"]) for item in candidates
            ],
            "source_ids": identifiers,
            "local_fields": {
                "version": local.version, "dlc": local.dlc,
                "dlc_version": local.dlc_version,
                "dlc_range": local.dlc_range,
                "version_tags": list(local.version_tags),
                "translation_group": None,
                "positive_tag": local.positive_tag,
                "negative_tags": list(local.negative_tags),
            },
            "local_name_evidence": local_names,
            "local_text_evidence": local_texts,
            "local_text_sources": local_text_sources,
        })
    series = [{
        "series_id": str(container["container_id"]).replace(
            "organization-", "series-", 1
        ),
        "relative_path": str(container["relative_path"]),
        "source_path": str(container["source_path"]),
        "directory_fingerprint": directory_fingerprint(
            Path(str(container["source_path"]))
        ),
        "raw_name": Path(str(container["source_path"])).name,
        "surface_local_fields": {
            "positive_tag": positive_tag_from_text(
                Path(str(container["source_path"])).name
            ),
            "negative_tags": list(negative_tags_from_text(
                Path(str(container["source_path"])).name
            )),
        },
        "series_range": container.get("series_range"),
        "series_count": container.get("series_count"),
        "surface_titles": container.get("surface_titles") or {
            "jp": None, "zh": None, "en": None,
        },
        "child_game_ids": list(container.get("child_game_ids", [])),
    } for container in series_containers]
    return make_analysis_document(
        str(root), str(scan["root_fingerprint"]), games, series,
        extra={
            "hierarchy_fingerprint": hierarchy["hierarchy_fingerprint"],
            "scan_fingerprint": scan["scan_fingerprint"],
            "hierarchy_unresolved_candidates": list(
                hierarchy.get("unresolved_candidates") or []
            ),
            "hierarchy_audit": {
                "scan_evidence": scan,
                "hierarchy_semantics": hierarchy_decisions or {},
                "compiled_hierarchy": hierarchy,
            },
        },
    )


def validate_candidate_seeds(
    analysis: dict[str, Any],
    local_semantics: dict[str, Any],
    document: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, dict[str, list[str]]]]:
    """验证 Skill 对用户明确详情链接的逐游戏映射，并规范为来源 ID。"""
    if document.get("schema_version") != 1:
        raise ValueError("candidate seeds schema_version 必须为 1")
    if document.get("analysis_fingerprint") != analysis.get("input_fingerprint"):
        raise ValueError("candidate seeds 与 analysis 指纹不一致")
    local_fingerprint = canonical_sha256(local_semantics)
    if document.get("local_semantics_fingerprint") != local_fingerprint:
        raise ValueError("candidate seeds 与 local semantics 指纹不一致")
    game_ids = {str(item.get("game_id")) for item in analysis.get("games", [])}
    assignments = document.get("assignments")
    if not isinstance(assignments, list):
        raise ValueError("candidate seeds assignments 必须为数组")
    normalized: list[dict[str, Any]] = []
    identifiers: dict[str, dict[str, list[str]]] = {}
    seen: set[tuple[str, str, str]] = set()
    for raw in assignments:
        if not isinstance(raw, dict):
            raise ValueError("candidate seed assignment 必须为对象")
        game_id = str(raw.get("game_id") or "")
        source = str(raw.get("source") or "")
        source_id = str(raw.get("source_id") or "").strip()
        reason = str(raw.get("reason") or "").strip()
        if game_id not in game_ids:
            raise ValueError(f"candidate seed game_id 不存在：{game_id}")
        if source == "VNDB" and re.fullmatch(r"v\d+", source_id):
            source_key = "vndb"
            canonical_url = f"https://vndb.org/{source_id}"
        elif source == "DLsite" and re.fullmatch(
            r"(?:RJ|VJ|BJ|EJ)\d{6,8}", source_id.upper()
        ):
            source_key = "dlsite"
            source_id = source_id.upper()
            canonical_url = (
                f"{DLSITE_BASE}/maniax/work/=/product_id/{source_id}.html"
            )
        elif source == "Steam" and source_id.isdigit():
            source_key = "steam"
            canonical_url = f"{STEAM_STORE}/app/{source_id}/"
        else:
            raise ValueError(
                f"candidate seed 来源或 ID 不合法：{source} {source_id}"
            )
        supplied_url = str(raw.get("url") or "").strip()
        if supplied_url.rstrip("/") != canonical_url.rstrip("/"):
            raise ValueError(
                f"candidate seed URL 与来源 ID 不一致：{supplied_url}"
            )
        if not reason:
            raise ValueError("candidate seed 必须说明用户链接与本地游戏的对应依据")
        identity = (game_id, source, source_id)
        if identity in seen:
            raise ValueError(f"candidate seed 重复：{identity}")
        seen.add(identity)
        item = {
            "game_id": game_id,
            "source": source,
            "source_id": source_id,
            "url": canonical_url,
            "reason": reason,
        }
        provided_url = str(raw.get("provided_url") or "").strip()
        if provided_url:
            item["provided_url"] = provided_url
        release_titles = raw.get("release_title_evidence")
        if release_titles is not None:
            if (
                source != "VNDB"
                or not provided_url
                or not re.fullmatch(r"https://vndb\.org/r\d+/?", provided_url)
                or not isinstance(release_titles, dict)
                or not release_titles
                or set(release_titles) - {"jp", "zh", "en"}
            ):
                raise ValueError(
                    "release_title_evidence 只可用于带 VNDB release 链接的种子"
                )
            normalized_titles: dict[str, str] = {}
            for language, evidence in release_titles.items():
                if isinstance(evidence, dict):
                    if set(evidence) != {"value", "reason"}:
                        raise ValueError(
                            f"release_title_evidence.{language} 字段无效：{game_id}"
                        )
                    value = str(evidence.get("value") or "").strip()
                    title_reason = str(evidence.get("reason") or "").strip()
                    if not value or not title_reason:
                        raise ValueError(
                            f"release_title_evidence.{language} 值和理由不能为空：{game_id}"
                        )
                else:
                    # 内部候选种子已在 user-supplements 阶段验证理由，只携带标题值。
                    value = str(evidence or "").strip()
                    if not value:
                        raise ValueError(
                            f"release_title_evidence.{language} 值不能为空：{game_id}"
                        )
                normalized_titles[language] = value
            item["release_title_evidence"] = normalized_titles
        normalized.append(item)
        game_identifiers = identifiers.setdefault(
            game_id, {
                "vndb": [], "dlsite": [], "steam": [],
                "vndb_release": [],
            }
        )
        game_identifiers[source_key].append(source_id)
        if source == "VNDB" and provided_url:
            release_match = re.fullmatch(
                r"https://vndb\.org/(r\d+)/?", provided_url
            )
            if release_match:
                game_identifiers["vndb_release"].append(
                    release_match.group(1)
                )
                if release_titles:
                    game_identifiers.setdefault(
                        "vndb_release_titles", {}
                    )[release_match.group(1)] = normalized_titles
    normalized.sort(
        key=lambda item: (
            item["game_id"], item["source"], item["source_id"]
        )
    )
    normalized_document = {
        "schema_version": 1,
        "analysis_fingerprint": analysis["input_fingerprint"],
        "local_semantics_fingerprint": local_fingerprint,
        "assignments": normalized,
    }
    return normalized_document, identifiers


def candidate_seeds_from_user_supplements(
    analysis: dict[str, Any],
    local_semantics: dict[str, Any],
    document: dict[str, Any],
) -> dict[str, Any]:
    """把已绑定的人工补充详情链接转换为现有候选种子契约。"""
    normalized = validate_user_supplements(analysis, document)
    expected_fingerprint = canonical_sha256(normalized)
    if (
        local_semantics.get("user_supplements") != normalized
        or local_semantics.get("user_supplements_fingerprint")
        != expected_fingerprint
    ):
        raise ValueError("user supplements 与 local semantics 绑定不一致")
    return {
        "schema_version": 1,
        "analysis_fingerprint": analysis["input_fingerprint"],
        "local_semantics_fingerprint": canonical_sha256(local_semantics),
        "assignments": list(normalized["candidate_seeds"]),
    }


def merge_candidate_seed_documents(
    analysis: dict[str, Any],
    local_semantics: dict[str, Any],
    documents: Sequence[dict[str, Any]],
) -> dict[str, Any] | None:
    """合并旧候选种子与人工补充种子，并复用同一严格校验。"""
    assignments: list[dict[str, Any]] = []
    for document in documents:
        normalized, _identifiers = validate_candidate_seeds(
            analysis, local_semantics, document,
        )
        assignments.extend(normalized["assignments"])
    if not assignments:
        return None
    combined = {
        "schema_version": 1,
        "analysis_fingerprint": analysis["input_fingerprint"],
        "local_semantics_fingerprint": canonical_sha256(local_semantics),
        "assignments": assignments,
    }
    normalized, _identifiers = validate_candidate_seeds(
        analysis, local_semantics, combined,
    )
    return normalized


def attach_user_supplements_to_search_evidence(
    evidence: dict[str, Any],
    supplements: dict[str, Any] | None,
) -> dict[str, Any]:
    """把人工补充正文纳入追加式搜索事实和搜索指纹。"""
    if supplements is None:
        return evidence
    body = {
        key: value for key, value in evidence.items()
        if key != "search_fingerprint"
    }
    body["user_supplements"] = supplements
    body["user_supplements_fingerprint"] = canonical_sha256(supplements)
    return {**body, "search_fingerprint": canonical_sha256(body)}


def build_search_evidence(
    analysis: dict[str, Any],
    local_semantics: dict[str, Any],
    sources: Sequence[Any],
    candidate_seeds: dict[str, Any] | None = None,
    progress: Callable[[str, int, int], None] | None = None,
) -> dict[str, Any]:
    """校验 Skill 语义后，仅按其中的关键词构建三站查询证据。"""
    semantics_by_id = validate_local_semantics(analysis, local_semantics)
    search_analysis = analysis
    normalized_seeds: dict[str, Any] | None = None
    if candidate_seeds is not None:
        normalized_seeds, seed_identifiers = validate_candidate_seeds(
            analysis, local_semantics, candidate_seeds
        )
        search_analysis = copy.deepcopy(analysis)
        for game in search_analysis.get("games", []):
            game_id = str(game.get("game_id") or "")
            if game_id not in seed_identifiers:
                continue
            existing = game.setdefault(
                "source_ids", {"vndb": [], "dlsite": [], "steam": []}
            )
            for source_key, values in seed_identifiers[game_id].items():
                if source_key == "vndb_release_titles":
                    existing["trusted_vndb_release_titles"] = dict(values)
                    continue
                existing[source_key] = list(dict.fromkeys([
                    *(existing.get(source_key) or []),
                    *values,
                ]))
                existing[f"trusted_{source_key}"] = list(dict.fromkeys([
                    *(existing.get(f"trusted_{source_key}") or []),
                    *values,
                ]))
    document = make_search_document(
        search_analysis,
        local_semantics,
        semantics_by_id,
        sources,
        _remote_dict,
        progress,
    )
    if normalized_seeds is None:
        return document
    body = {
        key: value for key, value in document.items()
        if key != "search_fingerprint"
    }
    body["candidate_seeds"] = normalized_seeds
    body["candidate_seeds_fingerprint"] = canonical_sha256(normalized_seeds)
    return {**body, "search_fingerprint": canonical_sha256(body)}


def preflight_search_sources(sources: Sequence[Any]) -> None:
    """顺序验证三来源，避免并发 TLS 握手超时；全部通过才查询游戏。"""
    if not sources:
        raise ValueError("三来源连通预检缺少来源客户端")
    failures: dict[int, str] = {}
    for index, source in enumerate(sources):
        try:
            source.probe_connectivity()
        except Exception as exc:
            failures[index] = (
                f"{type(exc).__name__}: {exc}"
                if str(exc) else type(exc).__name__
            )
    if failures:
        details = "；".join(
            f"{sources[index].name}: {failures[index]}"
            for index in sorted(failures)
        )
        raise RuntimeError(
            f"三来源连通预检失败，已在正式搜索前停止：{details}"
        )


def _candidate_lookup(search_game: dict[str, Any]) -> dict[tuple[str, str, str], dict[str, Any]]:
    candidates: dict[tuple[str, str, str], dict[str, Any]] = {}
    for source in search_game.get("sources", []):
        for candidate in source.get("candidates", []):
            identity = (
                str(candidate.get("source") or source.get("source") or ""),
                str(candidate.get("source_id") or ""),
                str(candidate.get("url") or ""),
            )
            candidates[identity] = candidate
    return candidates


def _approved_remote_entries(
    search_game: dict[str, Any],
    remote_semantic: dict[str, Any],
) -> list[RemoteEntry]:
    """仅把 Skill 明确采用的字段放回 RemoteEntry，保留原始参考字段。"""
    lookup = _candidate_lookup(search_game)
    returned = remote_semantic.get("returned_titles") or {}
    field_sources = remote_semantic.get("field_sources") or {}
    remote_engine = remote_semantic.get("engine") or {}
    entries: list[RemoteEntry] = []
    for selected in remote_semantic.get("selected_candidates", []):
        identity = (
            str(selected.get("source") or ""),
            str(selected.get("source_id") or ""),
            str(selected.get("url") or ""),
        )
        raw = lookup[identity]
        source = identity[0]
        release = selected.get("release") if source == "VNDB" else None
        display_source_id = (
            str(release.get("source_id") or "") if isinstance(release, dict)
            else identity[1]
        )
        display_url = (
            str(release.get("url") or "") if isinstance(release, dict)
            else identity[2]
        )
        title_values = {
            "jp_title": returned.get("jp") if field_sources.get("jp") == source else None,
            "zh_title": returned.get("zh") if field_sources.get("zh") == source else None,
            "en_title": returned.get("en") if field_sources.get("en") == source else None,
        }
        primary_title = next(
            (value for value in title_values.values() if value),
            str(raw.get("title") or ""),
        )
        entries.append(RemoteEntry(
            title=primary_title,
            url=display_url,
            source=source,
            source_id=display_source_id or None,
            developer=(
                remote_semantic.get("developer")
                if field_sources.get("developer") == source else None
            ),
            engine=(
                remote_engine.get("value")
                if isinstance(remote_engine, dict)
                and remote_engine.get("source") == source else None
            ),
            version=raw.get("version"),
            positive_tag=raw.get("positive_tag"),
            languages=tuple(raw.get("languages") or ()),
            remote_dlc=tuple(raw.get("remote_dlc") or ()),
            remote_tags=tuple(raw.get("remote_tags") or ()),
            related_editions=tuple(raw.get("related_editions") or ()),
            audience_classification=raw.get("audience_classification"),
            audience_evidence=tuple(raw.get("audience_evidence") or ()),
            release_year=raw.get("release_year"),
            release_year_evidence=tuple(raw.get("release_year_evidence") or ()),
            maximum_release_age=raw.get("maximum_release_age"),
            **title_values,
        ))
    return entries


def _reference_remote_entries(
    search_game: dict[str, Any], remote_semantic: dict[str, Any],
) -> list[RemoteEntry]:
    """把 Skill 确认的同游戏参考候选还原为只读远程条目。"""
    lookup = _candidate_lookup(search_game)
    entries: list[RemoteEntry] = []
    for reference in remote_semantic.get("reference_candidates", []):
        identity = (
            str(reference.get("source") or ""),
            str(reference.get("source_id") or ""),
            str(reference.get("url") or ""),
        )
        raw = lookup[identity]
        entries.append(RemoteEntry(
            title=str(raw.get("title") or ""),
            url=identity[2],
            source=identity[0],
            source_id=identity[1] or None,
            developer=raw.get("developer"),
            engine=raw.get("engine"),
            jp_title=raw.get("jp_title"),
            zh_title=raw.get("zh_title"),
            en_title=raw.get("en_title"),
            version=raw.get("version"),
            positive_tag=raw.get("positive_tag"),
            languages=tuple(raw.get("languages") or ()),
            remote_dlc=tuple(raw.get("remote_dlc") or ()),
            remote_tags=tuple(raw.get("remote_tags") or ()),
            related_editions=tuple(raw.get("related_editions") or ()),
            audience_classification=raw.get("audience_classification"),
            audience_evidence=tuple(raw.get("audience_evidence") or ()),
            release_year=raw.get("release_year"),
            release_year_evidence=tuple(raw.get("release_year_evidence") or ()),
            maximum_release_age=raw.get("maximum_release_age"),
        ))
    return entries


def _trusted_reference_payload(
    selected: Sequence[RemoteEntry], references: Sequence[RemoteEntry],
) -> tuple[RemoteReferences, list[dict[str, str]]]:
    """合并可信 URL，只计算参考与快捷方式，不返回任何命名字段。"""
    by_identity: dict[tuple[str, str, str], RemoteEntry] = {}
    for entry in (*selected, *references):
        identity = (entry.source, entry.source_id or "", entry.url)
        by_identity.setdefault(identity, entry)
    trusted = list(by_identity.values())
    return select_remote_references(trusted), build_source_shortcuts(trusted)


def _pending_source_shortcuts(
    source: Path, shortcuts: Sequence[dict[str, str]],
) -> list[dict[str, str]]:
    """只保留尚未存在的快捷方式动作；精确相同的既有文件直接复用。"""
    pending: list[dict[str, str]] = []
    for shortcut in shortcuts:
        path = source / str(shortcut["name"])
        if not path.exists():
            pending.append(dict(shortcut))
            continue
        if not path.is_file():
            raise ValueError(f"来源快捷方式路径不是文件：{path}")
        try:
            current = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"无法读取既有来源快捷方式：{path}") from exc
        expected = internet_shortcut_text(str(shortcut["url"]))
        if current != expected:
            raise ValueError(f"既有来源快捷方式内容冲突：{path}")
    return pending


def _unchecked_local_metadata(metadata: LocalMetadata) -> LocalMetadata:
    """远程未确认时保留本地 developer，并在字段内部标记待核。"""
    developer = str(metadata.developer or "unknown").strip()
    if not developer.endswith("#uncheck"):
        developer = f"{developer} #uncheck"
    return replace(metadata, developer=developer)


def _fallback_local_metadata(
    metadata: LocalMetadata,
    selected: Sequence[RemoteEntry],
    references: Sequence[RemoteEntry],
) -> LocalMetadata:
    """没有最终采用候选时，本地尽力名称一律显式标记待核。"""
    del selected, references
    return _unchecked_local_metadata(metadata)


def _route_to_query_failure(
    entry: PlanEntry, query_reason: str,
) -> PlanEntry:
    """把可执行的本地尽力名称放入同一规划锚点下的查询失败容器。"""
    entry.notes.append(query_reason)
    if entry.status != "可执行" or entry.target is None:
        return entry
    target = entry.target
    failure_target = (
        target if target.parent.name == QUERY_FAILURE_CONTAINER
        else target.parent / QUERY_FAILURE_CONTAINER / target.name
    ).resolve()
    if len(str(failure_target)) > 240 or (
        entry.marker_name
        and len(str(failure_target / entry.marker_name)) > 240
    ):
        entry.status = "跳过"
        entry.target = None
        entry.marker_name = None
        entry.notes.append("查询失败容器中的目标路径超过 Windows 安全长度 240 字符")
        return entry
    if failure_target.exists() and failure_target != entry.source:
        entry.status = "跳过"
        entry.target = None
        entry.marker_name = None
        entry.notes.append(f"查询失败目标已存在：{failure_target}")
        return entry
    entry.target = failure_target
    entry.notes.append(f"远程未确认，尽力规范化并移入：{failure_target.parent}")
    return entry


def series_propagated_preview_line_indexes(
    source: Path, metadata: dict[str, Any], tag_propagated: bool,
) -> list[int]:
    """记录由成员补入、且系列表面原名中不存在的预览字段。"""
    raw_name = source.name
    indexes: list[int] = []
    for index, key in (
        (1, "developer"), (2, "jp_title"), (3, "zh_title"), (4, "en_title"),
    ):
        value = str(metadata.get(key) or "").strip()
        comparison = re.sub(r"\s+#uncheck$", "", value, flags=re.I)
        if comparison and comparison not in raw_name:
            indexes.append(index)
    if tag_propagated:
        indexes.append(5)
    return indexes


def child_propagated_preview_line_indexes(
    titles: dict[str, Any], remote_semantic: dict[str, Any],
    tag_propagated: bool,
    propagated_title_languages: Sequence[str] = (),
) -> list[int]:
    """记录只填入空语种槽的标题与标签传播行。"""
    del titles, remote_semantic
    indexes = [
        {"jp": 2, "zh": 3, "en": 4}[language]
        for language in propagated_title_languages
        if language in {"jp", "zh", "en"}
    ]
    if tag_propagated:
        indexes.append(5)
    return sorted(set(indexes))


def fill_empty_child_title_slots(
    entry: dict[str, Any], common_titles: dict[str, str | None],
) -> list[str]:
    """仅向三个标题槽都为空的可执行子游戏补入审核共同标题。"""
    if entry.get("kind") != "game" or entry.get("status") != "可执行":
        return []
    metadata = dict(entry.get("metadata") or {})
    title_fields = {"jp": "jp_title", "zh": "zh_title", "en": "en_title"}
    if any(metadata.get(field) for field in title_fields.values()):
        return []
    filled: dict[str, str] = {}
    for language, field_name in title_fields.items():
        value = common_titles.get(language)
        if value and not metadata.get(field_name):
            metadata[field_name] = value
            filled[language] = value
    if not filled:
        return []
    entry["metadata"] = metadata
    entry["target_preview_lines"] = list(target_name_lines(metadata))
    entry["propagated_title_context"] = {
        language: filled.get(language)
        for language in ("jp", "zh", "en")
    }
    indexes = {"jp": 2, "zh": 3, "en": 4}
    entry["propagated_preview_line_indexes"] = sorted(set(
        list(entry.get("propagated_preview_line_indexes") or ())
        + [indexes[language] for language in filled]
    ))
    target = Path(str(entry["target"])).resolve()
    new_name = sanitize_component(" ".join(target_basename_fields(metadata)))
    entry["target"] = str(target.with_name(new_name))
    entry.setdefault("notes", []).append(
        "仅在子游戏日中英标题槽全部为空时补入 Skill 已审核的系列共同标题"
    )
    return list(filled)


def build_manifest_from_semantics(
    analysis: dict[str, Any],
    local_semantics: dict[str, Any],
    search_evidence: dict[str, Any],
    remote_semantics: dict[str, Any],
) -> dict[str, Any]:
    """消费两阶段 Skill 判断，生成仍受现有 apply/undo 保护的 manifest。"""
    root = Path(str(analysis["root"])).resolve()
    expected_root_fingerprint = str(
        analysis.get("root_fingerprint") or ""
    )
    if not root.is_dir() or not expected_root_fingerprint:
        raise ValueError("分析输入缺少可验证的扫描根目录指纹")
    current_root_fingerprint = directory_fingerprint(root)
    if current_root_fingerprint != expected_root_fingerprint:
        raise ValueError("扫描根目录在分析后发生变化，必须重新分析")
    local_by_id = validate_local_semantics(analysis, local_semantics)
    local_series_by_id = validate_local_series_semantics(
        analysis, local_semantics
    )
    remote_by_id = validate_remote_semantics(
        analysis, local_semantics, search_evidence, remote_semantics
    )
    search_by_id = {
        str(item["game_id"]): item for item in search_evidence.get("games", [])
    }
    analysis_by_id = {
        str(item["game_id"]): item for item in analysis.get("games", [])
    }
    series_by_relative = {
        str(item["relative_path"]): item for item in analysis.get("series", [])
    }
    series_by_id = {
        str(item["series_id"]): item for item in analysis.get("series", [])
    }
    # 先从每个成员已审核标签中计算共同标签，再与合集表面标签合并后下传。
    series_tags_by_relative: dict[
        str, tuple[str | None, tuple[str, ...]]
    ] = {}
    series_tag_sources_by_id: dict[
        str, tuple[str | None, tuple[str, ...], bool]
    ] = {}
    for series in analysis.get("series", []):
        reviewed_surface = (
            local_series_by_id[str(series["series_id"])]
            .get("reviewed_surface_fields") or {}
        )
        surface_positive = reviewed_surface.get("positive_tag")
        surface_negative = tuple(reviewed_surface.get("negative_tags") or ())
        child_fields = [
            local_by_id[str(child_id)].get("reviewed_local_fields") or {}
            for child_id in series.get("child_game_ids", [])
        ]
        common_positive, common_negative = common_child_tags(child_fields)
        effective_positive = merge_positive_tags(
            surface_positive, common_positive
        )
        effective_negative = merge_negative_tags(
            surface_negative, common_negative
        )
        tags_from_children = (
            effective_positive != surface_positive
            or effective_negative != surface_negative
        )
        series_tags_by_relative[str(series.get("relative_path") or "")] = (
            effective_positive,
            effective_negative,
        )
        series_tag_sources_by_id[str(series["series_id"])] = (
            effective_positive, effective_negative, tags_from_children
        )
    entries: list[dict[str, Any]] = []
    for game_id, local_semantic in local_by_id.items():
        evidence = analysis_by_id[game_id]
        source = Path(str(evidence["source_path"])).resolve()
        fields_data = local_semantic.get("reviewed_local_fields") or {}
        detection = GameDetection(
            root=source,
            engine=normalize_engine_label(fields_data.get("engine")),
            primary_exe=(source / str(evidence["primary_exe"])).resolve(),
            evidence=tuple(evidence.get("engine_evidence") or ()),
        )
        credit = local_semantic.get("translation_credit") or {}
        parent_series = series_by_relative.get(
            str(evidence.get("series_relative_path") or "")
        )
        inherited_positive, inherited_negative = (
            series_tags_by_relative.get(
                str(evidence.get("series_relative_path") or ""),
                (None, ()),
            ) if parent_series else (None, ())
        )
        local_positive = fields_data.get("positive_tag")
        local_negative = tuple(fields_data.get("negative_tags") or ())
        effective_child_positive = merge_positive_tags(
            local_positive, inherited_positive
        )
        effective_child_negative = merge_negative_tags(
            local_negative, inherited_negative
        )
        tag_propagated = (
            effective_child_positive != local_positive
            or effective_child_negative != local_negative
        )
        titles = local_semantic.get("titles") or {}
        developer_evidence = local_semantic.get("developer_evidence") or {}
        local_metadata = LocalMetadata(
            engine=detection.engine,
            developer=developer_evidence.get("value"),
            jp_title=titles.get("jp"),
            zh_title=titles.get("zh"),
            en_title=titles.get("en"),
            version=fields_data.get("version"),
            dlc=bool(fields_data.get("dlc")),
            dlc_version=fields_data.get("dlc_version"),
            dlc_range=fields_data.get("dlc_range"),
            version_tags=tuple(fields_data.get("version_tags") or ()),
            translation_group=(
                credit.get("value")
                if isinstance(credit, dict) and credit.get("value")
                else None
            ),
            positive_tag=effective_child_positive,
            negative_tags=effective_child_negative,
            evidence={
                "Skill": [str(local_semantic.get("reason") or "")],
            },
        )
        search_game = search_by_id[game_id]
        remote_semantic = remote_by_id[game_id]
        propagated_line_indexes = child_propagated_preview_line_indexes(
            titles, remote_semantic, tag_propagated,
        )
        query_outcome = classify_query_outcome(remote_semantic)
        auxiliary_actions: list[dict[str, str]] = []
        auxiliary_notes: list[str] = []
        for auxiliary in local_semantic.get("auxiliary_directories") or []:
            source_name = str(auxiliary["relative_path"])
            target_name = auxiliary_target_name(
                source_name, str(auxiliary["kind"]),
            )
            target_child = source / target_name
            if target_child.exists() and target_name.casefold() != source_name.casefold():
                auxiliary_notes.append(
                    f"附属目录目标已存在，跳过内部动作：{source_name} -> {target_name}"
                )
                continue
            if source_name != target_name:
                auxiliary_actions.append({
                    "source_name": source_name,
                    "target_name": target_name,
                    "kind": str(auxiliary["kind"]),
                    "source_fingerprint": str(auxiliary["directory_fingerprint"]),
                })
        reports = search_reports_for_preview(search_game)
        references = _reference_remote_entries(search_game, remote_semantic)
        reference_identities = list(
            remote_semantic.get("reference_candidates") or []
        )
        if (
            local_semantic.get("review_state") != "accepted"
            or (
                not query_outcome.found
                and bool(remote_semantic.get("selected_candidates"))
            )
        ):
            approved = _approved_remote_entries(search_game, remote_semantic)
            selected_sources = sorted(
                {entry.source for entry in approved},
                key=lambda value: {
                    "VNDB": 0, "DLsite": 1, "Steam": 2,
                }.get(value, 99),
            )
            all_candidates = [
                candidate
                for source_report in search_game.get("sources", [])
                for candidate in source_report.get("candidates", [])
            ]
            refs, shortcuts = _trusted_reference_payload(approved, references)
            shortcuts = _pending_source_shortcuts(source, shortcuts)
            skipped = PlanEntry(
                status="跳过",
                source=source,
                target=None,
                marker_name=None,
                metadata=local_metadata,
                local_metadata=local_metadata,
                local_evidence=list(detection.evidence),
                remote_other=all_candidates,
                remote_highest=_remote_dict(refs.highest_version),
                remote_best_tag=_remote_dict(refs.best_positive_tag),
                notes=[
                    str(local_semantic.get("reason") or "本地语义待判断"),
                    str(remote_semantic.get("reason") or "远程候选待判断"),
                ],
                remote_searches=reports,
                selected_sources=selected_sources,
                source_shortcuts=shortcuts,
                source_fingerprint=(
                    directory_fingerprint(source) if shortcuts else None
                ),
            ).to_dict()
            skipped.update({
                "kind": "game",
                "game_id": game_id,
                "query_outcome": asdict(query_outcome),
                "selected_candidates": list(
                    remote_semantic.get("selected_candidates") or []
                ),
                "reference_candidates": reference_identities,
                "source_disposition": dict(
                    remote_semantic.get("source_disposition") or {}
                ),
                "preview_candidate_keys": dict(remote_semantic.get("preview_candidate_keys") or {}),
                "preview_candidate_dispositions": dict(remote_semantic.get("preview_candidate_dispositions") or {}),
                "local_title_segments": list(titles.get("local_segments") or []),
                "local_title_segments_by_language": dict(
                    titles.get("local_segments_by_language") or {}
                ),
                "parent_series_source": (
                    str(parent_series["source_path"]) if parent_series else None
                ),
                "auxiliary_directory_renames": auxiliary_actions,
                "propagated_preview_line_indexes": propagated_line_indexes,
                "standalone_dlc_title_language": (
                    remote_semantic.get("standalone_dlc_title_language")
                    or local_semantic.get("standalone_dlc_title_language")
                ),
            })
            skipped["notes"].extend(auxiliary_notes)
            entries.append(skipped)
            continue
        if not query_outcome.found:
            approved = _approved_remote_entries(search_game, remote_semantic)
            refs, shortcuts = _trusted_reference_payload(approved, references)
            shortcuts = _pending_source_shortcuts(source, shortcuts)
            fallback = _fallback_local_metadata(
                local_metadata, approved, references
            )
            local_fallback_entry = plan_game(
                    root,
                    detection,
                    fallback,
                    [],
                    remote_searches=reports,
                    english_fallback=local_semantic.get("english_fallback"),
                    allow_non_ascii_target=bool(
                        (local_semantic.get("english_path_override") or {}).get(
                            "allow_non_ascii"
                        )
                    ),
                    standalone_dlc_title_language=(
                        remote_semantic.get("standalone_dlc_title_language")
                        or local_semantic.get("standalone_dlc_title_language")
                    ),
                )
            validate_planned_title_provenance(
                local_fallback_entry.metadata, remote_semantic,
                local_fallback_entry.field_sources,
                local_semantic.get("english_fallback"),
            )
            planned_entry = _route_to_query_failure(
                local_fallback_entry,
                query_outcome.reason,
            )
            planned = planned_entry.to_dict()
            planned["remote_highest"] = _remote_dict(refs.highest_version)
            planned["remote_best_tag"] = _remote_dict(refs.best_positive_tag)
            planned["source_shortcuts"] = shortcuts
            if shortcuts and not planned.get("source_fingerprint"):
                planned["source_fingerprint"] = directory_fingerprint(source)
            planned.update({
                "kind": "game",
                "game_id": game_id,
                "query_outcome": asdict(query_outcome),
                "parent_series_source": (
                    str(parent_series["source_path"]) if parent_series else None
                ),
                "semantic_reason": remote_semantic.get("reason"),
                "selected_candidates": list(
                    remote_semantic.get("selected_candidates") or []
                ),
                "reference_candidates": reference_identities,
                "source_disposition": dict(
                    remote_semantic.get("source_disposition") or {}
                ),
                "preview_candidate_keys": dict(remote_semantic.get("preview_candidate_keys") or {}),
                "preview_candidate_dispositions": dict(remote_semantic.get("preview_candidate_dispositions") or {}),
                "local_title_segments": list(titles.get("local_segments") or []),
                "local_title_segments_by_language": dict(
                    titles.get("local_segments_by_language") or {}
                ),
                "auxiliary_directory_renames": auxiliary_actions,
                "propagated_preview_line_indexes": propagated_line_indexes,
                "standalone_dlc_title_language": (
                    remote_semantic.get("standalone_dlc_title_language")
                    or local_semantic.get("standalone_dlc_title_language")
                ),
            })
            planned["notes"].extend(auxiliary_notes)
            entries.append(planned)
            continue
        approved = _approved_remote_entries(search_game, remote_semantic)
        selected_sources = sorted(
            {entry.source for entry in approved},
            key=lambda value: {"VNDB": 0, "DLsite": 1, "Steam": 2}.get(
                value, 99
            ),
        )
        planned = plan_game(
            root,
            detection,
            local_metadata,
            approved,
            selected_sources=selected_sources,
            remote_searches=reports,
            english_fallback=local_semantic.get("english_fallback"),
            allow_non_ascii_target=bool(
                (local_semantic.get("english_path_override") or {}).get(
                    "allow_non_ascii"
                )
            ),
            standalone_dlc_title_language=(
                remote_semantic.get("standalone_dlc_title_language")
                or local_semantic.get("standalone_dlc_title_language")
            ),
            resolved_remote_semantics=remote_semantic,
        ).to_dict()
        refs, shortcuts = _trusted_reference_payload(approved, references)
        shortcuts = _pending_source_shortcuts(source, shortcuts)
        planned["remote_highest"] = _remote_dict(refs.highest_version)
        planned["remote_best_tag"] = _remote_dict(refs.best_positive_tag)
        planned["source_shortcuts"] = shortcuts
        planned.update({
            "kind": "game",
            "game_id": game_id,
            "query_outcome": asdict(query_outcome),
            "parent_series_source": (
                str(parent_series["source_path"]) if parent_series else None
            ),
            "semantic_reason": remote_semantic.get("reason"),
            "selected_candidates": list(
                remote_semantic.get("selected_candidates") or []
            ),
            "reference_candidates": reference_identities,
            "source_disposition": dict(
                remote_semantic.get("source_disposition") or {}
            ),
            "preview_candidate_keys": dict(remote_semantic.get("preview_candidate_keys") or {}),
            "preview_candidate_dispositions": dict(remote_semantic.get("preview_candidate_dispositions") or {}),
            "local_title_segments": list(titles.get("local_segments") or []),
            "local_title_segments_by_language": dict(
                titles.get("local_segments_by_language") or {}
            ),
            "auxiliary_directory_renames": auxiliary_actions,
            "propagated_preview_line_indexes": propagated_line_indexes,
            "standalone_dlc_title_language": (
                remote_semantic.get("standalone_dlc_title_language")
                or local_semantic.get("standalone_dlc_title_language")
            ),
        })
        planned["notes"].extend(auxiliary_notes)
        entries.append(planned)

    unresolved_ids: set[str] = set()
    unresolved_by_series: dict[str, list[str]] = {}
    for candidate in unresolved_candidates_for_preview(
        analysis, local_semantics,
    ):
        candidate_id = str(candidate["candidate_id"])
        synthetic_id = f"unresolved-{candidate_id}"
        unresolved_ids.add(synthetic_id)
        source = (root / Path(str(candidate["exe_relative_path"]))).resolve()
        parent_series = next((
            series for series in analysis.get("series", [])
            if _inside(source, Path(str(series["source_path"])).resolve())
        ), None)
        if parent_series:
            unresolved_by_series.setdefault(
                str(parent_series["series_id"]), []
            ).append(candidate_id)
        entries.append({
            "kind": "game",
            "game_id": synthetic_id,
            "status": "跳过",
            "source": str(source),
            "target": None,
            "marker_name": None,
            "source_fingerprint": None,
            "metadata": {},
            "local_metadata": {},
            "local_evidence": [str(candidate.get("reason") or "")],
            "notes": [
                "独立启动文件没有自己的目录，禁止自动拆分共享资源；需人工判断后重新预览",
                str(candidate.get("reason") or ""),
            ],
            "remote_searches": [],
            "source_shortcuts": [],
            "selected_candidates": [],
            "reference_candidates": [],
            "source_disposition": {},
            "query_outcome": {
                "found": False,
                "reason": "未分目录独立启动项未联网搜索",
            },
            "parent_series_source": (
                str(parent_series["source_path"]) if parent_series else None
            ),
            "unresolved_launcher": True,
            "candidate_id": candidate_id,
        })

    entry_by_id = {
        str(entry["game_id"]): entry for entry in entries
    }
    series_entries: list[dict[str, Any]] = []
    for series_id, series in series_by_id.items():
        child_ids = [str(value) for value in series.get("child_game_ids", [])]
        child_evidence = [analysis_by_id[value] for value in child_ids]
        child_entries = [entry_by_id[value] for value in child_ids]
        child_local = [local_by_id[value] for value in child_ids]
        child_remote = [remote_by_id[value] for value in child_ids]
        all_children_found = not unresolved_by_series.get(series_id) and all(
            bool((entry.get("query_outcome") or {}).get("found"))
            for entry in child_entries
        )
        final_child_developers = [
            str((entry.get("metadata") or {}).get("developer") or "").strip()
            or None
            for entry in child_entries
        ]
        local_child_developers = [
            str((item.get("developer_evidence") or {}).get("value") or "").strip()
            or None
            for item in child_local
        ]
        common_series_developer = series_developer_from_children(
            final_child_developers,
            local_child_developers,
            all_children_found=all_children_found,
        )
        series_developers = (
            [common_series_developer for _ in child_ids]
            if common_series_developer else [None for _ in child_ids]
        )
        # 标题只用已审核共同值填补空槽；不同标题不裁剪、不任选。
        resolved_common_titles = common_series_titles(
            child_local,
            child_remote,
            allow_local_fallback=not all_children_found,
        )
        surface_titles = {
            language: str((series.get("surface_titles") or {}).get(language) or "").strip()
            or None
            for language in ("jp", "zh", "en")
        }
        resolved_series_titles = (
            surface_titles if any(surface_titles.values()) else resolved_common_titles
        )
        series_common_titles: list[dict[str, str | None]] = (
            [resolved_series_titles]
            if any(resolved_series_titles.values()) else []
        )
        for child_entry in child_entries:
            fill_empty_child_title_slots(child_entry, resolved_common_titles)
        layout = plan_series_layout(
            [normalize_engine_label(item.get("engine")) for item in child_evidence],
            series_developers,
            series_common_titles,
        )
        series_positive, series_negative, series_tag_from_children = (
            series_tag_sources_by_id.get(series_id, (None, (), False))
        )
        if not all_children_found:
            series_source = Path(str(series["source_path"])).resolve()
            series_parent = series_source.parent
            child_engines = [
                normalize_engine_label(item.get("engine")) for item in child_evidence
            ]
            known_engines = {
                value for value in child_engines if not is_unknown_engine(value)
            }
            common_engine = (
                next(iter(known_engines))
                if len(known_engines) == 1
                and len(known_engines) == len(set(child_engines))
                and all(not is_unknown_engine(value) for value in child_engines)
                else None
            )
            engine_label = (
                common_engine
                if common_engine else (
                    UNKNOWN_ENGINE_LABEL if not known_engines else "多引擎"
                )
            )
            target_parent = series_hub_target_parent(root, series_source)
            status = "可执行"
            notes = ["合集存在未确认子游戏，归入 ~合集 并保留 #uncheck 语义"]
            if not common_series_developer:
                notes = ["合集子游戏制作方缺少或冲突，外层保留原名并归入 ~合集"]
            if unresolved_by_series.get(series_id):
                status = "跳过"
                notes = [
                    "合集含未分目录独立启动项，禁止移动合集；需人工拆分后重新预览"
                ]
            failure_metadata = replace(normalize_metadata(LocalMetadata(
                engine=None,
                developer=(
                    f"{common_series_developer} #uncheck"
                    if common_series_developer else None
                ),
                jp_title=resolved_series_titles["jp"],
                zh_title=resolved_series_titles["zh"],
                en_title=resolved_series_titles["en"],
                positive_tag=series_positive,
                negative_tags=series_negative,
            )), engine=None)
            failure_metadata_dict = {
                **asdict(failure_metadata),
                "series_range": series.get("series_range"),
                "series_count": series.get("series_count"),
            }
            if not common_series_developer or not any((
                failure_metadata.jp_title,
                failure_metadata.zh_title,
                failure_metadata.en_title,
            )):
                notes.append("合集缺少共同制作方或标题，保留原名")
                series_target = (target_parent / series_source.name).resolve()
            else:
                fitted, _series_name, fitted_target, shortening, fit_error = (
                    fit_target_name(
                        failure_metadata_dict,
                        target_parent,
                        series_target_basename_fields,
                    )
                )
                if fitted is None or fitted_target is None:
                    notes.append((fit_error or "合集预计名称无法缩短") + "；保留原名")
                    series_target = (target_parent / series_source.name).resolve()
                else:
                    failure_metadata_dict = fitted
                    series_target = fitted_target
                    if shortening:
                        notes.append("名称缩短：" + "、".join(shortening))
            if status == "可执行" and series_target == series_source:
                status = "跳过"
                notes.append("合集已位于 ~合集 且名称未变")
            if status == "可执行" and len(series_target.name) > PREFERRED_TARGET_BASENAME_LENGTH:
                status = "跳过"
                notes.append("合集原名超过 100 字符，不能安全归档")
            if status == "可执行" and len(str(series_target)) > 240:
                status = "跳过"
                notes.append("合集目标超过 Windows 安全长度 240 字符")
            if (
                status == "可执行"
                and series_target.exists()
                and series_target != series_source
            ):
                status = "跳过"
                notes.append(f"合集目标已存在：{series_target}")
            series_entry = {
                "kind": "series",
                "series_id": series_id,
                "status": status,
                "source": str(series_source),
                "target": str(series_target) if status == "可执行" else None,
                "marker_name": None,
                "source_fingerprint": (
                    directory_fingerprint(series_source)
                    if status == "可执行" else None
                ),
                "metadata": failure_metadata_dict,
                "local_metadata": failure_metadata_dict,
                "local_evidence": ["子游戏查询完整性"],
                "notes": notes,
                "remote_searches": [],
                "source_shortcuts": [],
                "query_outcome": {
                    "found": False,
                    "reason": "合集子游戏未全部查询成功",
                },
                "propagated_preview_line_indexes": (
                    series_propagated_preview_line_indexes(
                        series_source, failure_metadata_dict,
                        series_tag_from_children,
                    )
                ),
                "series_engine_layout": engine_label,
            }
            series_entries.append(series_entry)
            if status != "可执行":
                continue
            for child_entry in child_entries:
                child_source = Path(str(child_entry["source"])).resolve()
                child_entry["runtime_source"] = str(
                    series_target / child_source.relative_to(series_source)
                )
                if (
                    child_entry.get("status") == "可执行"
                    and child_entry.get("target")
                ):
                    child_entry["target"] = str(
                        (series_target / Path(str(child_entry["target"])).name).resolve()
                    )
            continue
        if layout is None:
            def one_common(values: Sequence[str | None]) -> str | None:
                cleaned = {
                    value.strip() for value in values if value and value.strip()
                }
                return next(iter(cleaned)) if len(cleaned) == 1 else None

            known_engines = [
                str(item.get("engine") or "")
                for item in child_evidence
                if item.get("engine") and not is_unknown_engine(item.get("engine"))
            ]
            common_engine = one_common(known_engines)
            engine_label = (
                common_engine
                if common_engine and len(known_engines) == len(child_evidence)
                else "多引擎" if known_engines else UNKNOWN_ENGINE_LABEL
            )
            common_titles = {
                language: resolved_series_titles[language]
                for language in ("jp", "zh", "en")
            }
            skipped_metadata = replace(normalize_metadata(LocalMetadata(
                engine=None,
                developer=series_developers[0] if series_developers else None,
                jp_title=common_titles["jp"],
                zh_title=common_titles["zh"],
                en_title=common_titles["en"],
                positive_tag=series_positive,
                negative_tags=series_negative,
            )), engine=None)
            skipped_metadata_dict = {
                **asdict(skipped_metadata),
                "series_range": series.get("series_range"),
                "series_count": series.get("series_count"),
            }
            missing = []
            if not series_developers or not series_developers[0]:
                missing.append("共同 developer")
            if not any(common_titles.values()):
                missing.append("共同日中英标题")
            series_source = Path(str(series["source_path"])).resolve()
            publisher_only_basename = (
                series_developer_only_basename(
                    series_source.name,
                    series_developers[0] if series_developers else None,
                )
                if all_children_found else None
            )
            publisher_parent = series_hub_target_parent(root, series_source)
            publisher_only_target = (
                (publisher_parent / sanitize_component(publisher_only_basename)).resolve()
                if publisher_only_basename else (publisher_parent / series_source.name).resolve()
            )
            publisher_only_status = "跳过"
            publisher_only_notes = [
                "系列保留自身原名；没有共同标题时不借用子游戏标题",
                "缺少或冲突：" + "、".join(missing or ["共同标题"]),
            ]
            if publisher_only_target != series_source:
                if len(publisher_only_target.name) > PREFERRED_TARGET_BASENAME_LENGTH:
                    publisher_only_notes.append(
                        "合集制作方传播名称超过 100 字符；原标题不可自动删减"
                    )
                    publisher_only_target = None
                elif len(str(publisher_only_target)) > 240:
                    publisher_only_notes.append("合集制作方传播目标超过 Windows 安全长度 240 字符")
                    publisher_only_target = None
                elif publisher_only_target.exists():
                    publisher_only_notes.append(
                        f"合集制作方传播目标已存在：{publisher_only_target}"
                    )
                    publisher_only_target = None
                else:
                    publisher_only_status = "可执行"
                    publisher_only_notes = (
                        ["所有子游戏搜索成功；仅替换合集原名中已由成员证实的制作方",
                         "保留合集自己的原标题文本，不借用子游戏标题"]
                        if publisher_only_basename else
                        ["共同字段不足；保留合集原名，仅归入 ~合集"]
                    )
            elif publisher_only_target == series_source:
                publisher_only_target = None
                publisher_only_notes.append("合集制作方与名称已规范")
            series_entry = {
                "kind": "series",
                "series_id": series_id,
                "status": publisher_only_status,
                "source": str(series_source),
                "target": (
                    str(publisher_only_target)
                    if publisher_only_status == "可执行" and publisher_only_target
                    else None
                ),
                "marker_name": None,
                "source_fingerprint": (
                    directory_fingerprint(series_source)
                    if publisher_only_status == "可执行" else None
                ),
                "metadata": skipped_metadata_dict,
                "local_metadata": skipped_metadata_dict,
                "local_evidence": ["合集原始标题文本保留；制作方由全员成功成员汇总"],
                "notes": publisher_only_notes,
                "remote_searches": [],
                "source_shortcuts": [],
                "target_preview_lines": (
                    ["", Path(str(publisher_only_target)).name, "", "", "", "", ""]
                    if publisher_only_status == "可执行" and publisher_only_target
                    else None
                ),
                "propagated_preview_line_indexes": [],
                "propagated_basename_fragments": (
                    [f"[{series_developers[0]}]"]
                    if publisher_only_status == "可执行" and publisher_only_basename
                    and series_developers
                    else []
                ),
                "series_engine_layout": engine_label,
            }
            if series_entry["target_preview_lines"] is None:
                series_entry.pop("target_preview_lines")
            series_entries.append(series_entry)
            if publisher_only_status == "可执行" and publisher_only_target:
                for child_entry in child_entries:
                    child_source = Path(str(child_entry["source"])).resolve()
                    child_entry["runtime_source"] = str(
                        publisher_only_target
                        / child_source.relative_to(series_source)
                    )
                    if child_entry.get("status") == "可执行" and child_entry.get("target"):
                        child_entry["target"] = str(
                            publisher_only_target
                            / Path(str(child_entry["target"])).name
                        )
            continue
        series_developer = layout.developer
        series_metadata = replace(normalize_metadata(LocalMetadata(
            engine=None,
            developer=series_developer,
            jp_title=layout.jp_title,
            zh_title=layout.zh_title,
            en_title=layout.en_title,
            positive_tag=series_positive,
            negative_tags=series_negative,
        )), engine=None)
        series_metadata_dict = {
            **asdict(series_metadata),
            "series_range": series.get("series_range"),
            "series_count": series.get("series_count"),
        }
        series_source = Path(str(series["source_path"])).resolve()
        # 合集统一汇入扫描根的组织锚点；引擎只编码在子游戏 basename。
        target_parent = series_hub_target_parent(root, series_source)
        notes = ["系列：子游戏分别搜索；共同名称来自 Skill 语义"]
        notes.append("引擎仅写入各子游戏名称")
        fitted, _series_name, fitted_target, shortening, fit_error = fit_target_name(
            series_metadata_dict, target_parent, series_target_basename_fields,
        )
        status = "可执行" if fitted is not None else "跳过"
        series_target = fitted_target or series_source
        if fitted is not None:
            series_metadata_dict = fitted
            if shortening:
                notes.append("名称缩短：" + "、".join(shortening))
        else:
            notes.append(fit_error or "合集预计名称无法缩短")
        if status == "可执行" and series_target == series_source:
            status = "跳过"
            notes.append("系列名称已规范")
        elif status == "可执行" and series_target.exists():
            status = "跳过"
            notes.append(f"系列目标已存在：{series_target}")
        series_entry = {
            "kind": "series",
            "series_id": series_id,
            "status": status,
            "source": str(series_source),
            "target": str(series_target) if status == "可执行" else None,
            "marker_name": None,
            "source_fingerprint": (
                directory_fingerprint(series_source)
                if status == "可执行" else None
            ),
            "metadata": series_metadata_dict,
            "target_preview_lines": list(
                series_target_name_lines(series_metadata_dict)
            ),
            "local_metadata": series_metadata_dict,
            "local_evidence": [
                "Skill 系列共同名称",
                f"子游戏引擎布局：{layout.engine_label}",
            ],
            "notes": notes,
            "remote_searches": [],
            # 仅当同一 VN 被用户以多个已验证 release 明确拆成成员时，
            # 合集保留该共同 VN 的回源快捷方式；普通合集仍不建外层链接。
            "source_shortcuts": [],
            "propagated_preview_line_indexes": (
                series_propagated_preview_line_indexes(
                    series_source, series_metadata_dict,
                    series_tag_from_children,
                )
            ),
            "series_engine_layout": layout.engine_label,
        }
        child_vndb = [
            next((
                candidate for candidate in (remote_by_id[child_id].get(
                    "selected_candidates"
                ) or [])
                if candidate.get("source") == "VNDB"
            ), None)
            for child_id in child_ids
        ]
        common_vndb_ids = {
            str(candidate.get("source_id") or "")
            for candidate in child_vndb if candidate
        }
        release_ids = {
            str((candidate.get("release") or {}).get("source_id") or "")
            for candidate in child_vndb if candidate
        }
        if (
            len(child_vndb) == len(child_ids)
            and len(common_vndb_ids) == 1
            and len(release_ids - {""}) > 1
        ):
            common_vndb = next(iter(common_vndb_ids))
            if re.fullmatch(r"v\d+", common_vndb):
                series_entry["source_shortcuts"] = [{
                    "name": f"[VNDB] {common_vndb}.url",
                    "url": f"https://vndb.org/{common_vndb}",
                }]
                series_entry["notes"].append(
                    "同一 VN 的多个已验证 release 分篇：合集保留共同 VN 快捷方式"
                )
        series_entries.append(series_entry)
        if status != "可执行":
            continue
        for child_evidence_item, child_entry in zip(child_evidence, child_entries):
            child_source = Path(str(child_entry["source"])).resolve()
            child_entry["runtime_source"] = str(
                series_target / child_source.relative_to(series_source)
            )
            if child_entry.get("status") == "可执行" and child_entry.get("target"):
                old_target = Path(str(child_entry["target"])).resolve()
                child_entry["target"] = str(
                    (series_target / old_target.name).resolve()
                )
    ordered_entries = order_and_number_preview_entries(
        [*series_entries, *entries]
    )
    expected_game_ids = {
        str(item["game_id"]) for item in analysis.get("games", [])
    } | unresolved_ids
    preview_game_ids = [
        str(item.get("game_id") or "")
        for item in ordered_entries if item.get("kind") == "game"
    ]
    if len(preview_game_ids) != len(set(preview_game_ids)):
        raise ValueError("最终预览存在重复 game_id")
    if set(preview_game_ids) != expected_game_ids:
        missing = expected_game_ids - set(preview_game_ids)
        extra = set(preview_game_ids) - expected_game_ids
        raise ValueError(
            "最终预览游戏覆盖不完整："
            f"缺少={','.join(sorted(missing)) or '无'}；"
            f"多出={','.join(sorted(extra)) or '无'}"
        )
    for entry in ordered_entries:
        entry.pop("display_number", None)
    for entry in ordered_entries:
        if entry.get("kind") == "game" and str(entry.get("game_id")) in remote_by_id:
            entry["nonadopted_candidate_reasons"] = list(
                remote_by_id[str(entry["game_id"])].get(
                    "nonadopted_candidate_reasons"
                ) or []
            )
        entry["original_preview_lines"] = list(
            literal_original_name_lines(
                entry.get("source") or "", entry.get("local_metadata") or {}
            )
        )
        if (
            entry.get("kind") == "series"
            and entry.get("target")
            and Path(str(entry["target"])).name == Path(str(entry["source"])).name
        ):
            entry["target_preview_lines"] = list(
                entry["original_preview_lines"]
            )
    legacy_engine_parents: dict[str, Path] = {}
    for entry in ordered_entries:
        if entry.get("status") != "可执行" or not entry.get("target"):
            continue
        source = Path(str(entry["source"])).resolve()
        target = Path(str(entry["target"])).resolve()
        engine = str(
            (entry.get("metadata") or {}).get("engine")
            or entry.get("series_engine_layout")
            or ""
        )
        old_parent = source.parent
        if (
            engine and old_parent != root
            and old_parent.name.casefold() == engine.casefold()
            and target.parent == old_parent.parent
        ):
            legacy_engine_parents[str(old_parent).casefold()] = old_parent
            entry.setdefault("notes", []).append(
                f"批准后仅在为空时清理旧引擎目录：{old_parent}"
            )
    current_root_fingerprint = directory_fingerprint(root)
    if current_root_fingerprint != expected_root_fingerprint:
        raise ValueError("扫描根目录在预览生成期间发生变化，必须重新分析")
    manifest: dict[str, Any] = {
        "schema_version": 2,
        "preview_contract_version": PREVIEW_CONTRACT_VERSION,
        "root": str(root),
        "scan_root_fingerprint": current_root_fingerprint,
        "pipeline_inputs": {
            "analysis_fingerprint": analysis["input_fingerprint"],
            "scan_root_fingerprint": current_root_fingerprint,
            "local_semantics_fingerprint": canonical_sha256(local_semantics),
            "search_fingerprint": search_evidence["search_fingerprint"],
            "remote_semantics_fingerprint": canonical_sha256(remote_semantics),
        },
        "entries": ordered_entries,
        "remove_empty_parents": [
            str(path) for _, path in sorted(legacy_engine_parents.items())
        ],
    }
    manifest["approval_hash"] = approval_hash(manifest)
    return manifest


def build_manifest(
    scan_root: Path,
    client: Any | None = None,
    include_paths: Sequence[Path] | None = None,
) -> dict[str, Any]:
    root = scan_root.resolve()
    if not root.is_dir():
        raise ValueError(f"扫描根目录不存在：{root}")
    modern_client = client is None or isinstance(client, MetadataAggregator)
    client = client or MetadataAggregator([
        VNDBClient(), DLsiteClient(), SteamClient(),
    ])
    entries: list[dict[str, Any]] = []
    detections = discover_games(root, include_paths=include_paths)
    series_containers = discover_series_containers(root, detections)
    series_positive_by_child = {
        child.root: positive_tag_from_text(series.root.name)
        for series in series_containers
        for child in series.children
    }
    series_negative_by_child = {
        child.root: negative_tags_from_text(series.root.name)
        for series in series_containers
        for child in series.children
    }
    for detection in detections:
        ancestor_names: list[str] = []
        current = detection.root.parent
        while current != root and current != current.parent:
            ancestor_names.append(current.name)
            current = current.parent
        local_texts = _read_local_texts(detection.root)
        local_name_evidence = _read_local_name_evidence(detection.root)
        local_evidence_texts = [*local_name_evidence, *local_texts]
        metadata = parse_local_metadata(
            detection.root.name,
            local_evidence_texts,
            ancestor_names=ancestor_names,
            series_positive_tag=series_positive_by_child.get(detection.root),
            series_negative_tags=series_negative_by_child.get(detection.root, ()),
            tag_evidence_texts=local_texts,
        )
        query_titles = [
            value for value in (
                metadata.jp_title, metadata.zh_title, metadata.en_title
            ) if value
        ]
        identifiers = extract_source_ids(
            detection.root.name, local_texts, file_names=local_name_evidence,
        )
        has_identifier = any(identifiers.values())
        query = query_titles[0] if query_titles else None
        remotes: list[RemoteEntry] = []
        source_notes: list[str] = []
        remote_searches: list[dict[str, Any]] = []
        selected_source: str | None = None
        selected_sources: list[str] = []
        selection_notes: list[str] = []
        if query or has_identifier:
            try:
                if modern_client:
                    remotes, source_notes = client.search(
                        query_titles, identifiers, metadata.developer,
                        title_map={
                            "jp": metadata.jp_title,
                            "zh": metadata.zh_title,
                            "en": metadata.en_title,
                        },
                    )
                    reports = getattr(client, "last_reports", [])
                    selected_reports, selection_notes = select_naming_sources(reports)
                    selected_sources = [
                        str(report["source"]) for report in selected_reports
                    ]
                    selected_source = selected_sources[0] if selected_sources else ""
                    remote_searches = _serialize_search_reports(reports)
                else:
                    remotes = client.search(query, aliases=query_titles[1:])
            except Exception as exc:
                source_notes.append(
                    "元数据来源 "
                    f"查询失败：{type(exc).__name__}"
                )
        entry = plan_game(
            root, detection, metadata, remotes,
            selected_source=selected_source if modern_client else None,
            selected_sources=selected_sources if modern_client else (),
            remote_searches=remote_searches,
        )
        entry.notes.extend(selection_notes)
        if not query and not has_identifier:
            entry.notes.append("没有可用于远程搜索的标题或来源 ID")
        if not remotes and not source_notes:
            entry.notes.append(
                "VNDB、DLsite、Steam "
                "未找到可信同游戏条目"
            )
        entry.notes.extend(source_notes)
        if not remotes:
            entry.status = "跳过"
            entry.target = None
            entry.marker_name = None
            entry.source_fingerprint = None
        entries.append(entry.to_dict())

    # 合集只使用本地表面和子游戏共同信息；父动作先执行，子动作改用父移动后的路径。
    series_entries: list[dict[str, Any]] = []
    for series in series_containers:
        child_indexes = [
            index for index, detection in enumerate(detections)
            if detection.root in {child.root for child in series.children}
        ]
        child_metadata = [
            LocalMetadata(**{
                key: value for key, value in entries[index].get("local_metadata", {}).items()
                if key in {item.name for item in fields(LocalMetadata)}
            })
            for index in child_indexes
        ]
        series_name = compose_series_name(series.root.name, child_metadata)
        if not series_name:
            continue
        series_target = (
            series_hub_target_parent(root, series.root) / series_name
        ).resolve()
        notes = ["合集：不执行远程搜索、不创建来源快捷方式"]
        status = "可执行"
        if series_target == series.root:
            status = "跳过"
            notes.append("合集名称已规范")
        elif series_target.exists():
            status = "跳过"
            notes.append(f"合集目标已存在：{series_target}")
        series_entry = {
            "kind": "series", "status": status,
            "source": str(series.root),
            "target": str(series_target) if status == "可执行" else None,
            "marker_name": None,
            "source_fingerprint": (
                directory_fingerprint(series.root) if status == "可执行" else None
            ),
            "metadata": asdict(parse_local_metadata(series_name)),
            "local_metadata": asdict(parse_local_metadata(series.root.name)),
            "local_evidence": ["合集表面", "子游戏共同标题与正面标签交集"],
            "notes": notes, "remote_searches": [], "source_shortcuts": [],
        }
        series_entries.append(series_entry)
        if status != "可执行":
            continue
        for index in child_indexes:
            child_entry = entries[index]
            original_source = Path(child_entry["source"]).resolve()
            child_entry["runtime_source"] = str(
                series_target / original_source.relative_to(series.root)
            )
            if child_entry.get("status") == "可执行" and child_entry.get("target"):
                original_target = Path(child_entry["target"]).resolve()
                child_entry["target"] = str(
                    series_target / original_target.relative_to(series.root)
                )
            child_entry.setdefault("notes", []).append(
                f"依赖合集先移动：{series.root.name} -> {series_name}"
            )
    entries = [*series_entries, *entries]
    manifest: dict[str, Any] = {
        "schema_version": 1, "root": str(root), "entries": entries
    }
    manifest["approval_hash"] = approval_hash(manifest)
    return manifest


def approval_hash(manifest: dict[str, Any]) -> str:
    return manifest_approval_hash(manifest)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _capture_structure(root: Path) -> list[dict[str, Any]]:
    """记录名称、层级和轻量文件状态；不复制游戏内容。"""
    records: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*"), key=lambda item: str(item).casefold()):
        stat = path.stat()
        records.append({
            "path": path.relative_to(root).as_posix(),
            "kind": "directory" if path.is_dir() else "file",
            "size": None if path.is_dir() else stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        })
    return records


def write_before_snapshot(
    manifest: dict[str, Any], approval: str
) -> dict[str, str]:
    """在批准根目录外原子写入 JSON、Markdown 与清单副本。"""
    root = Path(manifest["root"]).resolve()
    project_root = Path(__file__).resolve().parents[4]
    base = Path(manifest.get("snapshot_root") or (
        project_root / "~outputs-final" / "rename-snapshots"
    )).resolve()
    final_dir = base / approval
    staging = base / f".{approval}.tmp"
    if _inside(final_dir, root) or _inside(staging, root):
        raise PreflightError("执行前快照必须位于游戏扫描根目录之外")
    if final_dir.exists() or staging.exists():
        raise PreflightError(f"执行前快照目录已存在：{final_dir}")
    base.mkdir(parents=True, exist_ok=True)
    try:
        staging.mkdir()
        affected_entries = [
            entry for entry in manifest.get("entries", [])
            if entry.get("status") == "可执行"
            or entry.get("source_shortcuts") or entry.get("shortcut_remove")
            or entry.get("auxiliary_directory_renames")
        ]
        affected_actions = [{
            "kind": entry.get("kind", "game"),
            "source": entry.get("source"),
            "runtime_source": entry.get("runtime_source"),
            "target": entry.get("target"),
            "source_fingerprint": entry.get("source_fingerprint"),
            "auxiliary_directory_renames": (
                entry.get("auxiliary_directory_renames") or []
            ),
            "parent_dependency": (
                {"runtime_source": entry.get("runtime_source")}
                if entry.get("runtime_source") else None
            ),
        } for entry in affected_entries]
        planned_files: list[dict[str, str]] = []
        for entry in affected_entries:
            destination = Path(
                entry.get("target")
                if entry.get("status") == "可执行"
                else entry.get("runtime_source") or entry["source"]
            ).resolve()
            if entry.get("marker_name"):
                planned_files.append({
                    "path": str(destination / entry["marker_name"]),
                    "kind": "marker_txt", "content": "",
                })
            for shortcut in entry.get("source_shortcuts") or []:
                planned_files.append({
                    "path": str(destination / shortcut["name"]),
                    "kind": "internet_shortcut",
                    "content": internet_shortcut_text(shortcut["url"]),
                })
        captured_structure = _capture_structure(root)
        snapshot = {
            "schema_version": 1,
            "root": str(root),
            "approval_hash": approval,
            "affected_actions": affected_actions,
            "planned_files": planned_files,
            "captured_structure": captured_structure,
        }
        json_path = staging / "before-snapshot.json"
        markdown_path = staging / "before-snapshot.md"
        manifest_path = staging / "approved-manifest.json"
        json_path.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        affected_roots = []
        for entry in affected_entries:
            try:
                affected_roots.append(
                    Path(entry["source"]).resolve().relative_to(root).as_posix()
                )
            except ValueError:
                pass
        tree_lines = []
        for item in captured_structure:
            if not any(
                item["path"] == prefix or item["path"].startswith(prefix + "/")
                for prefix in affected_roots
            ):
                continue
            depth = item["path"].count("/")
            suffix = "/" if item["kind"] == "directory" else ""
            tree_lines.append(f"{'  ' * depth}- {Path(item['path']).name}{suffix}")
        action_lines = [
            f"- `{item['source']}` → `{item.get('target') or item.get('runtime_source') or item['source']}`"
            for item in affected_actions
        ]
        markdown_path.write_text(
            "# 重命名前名称与结构快照\n\n"
            f"- 扫描根：`{root}`\n"
            f"- 批准哈希：`{approval}`\n"
            f"- 记录条目：{len(captured_structure)}\n\n"
            "## 受影响动作\n\n" + "\n".join(action_lines) +
            "\n\n## 原始受影响目录树\n\n" + "\n".join(tree_lines) + "\n",
            encoding="utf-8",
        )
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        loaded_snapshot = json.loads(json_path.read_text(encoding="utf-8"))
        loaded_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if loaded_snapshot.get("approval_hash") != approval:
            raise PreflightError("执行前快照批准哈希复读校验失败")
        if approval_hash(loaded_manifest) != approval:
            raise PreflightError("执行前批准清单复读校验失败")
        staging.rename(final_dir)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    result = {
        "approval_hash": approval,
        "json_path": str(final_dir / "before-snapshot.json"),
        "markdown_path": str(final_dir / "before-snapshot.md"),
        "manifest_path": str(final_dir / "approved-manifest.json"),
    }
    result.update({
        "json_sha256": file_sha256(Path(result["json_path"])),
        "markdown_sha256": file_sha256(Path(result["markdown_path"])),
        "manifest_sha256": file_sha256(Path(result["manifest_path"])),
    })
    return result


def verify_before_snapshot(snapshot: dict[str, Any]) -> None:
    for path_key, hash_key in (
        ("json_path", "json_sha256"),
        ("markdown_path", "markdown_sha256"),
        ("manifest_path", "manifest_sha256"),
    ):
        path = Path(str(snapshot.get(path_key) or ""))
        expected = snapshot.get(hash_key)
        if not path.is_file() or not expected or file_sha256(path) != expected:
            raise PreflightError(f"执行前快照缺失或已被篡改：{path}")
    payload = json.loads(Path(snapshot["json_path"]).read_text(encoding="utf-8"))
    approved = json.loads(Path(snapshot["manifest_path"]).read_text(encoding="utf-8"))
    expected_approval = snapshot.get("approval_hash")
    if (
        not expected_approval
        or payload.get("approval_hash") != expected_approval
        or approval_hash(approved) != expected_approval
    ):
        raise PreflightError("执行前快照内容校验失败")


def snapshot_differences(
    root: Path, snapshot_reference: dict[str, Any]
) -> list[dict[str, str]]:
    """撤销后只报告差异；外部新增或修改内容绝不删除。"""
    verify_before_snapshot(snapshot_reference)
    payload = json.loads(
        Path(snapshot_reference["json_path"]).read_text(encoding="utf-8")
    )
    before = {item["path"]: item for item in payload["captured_structure"]}
    after = {item["path"]: item for item in _capture_structure(root)}
    differences: list[dict[str, str]] = []
    for path in sorted(before.keys() | after.keys(), key=str.casefold):
        if path not in before:
            differences.append({"path": path, "change": "新增"})
        elif path not in after:
            differences.append({"path": path, "change": "缺失"})
        elif any(
            before[path].get(key) != after[path].get(key)
            for key in ("kind", "size", "mtime_ns")
        ):
            differences.append({"path": path, "change": "已修改"})
    return differences


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _path_after_moves(
    path: Path, moves: Sequence[tuple[Path, Path]]
) -> Path:
    """目录父级被移动时，把待清理路径映射到其当前实际位置。"""
    current = path.resolve()
    for source, target in moves:
        try:
            relative = current.relative_to(source.resolve())
        except ValueError:
            continue
        current = target.resolve() / relative
    return current


def _active_manifest_entries(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """返回会产生移动或文件动作的清单条目。"""
    return [
        entry for entry in manifest.get("entries", [])
        if entry.get("status") == "可执行"
        or entry.get("source_shortcuts") or entry.get("shortcut_remove")
        or entry.get("auxiliary_directory_renames")
    ]


def _receipt_actions_for_manifest(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """从批准清单唯一重建回执动作，undo 不信任可编辑回执本身。"""
    return [
        {
            "source": entry["source"],
            "runtime_source": entry.get("runtime_source"),
            "target": (
                entry["target"] if entry.get("status") == "可执行"
                else entry.get("runtime_source") or entry["source"]
            ),
            "marker_name": entry.get("marker_name"),
            "marker_remove": entry.get("marker_remove"),
            "source_shortcuts": entry.get("source_shortcuts") or [],
            "shortcut_remove": entry.get("shortcut_remove") or [],
            "auxiliary_directory_renames": (
                entry.get("auxiliary_directory_renames") or []
            ),
        }
        for entry in _active_manifest_entries(manifest)
    ]


def _expected_created_parents(
    manifest: dict[str, Any], snapshot_payload: dict[str, Any]
) -> list[str]:
    """按批准动作模拟目录移动，重建 apply 实际记录的新父目录。"""
    root = Path(manifest["root"]).resolve()
    directories = {root}
    directories.update(
        (root / item["path"]).resolve()
        for item in snapshot_payload.get("captured_structure", [])
        if item.get("kind") == "directory"
    )
    created: list[str] = []
    for entry in manifest.get("entries", []):
        if entry.get("status") != "可执行":
            continue
        source = Path(entry.get("runtime_source") or entry["source"]).resolve()
        target = Path(entry["target"]).resolve()
        parent = target.parent
        if parent not in directories:
            created.append(str(parent))
            current = parent
            while _inside(current, root):
                directories.add(current)
                if current == root:
                    break
                current = current.parent
        if source != target:
            remapped: set[Path] = set()
            for directory in directories:
                try:
                    relative = directory.relative_to(source)
                except ValueError:
                    remapped.add(directory)
                else:
                    remapped.add((target / relative).resolve())
            directories = remapped
    return created


def apply_manifest_data(
    manifest: dict[str, Any], approval: str, *, preflight_only: bool = False
) -> dict[str, Any]:
    expected = approval_hash(manifest)
    if approval != expected or manifest.get("approval_hash", expected) != expected:
        raise ApprovalError("批准码与当前预览清单不一致")
    try:
        validate_preview_contract_version(manifest)
        validate_combined_approval_manifest(manifest)
    except ValueError as exc:
        raise ApprovalError(str(exc)) from exc
    root = Path(manifest["root"]).resolve()
    expected_root_fingerprint = manifest.get("scan_root_fingerprint")
    if manifest.get("schema_version") == 2 and not expected_root_fingerprint:
        raise PreflightError("语义清单缺少扫描根目录指纹")
    if (
        expected_root_fingerprint
        and directory_fingerprint(root) != expected_root_fingerprint
    ):
        raise PreflightError("扫描根目录在预览后发生变化")
    ready = [
        entry for entry in manifest.get("entries", [])
        if entry.get("status") == "可执行"
    ]
    active = _active_manifest_entries(manifest)
    source_keys = {
        str(Path(entry["source"]).resolve()).casefold(): index
        for index, entry in enumerate(ready)
    }
    target_keys: dict[str, int] = {}
    marker_keys: dict[str, int] = {}
    shortcut_keys: dict[str, int] = {}
    for index, entry in enumerate(ready):
        target = Path(entry["target"]).resolve()
        target_key = str(target).casefold()
        if target_key in target_keys:
            raise PreflightError(f"清单包含重复目标：{target}")
        target_keys[target_key] = index
        if target_key in source_keys and source_keys[target_key] != index:
            raise PreflightError(f"目标与其他源目录冲突：{target}")
        marker_name = entry.get("marker_name")
        if marker_name:
            marker = target / marker_name
            marker_key = str(marker).casefold()
            if marker_key in marker_keys:
                raise PreflightError(f"清单包含重复标记路径：{marker}")
            marker_keys[marker_key] = index
    for index, entry in enumerate(active):
        source = Path(entry["source"]).resolve()
        destination = (
            Path(entry["target"]).resolve()
            if entry.get("status") == "可执行"
            else Path(entry.get("runtime_source") or entry["source"]).resolve()
        )
        for action_key in ("source_shortcuts", "shortcut_remove"):
            for shortcut in entry.get(action_key) or []:
                name = str(shortcut.get("name") or "")
                url = str(shortcut.get("url") or "")
                if (
                    not name or Path(name).name != name
                    or not name.casefold().endswith(".url")
                    or not re.match(r"^https?://[^\r\n]+$", url, re.I)
                ):
                    raise PreflightError(f"非法来源快捷方式动作：{shortcut}")
                shortcut_path = destination / name
                shortcut_key = str(shortcut_path).casefold()
                if shortcut_key in shortcut_keys:
                    raise PreflightError(f"清单包含重复来源快捷方式：{shortcut_path}")
                shortcut_keys[shortcut_key] = index
        auxiliary_targets: set[str] = set()
        for auxiliary in entry.get("auxiliary_directory_renames") or []:
            source_name = str(auxiliary.get("source_name") or "")
            target_name = str(auxiliary.get("target_name") or "")
            kind = str(auxiliary.get("kind") or "")
            try:
                expected_source, expected_target = (
                    (auxiliary_target_name(target_name, kind), target_name)
                    if manifest.get("mode") == "undo"
                    else (source_name, auxiliary_target_name(source_name, kind))
                )
            except ValueError as exc:
                raise PreflightError(f"非法附属目录动作：{auxiliary}") from exc
            if (
                Path(source_name).name != source_name
                or Path(target_name).name != target_name
                or source_name == target_name
                or source_name != expected_source
                or target_name != expected_target
            ):
                raise PreflightError(f"非法附属目录动作：{auxiliary}")
            source_child = source / source_name
            target_child = source / target_name
            if not source_child.is_dir():
                raise PreflightError(f"附属目录不存在：{source_child}")
            if directory_fingerprint(source_child) != auxiliary.get("source_fingerprint"):
                raise PreflightError(f"附属目录在预览后发生变化：{source_child}")
            target_key = target_name.casefold()
            if target_key in auxiliary_targets or (
                target_child.exists() and source_child != target_child
            ):
                raise PreflightError(f"附属目录目标冲突：{target_child}")
            auxiliary_targets.add(target_key)
    remove_empty_parents = [
        Path(value).resolve() for value in manifest.get("remove_empty_parents", [])
    ]
    for parent in remove_empty_parents:
        if not _inside(parent, root):
            raise PreflightError(f"待清理目录超出批准根目录：{parent}")
        if not parent.is_dir():
            raise PreflightError(f"待清理目录不存在：{parent}")
    # 完整预检必须先于任何目录创建或移动。
    for entry in ready:
        source = Path(entry["source"]).resolve()
        target = Path(entry["target"]).resolve()
        if not _inside(source, root) or not _inside(target, root):
            raise PreflightError(f"路径超出批准根目录：{source} -> {target}")
        if not source.is_dir():
            raise PreflightError(f"源目录不存在：{source}")
        if directory_fingerprint(source) != entry.get("source_fingerprint"):
            raise PreflightError(f"源目录在预览后发生变化：{source}")
        if target.exists() and target != source:
            raise PreflightError(f"目标已存在：{target}")
        marker_name = entry.get("marker_name")
        if marker_name and (target / marker_name).exists():
            raise PreflightError(f"标记 txt 已存在：{target / marker_name}")
        marker_remove = entry.get("marker_remove")
        if marker_remove:
            marker = source / marker_remove
            if not marker.is_file() or marker.stat().st_size != 0:
                raise PreflightError(f"待撤销标记不是零字节文件：{marker}")
    for entry in active:
        if entry.get("status") == "可执行":
            continue
        source = Path(entry["source"]).resolve()
        if not _inside(source, root) or not source.is_dir():
            raise PreflightError(f"来源快捷方式目录无效：{source}")
        if directory_fingerprint(source) != entry.get("source_fingerprint"):
            raise PreflightError(f"源目录在预览后发生变化：{source}")
    for entry in active:
        source = Path(entry["source"]).resolve()
        for shortcut in entry.get("source_shortcuts") or []:
            path = source / shortcut["name"]
            if path.exists():
                raise PreflightError(f"来源快捷方式已存在：{path}")
        for shortcut in entry.get("shortcut_remove") or []:
            path = source / shortcut["name"]
            if not path.is_file():
                raise PreflightError(f"待撤销来源快捷方式不存在：{path}")
            try:
                current = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise PreflightError(f"无法读取待撤销来源快捷方式：{path}") from exc
            if current != internet_shortcut_text(shortcut["url"]):
                raise PreflightError(f"待撤销来源快捷方式内容已变化：{path}")

    if preflight_only:
        return {
            "status": "preflight-ok", "root": str(root),
            "approval_hash": expected, "actions": [],
        }
    moved: list[tuple[Path, Path]] = []
    markers: list[Path] = []
    removed_markers: list[Path] = []
    created_shortcuts: list[Path] = []
    removed_shortcuts: list[tuple[Path, bytes]] = []
    auxiliary_moves: list[tuple[Path, Path]] = []
    created_parents: list[Path] = []
    try:
        for entry in active:
            source = Path(entry["source"]).resolve()
            for shortcut in entry.get("shortcut_remove") or []:
                path = source / shortcut["name"]
                payload = path.read_bytes()
                path.unlink()
                removed_shortcuts.append((path, payload))
        for entry in ready:
            source = Path(entry.get("runtime_source") or entry["source"]).resolve()
            target = Path(entry["target"]).resolve()
            marker_remove = entry.get("marker_remove")
            if marker_remove:
                old_marker = source / marker_remove
                old_marker.unlink()
                removed_markers.append(old_marker)
            if not target.parent.exists():
                target.parent.mkdir(parents=True, exist_ok=False)
                created_parents.append(target.parent)
            if source != target:
                source.rename(target)
                moved.append((source, target))
            marker_name = entry.get("marker_name")
            if marker_name:
                marker = target / marker_name
                with marker.open("xb"):
                    pass
                markers.append(marker)
        for entry in active:
            destination = (
                Path(entry["target"]).resolve()
                if entry.get("status") == "可执行"
                else Path(entry.get("runtime_source") or entry["source"]).resolve()
            )
            for auxiliary in entry.get("auxiliary_directory_renames") or []:
                auxiliary_source = destination / auxiliary["source_name"]
                auxiliary_target = destination / auxiliary["target_name"]
                auxiliary_source.rename(auxiliary_target)
                auxiliary_moves.append((auxiliary_source, auxiliary_target))
        for entry in active:
            destination = (
                Path(entry["target"]).resolve()
                if entry.get("status") == "可执行"
                else Path(entry.get("runtime_source") or entry["source"]).resolve()
            )
            for shortcut in entry.get("source_shortcuts") or []:
                path = destination / shortcut["name"]
                with path.open("x", encoding="utf-8", newline="\n") as handle:
                    handle.write(internet_shortcut_text(shortcut["url"]))
                created_shortcuts.append(path)
        for parent in sorted(
            (_path_after_moves(value, moved) for value in remove_empty_parents),
            key=lambda value: len(value.parts), reverse=True,
        ):
            try:
                parent.rmdir()
            except OSError:
                pass
    except Exception:
        for auxiliary_source, auxiliary_target in reversed(auxiliary_moves):
            if auxiliary_target.exists() and not auxiliary_source.exists():
                auxiliary_target.rename(auxiliary_source)
        for shortcut in reversed(created_shortcuts):
            if shortcut.is_file():
                shortcut.unlink()
        for marker in reversed(markers):
            if marker.exists() and marker.stat().st_size == 0:
                marker.unlink()
        for source, target in reversed(moved):
            if target.exists() and not source.exists():
                target.rename(source)
        for marker in removed_markers:
            if not marker.exists() and marker.parent.exists():
                with marker.open("xb"):
                    pass
        for shortcut, payload in removed_shortcuts:
            if not shortcut.exists() and shortcut.parent.exists():
                shortcut.write_bytes(payload)
        rollback_moves = [
            (target, source) for source, target in reversed(moved)
        ]
        for parent in sorted(
            (_path_after_moves(value, rollback_moves) for value in created_parents),
            key=lambda value: len(value.parts), reverse=True,
        ):
            try:
                parent.rmdir()
            except OSError:
                pass
        raise
    result = {
        "status": "undone" if manifest.get("mode") == "undo" else "applied",
        "root": str(root), "approval_hash": expected,
        "created_parents": [str(path) for path in created_parents],
        "actions": _receipt_actions_for_manifest(manifest),
    }
    if manifest.get("mode") == "undo" and manifest.get("restore_snapshot"):
        result["snapshot_differences"] = snapshot_differences(
            root, manifest["restore_snapshot"]
        )
    return result


def build_undo_manifest(receipt: dict[str, Any]) -> dict[str, Any]:
    """把执行回执转换为新的、仍需独立批准的反向清单。"""
    if receipt.get("before_snapshot"):
        snapshot = receipt["before_snapshot"]
        verify_before_snapshot(snapshot)
        approved = json.loads(
            Path(snapshot["manifest_path"]).read_text(encoding="utf-8")
        )
        snapshot_payload = json.loads(
            Path(snapshot["json_path"]).read_text(encoding="utf-8")
        )
        if (
            receipt.get("approval_hash") != approval_hash(approved)
            or Path(str(receipt.get("root") or "")).resolve()
            != Path(str(approved.get("root") or "")).resolve()
            or receipt.get("actions") != _receipt_actions_for_manifest(approved)
            or receipt.get("created_parents", [])
            != _expected_created_parents(approved, snapshot_payload)
        ):
            raise PreflightError("执行回执与批准清单不一致")
    root = Path(receipt["root"]).resolve()
    entries: list[dict[str, Any]] = []
    for action in reversed(receipt.get("actions", [])):
        source = Path(action["target"]).resolve()
        target = Path(action.get("runtime_source") or action["source"]).resolve()
        entries.append({
            "status": "可执行",
            "source": str(source),
            "target": str(target),
            "marker_name": action.get("marker_remove"),
            "marker_remove": action.get("marker_name"),
            "source_shortcuts": action.get("shortcut_remove") or [],
            "shortcut_remove": action.get("source_shortcuts") or [],
            "auxiliary_directory_renames": [{
                **auxiliary,
                "source_name": auxiliary["target_name"],
                "target_name": auxiliary["source_name"],
                "source_fingerprint": directory_fingerprint(
                    source / auxiliary["target_name"]
                ),
            } for auxiliary in action.get("auxiliary_directory_renames") or []],
            "source_fingerprint": directory_fingerprint(source),
            "metadata": {},
            "local_evidence": ["执行回执"],
            "notes": ["撤销预览：恢复原目录路径"],
        })
    manifest: dict[str, Any] = {
        "schema_version": 1, "mode": "undo",
        "root": str(root), "entries": entries,
        "remove_empty_parents": receipt.get("created_parents", []),
        "restore_snapshot": receipt.get("before_snapshot"),
    }
    manifest["approval_hash"] = approval_hash(manifest)
    return manifest


def apply_manifest_with_receipt(
    manifest: dict[str, Any], approval: str, receipt_path: Path
) -> dict[str, Any]:
    """原子落盘执行回执；写入失败时立即按内存回执回滚。"""
    receipt_path = receipt_path.resolve()
    temp_path = receipt_path.with_name(receipt_path.name + ".tmp")
    root = Path(manifest["root"]).resolve()
    if _inside(receipt_path, root) or _inside(temp_path, root):
        raise PreflightError("回执必须位于游戏扫描根目录之外")
    if receipt_path.exists() or temp_path.exists():
        raise PreflightError(f"回执路径已存在：{receipt_path}")
    expected = approval_hash(manifest)
    if approval != expected or manifest.get("approval_hash", expected) != expected:
        raise ApprovalError("批准码与当前预览清单不一致")
    apply_manifest_data(manifest, approval, preflight_only=True)
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt: dict[str, Any] | None = None
    before_snapshot = write_before_snapshot(manifest, approval)
    try:
        receipt = apply_manifest_data(manifest, approval)
        receipt["before_snapshot"] = before_snapshot
        temp_path.write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temp_path.replace(receipt_path)
        return receipt
    except Exception:
        if receipt is not None:
            undo = build_undo_manifest(receipt)
            apply_manifest_data(undo, undo["approval_hash"])
        if temp_path.is_file():
            temp_path.unlink()
        raise


# Markdown 预览同时输出集中待判断清单，不在扫描中途逐项询问。
def _md(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("|", "\\|").replace("\n", "<br>")


def _remote_cell(
    value: dict[str, Any] | None, include_tag: bool = False
) -> str:
    if not value:
        return ""
    label_parts: list[str] = []
    if include_tag and value.get("positive_tag"):
        label_parts.append(value["positive_tag"])
    if value.get("version"):
        label_parts.append("v" + value["version"])
    label = " ".join(label_parts) or "条目"
    return f"[{_md(label)}]({_md(value.get('url'))})"


def _remote_list_cell(values: Sequence[dict[str, Any]]) -> str:
    links: list[str] = []
    for value in values:
        label = value.get("title") or "未命名条目"
        links.append(f"[{_md(label)}]({_md(value.get('url'))})")
    return "<br>".join(links)


def _bold_target_diff(source_name: str, target_name: str) -> str:
    """仅在预计名称中加粗相对原文件夹新增或改变的片段。"""
    parts: list[str] = []
    matcher = difflib.SequenceMatcher(a=source_name, b=target_name, autojunk=False)
    for operation, _, _, target_start, target_end in matcher.get_opcodes():
        if operation == "delete":
            continue
        value = _md(target_name[target_start:target_end])
        parts.append(value if operation == "equal" else f"**{value}**")
    return "".join(parts)


def _stack_cell(values: Sequence[str | None]) -> str:
    return "<br>".join(_md(value) if value else "&#x20;" for value in values)


def _metadata_other_line(metadata: dict[str, Any], original_name: str | None = None) -> str:
    values: list[str] = []
    dlc_range = str(metadata.get("dlc_range") or "")
    if metadata.get("version"):
        version = "v" + str(metadata["version"])
        if original_name:
            match = re.search(
                r"(?i)(?:v(?:er(?:sion)?)?\.?)\s*[0-9][0-9A-Za-z._-]*",
                original_name,
            )
            if match:
                version = match.group(0)
        if metadata.get("dlc_version"):
            values.append(
                f"本体{version} +DLCv{metadata['dlc_version']}"
            )
        elif dlc_range:
            values.append(f"{version} +DLC{dlc_range}")
        else:
            values.append(version + ("+DLC" if metadata.get("dlc") else ""))
    elif metadata.get("dlc"):
        values.append(f"+DLC{dlc_range}" if dlc_range else "+DLC")
    for version_tag in metadata.get("version_tags") or []:
        text = str(version_tag or "").strip().lstrip("$")
        if text and (not original_name or re.search(
            re.escape(text).replace(r"\~", r"\s*(?:-|~|～|〜)\s*"),
            original_name,
            re.I,
        )):
            values.append(text)
    group = metadata.get("translation_group")
    negative_tags = {
        str(value).lstrip("#").casefold()
        for value in metadata.get("negative_tags") or []
    }
    suppress_unknown_group = (
        str(group or "").lstrip("%") in UNKNOWN_TRANSLATION_GROUPS
        and bool(negative_tags & TRANSLATION_STATUS_TAGS)
    )
    if group and not suppress_unknown_group and (
        not original_name or f"%{group}" in original_name
    ):
        values.append(f"%{group}")
    tag = metadata.get("positive_tag")
    if tag and (not original_name or tag in original_name):
        values.append(tag if original_name else f"%{tag}")
    for tag in metadata.get("negative_tags") or []:
        values.append(f"#{tag}")
    return " ".join(values)


def _name_stacks(raw: dict[str, Any]) -> tuple[list[str], list[str]]:
    local = dict(raw.get("local_metadata") or {})
    target = dict(raw.get("metadata") or {})
    source_name = Path(raw.get("source") or "").name
    source_values = [
        (str(local.get("engine")) + "/") if local.get("engine") else "",
        f"[{local['developer']}]" if local.get("developer") else "",
        str(local.get("jp_title") or ""),
        str(local.get("zh_title") or ""),
        str(local.get("en_title") or ""),
        _metadata_other_line(local, source_name),
    ]
    if not raw.get("target"):
        return source_values, ["", "", "", "", "", ""]
    target_titles: list[str] = []
    title_seen = False
    for value in (
        target.get("jp_title"), target.get("zh_title"), target.get("en_title")
    ):
        if not value:
            target_titles.append("")
            continue
        target_titles.append(("=" if title_seen else "") + str(value))
        title_seen = True
    target_values = [
        (str(target.get("engine")) + "/") if target.get("engine") else "",
        f"[{target['developer']}]" if target.get("developer") else "",
        *target_titles,
        _metadata_other_line(target),
    ]
    highlighted = [
        value if value == source_values[index] or not value else f"**{_md(value)}**"
        for index, value in enumerate(target_values)
    ]
    return source_values, highlighted


def _round_cell(round_data: dict[str, Any] | None) -> str:
    values = ["", "", "", "", "", ""]
    if not round_data:
        values[1:4] = ["跳过", "跳过", "跳过"]
        return _stack_cell(values)
    language = str(round_data.get("language") or "")
    language_index = {"jp": 1, "zh": 2, "en": 3}.get(language, 1)
    language_label = {"jp": "日：", "zh": "中：", "en": "英："}.get(
        language, ""
    )
    circle = {1: "①", 2: "②", 3: "③"}.get(
        int(round_data.get("number") or 0), ""
    )
    status = round_data.get("status")
    if status == "success":
        result = dict(round_data.get("result") or {})
        developer = result.get("developer")
        engine = result.get("engine")
        top = f"制：{developer}" if developer else "制：无"
        if engine:
            top += f"；引擎：{engine}"
        values[0] = top
        for key, index, label in (
            ("jp_title", 1, "日："),
            ("zh_title", 2, "中："),
            ("en_title", 3, "英："),
        ):
            prefix = f"{circle}[v]" if index == language_index else ""
            displayed = (
                round_data.get("query") if index == language_index
                else result.get(key)
            )
            values[index] = f"{prefix}{label}{displayed or '无'}"
        source_id = result.get("source_id") or "无"
        url = result.get("url")
        values[4] = (
            f"ID：[{source_id}]({url})" if url else f"ID：{source_id}"
        )
    elif status == "failed":
        values[1:4] = ["失败", "失败", "失败"]
        values[language_index] = (
            f"{circle}[x]{language_label}{round_data.get('query') or ''}"
        )
    elif status == "covered":
        values[1:4] = ["跳过", "跳过", "跳过"]
        values[language_index] = (
            f"{circle} {language_label}{round_data.get('query') or ''}"
        )
    else:
        values[1:4] = ["跳过", "跳过", "跳过"]
    return _stack_cell(values)


def _render_search_preview(manifest: dict[str, Any]) -> list[str]:
    headers = [
        "原文件夹名称", "预计重命名的名称", "来源搜索结果",
        "①日文轮", "②中文轮", "③英文轮",
    ]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(":---" for _ in headers) + " |",
    ]
    source_order = ("VNDB", "DLsite", "Steam")
    for raw in manifest.get("entries", []):
        source_values, target_values = _name_stacks(raw)
        reports = {
            report.get("source"): report
            for report in raw.get("remote_searches", [])
        }
        for index, source in enumerate(source_order):
            report = reports.get(source, {"rounds": []})
            rounds = {
                int(item.get("number") or 0): item
                for item in report.get("rounds", [])
            }
            selected_sources = list(raw.get("selected_sources") or [])
            if not selected_sources and raw.get("selected_source"):
                selected_sources = [raw["selected_source"]]
            selected = source in selected_sources
            marker = (
                "[合并采用]" if selected and len(selected_sources) > 1
                else "[采用]" if selected else ""
            )
            heading = f"{marker}{source}"
            row = [
                _stack_cell(source_values) if index == 0 else _stack_cell([""] * 6),
                _stack_cell(target_values) if index == 0 else _stack_cell([""] * 6),
                _stack_cell([f"**{heading}**", "", "", "", "", ""]),
                _round_cell(rounds.get(1)),
                _round_cell(rounds.get(2)),
                _round_cell(rounds.get(3)),
            ]
            lines.append("| " + " | ".join(row) + " |")
    return lines


def render_preview_documents(manifest: dict[str, Any]) -> tuple[str, str]:
    modern_preview = any(
        "remote_searches" in raw for raw in manifest.get("entries", [])
    )
    headers = ["原文件夹名称", "预计重命名的名称", "缺失的备注"]
    lines = [
        "# 游戏目录重命名预览", "",
        f"批准码：`{manifest.get('approval_hash') or approval_hash(manifest)}`", "",
    ]
    if int(manifest.get("schema_version") or 1) >= 2:
        lines.extend(render_semantic_preview_markdown(manifest).splitlines())
    elif modern_preview:
        lines.extend(_render_search_preview(manifest))
    else:
        lines.extend([
            "| " + " | ".join(headers) + " |",
            "|" + "|".join("---" for _ in headers) + "|",
        ])
    decisions = ["# 待用户判断清单", ""]
    for raw in manifest.get("entries", []):
        notes = raw.get("notes", [])
        source_name = Path(raw.get("source") or "").name
        target_value = raw.get("target")
        target_display = ""
        if target_value:
            target_path = Path(target_value)
            target_display = (
                f"**{_md(target_path.parent.name)}/**"
                + _bold_target_diff(source_name, target_path.name)
            )
        if not modern_preview:
            row = [_md(source_name), target_display, _md("<br>".join(notes))]
            lines.append("| " + " | ".join(row) + " |")
        needs_decision = (
            raw.get("status") != "可执行"
            or bool(raw.get("remote_other", []))
            or any(
            token in note
            for note in notes
            for token in ("冲突", "难以判断", "无法可靠", "缺少")
            )
        )
        if needs_decision:
            remote_candidates = []
            for candidate in raw.get("remote_other", []):
                label = candidate.get("title") or "未命名条目"
                details = [
                    value for value in (
                        "v" + candidate["version"] if candidate.get("version") else None,
                        candidate.get("positive_tag"),
                    ) if value
                ]
                if details:
                    label += "（" + "，".join(details) + "）"
                fields = [
                    f"来源={candidate.get('source') or '未知'}",
                    f"ID={candidate.get('source_id') or '无'}",
                    f"developer={candidate.get('developer') or '无'}",
                    f"日文名={candidate.get('jp_title') or '无'}",
                    f"中文名={candidate.get('zh_title') or '无'}",
                    f"英文名={candidate.get('en_title') or '无'}",
                    "语言=" + (", ".join(candidate.get("languages") or []) or "无"),
                    "远程DLC=" + (
                        ", ".join(candidate.get("remote_dlc") or []) or "无"
                    ),
                    "远程标签=" + (
                        ", ".join(candidate.get("remote_tags") or []) or "无"
                    ),
                    "关联语言版本=" + (
                        ", ".join(candidate.get("related_editions") or []) or "无"
                    ),
                ]
                remote_candidates.append(
                    f"[{_md(label)}]({_md(candidate.get('url'))})"
                    + "（" + "，".join(_md(field) for field in fields) + "）"
                )
            skipped = raw.get("status") != "可执行"
            recommendation = (
                "保持原目录，补充或确认明确证据后重新预览。"
                if skipped
                else "核对上述警告与候选后再决定是否批准当前目标。"
            )
            impact = (
                (
                    "不会重命名；会在原目录创建 "
                    f"{len(raw.get('source_shortcuts') or [])} 个来源快捷方式。"
                )
                if skipped and raw.get("source_shortcuts")
                else "当前批准不会改动此条目。"
                if skipped
                else f"批准后将移动到：{_md(raw.get('target'))}"
            )
            decisions.extend([
                f"## {_md(raw.get('source'))}", "",
                "- 问题：" + ("；".join(notes) or "需要人工判断"),
                "- 本地证据：" + (
                    "；".join(raw.get("local_evidence", [])) or "无"
                ),
                "- 候选值：" + (
                    "；".join(remote_candidates) or "无可信远程候选"
                ),
                "- 推荐选择：" + recommendation,
                "- 影响：" + impact, "",
            ])
    shortcut_rows: list[str] = []
    for destination, shortcuts in group_source_shortcuts(manifest):
        names = "<br>".join(_md(shortcut.get("name")) for shortcut in shortcuts)
        links = "<br>".join(
            f"[{_md(shortcut.get('url'))}]({_md(shortcut.get('url'))})"
            for shortcut in shortcuts
        )
        shortcut_rows.append(
            "| " + " | ".join((_md(destination), names, links)) + " |"
        )
    if shortcut_rows:
        lines.extend([
            "", "## 批准后创建的来源快捷方式", "",
            "| 游戏目录 | 快捷方式文件 | 来源链接 |",
            "| --- | --- | --- |",
            *shortcut_rows,
        ])
    if len(decisions) == 2:
        decisions.extend(["没有待用户判断项。", ""])
    return "\n".join(lines) + "\n", "\n".join(decisions)


def _markdown_inline_html(value: str) -> str:
    """转换预览使用的有限 Markdown 内联语法，并转义其余远程或本地文本。"""
    tokens = re.compile(
        r"(\*\*.*?\*\*|\[[^\]]*\]\(https?://[^)]+\)|<br>|&#x20;)"
    )
    rendered: list[str] = []
    position = 0
    for match in tokens.finditer(value):
        rendered.append(html.escape(value[position:match.start()].replace(r"\|", "|")))
        token = match.group(0)
        if token == "<br>":
            rendered.append("<br>")
        elif token == "&#x20;":
            rendered.append("&nbsp;")
        elif token.startswith("**"):
            rendered.append(
                "<strong>" + html.escape(token[2:-2].replace(r"\|", "|"))
                + "</strong>"
            )
        else:
            link = re.fullmatch(r"\[([^\]]*)\]\((https?://[^)]+)\)", token)
            if link:
                label, url = link.groups()
                rendered.append(
                    f'<a href="{html.escape(url, quote=True)}" '
                    f'target="_blank" rel="noopener noreferrer">'
                    f"{html.escape(label)}</a>"
                )
        position = match.end()
    rendered.append(html.escape(value[position:].replace(r"\|", "|")))
    return "".join(rendered)


def _markdown_table_cells(line: str) -> list[str]:
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip())[1:-1]]


def render_preview_html(markdown_text: str) -> str:
    """把生成的受控 Markdown 预览转换成不截断字段的独立 HTML。"""
    body: list[str] = []
    table_open = False
    header_open = False
    for line in markdown_text.splitlines():
        if line.startswith("|") and line.endswith("|"):
            cells = _markdown_table_cells(line)
            separator = bool(cells) and all(
                re.fullmatch(r":?-{3,}:?", cell) for cell in cells
            )
            if not table_open:
                body.extend(["<div class=\"table-scroll\"><table><thead>"])
                table_open = True
                header_open = True
                body.append(
                    "<tr>" + "".join(
                        f"<th>{_markdown_inline_html(cell)}</th>" for cell in cells
                    ) + "</tr>"
                )
            elif separator:
                if header_open:
                    body.extend(["</thead><tbody>"])
                    header_open = False
            else:
                body.append(
                    "<tr>" + "".join(
                        f"<td>{_markdown_inline_html(cell)}</td>" for cell in cells
                    ) + "</tr>"
                )
            continue
        if table_open:
            body.extend([
                "</thead>" if header_open else "</tbody>",
                "</table></div>",
            ])
            table_open = False
            header_open = False
        if line.startswith("## "):
            body.append(f"<h2>{html.escape(line[3:])}</h2>")
        elif line.startswith("# "):
            body.append(f"<h1>{html.escape(line[2:])}</h1>")
        elif line:
            body.append(f"<p>{_markdown_inline_html(line)}</p>")
    if table_open:
        body.extend([
            "</thead>" if header_open else "</tbody>",
            "</table></div>",
        ])
    return """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>游戏目录重命名预览</title>
<style>
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
html, body { margin: 0; width: 100%; min-height: 100%; overflow-y: auto; overflow-x: hidden; }
body { font: 14px/1.5 system-ui, "Microsoft YaHei UI", sans-serif; padding: 16px; }
main { width: 100%; }
.table-scroll { width: 100%; overflow: visible; margin: 12px 0 28px; }
table { border-collapse: collapse; width: 100%; table-layout: auto; }
th, td { border: 1px solid #8888; padding: 8px 10px; text-align: left;
  vertical-align: top; white-space: normal; overflow-wrap: anywhere; }
thead th { position: sticky; top: 0; z-index: 2; background: Canvas; }
tr:nth-child(even) td { background: color-mix(in srgb, CanvasText 4%, Canvas); }
a { color: LinkText; }
h1, h2, p { max-width: 100%; overflow-wrap: anywhere; }
</style>
</head>
<body><main>
""" + "\n".join(body) + "\n</main></body>\n</html>\n"


def write_preview(
    manifest: dict[str, Any], output_dir: Path
) -> tuple[Path, Path, Path, Path]:
    scan_root = Path(manifest["root"]).resolve()
    if _inside(output_dir.resolve(), scan_root):
        raise ValueError("预览输出目录必须位于扫描根目录之外")
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "rename-manifest.json"
    table_path = output_dir / "preview.md"
    decisions_path = output_dir / "decisions.md"
    html_path = output_dir / "preview.html"
    table, decisions = render_preview_documents(manifest)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    table_path.write_text(table, encoding="utf-8")
    decisions_path.write_text(decisions, encoding="utf-8")
    html_path.write_text(
        render_semantic_preview_html(manifest)
        if int(manifest.get("schema_version") or 1) >= 2
        else render_preview_html(table),
        encoding="utf-8",
    )
    return manifest_path, table_path, decisions_path, html_path


def load_manifest(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json_document(document: dict[str, Any], path: Path) -> Path:
    """以 UTF-8 写入可审计阶段文件；调用方先完成所有验证。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def assert_stage_output_outside_root(
    analysis: dict[str, Any],
    output: Path,
) -> None:
    """阶段文件和目录不得写入只读扫描根。"""
    if _inside(output.resolve(), Path(str(analysis["root"])).resolve()):
        raise ValueError("阶段输出必须位于扫描根目录之外")


# CLI 边界：search 唯一允许网络；apply 只消费已批准的本地清单。
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="安全生成并执行游戏目录规范化清单")
    subparsers = parser.add_subparsers(dest="command", required=True)
    scan_evidence = subparsers.add_parser(
        "scan-evidence", help="只读收集全部启动候选和多层祖先事实"
    )
    scan_evidence.add_argument("root", type=Path)
    scan_evidence.add_argument("--output", type=Path, required=True)
    scan_evidence.add_argument(
        "--include-path", action="append", type=Path,
        help="只发现该相对子树；更新系列时需选完整合集及全部成员",
    )
    compile_hierarchy = subparsers.add_parser(
        "compile-hierarchy-semantics",
        help="验证 Skill 层级决策并生成最终 analysis-input",
    )
    compile_hierarchy.add_argument("scan_evidence", type=Path)
    compile_hierarchy.add_argument("decisions", type=Path)
    compile_hierarchy.add_argument("--output", type=Path, required=True)
    search = subparsers.add_parser(
        "search", help="验证 Skill 本地语义并生成三站搜索证据"
    )
    search.add_argument("analysis", type=Path)
    search.add_argument("local_semantics", type=Path)
    search.add_argument("--output", type=Path, required=True)
    search.add_argument("--timeout", type=float, default=4.0)
    search.add_argument("--max-results", type=int, default=12)
    _add_search_storage_arguments(search)
    search.add_argument(
        "--candidate-seeds",
        type=Path,
        help="Skill 对用户明确详情链接的逐游戏候选映射 JSON",
    )
    search.add_argument(
        "--user-supplements",
        type=Path,
        help="绑定当前 analysis 的用户人工补充 JSON；可与既有候选种子合并",
    )
    retry_errors = subparsers.add_parser(
        "retry-network-errors",
        help="在三来源预检后只重试旧证据中仍带网络错误的轮次",
    )
    retry_errors.add_argument("analysis", type=Path)
    retry_errors.add_argument("local_semantics", type=Path)
    retry_errors.add_argument("previous_evidence", type=Path)
    retry_errors.add_argument("--output", type=Path, required=True)
    retry_errors.add_argument("--timeout", type=float, default=4.0)
    retry_errors.add_argument("--max-results", type=int, default=12)
    _add_search_storage_arguments(retry_errors)
    compile_local = subparsers.add_parser(
        "compile-local-semantics",
        help="把 Skill 紧凑本地决策与分析证据编译为完整语义",
    )
    compile_local.add_argument("analysis", type=Path)
    compile_local.add_argument("decisions", type=Path)
    compile_local.add_argument(
        "--user-supplements",
        type=Path,
        help="仅覆盖显式人工证据并确认非游戏启动候选的 JSON",
    )
    compile_local.add_argument("--output", type=Path, required=True)
    compile_remote = subparsers.add_parser(
        "compile-remote-semantics",
        help="把 Skill 候选判断与搜索证据编译为完整远程语义",
    )
    compile_remote.add_argument("analysis", type=Path)
    compile_remote.add_argument("local_semantics", type=Path)
    compile_remote.add_argument("search_evidence", type=Path)
    compile_remote.add_argument("decisions", type=Path)
    compile_remote.add_argument("--output", type=Path, required=True)
    semantic_preview = subparsers.add_parser(
        "semantic-preview",
        help="消费完整本地、搜索和远程阶段并生成最终批准预览",
    )
    semantic_preview.add_argument("analysis", type=Path)
    semantic_preview.add_argument("local_semantics", type=Path)
    semantic_preview.add_argument("search_evidence", type=Path)
    semantic_preview.add_argument("remote_semantics", type=Path)
    semantic_preview.add_argument("--output-dir", type=Path, required=True)
    semantic_preview.add_argument(
        "--success-catalog",
        type=Path,
        default=SCRIPT_DIRECTORY.parents[3] / "data" / "successful-games.csv",
        help="跨批次持久化可信且命名成功的游戏；默认位于项目 data 目录",
    )
    combine_preview = subparsers.add_parser(
        "combine-preview", help="把多个批次 manifest 合并为单一批准码预览"
    )
    combine_preview.add_argument("manifests", nargs="+", type=Path)
    combine_preview.add_argument("--output-dir", type=Path, required=True)
    update_combined = subparsers.add_parser(
        "update-combined-preview",
        help="用一个或多个新单批预览替换旧汇总中的部分条目",
    )
    update_combined.add_argument("previous_manifest", type=Path)
    update_combined.add_argument("updated_manifests", nargs="+", type=Path)
    update_combined.add_argument("--output-dir", type=Path, required=True)
    apply_command = subparsers.add_parser("apply", help="执行批准码绑定的清单")
    apply_command.add_argument("manifest", type=Path)
    apply_command.add_argument("--approve", required=True)
    apply_command.add_argument("--receipt", type=Path)
    undo = subparsers.add_parser("undo-preview", help="由执行回执生成撤销预览")
    undo.add_argument("receipt", type=Path)
    undo.add_argument("--output-dir", type=Path, required=True)
    return parser


def _add_search_storage_arguments(command: argparse.ArgumentParser) -> None:
    """为正式搜索和网络错误恢复保留同一缓存与日志参数。"""
    intermediate = SCRIPT_DIRECTORY.parents[3] / "~outputs-intermediate"
    command.add_argument(
        "--cache-file", type=Path,
        default=intermediate / "rename-remote-cache" / "remote-search-cache.json",
    )
    command.add_argument(
        "--log-root", type=Path,
        default=intermediate / "rename-search-logs",
    )


def _prepare_search_sources(
    args: argparse.Namespace, analysis: dict[str, Any],
) -> tuple[list[CachedSource], RemoteSearchCache, Path]:
    """先真实预检三来源，再验证缓存位置并创建缓存包装器。"""
    clients = [
        VNDBClient(timeout=args.timeout, max_results=args.max_results),
        DLsiteClient(timeout=args.timeout, max_results=args.max_results),
        SteamClient(timeout=args.timeout, max_results=args.max_results),
    ]
    preflight_search_sources(clients)
    print("[preflight] VNDB、DLsite、Steam 连通且响应可解析", flush=True)
    cache_file = args.cache_file.resolve()
    assert_stage_output_outside_root(analysis, cache_file)
    cache = RemoteSearchCache(cache_file)
    sources = [
        CachedSource(client, cache, _remote_dict, RemoteEntry)
        for client in clients
    ]
    return sources, cache, cache_file


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "scan-evidence":
        root = args.root.resolve()
        output = args.output.resolve()
        if _inside(output, root):
            raise ValueError("扫描证据输出必须位于扫描根目录之外")
        document = build_scan_evidence(root, include_paths=args.include_path)
        write_json_document(document, output)
        print(json.dumps({
            "scan_evidence": str(output),
            "scan_fingerprint": document["scan_fingerprint"],
        }, ensure_ascii=False, indent=2))
        return 0
    if args.command == "compile-hierarchy-semantics":
        scan = load_manifest(args.scan_evidence)
        decisions = load_manifest(args.decisions)
        output = args.output.resolve()
        root = Path(str(scan["root"])).resolve()
        if _inside(output, root):
            raise ValueError("层级编译输出必须位于扫描根目录之外")
        compiled = compile_hierarchy_semantics(scan, decisions)
        analysis = build_analysis_input_from_hierarchy(
            scan, compiled, decisions
        )
        packet_output = output.with_name("local-review-packet.json")
        if packet_output == output:
            raise ValueError("分析输出不能使用审核包保留文件名")
        write_json_document(analysis, output)
        write_json_document(local_review_packet(analysis), packet_output)
        compiled_output = output.with_name("hierarchy-compiled.json")
        write_json_document(compiled, compiled_output)
        write_json_document(
            scan, output.with_name("scan-evidence.json")
        )
        write_json_document(
            decisions, output.with_name("hierarchy-semantics.json")
        )
        print(json.dumps({
            "analysis_input": str(output),
            "local_review_packet": str(packet_output),
            "input_fingerprint": analysis["input_fingerprint"],
            "compiled_hierarchy": str(compiled_output),
        }, ensure_ascii=False, indent=2))
        return 0
    if args.command == "search":
        analysis = load_manifest(args.analysis)
        output = args.output.resolve()
        assert_stage_output_outside_root(analysis, output)
        packet_output = output.with_name("remote-review-packet.json")
        if packet_output == output:
            raise ValueError("搜索输出不能使用审核包保留文件名")
        local_semantics = load_manifest(args.local_semantics)
        validate_local_semantics(analysis, local_semantics)
        seed_documents: list[dict[str, Any]] = []
        if args.candidate_seeds:
            seed_documents.append(load_manifest(args.candidate_seeds))
        normalized_supplements = None
        if args.user_supplements:
            supplied = load_manifest(args.user_supplements)
            normalized_supplements = validate_user_supplements(
                analysis, supplied,
            )
            seed_documents.append(
                candidate_seeds_from_user_supplements(
                    analysis, local_semantics, supplied,
                )
            )
        candidate_seeds = merge_candidate_seed_documents(
            analysis, local_semantics, seed_documents,
        )
        sources, cache, cache_file = _prepare_search_sources(args, analysis)
        run_dir = begin_search_audit(analysis, local_semantics, args.log_root)
        try:
            evidence = attach_search_audit(
                attach_user_supplements_to_search_evidence(
                    build_search_evidence(
                        analysis,
                        local_semantics,
                        sources,
                        candidate_seeds,
                        lambda source, completed, total: print(
                            f"[search] {source}: {completed}/{total}",
                            flush=True,
                        ),
                    ),
                    normalized_supplements,
                ),
                run_dir,
            )
            if evidence.get("candidate_seeds"):
                write_json_document(
                    evidence["candidate_seeds"],
                    run_dir / "candidate-seeds.json",
                )
            if evidence.get("user_supplements"):
                write_json_document(
                    evidence["user_supplements"],
                    run_dir / "user-supplements.json",
                )
            cache.flush()
            write_json_document(remote_review_packet(evidence), packet_output)
            finish_search_audit(run_dir, evidence)
            write_json_document(evidence, output)
        except Exception as exc:
            fail_search_audit(run_dir, "search", exc)
            raise
        print(json.dumps({
            "search_evidence": str(output),
            "remote_review_packet": str(packet_output),
            "remote_cache": str(cache_file),
            "search_fingerprint": evidence["search_fingerprint"],
            "audit_log_dir": str(run_dir),
        }, ensure_ascii=False, indent=2))
        return 0
    if args.command == "retry-network-errors":
        analysis = load_manifest(args.analysis)
        output = args.output.resolve()
        assert_stage_output_outside_root(analysis, output)
        packet_output = output.with_name("remote-review-packet.json")
        if packet_output == output:
            raise ValueError("搜索输出不能使用审核包保留文件名")
        local_semantics = load_manifest(args.local_semantics)
        validate_local_semantics(analysis, local_semantics)
        previous_evidence = load_manifest(args.previous_evidence)
        validate_search_audit(
            analysis, local_semantics, previous_evidence, verify_games=True
        )
        sources, cache, cache_file = _prepare_search_sources(args, analysis)
        run_dir = begin_search_audit(analysis, local_semantics, args.log_root)
        try:
            evidence = attach_search_audit(
                retry_search_network_errors(
                    analysis,
                    local_semantics,
                    previous_evidence,
                    sources,
                    _remote_dict,
                    lambda source, completed, total: print(
                        f"[retry] {source}: {completed}/{total}", flush=True
                    ),
                ),
                run_dir,
            )
            if evidence.get("candidate_seeds"):
                write_json_document(
                    evidence["candidate_seeds"],
                    run_dir / "candidate-seeds.json",
                )
            cache.flush()
            write_json_document(remote_review_packet(evidence), packet_output)
            finish_search_audit(run_dir, evidence)
            write_json_document(evidence, output)
        except Exception as exc:
            fail_search_audit(run_dir, "retry-network-errors", exc)
            raise
        print(json.dumps({
            "search_evidence": str(output),
            "remote_review_packet": str(packet_output),
            "remote_cache": str(cache_file),
            "search_fingerprint": evidence["search_fingerprint"],
            "audit_log_dir": str(run_dir),
        }, ensure_ascii=False, indent=2))
        return 0
    if args.command == "compile-local-semantics":
        analysis = load_manifest(args.analysis)
        output = args.output.resolve()
        assert_stage_output_outside_root(analysis, output)
        compiled = compile_local_semantics(
            analysis,
            load_manifest(args.decisions),
            (
                load_manifest(args.user_supplements)
                if args.user_supplements else None
            ),
        )
        write_json_document(compiled, output)
        print(json.dumps({
            "local_semantics": str(output),
            "local_semantics_fingerprint": canonical_sha256(compiled),
        }, ensure_ascii=False, indent=2))
        return 0
    if args.command == "compile-remote-semantics":
        analysis = load_manifest(args.analysis)
        output = args.output.resolve()
        assert_stage_output_outside_root(analysis, output)
        local_semantics = load_manifest(args.local_semantics)
        search_evidence = load_manifest(args.search_evidence)
        validate_search_audit(
            analysis, local_semantics, search_evidence, verify_games=False
        )
        compiled = compile_remote_semantics(
            analysis, local_semantics, search_evidence,
            load_manifest(args.decisions),
        )
        write_json_document(compiled, output)
        print(json.dumps({
            "remote_semantics": str(output),
            "remote_semantics_fingerprint": canonical_sha256(compiled),
        }, ensure_ascii=False, indent=2))
        return 0
    if args.command == "semantic-preview":
        analysis = load_manifest(args.analysis)
        assert_stage_output_outside_root(analysis, args.output_dir)
        assert_stage_output_outside_root(analysis, args.success_catalog)
        local_semantics = load_manifest(args.local_semantics)
        search_evidence = load_manifest(args.search_evidence)
        remote_semantics = load_manifest(args.remote_semantics)
        run_dir = validate_search_audit(
            analysis, local_semantics, search_evidence, verify_games=True
        )
        try:
            manifest = build_manifest_from_semantics(
                analysis, local_semantics, search_evidence, remote_semantics,
            )
            paths = write_preview(manifest, args.output_dir)
            structure_preview = write_folder_structure_preview(
                manifest, args.output_dir / "folder-structure-preview"
            )
            for name, document in (
                ("analysis-input.json", analysis),
                ("local-semantics.json", local_semantics),
                ("search-evidence.json", search_evidence),
                ("remote-semantics.json", remote_semantics),
            ):
                write_json_document(document, args.output_dir / name)
            if local_semantics.get("user_supplements"):
                write_json_document(
                    local_semantics["user_supplements"],
                    args.output_dir / "user-supplements.json",
                )
            hierarchy_audit = analysis.get("hierarchy_audit") or {}
            for name, key in (
                ("scan-evidence.json", "scan_evidence"),
                ("hierarchy-semantics.json", "hierarchy_semantics"),
                ("hierarchy-compiled.json", "compiled_hierarchy"),
            ):
                document = hierarchy_audit.get(key)
                if isinstance(document, dict) and document:
                    write_json_document(document, args.output_dir / name)
            catalog_stats = update_success_catalog(
                args.success_catalog,
                manifest,
                observed_at=datetime.now().astimezone().isoformat(
                    timespec="seconds"
                ),
            )
            append_catalog_conflicts_to_decisions(paths[2], catalog_stats)
            audit_preview_dir = complete_preview_audit(
                run_dir, remote_semantics, manifest, paths
            )
            write_json_document(
                json.loads(
                    (args.output_dir / "folder-structure-preview.json")
                    .read_text(encoding="utf-8")
                ),
                audit_preview_dir / "folder-structure-preview.json",
            )
            write_json_document(
                {
                    "catalog": str(args.success_catalog.resolve()),
                    **asdict(catalog_stats),
                },
                audit_preview_dir / "success-catalog-update.json",
            )
        except Exception as exc:
            fail_search_audit(run_dir, "semantic-preview", exc)
            raise
        print(json.dumps({
            "approval_hash": manifest["approval_hash"],
            "manifest": str(paths[0]),
            "preview": str(paths[1]),
            "decisions": str(paths[2]),
            "html": str(paths[3]),
            "folder_structure_preview": str(structure_preview),
            "success_catalog": str(args.success_catalog.resolve()),
            "success_catalog_update": asdict(catalog_stats),
        }, ensure_ascii=False, indent=2))
        return 0
    if args.command == "combine-preview":
        manifests = [load_manifest(path) for path in args.manifests]
        combined = build_combined_approval_manifest(manifests)
        apply_manifest_data(
            combined, combined["approval_hash"], preflight_only=True
        )
        paths = write_preview(combined, args.output_dir)
        structure_preview = write_folder_structure_preview(
            combined, args.output_dir / "folder-structure-preview"
        )
        print(json.dumps({
            "approval_hash": combined["approval_hash"],
            "child_approval_hashes": combined["child_approval_hashes"],
            "manifest": str(paths[0]), "preview": str(paths[1]),
            "decisions": str(paths[2]), "html": str(paths[3]),
            "folder_structure_preview": str(structure_preview),
        }, ensure_ascii=False, indent=2))
        return 0
    if args.command == "update-combined-preview":
        previous_path = args.previous_manifest.resolve()
        output_dir = args.output_dir.resolve()
        if output_dir == previous_path.parent:
            raise ValueError("局部更新预览必须写入新目录，保留旧汇总以供审计")
        previous = load_manifest(previous_path)
        root = Path(str(previous.get("root") or "")).resolve()
        if not root.is_dir():
            raise ValueError("旧汇总的扫描根目录不存在")
        updates = [load_manifest(path) for path in args.updated_manifests]
        current_fingerprint = directory_fingerprint(root)
        combined = build_updated_combined_approval_manifest(
            previous, updates, current_fingerprint,
        )
        apply_manifest_data(
            combined, combined["approval_hash"], preflight_only=True
        )
        paths = write_preview(combined, output_dir)
        structure_preview = write_folder_structure_preview(
            combined, output_dir / "folder-structure-preview"
        )
        print(json.dumps({
            "approval_hash": combined["approval_hash"],
            "manifest": str(paths[0]),
            "preview": str(paths[1]),
            "decisions": str(paths[2]),
            "html": str(paths[3]),
            "folder_structure_preview": str(structure_preview),
            "updated_manifests": [str(path.resolve()) for path in args.updated_manifests],
        }, ensure_ascii=False, indent=2))
        return 0
    if args.command == "undo-preview":
        manifest = build_undo_manifest(load_manifest(args.receipt))
        paths = write_preview(manifest, args.output_dir)
        structure_preview = write_folder_structure_preview(
            manifest, args.output_dir / "folder-structure-preview"
        )
        print(json.dumps({
            "approval_hash": manifest["approval_hash"],
            "manifest": str(paths[0]), "preview": str(paths[1]),
            "decisions": str(paths[2]), "html": str(paths[3]),
            "folder_structure_preview": str(structure_preview),
        }, ensure_ascii=False, indent=2))
        return 0
    manifest = load_manifest(args.manifest)
    receipt_path = args.receipt or args.manifest.with_name("apply-receipt.json")
    receipt = apply_manifest_with_receipt(
        manifest, args.approve, receipt_path
    )
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    try:
        raise SystemExit(main())
    except (ApprovalError, PreflightError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(2)
