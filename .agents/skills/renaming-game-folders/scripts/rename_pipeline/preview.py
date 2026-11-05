"""层级编号、语义行模型与不依赖 Markdown 的 HTML 渲染。"""

from __future__ import annotations

import copy
import difflib
import html
import re
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

from .approval import build_combined_approval_manifest
from .contracts import source_shortcut_metadata_tokens

from .naming import (
    _literal_tag_patterns,
    normalize_title_delimiters,
    original_name_lines,
    positive_tag_values,
    target_name_lines,
)
from .text_normalization import (
    LANGUAGE_EDITION_QUALIFIER_RE,
    developer_identity_key,
    strip_language_edition_qualifiers,
    title_identity_key,
)


SOURCES = ("VNDB", "DLsite", "Steam")
LANGUAGES = ("jp", "zh", "en")


def search_reports_for_preview(
    search_game: dict[str, Any],
) -> list[dict[str, Any]]:
    """把阶段化搜索证据转换为正式与实时预览共用的来源报告。"""
    reports: list[dict[str, Any]] = []
    for source in search_game.get("sources", []):
        rounds = []
        for item in source.get("rounds", []):
            candidates = item.get("candidates") or []
            candidate = candidates[0] if candidates else None
            release_pages = (candidate or {}).get("release_pages") or []
            if len(release_pages) == 1:
                # Release 页标题/制作方仍只取 Release；年份和年龄采用该 VN
                # 全 Release 汇总值，与为该 Release URL 命名的快捷方式一致。
                result = dict(release_pages[0])
                if candidate:
                    result["release_year"] = candidate.get("release_year")
                    result["audience_classification"] = candidate.get(
                        "audience_classification"
                    )
                result_page_label = "结果页：VNDB Release 页（r 类）"
            elif release_pages:
                result = None
                result_page_label = "结果页：多个 VNDB Release 页，字段不合并"
            else:
                result = candidate
                result_page_label = _candidate_page_label(
                    str(source.get("source") or ""), result
                )
            rounds.append({
                "number": item.get("number"),
                "language": item.get("language"),
                "query": item.get("query"),
                "status": item.get("status"),
                "title": item.get("query"),
                "result": result,
                "candidate_keys": [
                    str(value.get("candidate_key") or "")
                    for value in candidates
                ],
                "result_page_label": result_page_label,
                **({"error": item.get("error")} if item.get("error") else {}),
                **({
                    "fallback_round": item.get("fallback_round"),
                    "fallback_status": item.get("fallback_status"),
                    "fallback_query": item.get("fallback_query"),
                    "discovery_method": item.get("discovery_method"),
                } if item.get("fallback_round") else {}),
                **({"fallback_error": item.get("fallback_error")}
                   if item.get("fallback_error") else {}),
                **({"prior_error": item.get("prior_error")}
                   if item.get("prior_error") else {}),
                **({"prior_status": item.get("prior_status")}
                   if item.get("prior_status") else {}),
            })
        reports.append({
            "source": source.get("source"),
            "entries": copy.deepcopy(source.get("candidates") or []),
            "rounds": rounds,
            "fallback_attempts": copy.deepcopy(
                source.get("fallback_attempts") or []
            ),
        })
    return reports


def _candidate_page_label(
    source: str, candidate: dict[str, Any] | None,
) -> str:
    """标识 VNDB 返回项是作品页还是 release 页，不把两类字段混看。"""
    if source.casefold() != "vndb" or not candidate:
        return ""
    candidate_id = str(candidate.get("source_id") or "").strip()
    if not candidate_id:
        url = str(candidate.get("url") or "")
        match = re.search(r"/([vr]\d+)/?$", url, flags=re.IGNORECASE)
        candidate_id = match.group(1) if match else ""
    if re.fullmatch(r"v\d+", candidate_id, flags=re.IGNORECASE):
        return "结果页：VNDB 作品页（v 类）"
    if re.fullmatch(r"r\d+", candidate_id, flags=re.IGNORECASE):
        return "结果页：VNDB Release 页（r 类）"
    return ""


def group_source_shortcuts(
    manifest: dict[str, Any],
) -> list[tuple[str, list[dict[str, Any]]]]:
    """按游戏目录汇总来源快捷方式，供 Markdown 与 HTML 共用。"""
    groups: list[tuple[str, list[dict[str, Any]]]] = []
    for entry in manifest.get("entries", []):
        shortcuts = list(entry.get("source_shortcuts") or [])
        if not shortcuts:
            continue
        destination = (
            entry.get("target") if entry.get("status") == "可执行"
            else entry.get("runtime_source") or entry.get("source")
        )
        groups.append((str(destination or ""), shortcuts))
    return groups


