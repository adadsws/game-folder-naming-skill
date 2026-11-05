"""持久化成功命名游戏：按可信来源 ID 合并、稳定排序并原子写 CSV。"""

from __future__ import annotations

import csv
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .naming import target_basename_fields
from .text_normalization import strip_language_edition_qualifiers


CATALOG_FIELDS = (
    "canonical_id",
    "VNDB_ID", "DLSITE_ID", "STEAM_ID",
    "VNDB_URL", "DLSITE_URL", "STEAM_URL",
    "developer", "engine", "jp_title", "zh_title", "en_title",
    "normalized_name", "version", "dlc", "translation_group",
    "positive_tag", "negative_tags", "game_hash",
    "first_seen", "last_seen",
)
SOURCE_COLUMNS = {
    "VNDB": ("VNDB_ID", "VNDB_URL"),
    "DLsite": ("DLSITE_ID", "DLSITE_URL"),
    "Steam": ("STEAM_ID", "STEAM_URL"),
}


@dataclass(frozen=True)
class CatalogUpdateStats:
    added: int = 0
    merged: int = 0
    unchanged: int = 0
    conflicts: int = 0
    conflict_details: tuple[str, ...] = ()


def append_catalog_conflicts_to_decisions(
    path: str | Path, stats: CatalogUpdateStats,
) -> None:
    """把保留旧值的冲突逐项追加到本轮待判断清单。"""
    if not stats.conflict_details:
        return
    decisions_path = Path(path)
    current = decisions_path.read_text(encoding="utf-8")
    section = ["", "## 成功游戏总表冲突", ""]
    section.extend(f"- {detail}" for detail in stats.conflict_details)
    decisions_path.write_text(
        current.rstrip() + "\n" + "\n".join(section) + "\n",
        encoding="utf-8",
    )


def _tokens(value: str) -> set[str]:
    return {item.strip() for item in value.split(";") if item.strip()}


def _joined(values: Iterable[str]) -> str:
    return ";".join(sorted(set(values), key=_natural_id_key))


def _natural_id_key(value: str) -> tuple[str, int, str]:
    match = re.match(r"(?i)^([^0-9]*)([0-9]+)(.*)$", value.strip())
    if not match:
        return (value.casefold(), -1, "")
    return (match.group(1).casefold(), int(match.group(2)), match.group(3).casefold())


def _canonical_id(row: Mapping[str, str]) -> str:
    for column in ("VNDB_ID", "DLSITE_ID", "STEAM_ID"):
        values = _tokens(str(row.get(column) or ""))
        if values:
            return min(values, key=_natural_id_key)
    return ""


def _row_sort_key(row: Mapping[str, str]) -> tuple[int, tuple[str, int, str]]:
    for rank, column in enumerate(("VNDB_ID", "DLSITE_ID", "STEAM_ID")):
        values = _tokens(str(row.get(column) or ""))
        if values:
            return rank, min((_natural_id_key(value) for value in values))
    return 99, ("", -1, "")


def _catalog_row(entry: Mapping[str, Any], observed_at: str) -> dict[str, str] | None:
    if (
        entry.get("kind") != "game"
        or entry.get("status") != "可执行"
        or not bool((entry.get("query_outcome") or {}).get("found"))
        or not entry.get("target")
    ):
        return None
    row = {field: "" for field in CATALOG_FIELDS}
    for candidate in entry.get("selected_candidates") or ():
        source = str(candidate.get("source") or "")
        columns = SOURCE_COLUMNS.get(source)
        if not columns:
            continue
        id_column, url_column = columns
        source_id = str(candidate.get("source_id") or "").strip()
        url = str(candidate.get("url") or "").strip()
        if source_id:
            row[id_column] = _joined([*_tokens(row[id_column]), source_id])
        if url:
            row[url_column] = _joined([*_tokens(row[url_column]), url])
    row["canonical_id"] = _canonical_id(row)
    if not row["canonical_id"]:
        return None
    metadata = entry.get("metadata") or {}
    for field in (
        "developer", "engine", "jp_title", "zh_title", "en_title",
        "version", "translation_group", "positive_tag",
    ):
        row[field] = str(metadata.get(field) or "")
    row["dlc"] = "1" if metadata.get("dlc") else "0"
    row["negative_tags"] = _joined(
        str(value) for value in metadata.get("negative_tags") or ()
    )
    row["normalized_name"] = Path(str(entry["target"])).name
    row["game_hash"] = str(entry.get("source_fingerprint") or "")
    row["first_seen"] = observed_at
    row["last_seen"] = observed_at
    return row


