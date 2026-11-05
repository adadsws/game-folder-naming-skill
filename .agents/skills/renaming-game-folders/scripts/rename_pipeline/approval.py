"""单清单与跨批组合清单的批准哈希、构建和一致性校验。"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Sequence


PREVIEW_CONTRACT_VERSION = 68


def validate_preview_contract_version(manifest: dict[str, Any]) -> None:
    """Schema v2 正式清单必须绑定当前 HTML/Markdown 预览契约。"""
    if int(manifest.get("schema_version") or 1) < 2:
        return
    if manifest.get("preview_contract_version") != PREVIEW_CONTRACT_VERSION:
        raise ValueError(
            "manifest 预览契约版本已过期，必须重新生成正式预览"
        )


def manifest_approval_hash(manifest: dict[str, Any]) -> str:
    """计算忽略自身 approval_hash 字段的稳定 SHA-256。"""
    payload = copy.deepcopy(manifest)
    payload.pop("approval_hash", None)
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _root_key(value: Any) -> str:
    return str(Path(str(value or "")).resolve()).casefold()


def build_combined_approval_manifest(
    manifests: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """把有序子 manifest 封装为一个可统一预检和执行的组合 manifest。"""
    children = [copy.deepcopy(item) for item in manifests]
    if not children:
        raise ValueError("至少需要一个 manifest 才能生成汇总批准码")
    if any(item.get("approval_kind") == "combined" for item in children):
        raise ValueError("组合 manifest 不得再次嵌套组合")

    child_hashes: list[str] = []
    root = str(children[0].get("root") or "")
    root_key = _root_key(root)
    schema_version = int(children[0].get("schema_version") or 1)
    scan_fingerprint = children[0].get("scan_root_fingerprint")
    for index, child in enumerate(children, start=1):
        validate_preview_contract_version(child)
        expected = manifest_approval_hash(child)
        declared = str(child.get("approval_hash") or "")
        if not declared or declared != expected:
            raise ValueError(f"第 {index} 个子 manifest 批准哈希无效")
        if _root_key(child.get("root")) != root_key:
            raise ValueError("组合 manifest 的扫描根不一致")
        if int(child.get("schema_version") or 1) != schema_version:
            raise ValueError("组合 manifest 的 schema_version 不一致")
        if child.get("scan_root_fingerprint") != scan_fingerprint:
            raise ValueError("组合 manifest 的扫描根指纹不一致")
        if child.get("mode"):
            raise ValueError("撤销 manifest 不参与跨批组合批准")
        child_hashes.append(expected)
    if len(set(child_hashes)) != len(child_hashes):
        raise ValueError("组合 manifest 包含重复子批准哈希")

    combined: dict[str, Any] = {
        "schema_version": schema_version,
        "approval_kind": "combined",
        "preview_contract_version": PREVIEW_CONTRACT_VERSION,
        "root": root,
        "scan_root_fingerprint": scan_fingerprint,
        "child_approval_hashes": child_hashes,
        "child_manifests": children,
        "entries": [
            copy.deepcopy(entry)
            for child in children
            for entry in child.get("entries", [])
        ],
    }
    snapshot_roots = {str(item.get("snapshot_root") or "") for item in children}
    if len(snapshot_roots) == 1 and next(iter(snapshot_roots)):
        combined["snapshot_root"] = next(iter(snapshot_roots))
    combined["approval_hash"] = manifest_approval_hash(combined)
    validate_combined_approval_manifest(combined)
    return combined


def validate_combined_approval_manifest(manifest: dict[str, Any]) -> None:
    """复验组合外层、每个子清单及扁平动作视图完全一致。"""
    if manifest.get("approval_kind") != "combined":
        return
    children = manifest.get("child_manifests") or []
    if not isinstance(children, list) or not children:
        raise ValueError("组合 manifest 缺少子 manifest")
    rebuilt = build_combined_approval_manifest_unchecked(children)
    for key in (
        "schema_version", "approval_kind", "preview_contract_version",
        "root", "scan_root_fingerprint",
        "child_approval_hashes", "entries",
    ):
        if manifest.get(key) != rebuilt.get(key):
            raise ValueError(f"组合 manifest 字段不一致：{key}")
    expected = manifest_approval_hash(manifest)
    if str(manifest.get("approval_hash") or "") != expected:
        raise ValueError("汇总批准码与组合 manifest 不一致")


def build_combined_approval_manifest_unchecked(
    manifests: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """内部重建入口；校验子清单后返回组合视图，避免递归调用。"""
    children = [copy.deepcopy(item) for item in manifests]
    if not children:
        raise ValueError("组合 manifest 缺少子 manifest")
    root = str(children[0].get("root") or "")
    root_key = _root_key(root)
    schema_version = int(children[0].get("schema_version") or 1)
    scan_fingerprint = children[0].get("scan_root_fingerprint")
    hashes: list[str] = []
    for index, child in enumerate(children, start=1):
        validate_preview_contract_version(child)
        expected = manifest_approval_hash(child)
        if str(child.get("approval_hash") or "") != expected:
            raise ValueError(f"第 {index} 个子 manifest 批准哈希无效")
        if (
            _root_key(child.get("root")) != root_key
            or int(child.get("schema_version") or 1) != schema_version
            or child.get("scan_root_fingerprint") != scan_fingerprint
        ):
            raise ValueError("组合 manifest 的根、版本或指纹不一致")
        if child.get("approval_kind") == "combined" or child.get("mode"):
            raise ValueError("组合 manifest 不接受嵌套组合或撤销清单")
        hashes.append(expected)
    if len(set(hashes)) != len(hashes):
        raise ValueError("组合 manifest 包含重复子批准哈希")
    combined = {
        "schema_version": schema_version,
        "approval_kind": "combined",
        "preview_contract_version": PREVIEW_CONTRACT_VERSION,
        "root": root,
        "scan_root_fingerprint": scan_fingerprint,
        "child_approval_hashes": hashes,
        "child_manifests": children,
        "entries": [
            copy.deepcopy(entry)
            for child in children
            for entry in child.get("entries", [])
        ],
    }
    snapshot_roots = {str(item.get("snapshot_root") or "") for item in children}
    if len(snapshot_roots) == 1 and next(iter(snapshot_roots)):
        combined["snapshot_root"] = next(iter(snapshot_roots))
    combined["approval_hash"] = manifest_approval_hash(combined)
    return combined


def _entry_identity(entry: dict[str, Any]) -> tuple[str, str]:
    """用语义 ID 标识单游戏、合集及未决项，供局部预览更新对齐。"""
    kind = str(entry.get("kind") or "game")
    if kind == "series":
        identity = str(entry.get("series_id") or "")
    else:
        identity = str(entry.get("game_id") or entry.get("candidate_id") or "")
    if not identity:
        raise ValueError("局部更新条目缺少稳定 ID")
    return kind, identity


def build_updated_combined_approval_manifest(
    previous: dict[str, Any],
    updates: Sequence[dict[str, Any]],
    current_root_fingerprint: str,
) -> dict[str, Any]:
    """用新批次/条目替换旧汇总对应项，并给保留结果重签当前根指纹。"""
    validate_combined_approval_manifest(previous)
    if not updates:
        raise ValueError("至少需要一个更新 manifest")
    root = str(previous.get("root") or "")
    root_key = _root_key(root)
    if not current_root_fingerprint:
        raise ValueError("当前扫描根指纹不能为空")

    old_children = [copy.deepcopy(item) for item in previous["child_manifests"]]
    old_entries = [
        copy.deepcopy(entry)
        for child in old_children for entry in child.get("entries", [])
    ]
    old_by_id = {_entry_identity(item): item for item in old_entries}
    if len(old_by_id) != len(old_entries):
        raise ValueError("旧汇总包含重复条目 ID，拒绝局部更新")

    replacement: dict[tuple[str, str], dict[str, Any]] = {}
    for update in updates:
        validate_preview_contract_version(update)
        if (
            update.get("approval_kind") == "combined"
            or update.get("mode")
            or _root_key(update.get("root")) != root_key
            or update.get("scan_root_fingerprint") != current_root_fingerprint
        ):
            raise ValueError("更新 manifest 必须是同一扫描根当前指纹的单批正式预览")
        expected = manifest_approval_hash(update)
        if not update.get("approval_hash") or update["approval_hash"] != expected:
            raise ValueError("更新 manifest 批准哈希无效")
        for entry in update.get("entries", []):
            key = _entry_identity(entry)
            if key not in old_by_id:
                raise ValueError(f"局部更新不得加入旧预览外的新条目：{key[1]}")
            if str(entry.get("source") or "").casefold() != str(
                old_by_id[key].get("source") or ""
            ).casefold():
                raise ValueError(f"局部更新条目源路径与旧预览不一致：{key[1]}")
            if key in replacement:
                raise ValueError(f"多个更新 manifest 重复覆盖条目：{key[1]}")
            replacement[key] = copy.deepcopy(entry)
    if not replacement:
        raise ValueError("更新 manifest 没有可替换条目")
    update_template = copy.deepcopy(updates[0])
    update_template["entries"] = [copy.deepcopy(item) for item in replacement.values()]
    update_template["scan_root_fingerprint"] = current_root_fingerprint
    update_template["approval_hash"] = manifest_approval_hash(update_template)

    # 合集父项与所有成员作为一个更新单位，避免局部刷新留下旧传播结果。
    old_series = {
        str(entry.get("series_id") or ""): entry
        for entry in old_entries if entry.get("kind") == "series"
    }
    for series_id, series_entry in old_series.items():
        parent = str(series_entry.get("source") or "")
        member_keys = {
            _entry_identity(entry) for entry in old_entries
            if entry.get("kind") != "series"
            and str(entry.get("parent_series_source") or "").casefold()
            == parent.casefold()
        }
        series_key = _entry_identity(series_entry)
        group = member_keys | {series_key}
        overlap = group & replacement.keys()
        if overlap and overlap != group:
            raise ValueError(
                f"系列必须连同外层及全部子游戏一起局部更新：{series_id}"
            )

    # 更新条目必须在其旧子 manifest 中唯一归属；拆分后的旧子 manifest
    # 保留未选中项并按当前扫描根指纹重新计算批准哈希。
    output_children: list[dict[str, Any]] = []
    inserted_updates = False
    for child in old_children:
        child_entries = [copy.deepcopy(e) for e in child.get("entries", [])]
        child_keys = {_entry_identity(e) for e in child_entries}
        overlap = child_keys & replacement.keys()
        if not overlap:
            kept = child
            kept["scan_root_fingerprint"] = current_root_fingerprint
            kept["approval_hash"] = manifest_approval_hash(kept)
            output_children.append(kept)
            continue
        kept_entries = [e for e in child_entries if _entry_identity(e) not in overlap]
        if kept_entries:
            kept = copy.deepcopy(child)
            kept["entries"] = kept_entries
            kept["scan_root_fingerprint"] = current_root_fingerprint
            kept["approval_hash"] = manifest_approval_hash(kept)
            output_children.append(kept)
        if not inserted_updates:
            inserted_updates = True
            output_children.append(update_template)
    if not inserted_updates:
        raise ValueError("局部更新条目未对应旧汇总子清单")

    return build_combined_approval_manifest(output_children)
