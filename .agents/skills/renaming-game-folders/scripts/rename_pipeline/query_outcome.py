"""只根据已验证远程语义分类查询是否得到可信采用结果。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class QueryOutcome:
    found: bool
    reason: str


def classify_query_outcome(
    remote_semantic: Mapping[str, Any],
) -> QueryOutcome:
    """accepted 且至少采用一个真实候选时才算查询成功。"""
    accepted = remote_semantic.get("review_state") == "accepted"
    selected = list(remote_semantic.get("selected_candidates") or ())
    if accepted and selected:
        return QueryOutcome(True, "已采用可信远程候选")
    if selected:
        return QueryOutcome(False, "远程候选仍待判断")
    return QueryOutcome(False, "未找到可信远程候选")
