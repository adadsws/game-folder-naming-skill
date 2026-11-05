"""批量搜索审计：唯一运行目录、逐游戏指纹与阶段材料绑定。"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .contracts import canonical_sha256


def directory_fingerprint(directory: Path) -> str:
    """按相对结构、类型、大小和修改时间生成一个目录 SHA-256。"""
    digest = hashlib.sha256()
    directory = directory.resolve()
    paths = sorted(
        directory.rglob("*"),
        key=lambda item: str(item.relative_to(directory)).casefold(),
    )
    for path in paths:
        relative = path.relative_to(directory).as_posix()
        stat = path.stat()
        kind = "D" if path.is_dir() else "F"
        size = 0 if path.is_dir() else stat.st_size
        digest.update(
            f"{kind}\0{relative}\0{size}\0{stat.st_mtime_ns}\n".encode("utf-8")
        )
    return digest.hexdigest()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _write_json(path: Path, value: dict[str, Any], *, replace: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not replace:
        raise ValueError(f"审计文件已存在，拒绝覆盖：{path}")
    temporary = path.with_name(path.name + ".tmp")
    if temporary.exists():
        raise ValueError(f"审计临时文件已存在：{temporary}")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def _timestamp(now: datetime) -> str:
    offset = now.strftime("%z") or "+0000"
    return now.strftime("%Y-%m-%d_%H-%M-%S") + offset[:3] + "-" + offset[3:]


def _root_identifier(root: Path) -> str:
    stem = re.sub(r"[^0-9A-Za-z._-]+", "-", root.name).strip("-.") or "scan-root"
    suffix = hashlib.sha256(str(root).casefold().encode("utf-8")).hexdigest()[:10]
    return f"{stem}-{suffix}"


def begin_search_audit(
    analysis: dict[str, Any],
    local_semantics: dict[str, Any],
    log_root: Path,
    *,
    now: datetime | None = None,
) -> Path:
    """为一次完整批量搜索创建唯一日志目录并保存不可覆盖的输入。"""
    scan_root = Path(str(analysis["root"])).resolve()
    log_root = log_root.resolve()
    if _inside(log_root, scan_root):
        raise ValueError("搜索日志目录不得位于扫描根目录内")
    current = now or datetime.now().astimezone()
    base = log_root / _root_identifier(scan_root)
    timestamp = _timestamp(current)
    suffix = 1
    while True:
        candidate = base / (
            timestamp if suffix == 1 else f"{timestamp}-{suffix:02d}"
        )
        try:
            candidate.mkdir(parents=True, exist_ok=False)
            break
        except FileExistsError:
            # 多批同秒启动时由原子 mkdir 决定赢家，失败者继续取唯一后缀。
            suffix += 1
    run_id = uuid.uuid4().hex
    _write_json(candidate / "run.json", {
        "schema_version": 1,
        "audit_run_id": run_id,
        "status": "running",
        "stage": "search",
        "started_at": current.isoformat(),
        "finished_at": None,
        "scan_root": str(scan_root),
    })
    _write_json(candidate / "analysis-input.json", analysis)
    hierarchy_audit = analysis.get("hierarchy_audit") or {}
    for filename, key in (
        ("scan-evidence.json", "scan_evidence"),
        ("hierarchy-semantics.json", "hierarchy_semantics"),
        ("hierarchy-compiled.json", "compiled_hierarchy"),
    ):
        document = hierarchy_audit.get(key)
        if isinstance(document, dict) and document:
            _write_json(candidate / filename, document)
    _write_json(candidate / "local-semantics.json", local_semantics)
    _write_json(candidate / "game-hashes.json", {
        "schema_version": 1,
        "audit_run_id": run_id,
        "games": [{
            "game_id": str(game["game_id"]),
            "relative_path": str(game["relative_path"]),
            "sha256": str(game["directory_fingerprint"]),
        } for game in analysis.get("games", [])],
    })
    return candidate


def attach_search_audit(evidence: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    """把运行身份写入搜索证据，并重算其内嵌摘要。"""
    run_dir = run_dir.resolve()
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    body = {key: value for key, value in evidence.items() if key != "search_fingerprint"}
    body["audit_run_id"] = run["audit_run_id"]
    body["audit_log_dir"] = str(run_dir)
    return {**body, "search_fingerprint": canonical_sha256(body)}


def _update_run(run_dir: Path, **changes: Any) -> None:
    path = run_dir / "run.json"
    run = json.loads(path.read_text(encoding="utf-8"))
    run.update(changes)
    _write_json(path, run, replace=True)


def finish_search_audit(run_dir: Path, evidence: dict[str, Any]) -> None:
    """原子保存完整搜索证据，并把运行标为搜索完成。"""
    run_dir = run_dir.resolve()
    _write_json(run_dir / "search-evidence.json", evidence)
    _update_run(
        run_dir,
        status="search-complete",
        stage="search",
        finished_at=datetime.now().astimezone().isoformat(),
    )


def fail_search_audit(run_dir: Path, stage: str, error: BaseException) -> None:
    """保留已有证据并记录失败，不生成后续批准材料。"""
    _update_run(
        run_dir,
        status="failed",
        stage=stage,
        error=f"{type(error).__name__}: {error}",
        finished_at=datetime.now().astimezone().isoformat(),
    )


def validate_search_audit(
    analysis: dict[str, Any],
    local_semantics: dict[str, Any],
    evidence: dict[str, Any],
    *,
    verify_games: bool,
) -> Path:
    """验证运行身份、输入副本及可选的逐游戏当前目录指纹。"""
    run_dir = Path(str(evidence.get("audit_log_dir") or "")).resolve()
    if not run_dir.is_dir():
        raise ValueError("搜索审计日志目录不存在")
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    if run.get("audit_run_id") != evidence.get("audit_run_id"):
        raise ValueError("搜索证据 audit_run_id 与日志不一致")
    stored_evidence = json.loads(
        (run_dir / "search-evidence.json").read_text(encoding="utf-8")
    )
    if canonical_sha256(stored_evidence) != canonical_sha256(evidence):
        raise ValueError("搜索证据与审计日志保存内容不一致")
    stored_analysis = json.loads(
        (run_dir / "analysis-input.json").read_text(encoding="utf-8")
    )
    stored_local = json.loads(
        (run_dir / "local-semantics.json").read_text(encoding="utf-8")
    )
    if canonical_sha256(stored_analysis) != canonical_sha256(analysis):
        raise ValueError("搜索审计中的分析输入不一致")
    if canonical_sha256(stored_local) != canonical_sha256(local_semantics):
        raise ValueError("搜索审计中的本地语义不一致")
    hashes = json.loads((run_dir / "game-hashes.json").read_text(encoding="utf-8"))
    if hashes.get("audit_run_id") != evidence.get("audit_run_id"):
        raise ValueError("游戏目录指纹所属运行不一致")
    expected_hashes = [{
        "game_id": str(game["game_id"]),
        "relative_path": str(game["relative_path"]),
        "sha256": str(game["directory_fingerprint"]),
    } for game in analysis.get("games", [])]
    if hashes.get("games") != expected_hashes:
        raise ValueError("游戏目录指纹清单与分析输入不一致")
    if verify_games:
        root = Path(str(analysis["root"])).resolve()
        for item in hashes.get("games", []):
            relative_path = str(item["relative_path"])
            current = directory_fingerprint(root / relative_path)
            if current != item.get("sha256"):
                raise ValueError(f"游戏目录指纹已变化：{relative_path}")
    return run_dir


def complete_preview_audit(
    run_dir: Path,
    remote_semantics: dict[str, Any],
    manifest: dict[str, Any],
    preview_paths: tuple[Path, Path, Path, Path],
) -> Path:
    """补齐批准材料；已有预览时写入唯一修订目录，绝不覆盖旧版。"""
    run_dir = run_dir.resolve()
    preview_dir = run_dir
    if any((run_dir / name).exists() for name in (
        "remote-semantics.json", "rename-manifest.json", "approval.json",
        "preview.md", "decisions.md", "preview.html",
    )):
        revision_root = run_dir / "preview-revisions"
        candidate = revision_root / _timestamp(datetime.now().astimezone())
        suffix = 2
        while candidate.exists():
            candidate = revision_root / (
                _timestamp(datetime.now().astimezone()) + f"-{suffix:02d}"
            )
            suffix += 1
        candidate.mkdir(parents=True, exist_ok=False)
        preview_dir = candidate
    _write_json(preview_dir / "remote-semantics.json", remote_semantics)
    _write_json(preview_dir / "rename-manifest.json", manifest)
    _write_json(preview_dir / "planned-renames.json", {
        "schema_version": 1,
        "approval_hash": manifest["approval_hash"],
        "entries": [{
            "source": entry.get("source"),
            "runtime_source": entry.get("runtime_source"),
            "target": entry.get("target"),
            "status": entry.get("status"),
            "notes": entry.get("notes") or [],
        } for entry in manifest.get("entries", [])],
    })
    _write_json(preview_dir / "source-shortcuts.json", {
        "schema_version": 1,
        "approval_hash": manifest["approval_hash"],
        "games": [{
            "source": entry.get("source"),
            "target": entry.get("target"),
            "shortcuts": entry.get("source_shortcuts") or [],
        } for entry in manifest.get("entries", [])
        if entry.get("source_shortcuts")],
    })
    _write_json(preview_dir / "approval.json", {
        "schema_version": 1,
        "approval_hash": manifest["approval_hash"],
    })
    for source in preview_paths[1:]:
        destination = preview_dir / source.name
        if destination.exists():
            raise ValueError(f"审计文件已存在，拒绝覆盖：{destination}")
        temporary = destination.with_name(destination.name + ".tmp")
        temporary.write_bytes(source.read_bytes())
        temporary.replace(destination)
    _update_run(
        run_dir,
        status="complete",
        stage="semantic-preview",
        approval_hash=manifest["approval_hash"],
        latest_preview_dir=str(preview_dir),
        finished_at=datetime.now().astimezone().isoformat(),
    )
    return preview_dir
