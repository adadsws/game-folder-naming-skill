"""只根据已验证语义和引擎证据决定系列共同布局。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Sequence

from .engine_labels import (
    UNKNOWN_ENGINE_LABEL,
    is_unknown_engine,
    normalize_engine_label,
)


@dataclass(frozen=True)
class SeriesLayout:
    engine_label: str
    has_common_engine: bool
    developer: str
    jp_title: str | None
    zh_title: str | None
    en_title: str | None

    @property
    def engine(self) -> str | None:
        """兼容调用方：只有可靠共同引擎才返回具体引擎。"""
        return self.engine_label if self.has_common_engine else None


def _one_nonempty(values: Sequence[str | None]) -> str | None:
    normalized = {value.strip() for value in values if value and value.strip()}
    return next(iter(normalized)) if len(normalized) == 1 else None


def series_developer_from_children(
    final_developers: Sequence[str | None],
    local_developers: Sequence[str | None],
    *,
    all_children_found: bool,
) -> str | None:
    """脚本统一汇总合集制作方；远程值一致优先，否则用逐字一致的本地值。"""
    if not final_developers:
        return None
    if not all_children_found or any(
        not str(value or "").strip() for value in final_developers
    ):
        final_common = None
    else:
        final_common = _one_nonempty(final_developers)
    if final_common:
        return final_common
    if len(local_developers) != len(final_developers):
        return None
    if any(not str(value or "").strip() for value in local_developers):
        return None
    return _one_nonempty(local_developers)


def series_developer_only_basename(
    source_basename: str,
    target_developer: str | None,
) -> str | None:
    """Replace only the parent publisher after unanimous child confirmation."""
    target = str(target_developer or "").strip()
    match = re.match(r"^\[([^\]]+)\](\s+)(.+)$", source_basename)
    if not target or match is None:
        return None
    suffix = match.group(2) + match.group(3)
    if not suffix.strip():
        return None
    return f"[{target}]{suffix}"


def plan_series_layout(
    child_engines: Sequence[str | None],
    child_developers: Sequence[str | None],
    common_titles: Sequence[dict[str, Any]],
) -> SeriesLayout | None:
    """消费成员共同语义；冲突保持原系列目录，空语种槽可由唯一共同值补齐。"""
    developer = _one_nonempty(child_developers)
    if not developer or not common_titles:
        return None
    resolved_titles: dict[str, str | None] = {}
    for language in ("jp", "zh", "en"):
        values = [
            str(item.get(language) or "").strip() or None
            for item in common_titles
        ]
        resolved = _one_nonempty(values)
        distinct = {value.strip() for value in values if value and value.strip()}
        if len(distinct) > 1:
            return None
        resolved_titles[language] = resolved
    if not any(resolved_titles.values()):
        return None
    normalized_engines = [normalize_engine_label(value) for value in child_engines]
    known_engines = {
        value for value in normalized_engines if not is_unknown_engine(value)
    }
    has_common_engine = (
        len(known_engines) == 1
        and len(normalized_engines) > 0
        and all(value in known_engines for value in normalized_engines)
    )
    engine_label = (
        next(iter(known_engines))
        if has_common_engine
        else UNKNOWN_ENGINE_LABEL if not known_engines else "多引擎"
    )
    return SeriesLayout(
        engine_label=engine_label,
        has_common_engine=has_common_engine,
        developer=developer,
        jp_title=resolved_titles["jp"],
        zh_title=resolved_titles["zh"],
        en_title=resolved_titles["en"],
    )
