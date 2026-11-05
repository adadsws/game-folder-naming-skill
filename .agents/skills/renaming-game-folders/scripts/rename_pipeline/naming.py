"""名称格式的单一事实来源：原文证据与最终目标严格分离。"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any, Mapping, Sequence

from .text_normalization import (
    strip_language_edition_qualifiers,
    title_identity_key,
)
from .engine_labels import normalize_engine_label


WINDOWS_COMPONENT_REPLACEMENTS = str.maketrans(
    {"<": "＜", ">": "＞", ":": "：", '"': "＂", "/": "／", "\\": "＼",
     "|": "｜", "?": "？", "*": "＊"}
)


def sanitize_windows_component_text(value: str) -> str:
    """对目标 basename 与预览语义应用完全相同的 Windows 字符规范化。"""
    return unicodedata.normalize("NFC", value).translate(
        WINDOWS_COMPONENT_REPLACEMENTS
    )


def normalize_title_delimiters(value: str | None) -> str | None:
    """只规范进入最终名称的副标题分隔符。"""
    if not value:
        return value
    normalized = re.sub(
        r"\s+[（(]([^()（）]+)[)）]\s*$",
        lambda match: f"~{match.group(1).strip()}~",
        value.strip(),
    )
    normalized = re.sub(r"[～〜]", "~", normalized)
    normalized = re.sub(r"\s+(?:-|—)\s+", "~", normalized)
    normalized = re.sub(r"[：:]", "~", normalized)
    return re.sub(r"\s*~\s*", "~", normalized)


def _clean_prefix(value: Any, prefix: str) -> str:
    text = str(value or "").strip()
    while text.startswith(prefix):
        text = text[len(prefix):].lstrip()
    return text


def _literal(value: Any) -> str:
    """保留 Skill 已确认文本的原字形，仅完成字符串化。"""
    return str(value or "")


def positive_tag_values(value: Any) -> tuple[str, ...]:
    """单值字段中的独立正面语义在目标名中各占一个百分号标签。"""
    return tuple(_clean_prefix(part, "%") for part in _literal(value).split()
                 if _clean_prefix(part, "%"))


_DLC_RANGE_RE = re.compile(
    r"(?i)(?:\d+|[IVX]+)\s*(?:-|~|～|〜)\s*(?:\d+|[IVX]+)"
)


def normalize_dlc_range(value: Any) -> str:
    """校验并统一明确 DLC 枚举范围的分隔符，不推导缺失端点。"""
    text = str(value or "").strip()
    if not text:
        return ""
    if _DLC_RANGE_RE.fullmatch(text) is None:
        raise ValueError("dlc_range 格式无效")
    return re.sub(r"\s*(?:-|~|～|〜)\s*", "~", text)


_ADDON_RANGE_TAG_RE = re.compile(
    r"(?i)\+[A-Za-z]{1,8}(?:\d+|[IVX]+)\s*(?:-|~|～|〜)\s*(?:\d+|[IVX]+)"
)


def normalize_version_tags(values: Any) -> tuple[str, ...]:
    """校验 Skill 采用的本地版本附加标签。

    普通版本标签保留原字形；`+AP01-05` 这类带范围的附加包
    仅统一范围分隔符，不推导缺失内容。
    """
    if values in (None, ""):
        return ()
    if not isinstance(values, (list, tuple)):
        raise ValueError("version_tags 必须为数组")
    normalized: list[str] = []
    for raw in values:
        text = str(raw or "").strip().lstrip("$")
        if not text or any(char in text for char in "[]"):
            raise ValueError("version_tags 包含无效标签")
        if text.startswith("+"):
            if _ADDON_RANGE_TAG_RE.fullmatch(text) is None:
                raise ValueError("version_tags 附加包范围格式无效")
            text = re.sub(r"\s*(?:-|~|～|〜)\s*", "~", text)
        if text not in normalized:
            normalized.append(text)
    return tuple(normalized)


def target_name_lines(metadata: Mapping[str, Any]) -> tuple[str, ...]:
    """生成最终名称的七个语义字段，并保留全部字段的原始字形。"""
    raw_engine = _literal(metadata.get("engine")).rstrip("/")
    engine = normalize_engine_label(raw_engine) if raw_engine else ""
    developer = _literal(metadata.get("developer")).strip("[]")
    version_tags = normalize_version_tags(metadata.get("version_tags"))
    titles: list[str] = []
    title_seen = False
    seen_title_keys: set[str] = set()
    for key in ("jp_title", "zh_title", "en_title"):
        title = strip_language_edition_qualifiers(
            _literal(metadata.get(key))
        ) or ""
        for version_tag in version_tags:
            without_edition = re.sub(
                rf"\s*{re.escape(version_tag)}\s*$", "", title,
                flags=re.IGNORECASE,
            ).strip()
            if without_edition:
                title = without_edition
        title = normalize_title_delimiters(title) or ""
        identity = title_identity_key(title)
        if identity and identity in seen_title_keys:
            title = ""
        elif title:
            seen_title_keys.add(identity)
            title = ("=" if title_seen else "") + title.lstrip("=")
            title_seen = True
        titles.append(title)

    version = _clean_prefix(_literal(metadata.get("version")), "v")
    dlc_version = _clean_prefix(
        _literal(metadata.get("dlc_version")), "v"
    )
    dlc_range = normalize_dlc_range(metadata.get("dlc_range"))
    if dlc_version and dlc_range:
        raise ValueError("dlc_version 与 dlc_range 不能同时存在")
    version_line = f"v{version}" if version else ""
    # Skill 把独立 DLC 的身份写进标题；只有本地版本附带 DLC 时，
    # 才在末尾元数据组中保留 +DLC，避免标题与标签重复。
    dlc_in_title = any(
        re.search(r"(?i)(?<![A-Za-z])DLC(?![A-Za-z])", title.lstrip("="))
        for title in titles if title
    )
    if dlc_version and not dlc_in_title:
        version_line = " ".join(value for value in (
            f"本体v{version}" if version else "",
            f"+DLCv{dlc_version}",
        ) if value)
    elif dlc_range:
        version_line = " ".join(value for value in (
            f"v{version}" if version else "",
            f"+DLC{dlc_range}",
        ) if value)
    version_line = " ".join(value for value in (version_line, *version_tags) if value)

    tags: list[str] = []
    group = _clean_prefix(_literal(metadata.get("translation_group")), "%")
    positive = positive_tag_values(metadata.get("positive_tag"))
    normalized_negative = [
        _clean_prefix(_literal(value), "#")
        for value in metadata.get("negative_tags") or ()
    ]
    translation_statuses = {"eng", "生肉", "机翻", "ai汉化"}
    if group and not (
        group in {"未知汉化组", "汉化组未知", "Unknown_CH"}
        and any(value.casefold() in translation_statuses for value in normalized_negative)
    ):
        tags.append("%" + group)
    tags.extend("%" + value for value in positive)
    for tag in normalized_negative:
        if tag:
            tags.append("#" + tag)
    metadata_suffix = " ".join(value for value in (
        engine,
        version_line,
        (
            "+DLC"
            if metadata.get("dlc") and not dlc_in_title
            and not dlc_version and not dlc_range
            else ""
        ),
        *tags,
    ) if value)
    lines = (
        "",
        f"[{developer}]" if developer else "",
        *titles,
        f"[{metadata_suffix}]" if metadata_suffix else "",
        "",
    )
    return tuple(sanitize_windows_component_text(value) for value in lines)


def target_basename_fields(metadata: Mapping[str, Any]) -> tuple[str, ...]:
    """返回 developer、语言标题与方括号元数据三段 basename。"""
    return tuple(value for value in target_name_lines(metadata) if value)


_SERIES_RANGE_RE = re.compile(
    r"(?i)(?:\d+|[IVX]+)\s*[~～〜]\s*(?:\d+|[IVX]+)\s*$"
)
_SERIES_COUNT_LABEL_RE = re.compile(r"(?i)\d+部合集\s*$")


def series_target_name_lines(metadata: Mapping[str, Any]) -> tuple[str, ...]:
    """合集目标含共同标签，但不含引擎、版本、DLC 或汉化署名。"""
    normalized = dict(metadata)
    for key, empty_value in (
        ("engine", None),
        ("version", None),
        ("dlc", False),
        ("dlc_version", None),
        ("dlc_range", None),
        ("version_tags", ()),
        ("translation_group", None),
    ):
        normalized[key] = empty_value
    explicit_range = str(metadata.get("series_range") or "").strip()
    explicit_count = metadata.get("series_count")
    if explicit_count not in (None, ""):
        if isinstance(explicit_count, bool) or not isinstance(explicit_count, int):
            raise ValueError("series_count 必须是正整数")
        if explicit_count <= 0:
            raise ValueError("series_count 必须是正整数")
        if explicit_range:
            raise ValueError("series_count 与 series_range 不得同时填写")
    if explicit_range:
        if _SERIES_RANGE_RE.fullmatch(explicit_range) is None:
            raise ValueError("series_range 格式无效")
        explicit_range = re.sub(r"[～〜]", "~", explicit_range).replace(" ", "")
    for key in ("jp_title", "zh_title", "en_title"):
        title = str(normalized.get(key) or "").strip()
        if not title:
            continue
        match = _SERIES_RANGE_RE.search(title)
        if match:
            candidate = re.sub(r"[～〜]", "~", match.group(0)).replace(" ", "")
            if explicit_range and candidate.casefold() != explicit_range.casefold():
                raise ValueError("合集多语言标题包含冲突的显式范围")
            explicit_range = candidate
            title = title[:match.start()].rstrip()
        # 旧数据可能已把容器身份混在共同标题中；目标只保留一处规范“合集”。
        title = _SERIES_COUNT_LABEL_RE.sub("", title).rstrip()
        title = re.sub(r"\s*(?:合集|系列)\s*$", "", title).rstrip()
        normalized[key] = title or None

    lines = list(target_name_lines(normalized))
    collection_label = (
        f"{explicit_count}部合集" if explicit_count not in (None, "")
        else "合集" + explicit_range
    )
    title_indexes = [index for index in (2, 3, 4) if lines[index]]
    if title_indexes:
        # 与真实 basename 顺序一致，同时由预览渲染器拆成独立的合集范围行。
        lines[title_indexes[-1]] = lines[title_indexes[-1]] + " " + collection_label
    elif explicit_count not in (None, ""):
        # 纯计数合集允许没有作品共同标题；计数标签本身就是容器身份。
        lines[2] = collection_label
    return tuple(lines)


def series_target_basename_fields(metadata: Mapping[str, Any]) -> tuple[str, ...]:
    """返回带规范合集身份片段的真实目标 basename 字段。"""
    return tuple(value for value in series_target_name_lines(metadata) if value)


def _literal_tag_patterns(
    value: str, *, translation_group: bool = False,
) -> tuple[str, ...]:
    """把已确认标签映射为可能真实出现在 basename 中的字面形式。"""
    normalized = value.lstrip("%#")
    aliases = {
        "官中": (r"官中|官方中文",),
        "步兵": (r"步兵|uncen(?:sored)?|无码|無修",),
        "官中步兵": (
            r"官中步兵|官方中文.{0,8}(?:步兵|uncen(?:sored)?|无码|無修)",
            r"官中|官方中文",
            r"步兵|uncen(?:sored)?|无码|無修",
        ),
        "去码": (r"去码|去碼|去马赛克|去馬賽克|demosaic",),
        "ai汉化": (r"AI\s*(?:汉化|漢化|翻译|翻譯)",),
        "机翻": (r"机翻|機翻|Machine[_ ]?CH",),
        "ENG": (r"ENG",),
        "生肉": (r"生肉",),
        "全年龄": (r"全年[龄齢]",),
        "本地全年龄": (r"(?:本地)?全年[龄齢]",),
        "VNDB全年龄": (r"(?:VNDB)?全年[龄齢]",),
        "DLsite全年龄": (r"(?:DLsite)?全年[龄齢]",),
        "Steam全年龄": (r"(?:Steam)?全年[龄齢]",),
    }
    patterns = aliases.get(normalized, (re.escape(normalized),))
    if translation_group and not re.search(r"(?:汉化|漢化|翻译|翻譯)(?:组|組)?$", normalized):
        return tuple(
            rf"(?:{pattern})(?:汉化|漢化|翻译|翻譯)(?:组|組)?"
            for pattern in patterns
        ) + patterns
    return patterns


def literal_original_name_lines(
    source: str | Path, metadata: Mapping[str, Any] | None = None,
) -> tuple[str, ...]:
    """无损分栏 basename 中真实存在的版本、DLC 与标签字面片段。"""
    raw_name = Path(str(source)).name
    if not metadata:
        return ("", "", raw_name, "", "", "", "")

    candidates: list[tuple[int, int, str]] = []
    if metadata.get("version"):
        for version_match in re.finditer(
            r"(?i)(?:本体\s*)?(?:v(?:er(?:sion)?)?\.?)\s*"
            r"[0-9][0-9A-Za-z._-]*"
            r"(?:\s*\+?\s*DLC)?",
            raw_name,
        ):
            candidates.append((
                version_match.start(), version_match.end(), "version",
            ))
    if metadata.get("dlc"):
        for match in re.finditer(
            r"(?i)(?<![0-9A-Za-z])\+?DLC(?:\s*(?:\d+|[IVX]+)"
            r"\s*(?:-|~|～|〜)\s*(?:\d+|[IVX]+))?(?![A-Za-z])",
            raw_name,
        ):
            candidates.append((match.start(), match.end(), "version"))
    for version_tag in normalize_version_tags(metadata.get("version_tags")):
        pattern = re.escape(version_tag)
        if version_tag.startswith("+"):
            pattern = re.escape(version_tag).replace(r"\~", r"\s*(?:-|~|～|〜)\s*")
        else:
            pattern = rf"\$?{pattern}"
        for match in re.finditer(pattern, raw_name, re.I):
            candidates.append((match.start(), match.end(), "version"))

    tag_values = (
        (metadata.get("translation_group"), True),
        *((value, False) for value in positive_tag_values(metadata.get("positive_tag"))),
        *((value, False) for value in (metadata.get("negative_tags") or ())),
    )
    for value, is_translation_group in tag_values:
        text = str(value or "").strip()
        if not text:
            continue
        for pattern in _literal_tag_patterns(
            text, translation_group=is_translation_group,
        ):
            for match in re.finditer(rf"[%#]?(?:{pattern})", raw_name, re.I):
                candidates.append((match.start(), match.end(), "tag"))

    selected: list[tuple[int, int, str]] = []
    for candidate in sorted(candidates, key=lambda item: (item[0], -(item[1] - item[0]))):
        start, end, _ = candidate
        if any(start < other_end and end > other_start for other_start, other_end, _ in selected):
            continue
        selected.append(candidate)
    selected.sort(key=lambda item: item[0])
    if not selected:
        return ("", "", raw_name, "", "", "", "")

    residue = list(raw_name)
    for start, end, _ in selected:
        residue[start:end] = " " * (end - start)
    title_or_residue = re.sub(r"\s+", " ", "".join(residue)).strip()
    version_line = " ".join(
        raw_name[start:end] for start, end, kind in selected if kind == "version"
    )
    tags_line = " ".join(
        raw_name[start:end] for start, end, kind in selected if kind == "tag"
    )
    return ("", "", title_or_residue, "", "", version_line, tags_line)


def original_name_lines(
    metadata: Mapping[str, Any], source: str | Path,
    explicit_lines: Sequence[Any] | None = None,
) -> tuple[str, ...]:
    """生成原文件夹七行证据；显式原文优先且绝不简体化。"""
    if explicit_lines is not None:
        values = tuple(str(value or "") for value in explicit_lines)
        if len(values) != 7:
            raise ValueError("original_preview_lines 必须恰好包含 7 行")
        return values
    raw_name = Path(str(source)).name
    leading_developer = re.match(r"^\s*(\[[^\]]+\])", raw_name)
    developer_line = (
        leading_developer.group(1) if leading_developer else ""
    )
    title_lines = [
        str(metadata.get("jp_title") or ""),
        str(metadata.get("zh_title") or ""),
        str(metadata.get("en_title") or ""),
    ]
    if not any(title_lines):
        return ("", "", raw_name, "", "", "", "")
    version_line = ""
    version_match = re.search(
        r"(?i)(?:本体\s*)?(?:v(?:er(?:sion)?)?\.?)\s*"
        r"[0-9][0-9A-Za-z._-]*(?:\s*\+?\s*DLC)?",
        raw_name,
    )
    if version_match:
        version_line = version_match.group(0)
    elif metadata.get("dlc") and (dlc_match := re.search(
        r"(?i)\+?\s*DLC(?:\s*(?:\d+|[IVX]+)\s*(?:-|~|～|〜)"
        r"\s*(?:\d+|[IVX]+))?",
        raw_name,
    )):
        version_line = dlc_match.group(0)
    version_tag_matches: list[tuple[int, str]] = []
    for version_tag in normalize_version_tags(metadata.get("version_tags")):
        pattern = re.escape(version_tag)
        if version_tag.startswith("+"):
            pattern = re.escape(version_tag).replace(r"\~", r"\s*(?:-|~|～|〜)\s*")
        else:
            pattern = rf"\$?{pattern}"
        if match := re.search(pattern, raw_name, re.I):
            version_tag_matches.append((match.start(), match.group(0)))
    if version_tag_matches:
        version_line = " ".join(value for _, value in sorted(
            ([(-1, version_line)] if version_line else []) + version_tag_matches
        ))

    tag_matches: list[tuple[int, int, str]] = []
    positive_aliases = {
        "官中": r"官中|官方中文",
        "步兵": r"步兵|uncen(?:sored)?|无码|無修",
        "官中步兵": r"官中步兵|官方中文.{0,8}(?:步兵|uncen(?:sored)?|无码|無修)",
        "去码": r"去码|去碼|去马赛克|去馬賽克|demosaic",
    }
    values = (
        (metadata.get("translation_group"), True),
        *((value, False) for value in positive_tag_values(metadata.get("positive_tag"))),
        *((value, False) for value in (metadata.get("negative_tags") or ())),
    )
    for value, is_translation_group in values:
        text = str(value or "").lstrip("%#")
        if not text:
            continue
        patterns = (
            (positive_aliases.get(text, re.escape(text)),)
            if not is_translation_group else
            _literal_tag_patterns(text, translation_group=True)
        )
        for pattern in patterns:
            match = re.search(rf"[%#]?(?:{pattern})", raw_name, re.I)
            if match:
                tag_matches.append((match.start(), match.end(), match.group(0)))
                break
    tags_line = " ".join(value for _, _, value in sorted(tag_matches))
    return (
        "",
        developer_line,
        *title_lines,
        version_line,
        tags_line,
    )