def order_and_number_preview_entries(
    entries: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """按完整原始路径排序，并附加只用于显示的稳定层级编号。"""
    ordered = sorted(
        (copy.deepcopy(item) for item in entries),
        key=lambda item: str(item.get("source") or "").casefold(),
    )
    parent_numbers: dict[str, str] = {}
    child_counts: dict[str, int] = {}
    number = 0
    for item in ordered:
        parent = item.get("parent_series_source")
        parent_key = str(Path(str(parent)).resolve()).casefold() if parent else ""
        if parent_key and parent_key in parent_numbers:
            child_counts[parent_key] = child_counts.get(parent_key, 0) + 1
            item["display_number"] = (
                f"{parent_numbers[parent_key]}.{child_counts[parent_key]}"
            )
            continue
        number += 1
        item["display_number"] = str(number)
        if item.get("kind") == "series":
            source_key = str(Path(str(item.get("source") or "")).resolve()).casefold()
            parent_numbers[source_key] = str(number)
    return ordered


def name_preview_lines(
    entry: dict[str, Any], original: bool
) -> tuple[str, ...]:
    """把真实 basename 无损切成预览多行；连接各行必须还原路径名称。"""
    path_value = entry.get("source") if original else _preview_target_path(entry)
    basename = Path(str(path_value)).name if path_value else ""
    if not basename:
        return ("", "", "", "", "", "", "")
    semantic = _aligned_preview_semantics(entry, original)
    split = _split_exact_basename(basename, semantic)
    if split is None and original:
        split = _split_exact_basename(
            basename,
            original_name_lines(
                entry.get("local_metadata") or {}, entry.get("source") or "",
            ),
        )
    split = split or ("", "", basename, "", "", "", "")
    if original:
        split = _align_literal_original_lines(
            split, name_preview_lines(entry, False),
        )
    return split


def marker_preview_lines(entry: dict[str, Any]) -> tuple[str, ...]:
    """把英文投影前的完整规范名称 TXT 按目标名称语义槽分行。"""
    marker_name = str(entry.get("marker_name") or "")
    marker_metadata = entry.get("marker_metadata") or {}
    if not marker_name:
        return ()
    if marker_metadata:
        lines = list(target_name_lines(marker_metadata))
        if lines:
            last = max(
                (index for index, value in enumerate(lines) if value),
                default=-1,
            )
            if last >= 0:
                lines[last] += ".txt"
                if " ".join(value for value in lines if value) == marker_name:
                    return tuple(lines)
    return (marker_name,)


def _marker_preview_row_html(entry: dict[str, Any], entry_index: int) -> str:
    """生成插在游戏名称行与所属合集名称行之间的独立 TXT 行。"""
    marker_name = str(entry.get("marker_name") or "")
    marker_lines = marker_preview_lines(entry)
    marker_stack = _html_target_stack(
        tuple("" for _ in marker_lines), marker_lines,
    )
    return (
        f'<tr class="marker-preview-row" data-preview-entry="{entry_index}">'
        '<td class="name original marker-preview-empty"></td>'
        '<td class="name target marker-preview" '
        f'data-basename="{html.escape(marker_name, quote=True)}">'
        + marker_stack + '</td>'
        '<td></td><td></td><td></td><td></td>'
        '</tr>'
    )


def _preview_target_path(entry: dict[str, Any]) -> Any:
    """返回预计名称列对应的真实路径；无动作条目明确显示保持原名。"""
    if entry.get("status") == "可执行" and entry.get("target"):
        return entry.get("target")
    return entry.get("source")


def _tag_specs(metadata: dict[str, Any]) -> list[tuple[str, str, str]]:
    """按稳定语义槽列出真实可能显示的末尾标签。"""
    specs: list[tuple[str, str, str]] = []
    negative = [str(value or "").lstrip("%#") for value in (
        metadata.get("negative_tags") or ()
    ) if value]
    group = str(metadata.get("translation_group") or "").lstrip("%#")
    translation_statuses = {"eng", "生肉", "机翻", "ai汉化"}
    if group and not (
        group in {"未知汉化组", "汉化组未知", "Unknown_CH"}
        and any(value.casefold() in translation_statuses for value in negative)
    ):
        specs.append(("group:" + _alignment_key(group), "%" + group, group))
    for positive in positive_tag_values(metadata.get("positive_tag")):
        specs.append(("positive:" + _alignment_key(positive), "%" + positive, positive))
    for value in negative:
        specs.append(("negative:" + _alignment_key(value), "#" + value, value))
    return specs


def _tag_segments(
    line: str, metadata: dict[str, Any], *, original: bool,
) -> tuple[list[str], dict[str, str]]:
    """把同一方括号组内的每个标签拆成独立显示行。"""
    if not line:
        return [], {}
    specs = _tag_specs(metadata)
    matches: list[tuple[int, int, str]] = []
    for key, literal, semantic in specs:
        candidates = (
            _literal_tag_patterns(
                semantic, translation_group=key.startswith("group:"),
            ) if original
            else (re.escape(literal),)
        )
        found = None
        for pattern in candidates:
            candidate = re.search(
                (rf"[%#]?(?:{pattern})" if original else pattern), line, re.I,
            )
            if candidate and not any(
                candidate.start() < end and candidate.end() > start
                for start, end, _ in matches
            ):
                found = candidate
                break
        if found:
            matches.append((found.start(), found.end(), key))
    if not matches:
        return ["literal:0"], {"literal:0": line}
    matches.sort()
    values: dict[str, str] = {}
    for index, (start, _end, key) in enumerate(matches):
        segment_start = 0 if index == 0 else start
        segment_end = matches[index + 1][0] if index + 1 < len(matches) else len(line)
        values[key] = line[segment_start:segment_end]
    return [key for _, _, key in matches], values


def _separate_original_languages(
    entry: dict[str, Any], lines: Sequence[str],
) -> tuple[str, ...]:
    """只移动原名中的真实字面片段，使日中英各占自己的对齐行。"""
    result = list(lines)
    blob = "".join(result[2:5])
    if not blob:
        return tuple(result)
    if not result[1] and (developer := re.match(r"^(\[[^\]]+\]\s*)", blob)):
        result[1] = developer.group(1)
        blob = blob[developer.end():]
    metadata = entry.get("local_metadata") or {}
    candidates: list[tuple[int, int, int]] = []
    for row, field in ((2, "jp_title"), (3, "zh_title"), (4, "en_title")):
        title = str(metadata.get(field) or "")
        start = blob.find(title) if title else -1
        if start >= 0:
            candidates.append((start, start + len(title), row))
    selected: list[tuple[int, int, int]] = []
    for candidate in sorted(candidates, key=lambda item: (item[0], -(item[1] - item[0]))):
        start, end, _ = candidate
        if not any(start < other_end and end > other_start
                   for other_start, other_end, _ in selected):
            selected.append(candidate)
    if not selected:
        return tuple(result)
    selected.sort()
    result[2:5] = ["", "", ""]
    for index, (start, _end, row) in enumerate(selected):
        segment_start = 0 if index == 0 else start
        segment_end = selected[index + 1][0] if index + 1 < len(selected) else len(blob)
        result[row] += blob[segment_start:segment_end]
    return tuple(result)


def _original_display_components(
    entry: dict[str, Any],
) -> tuple[tuple[str, ...], list[str], dict[str, str]]:
    """从真实原 basename 独立定位字段，允许版本、DLC 与标签交错出现。"""
    raw = Path(str(entry.get("source") or "")).name
    metadata = entry.get("local_metadata") or {}
    candidates: list[tuple[int, int, str, int]] = []
    if developer := re.match(r"^\[[^\]]+\]", raw):
        candidates.append((developer.start(), developer.end(), "line:1", 0))
    for row, field in ((2, "jp_title"), (3, "zh_title"), (4, "en_title")):
        title = str(metadata.get(field) or "")
        if title and (start := raw.find(title)) >= 0:
            candidates.append((start, start + len(title), f"line:{row}", 1))
    # local_segments 是 Skill 已确认的 basename 原文字段。标题由本地文件证据
    # 恢复出准确字形时，它可能不再与 corrected title 逐字相同；这里只把原字段
    # 对齐到最相近的已标注语言槽，不改写内容，也不参与任何命名决策。
    labelled_titles = [
        (row, str(metadata.get(field) or ""))
        for row, field in ((2, "jp_title"), (3, "zh_title"), (4, "en_title"))
        if metadata.get(field)
    ]
    explicit_segment_rows: dict[str, int] = {}
    for language, row in (("jp", 2), ("zh", 3), ("en", 4)):
        for segment in (
            entry.get("local_title_segments_by_language") or {}
        ).get(language, []):
            explicit_segment_rows[str(segment)] = row
    for segment_value in entry.get("local_title_segments") or []:
        segment = str(segment_value or "")
        start = raw.find(segment) if segment else -1
        if start < 0 or not labelled_titles:
            continue
        if segment in explicit_segment_rows:
            candidates.append((
                start, start + len(segment),
                f"line:{explicit_segment_rows[segment]}", 1,
            ))
            continue
        segment_key = _alignment_key(segment)
        scores = [
            (
                difflib.SequenceMatcher(
                    None, segment_key, _alignment_key(title), autojunk=False,
                ).ratio(),
                row,
            )
            for row, title in labelled_titles
            if _alignment_key(title)
        ]
        if scores:
            score, row = max(scores, key=lambda item: (item[0], -item[1]))
            if score >= 0.35:
                candidates.append((
                    start, start + len(segment), f"line:{row}", 1,
                ))
    engine = str(metadata.get("engine") or "")
    if engine and (match := re.search(re.escape(engine), raw, re.I)):
        candidates.append((match.start(), match.end(), "line:5", 2))
    if metadata.get("version"):
        for match in re.finditer(
            r"(?i)(?:本体\s*)?(?:v(?:er(?:sion)?)?\.?)\s*"
            r"[0-9][0-9A-Za-z._-]*",
            raw,
        ):
            candidates.append((match.start(), match.end(), "line:6", 2))
    if metadata.get("dlc"):
        for match in re.finditer(r"(?i)(?<![0-9A-Za-z])\+?DLC(?![0-9A-Za-z])", raw):
            candidates.append((match.start(), match.end(), "line:6", 2))
    for key, _literal, semantic in _tag_specs(metadata):
        for pattern in _literal_tag_patterns(
            semantic, translation_group=key.startswith("group:"),
        ):
            if match := re.search(rf"[%#]?(?:{pattern})", raw, re.I):
                candidates.append((match.start(), match.end(), "tag:" + key, 3))
                break

    selected: list[tuple[int, int, str, int]] = []
    for candidate in sorted(
        candidates, key=lambda item: (item[3], item[0], -(item[1] - item[0])),
    ):
        start, end, _, _ = candidate
        if not any(start < other_end and end > other_start
                   for other_start, other_end, _, _ in selected):
            selected.append(candidate)
    selected.sort()
    if not selected:
        return (("", "", raw, "", "", "", ""), [], {})

    values: dict[str, str] = {}
    previous_end = 0
    previous_key = ""
    last_title_key = ""
    for start, end, key, _priority in selected:
        gap = raw[previous_end:start]
        if gap:
            title_keys = {"line:2", "line:3", "line:4"}
            stripped_gap = gap.strip()
            is_literal_separator = bool(stripped_gap) and all(
                not character.isalnum() for character in stripped_gap
            )
            if previous_key and key != previous_key and (
                (
                    previous_key in title_keys
                    and key in title_keys
                )
                or (
                    previous_key in title_keys
                    and is_literal_separator
                )
            ):
                # Semantic rows may reorder fields, but every literal separator
                # must remain visible.  Keep whitespace with the preceding title
                # and attach delimiters such as "=" or "[" to the field they
                # introduce instead of fabricating "[]" in a title row.
                leading_space = gap[:len(gap) - len(gap.lstrip())]
                values[previous_key] = (
                    values.get(previous_key, "") + leading_space
                )
                values[key] = values.get(key, "") + gap[len(leading_space):]
            elif gap.strip() and not previous_key.startswith("line:"):
                gap_key = last_title_key or "line:2"
                values[gap_key] = values.get(gap_key, "") + gap
            else:
                gap_key = previous_key or key
                values[gap_key] = values.get(gap_key, "") + gap
        values[key] = values.get(key, "") + raw[start:end]
        if key in {"line:2", "line:3", "line:4"}:
            last_title_key = key
        previous_key = key
        previous_end = end
    tail = raw[previous_end:]
    if tail:
        # A closing delimiter belongs to the final located field (normally the
        # last metadata tag), never to the last title merely because that field
        # uses a dynamic tag row.
        tail_key = previous_key or last_title_key or "line:2"
        values[tail_key] = values.get(tail_key, "") + tail

    base = tuple(values.get(f"line:{index}", "") for index in range(7))
    tag_order = [
        key for key, _, _ in _tag_specs(metadata)
        if "tag:" + key in values
    ]
    tags = {key: values["tag:" + key] for key in tag_order}
    return base, tag_order, tags


def _split_target_engine_version(
    suffix: str, metadata: dict[str, Any],
) -> tuple[str, str]:
    """把真实目标后缀按引擎与版本/DLC 切为两个显示行。"""
    markers: list[int] = []
    version = str(metadata.get("version") or "")
    if version:
        if metadata.get("dlc_version"):
            base_match = re.search(
                rf"(?i)(?<![0-9A-Za-z])本体(?=v{re.escape(version)})",
                suffix,
            )
            if base_match:
                markers.append(base_match.start())
        match = re.search(rf"(?i)(?<![0-9A-Za-z])v{re.escape(version)}", suffix)
        if match:
            markers.append(match.start())
    for version_tag in metadata.get("version_tags") or ():
        text = str(version_tag or "").strip().lstrip("$")
        if not text:
            continue
        pattern = re.escape(text)
        if text.startswith("+"):
            pattern = pattern.replace(r"\~", r"\s*(?:-|~|～|〜)\s*")
        match = re.search(pattern, suffix, re.I)
        if match:
            markers.append(match.start())
    if metadata.get("dlc"):
        dlc_range = str(metadata.get("dlc_range") or "")
        pattern = (
            rf"(?i)(?<![0-9A-Za-z])\+DLC{re.escape(dlc_range)}"
            if dlc_range else
            r"(?i)(?<![0-9A-Za-z])\+DLC(?![A-Za-z])"
        )
        match = re.search(pattern, suffix)
        if match:
            markers.append(match.start())
    if not markers:
        return suffix, ""
    split_at = min(markers)
    engine = str(metadata.get("engine") or "")
    if not engine:
        return "", suffix
    return suffix[:split_at], suffix[split_at:]


def _split_series_range(
    lines: tuple[str, ...], entry: dict[str, Any],
) -> tuple[tuple[str, ...], str]:
    """合集范围独占语言标题后的显示行，不改变真实 basename。"""
    result = list(lines)
    if entry.get("kind") != "series":
        return tuple(result), ""
    collection_pattern = re.compile(
        r"(?i)(?:\d+部合集|合集(?:(?:\d+|[IVX]+)\s*[~～〜]\s*(?:\d+|[IVX]+))?)\s*$",
    )
    range_pattern = re.compile(
        r"(?i)(?:\d+|[IVX]+)\s*[~～〜]\s*(?:\d+|[IVX]+)\s*$",
    )
    for index in (2, 3, 4):
        match = collection_pattern.search(result[index])
        if match is None:
            match = range_pattern.search(result[index])
        if match:
            value = result[index]
            prefix = value[:match.start()]
            title_end = len(prefix.rstrip())
            result[index] = value[:title_end]
            # The range moves to its semantic row, but the literal delimiter
            # before it is part of the basename and must move with it.
            return tuple(result), value[title_end:]
    return tuple(result), ""


def _validate_name_line_coverage(
    entry: dict[str, Any], original: tuple[str, ...], target: tuple[str, ...],
) -> None:
    """拒绝任何可见名称字符丢失、伪造或目标顺序漂移。"""
    original_basename = Path(str(entry.get("source") or "")).name
    target_basename = Path(str(_preview_target_path(entry) or "")).name
    original_visible = "".join(original)
    target_visible = "".join(target)
    # 原名允许按日中英语义槽重排，因此比较完整字符多重集；每个显示片段本身仍只由
    # 原 basename 的字面切片组成。目标格式顺序固定，必须逐字符完全一致。
    if Counter(original_visible) != Counter(original_basename):
        raise ValueError(
            "原文件夹名称预览未逐字符覆盖真实 basename："
            f"{original_basename!r}"
        )
    target_matches = (
        Counter(target_visible) == Counter(target_basename)
        if entry.get("status") != "可执行" or not entry.get("target")
        else target_visible == target_basename
    )
    if not target_matches:
        raise ValueError(
            "预计重命名名称预览与 manifest target basename 不一致："
            f"{target_basename!r}"
        )


def display_name_line_pair(
    entry: dict[str, Any],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """返回预览专用动态行：语言、引擎、版本及每个标签分别对齐。"""
    if entry.get("local_metadata"):
        original_base, original_order, original_tags = (
            _original_display_components(entry)
        )
    else:
        original_lines = name_preview_lines(entry, True)
        original_base = tuple(original_lines[:5]) + ("", original_lines[5])
        original_order, original_tags = _tag_segments(
            original_lines[6], {}, original=True,
        )
    original_base, original_range = _split_series_range(original_base, entry)
    if entry.get("status") != "可执行" or not entry.get("target"):
        tag_order = list(original_order) or ["empty:0"]
        retained = (
            tuple(original_base[:5]) + (original_range,)
            + tuple(original_base[5:])
            + tuple(original_tags.get(key, "") for key in tag_order)
        )
        pair = (retained, retained)
        _validate_name_line_coverage(entry, *pair)
        return pair
    target = name_preview_lines(entry, False)
    target_titles, target_range = _split_series_range(tuple(target[:5]), entry)
    target_engine, target_version = _split_target_engine_version(
        target[5], entry.get("metadata") or {},
    )
    target_order, target_tags = _tag_segments(
        target[6], entry.get("metadata") or {}, original=False,
    )
    tag_order = list(target_order)
    tag_order.extend(key for key in original_order if key not in tag_order)
    if not tag_order:
        tag_order = ["empty:0"]
    pair = (
        tuple(original_base[:5]) + (original_range,) + tuple(original_base[5:])
        + tuple(original_tags.get(key, "") for key in tag_order),
        target_titles + (target_range, target_engine, target_version)
        + tuple(target_tags.get(key, "") for key in tag_order),
    )
    _validate_name_line_coverage(entry, *pair)
    return pair


def _aligned_preview_semantics(
    entry: dict[str, Any], original: bool,
) -> tuple[str, ...]:
    """固定预览槽位，并在不改字的前提下把目标标签拆到第七行。"""
    semantic = _semantic_preview_lines(entry, original)
    if original or semantic[6] or not semantic[5]:
        return semantic
    suffix = semantic[5]
    tag_start = next((
        index for index, char in enumerate(suffix)
        if char in "%#" and index > 0 and suffix[index - 1] in "[ "
    ), None)
    if tag_start is None:
        return semantic
    aligned = list(semantic)
    if tag_start == 1 and suffix.startswith("["):
        aligned[5] = ""
        aligned[6] = suffix
    else:
        aligned[5] = suffix[:tag_start]
        aligned[6] = suffix[tag_start:]
    return tuple(aligned)


def _display_propagated_indexes(
    indexes: Sequence[int], target: tuple[str, ...],
) -> tuple[int, ...]:
    """传播字段为标签时，把旧元数据槽映射到全部独立标签行。"""
    displayed: list[int] = []
    for index in indexes:
        if index == 5 and any(target[8:]):
            displayed.extend(
                line_index for line_index in range(8, len(target))
                if target[line_index]
            )
        else:
            displayed.append(index)
    return tuple(dict.fromkeys(displayed))


def _split_exact_basename(
    basename: str, semantic: Sequence[str],
) -> tuple[str, ...] | None:
    """仅按 basename 中顺序出现的真实字段切行，空格归入相邻行。"""
    if len(semantic) != 7:
        return None
    result = ["" for _ in range(7)]
    cursor = 0
    previous_index: int | None = None
    found_any = False
    for index, raw_value in enumerate(semantic):
        value = str(raw_value or "")
        if not value:
            continue
        start = basename.find(value, cursor)
        if start < 0:
            return None
        if start > cursor:
            gap = basename[cursor:start]
            if previous_index is None:
                value = gap + value
            else:
                result[previous_index] += gap
        result[index] += value
        cursor = start + len(value)
        previous_index = index
        found_any = True
    if not found_any:
        return None
    if cursor < len(basename):
        if previous_index is None:
            return None
        result[previous_index] += basename[cursor:]
    values = tuple(result)
    return values if "".join(values) == basename else None


def _semantic_preview_lines(
    entry: dict[str, Any], original: bool,
) -> tuple[str, ...]:
    """只为差异着色读取审计字段，绝不用于拼装显示名称。"""
    key = "original_preview_lines" if original else "target_preview_lines"
    explicit = entry.get(key)
    if explicit is not None:
        values = tuple(str(value or "") for value in explicit)
        if len(values) != 7:
            raise ValueError(f"{key} 必须恰好包含 7 行")
        return values
    if original:
        return original_name_lines(
            entry.get("local_metadata") or {}, entry.get("source") or "",
        )
    return target_name_lines(entry.get("metadata") or {})


def _alignment_key(value: str) -> str:
    """只用于显示对齐的宽松比较，不改变预览中的磁盘原文。"""
    return re.sub(r"[\W_]+", "", value.casefold(), flags=re.UNICODE)


def _align_literal_original_lines(
    original: tuple[str, ...], target: tuple[str, ...],
) -> tuple[str, ...]:
    """把单行真实 basename 移到最匹配的目标语言行。"""
    title_occupied = [
        index for index in (0, 1, 2, 3, 4) if original[index]
    ]
    if title_occupied != [2] or len(target) != 7:
        return original
    raw_name = original[2]
    raw_key = _alignment_key(raw_name)
    matched: list[tuple[int, int, int]] = []
    target_language_rows = [
        index for index in (2, 3, 4) if target[index]
    ]
    for index in target_language_rows:
        title = target[index].lstrip("=").strip()
        title_key = _alignment_key(title)
        if title_key and title_key in raw_key:
            matched.append((raw_key.index(title_key), -len(title_key), index))
    if matched:
        destination = min(matched)[2]
    elif target_language_rows:
        destination = target_language_rows[0]
    else:
        destination = 2
    if destination == 2:
        return original
    aligned = list(original)
    aligned[2] = ""
    aligned[destination] = raw_name
    return tuple(aligned)


def _auxiliary_preview_lines(entry: dict[str, Any], original: bool) -> list[str]:
    """名称格第三区块：逐行显示内部资料目录的改名前后名称。"""
    key = "source_name" if original else "target_name"
    prefix = "原目录：" if original else "预计目录："
    return [
        prefix + str(item.get(key) or "")
        for item in entry.get("auxiliary_directory_renames") or []
    ]


def round_preview_lines(round_data: dict[str, Any] | None) -> tuple[str, ...]:
    """把单轮查询拆为十行，分别呈现原词与实际补搜状态。"""
    if not round_data or round_data.get("status") in {
        None, "missing", "covered",
    }:
        return ("跳过", "", "", "", "", "", "", "", "", "")
    query = str(round_data.get("query") or "")
    search_line = "搜索：" + query
    # 首轮错误保留在证据中；重试已得到有效响应时显示最终结果。
    source_error = str(round_data.get("error") or "")
    source_status = (
        "来源搜索 网络错误：" + source_error
        if source_error else
        "来源搜索 无结果"
        if round_data.get("status") != "success"
        or round_data.get("fallback_round") else
        "来源搜索 成功"
    )
    fallback_status = str(round_data.get("fallback_status") or "")
    fallback_error = str(round_data.get("fallback_error") or "")
    if round_data.get("discovery_method") == "source_short_keyword":
        short_query = str(round_data.get("fallback_query") or "")
        fallback_line = (
            "短词补搜：" + short_query + " 网络错误：" + fallback_error
            if fallback_status == "network_error" else
            "短词补搜：" + short_query + " 无结果"
            if fallback_status == "no_result" else
            "短词补搜：" + short_query + " 成功"
            if fallback_status == "success" else
            ""
        )
    else:
        fallback_line = ""
    if round_data.get("status") != "success":
        return (
            search_line, "", "", "", "", "", "", "",
            source_status, fallback_line,
        )
    if not round_data.get("result"):
        return (
            search_line, "", "", "", "", "", "", "",
            "来源搜索 成功（候选详情已省略）", fallback_line,
        )
    result = round_data.get("result") or {}
    release_year, age_text = source_shortcut_metadata_tokens(result)
    return (
        search_line,
        "制作方：" + str(result.get("developer") or "无"),
        "日：" + str(result.get("jp_title") or "无"),
        "中：" + str(result.get("zh_title") or "无"),
        "英：" + str(result.get("en_title") or "无"),
        "年份：" + (release_year or "无"),
        "年龄：" + (age_text or "无"),
        "",
        source_status,
        fallback_line,
    )


def _best_source_report(
    entry: dict[str, Any], source: str, report: dict[str, Any] | None,
) -> dict[str, Any]:
    """保留各轮查询事实，只让一个经审核候选在首次命中轮显示。"""
    view = copy.deepcopy(report or {})
    chosen = str((entry.get("preview_candidate_keys") or {}).get(source) or "")
    if not chosen:
        return view
    candidate = next(
        (item for item in view.get("entries") or []
         if str(item.get("candidate_key") or "") == chosen),
        None,
    )
    shown = False
    for round_data in view.get("rounds") or []:
        round_data["result"] = None
        if (
            shown or candidate is None
            or round_data.get("status") != "success"
            or chosen not in (round_data.get("candidate_keys") or [])
        ):
            continue
        release_pages = candidate.get("release_pages") or []
        if len(release_pages) == 1:
            result = dict(release_pages[0])
            result["candidate_key"] = chosen
            result["release_year"] = candidate.get("release_year")
            result["audience_classification"] = candidate.get(
                "audience_classification"
            )
        elif release_pages:
            continue
        else:
            result = candidate
        round_data["result"] = result
        shown = True
    return view


def _source_preview_lines(
    report: dict[str, Any] | None,
    source: str,
    adopted: bool,
    merged: bool,
    adopted_identity_links: Sequence[tuple[str, str]] = (),
    reference_line: str = "",
    nonadoption_reason: str = "",
) -> tuple[str, ...]:
    if (report or {}).get("pending"):
        return (source, "待处理", "", "", "", "", "")
    prefix = (
        "[合并采用]" if adopted and merged else "[采用]" if adopted else ""
    )
    identities = " ".join(
        f"[{label}]({url})" for label, url in adopted_identity_links
    )
    if adopted:
        return (
            prefix, source, identities, nonadoption_reason, "", "", reference_line,
        )
    return (
        source, "", "", nonadoption_reason, "", "", reference_line,
    )


def _displayed_source_candidates(
    report: dict[str, Any] | None,
) -> tuple[dict[str, Any], ...]:
    """只收集右侧日中英轮实际展示的成功候选。"""
    return tuple(
        result
        for item in (report or {}).get("rounds") or []
        if item.get("status") == "success"
        if isinstance((result := item.get("result")), dict) and result
    )


def _source_nonadoption_reason(
    entry: dict[str, Any], source: str, report: dict[str, Any] | None,
) -> str:
    """未采用来源只解释右侧可见候选，不混入采用格。"""
    states = entry.get("preview_candidate_dispositions") or {}
    if states.get(source) == "adopted" or (
        source not in states and source in set(entry.get("selected_sources") or [])
    ):
        return ""
    displayed_keys = {
        str(result.get("candidate_key") or "")
        for result in _displayed_source_candidates(report)
    }
    seen: set[tuple[str, str]] = set()
    labels: list[str] = []
    for item in entry.get("nonadopted_candidate_reasons") or []:
        if (
            str(item.get("source")) != source
            or not item.get("candidate_key")
            or str(item["candidate_key"]) not in displayed_keys
        ):
            continue
        reason = " ".join(str(item.get("reason") or "").split())
        reason = re.sub(r"https?://\S+", "详情页", reason)
        reason = re.sub(r"\bVNDB\s+[vr]\d+\b\s*", "VNDB 候选", reason, flags=re.I)
        reason = re.sub(r"\bSteam\s+\d{5,}\b\s*", "Steam 候选", reason, flags=re.I)
        reason = re.sub(r"\b(?:RJ|VJ|BJ|EJ)\d+\b\s*", "DLsite 候选", reason, flags=re.I)
        if not reason:
            continue
        state = str(item.get("disposition") or "")
        identity = (state, reason)
        if identity in seen:
            continue
        seen.add(identity)
        labels.append(("仅参考：" if state == "reference" else "未采用：") + reason)
    return "；".join(labels)


def _adopted_source_identity_links(
    entry: dict[str, Any], source: str, report: dict[str, Any] | None,
) -> tuple[tuple[str, str], ...]:
    """只返回右侧可见、最终采用且确实会创建快捷方式的编号。"""
    if source not in set(entry.get("selected_sources") or []):
        return ()
    displayed = {
        (str(item.get("source_id") or ""), str(item.get("url") or ""))
        for item in _displayed_source_candidates(report)
    }
    shortcut_urls = {
        str(item.get("url") or "")
        for item in entry.get("source_shortcuts") or []
        if str(item.get("url") or "").startswith(("http://", "https://"))
    }
    links: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for candidate in entry.get("selected_candidates") or []:
        if str(candidate.get("source") or "") != source:
            continue
        release = candidate.get("release") if source == "VNDB" else None
        url = (
            str(release.get("url") or "") if isinstance(release, dict)
            else str(candidate.get("url") or "")
        )
        label = (
            str(release.get("source_id") or "") if isinstance(release, dict)
            else str(candidate.get("source_id") or "")
        )
        identity = (label, url)
        if (
            not label or identity not in displayed
            or url not in shortcut_urls or identity in seen
        ):
            continue
        seen.add(identity)
        links.append(identity)
    return tuple(links)


def _source_reference_line(
    entry: dict[str, Any], source: str, report: dict[str, Any] | None,
) -> str:
    """仅在参考候选于右侧可见时显示其链接。"""
    displayed_urls = {
        str(item.get("url") or "")
        for item in _displayed_source_candidates(report)
    }
    parts: list[str] = []
    highest = entry.get("remote_highest") or {}
    if highest.get("source") == source and highest.get("url") in displayed_urls:
        label = "v" + str(highest.get("version") or highest.get("source_id") or "条目")
        parts.append(f"最高版本：[{label}]({highest['url']})")
    best = entry.get("remote_best_tag") or {}
    if best.get("source") == source and best.get("url") in displayed_urls:
        label = str(best.get("positive_tag") or best.get("source_id") or "条目")
        parts.append(f"最佳正面标签：[{label}]({best['url']})")
    return "；".join(parts)


def _inline(value: str) -> str:
    """渲染一个或多个受控 Markdown 链接，其余内容全部转义。"""
    parts: list[str] = []
    position = 0
    for match in re.finditer(r"\[([^]]+)\]\((https?://[^)]+)\)", value):
        parts.append(html.escape(value[position:match.start()]))
        label, url = match.groups()
        parts.append(
            f'<a href="{html.escape(url, quote=True)}" target="_blank" '
            f'rel="noopener noreferrer">{html.escape(label)}</a>'
        )
        position = match.end()
    parts.append(html.escape(value[position:]))
    return "".join(parts)


def _local_developer(entry: dict[str, Any]) -> str:
    original_lines = entry.get("original_preview_lines")
    if isinstance(original_lines, (list, tuple)) and len(original_lines) == 7:
        value = str(original_lines[1] or "").strip("[] ")
        return re.sub(
            r"\s+#(?:uncheck|eng)\s*$", "", value, flags=re.I
        ).strip()
    conflict = entry.get("developer_conflict") or {}
    conflict_local = conflict.get("local") if isinstance(conflict, dict) else None
    value = str(
        conflict_local
        or (entry.get("local_metadata") or {}).get("developer")
        or ""
    )
    return re.sub(r"\s+#(?:uncheck|eng)\s*$", "", value, flags=re.I).strip()


def _developer_differs(entry: dict[str, Any], value: str) -> bool:
    local = _local_developer(entry)
    return bool(
        local and value and value != "无"
        and developer_identity_key(local) != developer_identity_key(value)
    )


def _highlight_qualifiers_html(value: str) -> str:
    """来源标题保留原文，只把最终名称会删除的语言版说明标红。"""
    parts: list[str] = []
    position = 0
    for match in LANGUAGE_EDITION_QUALIFIER_RE.finditer(value):
        parts.append(html.escape(value[position:match.start()]))
        parts.append(
            '<span class="removed-search-qualifier">'
            + html.escape(match.group(0)) + "</span>"
        )
        position = match.end()
    parts.append(html.escape(value[position:]))
    return "".join(parts)


def _search_line_html(
    entry: dict[str, Any], value: str, line_index: int, *, contributed: bool = False,
) -> str:
    if line_index == 1 and value.startswith("制作方："):
        prefix, developer = value.split("：", 1)
        rendered = html.escape(developer)
        return html.escape(prefix + "：") + (
            f"<strong>{rendered}</strong>" if contributed else rendered
        )
    elif line_index in {2, 3, 4} and "：" in value:
        prefix, title = value.split("：", 1)
        rendered = _highlight_qualifiers_html(title)
        return html.escape(prefix + "：") + (
            f"<strong>{rendered}</strong>" if contributed else rendered
        )
    return _inline(value)


def _highlight_qualifiers_md(value: str) -> str:
    parts: list[str] = []
    position = 0
    for match in LANGUAGE_EDITION_QUALIFIER_RE.finditer(value):
        parts.append(_md(value[position:match.start()]))
        parts.append(
            '<span style="color:#b91c1c;font-weight:700">'
            + _md(match.group(0)) + "</span>"
        )
        position = match.end()
    parts.append(_md(value[position:]))
    return "".join(parts)


def _search_line_md(
    entry: dict[str, Any], value: str, line_index: int, *, contributed: bool = False,
) -> str:
    if line_index == 1 and value.startswith("制作方："):
        prefix, developer = value.split("：", 1)
        rendered = _md(developer)
        return _md(prefix + "：") + (
            f"**{rendered}**" if contributed else rendered
        )
    elif line_index in {2, 3, 4} and "：" in value:
        prefix, title = value.split("：", 1)
        rendered = _highlight_qualifiers_md(title)
        return _md(prefix + "：") + (
            f"**{rendered}**" if contributed else rendered
        )
    return _md(value)


def _md(value: str) -> str:
    return value.replace("|", "\\|")


def _entry_path_key(value: Any) -> str:
    return str(Path(str(value or "")).resolve()).casefold()


def _entry_basename_changed(entry: dict[str, Any]) -> bool:
    """只有真实 source/target basename 不同时才显示预计名称加粗。"""
    if entry.get("status") != "可执行" or not entry.get("target"):
        return False
    return Path(str(entry.get("source") or "")).name != Path(
        str(entry.get("target") or "")
    ).name


def _name_change_flags(
    original_value: str, target_value: str,
) -> tuple[list[bool], list[bool]]:
    """对齐同一语义槽，返回原名删除/替换与目标新增/替换字符。"""
    original = str(original_value or "").strip()
    target = str(target_value or "").strip()
    original_text = str(original_value or "")
    target_text = str(target_value or "")
    original_flags = [False] * len(original_text)
    target_flags = [False] * len(target_text)
    original_offset = len(original_text) - len(original_text.lstrip())
    target_offset = len(target_text) - len(target_text.lstrip())
    matcher = difflib.SequenceMatcher(None, original, target, autojunk=False)
    for operation, a1, a2, b1, b2 in matcher.get_opcodes():
        if operation in {"delete", "replace"}:
            for index in range(original_offset + a1, original_offset + a2):
                original_flags[index] = True
        if operation in {"insert", "replace"}:
            for index in range(target_offset + b1, target_offset + b2):
                target_flags[index] = True
    return original_flags, target_flags


def _original_change_flags(original_value: str, target_value: str) -> list[bool]:
    return _name_change_flags(original_value, target_value)[0]


def _target_change_flags(original_value: str, target_value: str) -> list[bool]:
    return _name_change_flags(original_value, target_value)[1]


def _original_diff_markdown(original_value: str, target_value: str) -> str:
    flags = _original_change_flags(original_value, target_value)
    if not original_value:
        return "&#x20;"
    parts: list[str] = []
    start = 0
    while start < len(original_value):
        changed = flags[start]
        end = start + 1
        while end < len(original_value) and flags[end] == changed:
            end += 1
        rendered = _md(original_value[start:end])
        if changed:
            rendered = '<span style="color:#b91c1c">' + rendered + "</span>"
        parts.append(rendered)
        start = end
    return "".join(parts)


def _original_diff_html(original_value: str, target_value: str) -> str:
    flags = _original_change_flags(original_value, target_value)
    parts: list[str] = []
    start = 0
    while start < len(original_value):
        changed = flags[start]
        end = start + 1
        while end < len(original_value) and flags[end] == changed:
            end += 1
        rendered = html.escape(original_value[start:end])
        if changed:
            rendered = f'<span class="removed-original-field">{rendered}</span>'
        parts.append(rendered)
        start = end
    return "".join(parts)


def _target_diff_markdown(
    original_value: str,
    target_value: str,
    annotations: Sequence[tuple[str, str]] = (),
) -> str:
    flags = _target_change_flags(original_value, target_value)
    if not target_value:
        return "&#x20;"
    classes = ["" for _ in target_value]
    occupied = [False for _ in target_value]
    for needle, class_name in annotations:
        start = target_value.find(needle)
        if start < 0 or any(occupied[start:start + len(needle)]):
            continue
        for index in range(start, start + len(needle)):
            classes[index] = class_name
            occupied[index] = True
    parts: list[str] = []
    start = 0
    while start < len(target_value):
        changed = flags[start]
        class_name = classes[start]
        end = start + 1
        while (
            end < len(target_value)
            and flags[end] == changed
            and classes[end] == class_name
        ):
            end += 1
        rendered = _md(target_value[start:end])
        if class_name == "propagated-name-field":
            rendered = '<span style="color:#1d4ed8">' + rendered + "</span>"
        parts.append(f"**{rendered}**" if changed else rendered)
        start = end
    return "".join(parts)


def _target_diff_html(
    original_value: str,
    target_value: str,
    annotations: Sequence[tuple[str, str]] = (),
) -> str:
    flags = _target_change_flags(original_value, target_value)
    classes = ["" for _ in target_value]
    occupied = [False for _ in target_value]
    for needle, class_name in annotations:
        start = target_value.find(needle)
        if start < 0:
            continue
        end = start + len(needle)
        if any(occupied[start:end]):
            continue
        for index in range(start, end):
            classes[index] = class_name
            occupied[index] = True
    parts: list[str] = []
    start = 0
    while start < len(target_value):
        changed = flags[start]
        class_name = classes[start]
        end = start + 1
        while (
            end < len(target_value)
            and flags[end] == changed
            and classes[end] == class_name
        ):
            end += 1
        rendered = html.escape(target_value[start:end])
        if class_name:
            rendered = f'<span class="{class_name}">{rendered}</span>'
        if changed:
            rendered = f"<strong>{rendered}</strong>"
        parts.append(rendered)
        start = end
    return "".join(parts)


def _semantic_contribution_key(field: str, value: Any) -> str:
    """按目标确定性规范比较语义字段，不把脚本格式化算作来源贡献。"""
    text = str(value or "").strip()
    if field == "developer":
        return developer_identity_key(text)
    if field.endswith("_title"):
        return title_identity_key(text)
    text = re.sub(r"^\s*=\s*", "", text)
    normalized = strip_language_edition_qualifiers(text) or ""
    return str(normalize_title_delimiters(normalized) or "").strip()


def _independent_local_contribution_value(
    entry: dict[str, Any], line_index: int, field: str,
) -> Any:
    """只读取当前条目真实原名槽，合集借用语义不能污染贡献基线。"""
    original_lines = display_name_line_pair(entry)[0]
    if line_index < len(original_lines):
        return original_lines[line_index]
    return None


def _round_contribution_indexes(entry: dict[str, Any], source: str) -> set[int]:
    """采用组各来源的最终同值字段均可显示对改名的佐证贡献。"""
    if source not in set(entry.get("selected_sources") or []):
        return set()
    keys_by_line = {
        1: "developer",
        2: "jp_title",
        3: "zh_title",
        4: "en_title",
    }
    final = entry.get("metadata") or {}
    final_lines = target_name_lines(final)
    return {
        line_index
        for line_index, field in keys_by_line.items()
        if final.get(field) and final_lines[line_index]
        and _semantic_contribution_key(
            field,
            _independent_local_contribution_value(entry, line_index, field),
        )
        != _semantic_contribution_key(field, final.get(field))
    }


def _adopted_round_value_for_comparison(
    entry: dict[str, Any], field: str, displayed: str,
) -> str:
    """应用 Skill 已批准的标题身份转换，不改写远程返回原文。"""
    language_by_field = {
        "jp_title": "jp", "zh_title": "zh", "en_title": "en",
    }
    language = language_by_field.get(field)
    if (
        language
        and entry.get("standalone_dlc_title_language") == language
        and displayed
        and "DLC" not in displayed.upper()
    ):
        return f"{displayed} DLC"
    return displayed


def _round_result_is_adopted(
    entry: dict[str, Any], source: str, round_data: dict[str, Any] | None,
) -> bool:
    """有候选身份时，只接受本来源最终采用的详情页返回字段。"""
    selected = [
        item for item in entry.get("selected_candidates") or []
        if str(item.get("source") or "") == source
    ]
    result = (round_data or {}).get("result") or {}
    if not selected or not isinstance(result, dict):
        return True
    source_id = str(result.get("source_id") or "")
    url = str(result.get("url") or "")
    if not source_id and not url:
        return True
    for candidate in selected:
        identities = [candidate]
        release = candidate.get("release")
        if isinstance(release, dict):
            identities.append(release)
        if any(
            (source_id and source_id == str(item.get("source_id") or ""))
            or (url and url == str(item.get("url") or ""))
            for item in identities
        ):
            return True
    return False


def _round_line_contributed(
    entry: dict[str, Any], source: str, index: int, rendered_value: str,
    round_data: dict[str, Any] | None = None,
) -> bool:
    """仅在当前展示值就是 AI 采用值时标记来源贡献。"""
    if (
        index not in _round_contribution_indexes(entry, source)
        or not _round_result_is_adopted(entry, source, round_data)
    ):
        return False
    field_by_line = {
        1: ("developer", "制作方："),
        2: ("jp_title", "日："),
        3: ("zh_title", "中："),
        4: ("en_title", "英："),
    }
    field, prefix = field_by_line[index]
    displayed = str(rendered_value or "")
    if displayed.startswith(prefix):
        displayed = displayed[len(prefix):]
    if not displayed or displayed == "无":
        return False
    displayed = _adopted_round_value_for_comparison(entry, field, displayed)
    final = (entry.get("metadata") or {}).get(field)
    return (
        _semantic_contribution_key(field, displayed)
        == _semantic_contribution_key(field, final)
    )


def _md_original_cell(
    original: tuple[str, ...], target: tuple[str, ...], *, show_removed: bool = True,
) -> str:
    values = []
    for index, value in enumerate(original):
        rendered = (
            _original_diff_markdown(value, target[index])
            if show_removed else _md(value) if value else "&#x20;"
        )
        values.append(rendered)
    return "<br>".join(values)


def _md_target_cell(
    original: tuple[str, ...], target: tuple[str, ...],
    developer_conflict: bool = False,
    propagated_line_indexes: Sequence[int] = (),
    show_changes: bool = True,
    propagated_basename_fragments: Sequence[str] = (),
) -> str:
    propagated = set(propagated_line_indexes)
    values = []
    for index, value in enumerate(target):
        rendered = (
            _target_diff_markdown(
                original[index], value,
                tuple(
                    (fragment, "propagated-name-field")
                    for fragment in propagated_basename_fragments
                    if fragment and fragment in value
                ),
            )
            if show_changes else _md(value) if value else "&#x20;"
        )
        if value and index in propagated:
            rendered = (
                '<span style="color:#1d4ed8;font-weight:700">'
                + rendered + "</span>"
            )
        values.append(rendered)
    return "<br>".join(values)


def _html_original_stack(
    original: tuple[str, ...], target: tuple[str, ...], *, show_removed: bool = True,
) -> str:
    parts: list[str] = []
    for index, value in enumerate(original):
        rendered = (
            _original_diff_html(value, target[index])
            if show_removed else _inline(value)
        )
        literal_class = " literal-basename" if value else ""
        parts.append(
            f'<div class="semantic-line{literal_class}">{rendered}</div>'
        )
    return '<div class="semantic-stack">' + "".join(parts) + "</div>"


def _html_target_stack(
    original: tuple[str, ...], target: tuple[str, ...],
    developer_conflict: bool = False,
    propagated_line_indexes: Sequence[int] = (),
    conflict_value: str = "",
    propagated_values: Sequence[str] = (),
    show_changes: bool = True,
    propagated_basename_fragments: Sequence[str] = (),
) -> str:
    propagated = set(propagated_line_indexes)
    parts: list[str] = []
    for index, value in enumerate(target):
        annotations: list[tuple[str, str]] = []
        if value and index == 2 and propagated_values:
            annotations.extend(
                (item, "propagated-name-field")
                for item in propagated_values if item
            )
        annotations.extend(
            (fragment, "propagated-name-field")
            for fragment in propagated_basename_fragments
            if fragment and fragment in value
        )
        rendered = (
            _target_diff_html(original[index], value, annotations)
            if show_changes else _highlight_literal_annotations(value, annotations)
        )
        if value and index in propagated:
            rendered = '<span class="propagated-name-field">' + rendered + "</span>"
        literal_class = " literal-basename" if value else ""
        parts.append(
            f'<div class="semantic-line{literal_class}">{rendered}</div>'
        )
    return '<div class="semantic-stack">' + "".join(parts) + "</div>"


def _highlight_literal_annotations(
    value: str, annotations: Sequence[tuple[str, str]],
) -> str:
    """仅为 basename 中真实存在的连续字面片段着色，并保持所有字符不变。"""
    intervals: list[tuple[int, int, str]] = []
    for needle, class_name in annotations:
        start = value.find(needle)
        if start < 0:
            continue
        end = start + len(needle)
        if any(start < current_end and end > current_start
               for current_start, current_end, _ in intervals):
            continue
        intervals.append((start, end, class_name))
    intervals.sort()
    if not intervals:
        return html.escape(value)
    parts: list[str] = []
    cursor = 0
    for start, end, class_name in intervals:
        parts.append(html.escape(value[cursor:start]))
        parts.append(
            f'<span class="{class_name}">{html.escape(value[start:end])}</span>'
        )
        cursor = end
    parts.append(html.escape(value[cursor:]))
    return "".join(parts)


def render_semantic_preview_markdown(manifest: dict[str, Any]) -> str:
    """生成与 HTML 相同语义行模型的可审计 Markdown 表格。"""
    headers = (
        "序号", "原文件夹名称", "预计重命名的名称", "来源搜索结果",
        "①日文轮", "②中文轮", "③英文轮",
    )
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    ordered = order_and_number_preview_entries(manifest.get("entries", []))
    series_by_source = {
        _entry_path_key(item.get("source")): item
        for item in ordered if item.get("kind") == "series"
    }

    for entry in ordered:
        original, target = display_name_line_pair(entry)
        original_cell = _md_original_cell(
            original, target, show_removed=entry.get("status") == "可执行",
        )
        target_cell = _md_target_cell(
            original, target, bool(entry.get("developer_conflict")),
            _display_propagated_indexes(
                entry.get("propagated_preview_line_indexes") or (), target,
            ),
            _entry_basename_changed(entry),
            entry.get("propagated_basename_fragments") or (),
        )
        parent = series_by_source.get(
            _entry_path_key(entry.get("parent_series_source"))
        ) if entry.get("parent_series_source") else None
        if parent:
            parent_original, parent_target = display_name_line_pair(parent)
            original_cell += "<br><br>" + _md_original_cell(
                parent_original,
                parent_target,
                show_removed=parent.get("status") == "可执行",
            )
            target_cell += "<br><br>" + _md_target_cell(
                parent_original, parent_target,
                bool(parent.get("developer_conflict")),
                _display_propagated_indexes(
                    parent.get("propagated_preview_line_indexes") or (),
                    parent_target,
                ),
                _entry_basename_changed(parent),
                parent.get("propagated_basename_fragments") or (),
            )

        original_aux = _auxiliary_preview_lines(entry, True)
        target_aux = _auxiliary_preview_lines(entry, False)
        if original_aux or target_aux:
            original_cell += "<br><br>" + "<br>".join(
                _md(value) for value in original_aux
            )
            target_cell += "<br><br>" + "<br>".join(
                f"**{_md(value)}**" for value in target_aux
            )
        reports = {
            str(item.get("source")): _best_source_report(
                entry, str(item.get("source")), item,
            )
            for item in entry.get("remote_searches") or []
        }
        adopted = {
            source for source, state in
            (entry.get("preview_candidate_dispositions") or {}).items()
            if state == "adopted"
        } or set(entry.get("selected_sources") or [])
        merged = len(adopted) > 1
        for source_index, source in enumerate(SOURCES):
            report = reports.get(source) or {}
            round_by_language = {
                str(item.get("language")): item
                for item in report.get("rounds", [])
            }
            source_values = _source_preview_lines(
                report, source, source in adopted, merged,
                _adopted_source_identity_links(entry, source, report),
                _source_reference_line(entry, source, report),
                _source_nonadoption_reason(entry, source, report),
            )
            row = (
                entry["display_number"] if source_index == 0 else "",
                original_cell if source_index == 0 else "",
                target_cell if source_index == 0 else "",
                "<br>".join(
                    _search_line_md(entry, value, index) if value else "&#x20;"
                    for index, value in enumerate(source_values)
                ),
                *(
                    "<br>".join(
                        _search_line_md(entry, value, index)
                        if not _round_line_contributed(
                            entry, source, index, value,
                            round_by_language.get(language),
                        )
                        else _search_line_md(
                            entry, value, index, contributed=True,
                        ) if value else "&#x20;"
                        for index, value in enumerate(round_preview_lines(
                            round_by_language.get(language)
                        ))
                    )
                    for language in LANGUAGES
                ),
            )
            lines.append("| " + " | ".join(row) + " |")
            if source_index == 0 and entry.get("marker_name"):
                marker_lines = marker_preview_lines(entry)
                marker_row = (
                    "", "", "<br>".join(
                        "**" + _md(value) + "**" if value else "&#x20;"
                        for value in marker_lines
                    ), "", "", "", "",
                )
                lines.append("| " + " | ".join(marker_row) + " |")
    return "\n".join(lines) + "\n"


def render_combined_semantic_preview_html(
    manifests: Sequence[dict[str, Any]],
) -> str:
    """把多个子 manifest 合并为一张由单一外层批准码约束的正式 HTML 表。"""
    return render_semantic_preview_html(
        build_combined_approval_manifest(manifests)
    )


def _preview_scope_html(
    manifest: dict[str, Any], entries: Sequence[dict[str, Any]],
) -> str:
    """Render a deduplicated, hierarchy-checked summary of the preview scope."""
    counted_entries = [
        entry for entry in entries if entry.get("kind") in {"game", "series"}
    ]
    sources: dict[str, str] = {}
    for entry in counted_entries:
        source = str(entry.get("source") or "")
        source_key = _entry_path_key(source)
        if not source_key:
            raise ValueError("preview entry is missing its source path")
        if source_key in sources:
            raise ValueError(
                "preview contains duplicate source paths: "
                f"{sources[source_key]!r} and {source!r}"
            )
        sources[source_key] = source

    series_sources = {
        _entry_path_key(str(entry.get("source") or ""))
        for entry in counted_entries if entry.get("kind") == "series"
    }
    series_count = len(series_sources)
    child_game_count = 0
    independent_game_count = 0
    for entry in counted_entries:
        if entry.get("kind") != "game":
            continue
        parent_source = str(entry.get("parent_series_source") or "")
        if parent_source:
            if (
                manifest.get("approval_kind") == "combined"
                and _entry_path_key(parent_source) not in series_sources
            ):
                raise ValueError(
                    "series child references a parent missing from the preview: "
                    f"{parent_source!r}"
                )
            child_game_count += 1
        else:
            independent_game_count += 1

    leaf_game_count = independent_game_count + child_game_count
    main_entry_count = series_count + leaf_game_count
    if main_entry_count != len(counted_entries):
        raise ValueError("preview summary count does not cover every main entry")

    root = html.escape(str(manifest.get("root") or "未提供"))
    return (
        '<section class="preview-summary" aria-label="本次处理概况">'
        '<div><strong>处理目录：</strong><code>' + root + '</code></div>'
        '<div><strong>数量梳理：</strong>'
        f'独立游戏 {independent_game_count} 个；合集 {series_count} 个'
        f'（合集内子游戏 {child_game_count} 个）</div>'
        '</section>'
    )

def render_semantic_preview_html(manifest: dict[str, Any]) -> str:
    """每个来源占一行；名称使用固定语言槽和动态单标签行。"""
    body: list[str] = []
    ordered = order_and_number_preview_entries(manifest.get("entries", []))
    series_by_source = {
        _entry_path_key(item.get("source")): item
        for item in ordered if item.get("kind") == "series"
    }

    for entry_index, entry in enumerate(ordered):
        original, target = display_name_line_pair(entry)
        parent = series_by_source.get(
            _entry_path_key(entry.get("parent_series_source"))
        ) if entry.get("parent_series_source") else None

        report_by_source = {
            str(item.get("source")): _best_source_report(
                entry, str(item.get("source")), item,
            )
            for item in entry.get("remote_searches") or []
        }
        adopted_sources = {
            source for source, state in
            (entry.get("preview_candidate_dispositions") or {}).items()
            if state == "adopted"
        } or set(entry.get("selected_sources") or [])
        reference_sources = {
            str(item.get("source") or "")
            for item in entry.get("reference_candidates") or []
        }
        source_disposition = (
            entry.get("preview_candidate_dispositions")
            or entry.get("source_disposition") or {}
        )
        merged = len(adopted_sources) > 1
        all_sources_failed = (
            entry.get("kind") == "game"
            and (
                bool(entry.get("unresolved_launcher"))
                or (
                    not bool((entry.get("query_outcome") or {}).get("found"))
                    and not reference_sources
                    and all(
                        any(
                            str(round_data.get("status") or "")
                            not in {"", "missing", "covered"}
                            for round_data in (
                                (report_by_source.get(source) or {}).get("rounds")
                                or []
                            )
                        )
                        for source in SOURCES
                    )
                )
            )
        )
        source_lines = {
            source: _source_preview_lines(
                report_by_source.get(source),
                source,
                source in adopted_sources,
                merged,
                _adopted_source_identity_links(
                    entry, source, report_by_source.get(source),
                ),
                _source_reference_line(
                    entry, source, report_by_source.get(source),
                ),
                _source_nonadoption_reason(
                    entry, source, report_by_source.get(source),
                ),
            )
            for source in SOURCES
        }
        round_lines: dict[tuple[str, str], tuple[str, ...]] = {}
        round_status: dict[tuple[str, str], str] = {}
        for source in SOURCES:
            rounds = {
                str(item.get("language")): item
                for item in (report_by_source.get(source) or {}).get("rounds", [])
            }
            for language in LANGUAGES:
                item = rounds.get(language)
                round_lines[(source, language)] = round_preview_lines(item)
                round_status[(source, language)] = str(
                    (item or {}).get("status") or ""
                )
        own_original_content = _html_original_stack(
            original,
            target,
            show_removed=entry.get("status") == "可执行",
        )
        semantic_target = _semantic_preview_lines(entry, False)
        conflict_value = semantic_target[1]
        propagated_indexes = _display_propagated_indexes(
            entry.get("propagated_preview_line_indexes") or (), target,
        )
        own_target_content = _html_target_stack(
            original, target, bool(entry.get("developer_conflict")),
            propagated_indexes,
            conflict_value,
            tuple(
                target[index]
                for index in propagated_indexes
                if 0 <= index < len(target)
            ),
            _entry_basename_changed(entry),
            entry.get("propagated_basename_fragments") or (),
        )
        original_name_rows = [own_original_content, "", ""]
        target_name_rows = [own_target_content, "", ""]
        original_basenames = [Path(str(entry.get("source") or "")).name, "", ""]
        target_basenames = [
            Path(str(_preview_target_path(entry) or "")).name, "", "",
        ]
        original_name_classes = ["", "", ""]
        target_name_classes = ["", "", ""]
        if parent:
            parent_original, parent_target = display_name_line_pair(parent)
            original_name_rows[1] = _html_original_stack(
                parent_original,
                parent_target,
                show_removed=parent.get("status") == "可执行",
            )
            parent_semantic_target = _semantic_preview_lines(parent, False)
            parent_propagated = _display_propagated_indexes(
                parent.get("propagated_preview_line_indexes") or (),
                parent_target,
            )
            target_name_rows[1] = _html_target_stack(
                parent_original, parent_target,
                bool(parent.get("developer_conflict")),
                parent_propagated,
                parent_semantic_target[1],
                tuple(
                    parent_target[index]
                    for index in parent_propagated
                    if 0 <= index < len(parent_target)
                ),
                _entry_basename_changed(parent),
                parent.get("propagated_basename_fragments") or (),
            )
            original_name_classes[1] = " series-name-cell"
            target_name_classes[1] = " series-name-cell"
            original_basenames[1] = Path(str(parent.get("source") or "")).name
            target_basenames[1] = (
                Path(str(_preview_target_path(parent) or "")).name
            )

        original_aux = _auxiliary_preview_lines(entry, True)
        target_aux = _auxiliary_preview_lines(entry, False)
        if original_aux or target_aux:
            original_name_rows[2] = "".join(
                f'<div>{html.escape(value)}</div>' for value in original_aux
            )
            target_name_rows[2] = "".join(
                f'<div><strong>{html.escape(value)}</strong></div>'
                for value in target_aux
            )
        for source_index, source in enumerate(SOURCES):
            cells: list[str] = []
            target_class = (
                "name target failed"
                if (
                    (
                        entry.get("status") == "可执行"
                        or entry.get("unresolved_launcher")
                    )
                    and not bool(
                        (entry.get("query_outcome") or {}).get("found", True)
                    )
                )
                else "name target"
            )
            if source_index == 0:
                number_rowspan = 4 if entry.get("marker_name") else 3
                cells.append(
                    f'<td class="number" rowspan="{number_rowspan}">'
                    f'{html.escape(str(entry["display_number"]))}</td>'
                )
            cells.append(
                f'<td class="name original{original_name_classes[source_index]}" '
                f'data-name-block="{source_index + 1}" '
                f'data-basename="{html.escape(original_basenames[source_index], quote=True)}">'
                + original_name_rows[source_index] + "</td>"
            )
            cells.append(
                f'<td class="{target_class}{target_name_classes[source_index]}" '
                f'data-name-block="{source_index + 1}" '
                f'data-basename="{html.escape(target_basenames[source_index], quote=True)}">'
                + target_name_rows[source_index] + "</td>"
            )
            source_report = report_by_source.get(source) or {}
            source_has_error = any(
                item.get("error") or item.get("fallback_error")
                for item in source_report.get("rounds", [])
            )
            source_attempted = any(
                str(item.get("status") or "")
                not in {"", "missing", "covered"}
                for item in source_report.get("rounds", [])
            )
            if source in adopted_sources:
                source_class = "source adopted"
            elif source_has_error:
                source_class = "source network-error"
            elif all_sources_failed:
                source_class = "source failed"
            elif (
                source_disposition.get(source) == "rejected"
                and bool(source_report.get("entries"))
                and source not in reference_sources
            ):
                source_class = "source failed"
            elif source_attempted and not source_report.get("entries"):
                source_class = "source no-result"
            else:
                source_class = "source"
            source_stack = "".join(
                '<div class="semantic-line">'
                + _search_line_html(entry, value, index) + "</div>"
                for index, value in enumerate(source_lines[source])
            )
            cells.append(
                f'<td class="{source_class}">'
                f'<div class="semantic-stack">{source_stack}</div></td>'
            )
            source_rounds = {
                str(item.get("language")): item
                for item in source_report.get("rounds", [])
            }
            for language in LANGUAGES:
                values = round_lines[(source, language)]
                round_item = source_rounds.get(language) or {}
                round_stack = "".join(
                    '<div class="semantic-line">'
                    + _search_line_html(
                        entry, value, index,
                        contributed=_round_line_contributed(
                            entry, source, index, value, round_item,
                        ),
                    ) + "</div>"
                    for index, value in enumerate(values)
                )
                if round_status[(source, language)] == "success":
                    class_name = "round success"
                elif round_item.get("error") or round_item.get("fallback_error"):
                    class_name = "round network-error"
                elif round_status[(source, language)] == "failed":
                    class_name = "round no-result"
                else:
                    class_name = "round"
                cells.append(
                    f'<td class="{class_name}">'
                    f'<div class="semantic-stack">{round_stack}</div></td>'
                )
            progress_state = str(entry.get("progress_state") or "")
            row_class = "entry-source-row"
            if progress_state in {"current", "pending"}:
                row_class += f" progress-{progress_state}"
            body.append(
                f'<tr class="{row_class}" data-preview-entry="{entry_index}">'
                + "".join(cells)
                + "</tr>"
            )
            if source_index == 0 and entry.get("marker_name"):
                body.append(_marker_preview_row_html(entry, entry_index))
    shortcut_rows: list[str] = []
    for destination, shortcuts in group_source_shortcuts(manifest):
        names = "".join(
            '<div class="shortcut-line">'
            + html.escape(str(shortcut.get("name") or ""))
            + "</div>"
            for shortcut in shortcuts
        )
        links = "".join(
            '<div class="shortcut-line">'
            f'<a href="{html.escape(str(shortcut.get("url") or ""), quote=True)}">'
            f'{html.escape(str(shortcut.get("url") or ""))}</a></div>'
            for shortcut in shortcuts
        )
        shortcut_rows.append(
            "<tr>"
            f"<td>{html.escape(destination)}</td>"
            f"<td>{names}</td>"
            f"<td>{links}</td>"
            "</tr>"
        )
    shortcut_section = ""
    if shortcut_rows:
        shortcut_section = (
            "<section><h2>批准后创建的来源快捷方式</h2>"
            "<div class=\"table-scroll\"><table><thead><tr>"
            "<th>游戏目录</th><th>快捷方式文件</th><th>来源链接</th>"
            "</tr></thead><tbody>"
            + "\n".join(shortcut_rows)
            + "</tbody></table></div></section>"
        )
    approval = html.escape(str(manifest.get("approval_hash") or ""))
    live = manifest.get("live_preview") or {}
    if live:
        page_title = "游戏目录重命名预览（处理中）"
        status_block = (
            '<div class="live-warning">处理中：此页面不是正式批准预览，'
            '不能用于执行重命名。</div>'
            '<div class="live-progress">进度：<strong>'
            + html.escape(str(live.get("completed") or 0))
            + "/"
            + html.escape(str(live.get("total") or 0))
            + "</strong>　阶段："
            + html.escape(str(live.get("stage") or ""))
            + "　当前游戏："
            + html.escape(str(live.get("current") or ""))
            + "　更新时间："
            + html.escape(str(live.get("updated_at") or ""))
            + "</div>"
        )
    elif manifest.get("approval_kind") == "combined":
        page_title = "游戏目录重命名完整预览"
        status_block = "<p>汇总批准码：<code>" + approval + "</code></p>"
    else:
        page_title = "游戏目录重命名完整预览"
        status_block = "<p>批准码：<code>" + approval + "</code></p>"
    summary_block = _preview_scope_html(manifest, ordered)
    return """<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>""" + page_title + """</title>
<style>
:root { color-scheme: light; }
* { box-sizing: border-box; }
html, body { margin: 0; width: 100%; min-height: 100%; overflow-x: hidden; }
body { background: #fff; color: #172033; font: 13px/1.45 system-ui, "Microsoft YaHei UI", sans-serif; padding: 12px; }
h1, h2, p { margin: 0 0 10px; }
section { margin-top: 20px; }
.preview-summary { margin: 0 0 10px; padding: 8px 10px; border: 1px solid #cbd5e1; border-radius: 6px; background: #f8fafc; }
.preview-summary > div + div { margin-top: 4px; }
.preview-summary code { white-space: normal; overflow-wrap: anywhere; }
.table-scroll { width: 100%; overflow: visible; }
table { width: 100%; border-collapse: collapse; table-layout: fixed; }
th, td { border: 1px solid #cbd5e1; padding: 5px 7px; text-align: left; vertical-align: top; white-space: normal; overflow-wrap: anywhere; }
thead th { position: sticky; top: 0; z-index: 3; background: #eaf0f7; }
.number { background: #f8fafc; font-weight: 700; }
.semantic-stack { display: flex; min-width: 0; flex-direction: column; }
.semantic-line { min-width: 0; min-height: 1.45em; }
.literal-basename { white-space: pre-wrap; }
.removed-original-field { color: #b91c1c; font-weight: 700; }
.propagated-name-field { color: #1d4ed8; font-weight: 700; }
.removed-search-qualifier { color: #b91c1c; font-weight: 700; }
.marker-preview { overflow-wrap: anywhere; }
.source.adopted, .round.success { background: #d9f7df; }
.source.failed, .source.network-error, .round.network-error { background: #fef3c7; color: #92400e; font-weight: 700; }
.source.no-result, .round.no-result { background: #f1f5f9; color: #475569; }
.name.original.series-name-cell, .name.target.series-name-cell { background: #e5e7eb; }

.progress-current td { background: #fff7d6; }
.progress-pending td { color: #64748b; background: #f8fafc; }
.live-warning { margin-bottom: 10px; padding: 10px; border: 1px solid #cbd5e1; background: #f8fafc; color: #334155; font-weight: 700; }
.live-progress { margin-bottom: 10px; padding: 8px 10px; border: 1px solid #cbd5e1; background: #fff; }
a { color: #0757a8; }
</style>
<script>
(() => {
  let pendingFrame = 0;
  function syncSemanticLines() {
    document.querySelectorAll(".entry-source-row").forEach((row) => {
      const stacks = Array.from(
        row.querySelectorAll(":scope > td.source > .semantic-stack, :scope > td.round > .semantic-stack, :scope > td.name > .semantic-stack")
      );
      const groups = stacks.map((stack) => Array.from(stack.children));
      groups.flat().forEach((line) => { line.style.minHeight = ""; });
      const lineCount = Math.max(0, ...groups.map((lines) => lines.length));
      for (let index = 0; index < lineCount; index += 1) {
        const height = Math.max(
          0,
          ...groups.map((lines) =>
            lines[index] ? lines[index].getBoundingClientRect().height : 0
          )
        );
        groups.forEach((lines) => {
          if (lines[index]) {
            lines[index].style.minHeight = `${Math.ceil(height)}px`;
          }
        });
      }
    });
  }
  function scheduleSemanticLineSync() {
    cancelAnimationFrame(pendingFrame);
    pendingFrame = requestAnimationFrame(() => {
      syncSemanticLines();
      pendingFrame = requestAnimationFrame(syncSemanticLines);
    });
  }
  window.addEventListener("load", () => {
    scheduleSemanticLineSync();
    const table = document.querySelector(".preview-table");
    if (window.ResizeObserver && table) {
      let observedWidth = Math.ceil(table.getBoundingClientRect().width);
      new ResizeObserver((entries) => {
        const nextWidth = Math.ceil(entries[0].contentRect.width);
        if (nextWidth !== observedWidth) {
          observedWidth = nextWidth;
          scheduleSemanticLineSync();
        }
      }).observe(table);
    }
  });
  window.addEventListener("resize", scheduleSemanticLineSync);
  if (document.fonts) document.fonts.ready.then(scheduleSemanticLineSync);
  window.syncSemanticLines = syncSemanticLines;
})();
</script></head><body><main>
<h1>""" + page_title + """</h1>
""" + summary_block + status_block + """
<div class="table-scroll"><table class="preview-table">
<colgroup><col style="width:4%"><col style="width:16%"><col style="width:16%"><col style="width:16%"><col style="width:16%"><col style="width:16%"><col style="width:16%"></colgroup>
<thead><tr>
<th>序号</th><th>原文件夹名称</th><th>预计重命名的名称</th>
<th>来源搜索结果</th><th>①日文轮</th><th>②中文轮</th><th>③英文轮</th>
</tr></thead><tbody>
""" + "\n".join(body) + "\n</tbody></table></div>" + shortcut_section + "</main></body></html>\n"
