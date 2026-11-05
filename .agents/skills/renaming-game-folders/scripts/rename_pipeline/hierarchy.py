"""多层 EXE 文件系统证据与 Skill 层级决策的严格编译边界。"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Sequence

from .audit_log import directory_fingerprint
from .contracts import canonical_sha256, stable_game_id


HIERARCHY_SCHEMA_VERSION = 2
HIERARCHY_DECISION_SCHEMA_VERSION = 2
GAME_ROOT_TYPES = {
    "single_game_wrapper", "series_child", "same_game_launcher",
}
CONTAINER_TYPES = {"series", "organization"}
_SERIES_COUNT_MARKER_RE = re.compile(r"(?<!@)@@?(\d+)(?!\d)")
IGNORED_EXE_TOKENS = (
    "unitycrashhandler", "unins", "uninstall", "crashpad", "reporter",
    "vc_redist",
)
SUPPORT_DIRECTORY_NAMES = {
    "app", "backup", "bepinex", "contents", "data", "lib", "managed",
    "movie", "movies", "mv", "resources", "swf_html", "translators", "www",
    "原exe", "原版备份", "备份",
}


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _relative_path(root: Path, path: Path) -> str:
    relative = path.resolve().relative_to(root.resolve()).as_posix()
    return relative or "."


def _resolve_relative_directory(root: Path, value: Any, label: str) -> Path:
    text = "" if value is None else str(value)
    if not text.strip():
        raise ValueError(f"{label}不能为空")
    path = (root / Path(text)).resolve()
    if not _inside(path, root) or not path.is_dir():
        raise ValueError(f"{label}不是扫描根内现有目录：{text}")
    return path


def _engine_signature(directory: Path) -> dict[str, Any]:
    """只记录紧邻 EXE 的本地签名；不根据目录名称猜引擎。"""
    try:
        names = {item.name.casefold(): item.name for item in directory.iterdir()}
    except OSError:
        names = {}
    rules = (
        ("UNITY", ("unityplayer.dll", "gameassembly.dll")),
        ("GAMEMAKER", ("data.win",)),
        ("RPGMAKER", ("nw.dll", "package.json")),
        ("GODOT", tuple(
            name for name in names if name.endswith(".pck")
        )),
        ("RPG_DEVELOPER_BAKIN", ("bakinplayer.exe",)),
    )
    for engine, markers in rules:
        found = [names[marker] for marker in markers if marker in names]
        if found:
            return {"engine": engine, "markers": found}
    return {"engine": UNKNOWN_ENGINE_LABEL, "markers": []}


def _standalone_swf_paths(selected: Path) -> list[Path]:
    """只补充可能独立启动的直属 SWF，不收集 data/lib 等资源动画。"""
    grouped: dict[Path, list[Path]] = {}
    try:
        for path in selected.rglob("*.swf"):
            if path.is_file():
                grouped.setdefault(path.parent.resolve(), []).append(path.resolve())
    except OSError:
        return []
    launchers: list[Path] = []
    for directory, swfs in grouped.items():
        if any(part.casefold() in SUPPORT_DIRECTORY_NAMES for part in directory.parts):
            continue
        try:
            has_exe = any(
                item.is_file()
                and item.suffix.casefold() == ".exe"
                and not any(token in item.stem.casefold() for token in IGNORED_EXE_TOKENS)
                for item in directory.iterdir()
            )
        except OSError:
            continue
        if has_exe:
            continue
        if len(swfs) == 1:
            launchers.append(swfs[0])
            continue
        directory_key = "".join(character for character in directory.name.casefold() if character.isalnum())
        matching = [
            path for path in swfs
            if directory_key and directory_key in "".join(
                character for character in path.stem.casefold() if character.isalnum()
            )
        ]
        launchers.extend(matching[:1])
    return launchers


def build_scan_evidence(
    scan_root: Path,
    include_paths: Sequence[Path] | None = None,
) -> dict[str, Any]:
    """收集全部 EXE、独立启动 SWF 候选和祖先事实，不决定最终游戏根。"""
    root = scan_root.resolve()
    if not root.is_dir():
        raise ValueError(f"扫描根目录不存在：{root}")
    selected_roots: list[Path] = []
    for relative in include_paths or (Path("."),):
        candidate = (root / relative).resolve()
        if not _inside(candidate, root):
            raise ValueError(f"include-path 超出扫描根目录：{relative}")
        if not candidate.is_dir():
            raise ValueError(f"include-path 不存在：{relative}")
        selected_roots.append(candidate)

    exe_paths: dict[str, Path] = {}
    for selected in selected_roots:
        try:
            paths = selected.rglob("*")
            for path in paths:
                if not path.is_file() or path.suffix.casefold() != ".exe":
                    continue
                lowered = path.stem.casefold()
                if any(token in lowered for token in IGNORED_EXE_TOKENS):
                    continue
                exe_paths.setdefault(str(path.resolve()).casefold(), path.resolve())
            for path in _standalone_swf_paths(selected):
                exe_paths.setdefault(str(path).casefold(), path)
        except OSError:
            continue

    candidates: list[dict[str, Any]] = []
    ancestor_paths: dict[str, Path] = {}
    for exe in sorted(exe_paths.values(), key=lambda item: str(item).casefold()):
        relative_exe = _relative_path(root, exe)
        candidate_id = "exe-" + hashlib.sha256(
            relative_exe.casefold().encode("utf-8")
        ).hexdigest()[:16]
        candidates.append({
            "candidate_id": candidate_id,
            "exe_relative_path": relative_exe,
            "candidate_directory": _relative_path(root, exe.parent),
            "signature": _engine_signature(exe.parent),
        })
        current = exe.parent
        while current != root and _inside(current, root):
            ancestor_paths.setdefault(str(current).casefold(), current)
            current = current.parent

    ancestors: list[dict[str, Any]] = []
    for directory in sorted(
        ancestor_paths.values(), key=lambda item: _relative_path(root, item).casefold()
    ):
        try:
            direct = sorted(directory.iterdir(), key=lambda item: item.name.casefold())
        except OSError:
            direct = []
        ancestors.append({
            "relative_path": _relative_path(root, directory),
            "direct_files": [item.name for item in direct if item.is_file()][:200],
            "direct_directories": [item.name for item in direct if item.is_dir()][:200],
            "tilde_organization_anchor": directory.name.startswith("~"),
        })

    body = {
        "schema_version": HIERARCHY_SCHEMA_VERSION,
        "root": str(root),
        "root_fingerprint": directory_fingerprint(root),
        "executable_candidates": candidates,
        "ancestor_nodes": ancestors,
    }
    return {**body, "scan_fingerprint": canonical_sha256(body)}


def compile_hierarchy_semantics(
    scan: dict[str, Any], decisions: dict[str, Any]
) -> dict[str, Any]:
    """验证 Skill 最终根决策、候选精确覆盖和父子包含关系。"""
    if scan.get("schema_version") != HIERARCHY_SCHEMA_VERSION:
        raise ValueError("scan-evidence schema_version 不受支持")
    scan_body = {key: value for key, value in scan.items() if key != "scan_fingerprint"}
    if scan.get("scan_fingerprint") != canonical_sha256(scan_body):
        raise ValueError("scan-evidence 自身摘要不匹配")
    root = Path(str(scan.get("root") or "")).resolve()
    if not root.is_dir() or directory_fingerprint(root) != scan.get("root_fingerprint"):
        raise ValueError("扫描根在层级判断后发生变化")
    if decisions.get("schema_version") != HIERARCHY_SCHEMA_VERSION:
        raise ValueError("hierarchy-semantics schema_version 不受支持")
    decision_schema = decisions.get("decision_schema_version", 1)
    if decision_schema not in {1, HIERARCHY_DECISION_SCHEMA_VERSION}:
        raise ValueError("hierarchy decision_schema_version 不受支持")
    if decisions.get("scan_fingerprint") != scan.get("scan_fingerprint"):
        raise ValueError("hierarchy-semantics 引用的扫描摘要不匹配")

    candidate_by_id = {
        str(item["candidate_id"]): item
        for item in scan.get("executable_candidates", [])
    }
    covered_by: dict[str, str] = {}
    compiled_roots: list[dict[str, Any]] = []
    root_paths: list[Path] = []
    relative_roots: set[str] = set()
    for item in decisions.get("game_roots", []):
        root_path = _resolve_relative_directory(
            root, item.get("relative_path"), "game_root.relative_path"
        )
        relative = _relative_path(root, root_path)
        if relative.casefold() in {value.casefold() for value in relative_roots}:
            raise ValueError(f"重复最终游戏根：{relative}")
        relative_roots.add(relative)
        root_type = str(item.get("type") or "")
        if root_type not in GAME_ROOT_TYPES:
            raise ValueError(f"未知游戏根类型：{root_type}")
        confidence = item.get("confidence")
        if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise ValueError(f"confidence 必须位于 0 到 1：{relative}")
        reason = str(item.get("reason") or "").strip()
        if not reason:
            raise ValueError(f"层级判断理由不能为空：{relative}")
        candidate_ids = [str(value) for value in item.get("candidate_ids", [])]
        if not candidate_ids:
            raise ValueError(f"最终游戏根没有启动候选：{relative}")
        for candidate_id in candidate_ids:
            candidate = candidate_by_id.get(candidate_id)
            if candidate is None:
                raise ValueError(f"未知启动候选：{candidate_id}")
            if candidate_id in covered_by:
                raise ValueError(
                    f"启动候选重复覆盖：{candidate_id}；"
                    f"{covered_by[candidate_id]} / {relative}"
                )
            exe_path = (root / Path(candidate["exe_relative_path"])).resolve()
            if not _inside(exe_path, root_path):
                raise ValueError(f"启动候选不在最终游戏根内：{candidate_id}")
            covered_by[candidate_id] = relative
        root_paths.append(root_path)
        compiled_roots.append({
            **item,
            "relative_path": relative,
            "source_path": str(root_path),
            "game_id": stable_game_id(root, root_path),
            "candidate_ids": candidate_ids,
        })

    unresolved: list[dict[str, Any]] = []
    unresolved_ids: set[str] = set()
    for item in decisions.get("unresolved_candidates", []):
        candidate_id = str(item.get("candidate_id") or "")
        candidate = candidate_by_id.get(candidate_id)
        if candidate is None:
            raise ValueError(f"未知未分目录启动候选：{candidate_id}")
        if candidate_id in covered_by or candidate_id in unresolved_ids:
            raise ValueError(f"启动候选重复覆盖：{candidate_id}")
        confidence = item.get("confidence")
        if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise ValueError(f"confidence 必须位于 0 到 1：{candidate_id}")
        reason = str(item.get("reason") or "").strip()
        if not reason:
            raise ValueError(f"未分目录启动候选理由不能为空：{candidate_id}")
        unresolved_ids.add(candidate_id)
        unresolved.append({**item, **candidate})

    missing = set(candidate_by_id) - set(covered_by) - unresolved_ids
    if missing:
        raise ValueError("启动候选未覆盖：" + "、".join(sorted(missing)))
    for index, left in enumerate(root_paths):
        for right in root_paths[index + 1:]:
            if _inside(left, right) or _inside(right, left):
                raise ValueError(
                    "叶子游戏根不得互相包含："
                    f"{_relative_path(root, left)} / {_relative_path(root, right)}"
                )

    compiled_containers: list[dict[str, Any]] = []
    root_by_relative = {
        item["relative_path"].casefold(): item for item in compiled_roots
    }
    for item in decisions.get("containers", []):
        container_path = _resolve_relative_directory(
            root, item.get("relative_path"), "container.relative_path"
        )
        relative = _relative_path(root, container_path)
        container_type = str(item.get("type") or "")
        if container_type not in CONTAINER_TYPES:
            raise ValueError(f"未知容器类型：{container_type}")
        series_range = item.get("series_range")
        if series_range not in (None, ""):
            if container_type != "series":
                raise ValueError("只有 series 容器可填写 series_range")
            if not isinstance(series_range, str) or re.fullmatch(
                r"(?i)(?:\d+|[IVX]+)~(?:\d+|[IVX]+)", series_range,
            ) is None:
                raise ValueError(f"series_range 格式无效：{relative}")
        if decision_schema >= 2 and "series_count" in item:
            raise ValueError("series_count 由层级编译器生成，AI 决策不得填写")
        series_count = item.get("series_count")
        if series_count not in (None, ""):
            if container_type != "series":
                raise ValueError("只有 series 容器可填写 series_count")
            if isinstance(series_count, bool) or not isinstance(series_count, int):
                raise ValueError(f"series_count 必须是正整数：{relative}")
            if series_count <= 0:
                raise ValueError(f"series_count 必须是正整数：{relative}")
            marker_counts = {
                int(match.group(1))
                for match in _SERIES_COUNT_MARKER_RE.finditer(container_path.name)
            }
            if series_count not in marker_counts:
                raise ValueError(
                    f"series_count 缺少同值 @数字/@@数字原名证据：{relative}"
                )
        if series_count not in (None, "") and series_range not in (None, ""):
            raise ValueError(f"series_count 与 series_range 不得同时填写：{relative}")
        surface_titles = item.get("surface_titles")
        if surface_titles not in (None, {}):
            if container_type != "series":
                raise ValueError("只有 series 容器可填写 surface_titles")
            if not isinstance(surface_titles, dict) or set(surface_titles) != {
                "jp", "zh", "en",
            }:
                raise ValueError(f"surface_titles 必须完整包含 jp/zh/en：{relative}")
            for language, value in surface_titles.items():
                if value is None:
                    continue
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(
                        f"surface_titles.{language} 必须是非空字符串或 null：{relative}"
                    )
                if value not in container_path.name:
                    raise ValueError(
                        f"surface_titles.{language} 不在容器 basename 中：{relative}"
                    )
        child_roots = [str(value) for value in item.get("child_roots", [])]
        child_games: list[str] = []
        for child in child_roots:
            compiled_child = root_by_relative.get(child.casefold())
            if compiled_child is None:
                raise ValueError(f"容器引用未知游戏根：{child}")
            child_path = Path(compiled_child["source_path"])
            if child_path == container_path or not _inside(child_path, container_path):
                raise ValueError(f"容器不包含子游戏根：{relative} / {child}")
            child_games.append(str(compiled_child["game_id"]))
        prefix = "series" if container_type == "series" else "organization"
        compiled_containers.append({
            **item,
            "relative_path": relative,
            "source_path": str(container_path),
            "child_roots": child_roots,
            "child_game_ids": child_games,
            "container_id": prefix + "-" + hashlib.sha256(
                relative.casefold().encode("utf-8")
            ).hexdigest()[:16],
        })

    series_containers = [
        item for item in compiled_containers if item.get("type") == "series"
    ]
    # AI 只判断容器类型和成员关系；结构确定后，明确的 @数字/@@数字标记
    # 属于可唯一派生的格式事实，由编译器写入完整层级产物。
    for container in series_containers:
        container_path = Path(str(container["source_path"]))
        nested = next((
            item for item in series_containers
            if item is not container
            and _inside(Path(str(item["source_path"])), container_path)
        ), None)
        marker_counts = {
            int(match.group(1))
            for match in _SERIES_COUNT_MARKER_RE.finditer(container_path.name)
        }
        if nested is not None:
            if decision_schema >= 2 and marker_counts:
                raise ValueError(
                    "带数字计数标记的上层容器包含另一层 series，必须判为 organization："
                    f"{container['relative_path']} / {nested['relative_path']}"
                )
            if container.get("series_count") not in (None, ""):
                raise ValueError(
                    "series_count 只允许子游戏上面一层合集，不得包含另一层 series："
                    f"{container['relative_path']} / {nested['relative_path']}"
                )
            continue
        if len(marker_counts) > 1:
            raise ValueError(
                f"series_count 原名包含多个不同计数标记：{container['relative_path']}"
            )
        derived_count = next(iter(marker_counts), None)
        if derived_count is not None:
            if container.get("series_range") not in (None, ""):
                raise ValueError(
                    f"series_count 与 series_range 不得同时填写：{container['relative_path']}"
                )
            legacy_count = container.get("series_count")
            if legacy_count not in (None, "", derived_count):
                raise ValueError(
                    "series_count 缺少同值 @数字/@@数字原名证据："
                    f"{container['relative_path']}"
                )
            container["series_count"] = derived_count

    for counted in (
        item for item in series_containers if item.get("series_count") not in (None, "")
    ):
        counted_path = Path(str(counted["source_path"]))
        nested = next((
            item for item in series_containers
            if item is not counted
            and _inside(Path(str(item["source_path"])), counted_path)
        ), None)
        if nested is not None:
            raise ValueError(
                "series_count 只允许子游戏上面一层合集，不得包含另一层 series："
                f"{counted['relative_path']} / {nested['relative_path']}"
            )

    body = {
        "schema_version": HIERARCHY_SCHEMA_VERSION,
        "root": str(root),
        "root_fingerprint": scan["root_fingerprint"],
        "scan_fingerprint": scan["scan_fingerprint"],
        "game_roots": sorted(
            compiled_roots, key=lambda item: item["relative_path"].casefold()
        ),
        "containers": sorted(
            compiled_containers, key=lambda item: item["relative_path"].casefold()
        ),
        "unresolved_candidates": sorted(
            unresolved, key=lambda item: item["exe_relative_path"].casefold()
        ),
    }
    return {**body, "hierarchy_fingerprint": canonical_sha256(body)}
from .engine_labels import UNKNOWN_ENGINE_LABEL
