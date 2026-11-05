"""引擎占位的兼容读取与规范输出。"""

from __future__ import annotations

from typing import Any


UNKNOWN_ENGINE_LABEL = "引擎未知"
_LEGACY_UNKNOWN_ENGINE_LABELS = frozenset({"未知", UNKNOWN_ENGINE_LABEL})


def is_unknown_engine(value: Any) -> bool:
    """识别空值、新占位及旧版占位。"""
    return not str(value or "").strip() or (
        str(value).strip() in _LEGACY_UNKNOWN_ENGINE_LABELS
    )


def normalize_engine_label(value: Any) -> str:
    """兼容读取旧值，并始终输出明确的引擎占位。"""
    text = str(value or "").strip()
    return UNKNOWN_ENGINE_LABEL if is_unknown_engine(text) else text