def _load_catalog(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != CATALOG_FIELDS:
            raise ValueError("成功游戏 CSV 列结构不匹配")
        rows = [
            {field: str(row.get(field) or "") for field in CATALOG_FIELDS}
            for row in reader
        ]
    for row in rows:
        if row.get("engine") != "未知":
            continue
        row["engine"] = "引擎未知"
        row["normalized_name"] = re.sub(
            r"\[未知(?=[\s\]])", "[引擎未知",
            row.get("normalized_name", ""), count=1,
        )
    return rows


def _ids_overlap(left: Mapping[str, str], right: Mapping[str, str]) -> bool:
    return any(
        bool(_tokens(str(left.get(column) or "")) & _tokens(str(right.get(column) or "")))
        for column in ("VNDB_ID", "DLSITE_ID", "STEAM_ID")
    )


def _allows_unknown_group_cleanup(
    column: str,
    old: str,
    new: str,
    incoming: Mapping[str, str],
) -> bool:
    """允许明确翻译类标签取代新旧未知汉化组占位。"""
    negative_tags = {
        value.casefold() for value in _tokens(
            str(incoming.get("negative_tags") or "")
        )
    } & {"eng", "生肉", "机翻", "ai汉化"}
    if not negative_tags:
        return False
    if column == "translation_group":
        return old in {"未知汉化组", "汉化组未知", "Unknown_CH"} and not new
    if column != "normalized_name" or not new:
        return False
    cleaned_old = re.sub(
        r"(?<!\S)%(?:未知汉化组|汉化组未知|Unknown_CH)(?!\S)", "", old
    )
    cleaned_new = new
    for tag in negative_tags:
        cleaned_old = re.sub(
            rf"(?<!\S)#{re.escape(tag)}(?!\S)", "", cleaned_old,
            flags=re.I,
        )
        cleaned_new = re.sub(
            rf"(?<!\S)#{re.escape(tag)}(?!\S)", "", cleaned_new,
            flags=re.I,
        )
    normalize = lambda value: re.sub(
        r"\[\s*]", "", re.sub(r"\s+", " ", value)
    ).strip()
    return normalize(cleaned_old) == normalize(cleaned_new)


def _allows_unknown_engine_label_upgrade(
    column: str, old: str, new: str,
) -> bool:
    """允许旧引擎占位升级为含义明确的新字面值。"""
    return column == "engine" and old == "未知" and new == "引擎未知"


def _allows_obsolete_negative_placeholder_cleanup(
    column: str, old: str, new: str,
) -> bool:
    """允许删除旧规则制造的泛化负面标签，但不丢弃其他具体标签。"""
    if column != "negative_tags":
        return False
    old_tags = set(_tokens(old))
    new_tags = set(_tokens(new))
    return "负面标签" in old_tags and (
        old_tags - {"负面标签"}
    ).issubset(new_tags)


def _allows_vndb_id_version_cleanup(
    column: str, old: str, new: str, incoming: Mapping[str, str],
) -> bool:
    """修复旧流程把 VNDB 的 v数字身份误存为本地版本的目录册行。"""
    if column != "version" or not old or old == new:
        return False
    return any(
        source_id.casefold() == f"v{old}".casefold()
        for source_id in _tokens(str(incoming.get("VNDB_ID") or ""))
    )


def _allows_stale_negative_metadata_cleanup(
    column: str,
    old: str,
    new: str,
    incoming: Mapping[str, str],
    merged: Mapping[str, str],
) -> bool:
    """目标名已按当前清单重绘时，清除与名称不一致的旧负面标签元数据。"""
    if (
        column != "negative_tags"
        or not old
        or old == new
        or merged.get("normalized_name") != incoming.get("normalized_name")
    ):
        return False
    rendered_tags = {
        value.casefold()
        for value in re.findall(
            r"(?<!\S)#([^\s\]]+)",
            str(merged.get("normalized_name") or ""),
        )
    }
    return any(
        tag.casefold() not in rendered_tags for tag in _tokens(old)
    )


def _allows_translation_credit_suffix_preservation(
    column: str, old: str, new: str,
) -> bool:
    """允许把旧目录册中被截断的翻译署名升级为完整字面值。"""
    suffix = r"(?:汉化|漢化|翻译|翻譯)(?:组|組)?"
    if column == "translation_group":
        return bool(old and re.fullmatch(re.escape(old) + suffix, new))
    if column != "normalized_name" or not old or not new:
        return False
    old_groups = re.findall(r"(?<!\S)%([^\s\]]+)", old)
    new_groups = re.findall(r"(?<!\S)%([^\s\]]+)", new)
    for old_group in old_groups:
        for new_group in new_groups:
            if not re.fullmatch(re.escape(old_group) + suffix, new_group):
                continue
            if old.replace("%" + old_group, "%" + new_group, 1) == new:
                return True
    return False


def _allows_deterministic_name_rerender(
    column: str, new: str, incoming: Mapping[str, str], *, conflicts: bool,
) -> bool:
    """允许目录册名称升级为当前确定性渲染器的精确结果。"""
    if column != "normalized_name" or not new or conflicts:
        return False
    metadata = {
        key: incoming.get(key) or None
        for key in (
            "developer", "engine", "jp_title", "zh_title", "en_title",
            "version", "translation_group", "positive_tag",
        )
    }
    metadata["dlc"] = str(incoming.get("dlc") or "") == "1"
    metadata["negative_tags"] = sorted(
        _tokens(str(incoming.get("negative_tags") or "")),
        key=_natural_id_key,
    )
    expected = " ".join(target_basename_fields(metadata))
    return new == expected


def _allows_title_qualifier_cleanup(
    column: str, old: str, new: str,
) -> bool:
    """允许从旧目录册标题中删除明确的发行限定词。"""
    return bool(
        column in {"jp_title", "zh_title", "en_title"}
        and old and new and old != new
        and strip_language_edition_qualifiers(old) == new
    )


def _merge_rows(
    existing: dict[str, str], incoming: Mapping[str, str],
) -> tuple[dict[str, str], tuple[str, ...], bool]:
    merged = dict(existing)
    conflicts: list[str] = []
    changed = False
    for column in (
        "VNDB_ID", "DLSITE_ID", "STEAM_ID",
        "VNDB_URL", "DLSITE_URL", "STEAM_URL",
    ):
        value = _joined([
            *_tokens(merged.get(column, "")),
            *_tokens(str(incoming.get(column) or "")),
        ])
        if value != merged.get(column, ""):
            merged[column] = value
            changed = True
    for column in CATALOG_FIELDS:
        if column in {
            "canonical_id", "VNDB_ID", "DLSITE_ID", "STEAM_ID",
            "VNDB_URL", "DLSITE_URL", "STEAM_URL", "first_seen", "last_seen",
        }:
            continue
        old = merged.get(column, "")
        new = str(incoming.get(column) or "")
        if _allows_unknown_group_cleanup(
            column, old, new, incoming
        ) or _allows_unknown_engine_label_upgrade(
            column, old, new
        ) or _allows_obsolete_negative_placeholder_cleanup(
            column, old, new
        ) or _allows_vndb_id_version_cleanup(
            column, old, new, incoming
        ) or _allows_stale_negative_metadata_cleanup(
            column, old, new, incoming, merged
        ) or _allows_translation_credit_suffix_preservation(
            column, old, new
        ) or _allows_title_qualifier_cleanup(
            column, old, new
        ) or _allows_deterministic_name_rerender(
            column, new, incoming, conflicts=bool(conflicts)
        ):
            merged[column] = new
            changed = True
        elif not old and new:
            merged[column] = new
            changed = True
        elif old and new and old != new:
            identity = _canonical_id(merged) or _canonical_id(incoming) or "ID未知"
            conflicts.append(
                f"{identity} {column}：保留 {old}；拒绝 {new}"
            )
    merged["canonical_id"] = _canonical_id(merged)
    if incoming.get("last_seen") and incoming["last_seen"] != merged.get("last_seen"):
        merged["last_seen"] = str(incoming["last_seen"])
        changed = True
    return merged, tuple(conflicts), changed


def _write_catalog(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8-sig", newline="", delete=False,
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp",
    )
    temporary = Path(handle.name)
    try:
        with handle:
            writer = csv.DictWriter(handle, fieldnames=CATALOG_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def update_success_catalog(
    path: str | Path,
    manifest: Mapping[str, Any],
    *,
    observed_at: str,
) -> CatalogUpdateStats:
    """合并本次可信且可执行的叶子游戏；重复运行保持一游戏一行。"""
    catalog_path = Path(path)
    rows = _load_catalog(catalog_path)
    added = merged_count = unchanged = 0
    conflict_details: list[str] = []
    for entry in manifest.get("entries") or ():
        incoming = _catalog_row(entry, observed_at)
        if incoming is None:
            continue
        matching = [
            index for index, row in enumerate(rows)
            if _ids_overlap(row, incoming)
        ]
        if not matching:
            rows.append(incoming)
            added += 1
            continue
        base_index = matching[0]
        combined = rows[base_index]
        for duplicate_index in reversed(matching[1:]):
            combined, extra_conflicts, _ = _merge_rows(
                combined, rows[duplicate_index]
            )
            conflict_details.extend(extra_conflicts)
            rows.pop(duplicate_index)
        combined, extra_conflicts, changed = _merge_rows(combined, incoming)
        conflict_details.extend(extra_conflicts)
        rows[base_index] = combined
        if changed:
            merged_count += 1
        else:
            unchanged += 1
    rows.sort(key=_row_sort_key)
    _write_catalog(catalog_path, rows)
    unique_conflicts = tuple(dict.fromkeys(conflict_details))
    return CatalogUpdateStats(
        added, merged_count, unchanged, len(unique_conflicts), unique_conflicts
    )
