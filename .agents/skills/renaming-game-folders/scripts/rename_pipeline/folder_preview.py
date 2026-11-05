"""按批准 manifest 模拟最终目录树，并为空目录与项目文件动作建预览。"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Mapping


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _atomic_directory_replace(source: Path, target: Path) -> None:
    """同卷原子改名；Windows 显式启用扩展长度路径。"""
    if os.name != "nt":
        os.replace(source, target)
        return

    def extended(path: Path) -> str:
        value = str(path.resolve())
        if value.startswith("\\\\?\\"):
            return value
        if value.startswith("\\\\"):
            return "\\\\?\\UNC\\" + value[2:]
        return "\\\\?\\" + value

    os.replace(extended(source), extended(target))


def _transactional_directory_fallback(
    target: Path, relative_directories: list[Path],
    relative_files: list[tuple[Path, str]],
) -> None:
    """原子改名被 Windows 拒绝时，用外部完成标记保护直接创建。"""
    marker = target.parent / f".{target.name}.incomplete"
    if marker.exists() or target.exists():
        raise ValueError(f"纯目录预览回退目标或标记已存在：{target}")
    marker.write_text(str(target), encoding="utf-8")
    try:
        target.mkdir()
        for relative in relative_directories:
            (target / relative).mkdir(parents=True, exist_ok=True)
        for relative, content in relative_files:
            file_path = target / relative
            file_path.parent.mkdir(parents=True, exist_ok=True)
            with file_path.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
        marker.unlink()
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise


def _captured_items(root: Path) -> dict[Path, bool]:
    """值为 True 表示目录，False 表示文件；文件只参与空目录模拟。"""
    items: dict[Path, bool] = {root: True}
    for current, directories, files in os.walk(root):
        current_path = Path(current).resolve()
        for name in directories:
            items[(current_path / name).resolve()] = True
        # 目录动作只需知道这里是否有文件；保留一个代表即可判断父目录非空。
        if files:
            items[(current_path / files[0]).resolve()] = False
    return items


def _add_parent_directories(
    items: dict[Path, bool], path: Path, root: Path,
) -> None:
    current = path.resolve()
    while _inside(current, root):
        items[current] = True
        if current == root:
            break
        current = current.parent


def simulate_folder_structure(
    manifest: Mapping[str, Any],
) -> list[Path]:
    """按父到子动作顺序返回相对扫描根的最终目录集合。"""
    root = Path(str(manifest["root"])).resolve()
    if not root.is_dir():
        raise ValueError(f"扫描根目录不存在：{root}")
    items = _captured_items(root)
    for entry in manifest.get("entries") or ():
        if entry.get("status") == "可执行" and entry.get("target"):
            source = Path(str(entry.get("runtime_source") or entry["source"])).resolve()
            target = Path(str(entry["target"])).resolve()
            if not _inside(source, root) or not _inside(target, root):
                raise ValueError(f"目录预览动作超出扫描根：{source} -> {target}")
            matching = [
                (path, is_directory)
                for path, is_directory in items.items()
                if path == source or source in path.parents
            ]
            if not matching:
                raise ValueError(f"目录预览找不到运行时源路径：{source}")
            _add_parent_directories(items, target.parent, root)
            for path, _ in matching:
                items.pop(path, None)
            for path, is_directory in matching:
                items[(target / path.relative_to(source)).resolve()] = is_directory
        game_root = Path(str(
            entry.get("target")
            if entry.get("status") == "可执行" and entry.get("target")
            else entry.get("runtime_source") or entry.get("source")
        )).resolve()
        for auxiliary in entry.get("auxiliary_directory_renames") or ():
            source = (game_root / str(auxiliary["source_name"])).resolve()
            target = (game_root / str(auxiliary["target_name"])).resolve()
            matching = [
                (path, is_directory)
                for path, is_directory in items.items()
                if path == source or source in path.parents
            ]
            if not matching:
                raise ValueError(f"目录预览找不到附属目录：{source}")
            for path, _ in matching:
                items.pop(path, None)
            for path, is_directory in matching:
                items[(target / path.relative_to(source)).resolve()] = is_directory
    for raw_parent in manifest.get("remove_empty_parents") or ():
        parent = Path(str(raw_parent)).resolve()
        descendants = [
            path for path in items
            if path != parent and parent in path.parents
        ]
        if not descendants:
            items.pop(parent, None)
    return sorted(
        (
            path.relative_to(root)
            for path, is_directory in items.items()
            if is_directory and path != root
        ),
        key=lambda path: (len(path.parts), str(path).casefold()),
    )


def simulate_project_files(
    manifest: Mapping[str, Any],
) -> list[tuple[Path, str]]:
    """列出项目会新增/恢复的文件相对路径与预览内容，不复制游戏文件。"""
    root = Path(str(manifest["root"])).resolve()
    planned: dict[Path, str] = {}
    for entry in manifest.get("entries") or ():
        destination = Path(str(
            entry.get("target")
            if entry.get("status") == "可执行" and entry.get("target")
            else entry.get("runtime_source") or entry.get("source")
        )).resolve()
        if not _inside(destination, root):
            raise ValueError(f"目录预览文件动作超出扫描根：{destination}")

        marker_name = str(entry.get("marker_name") or "")
        if marker_name:
            if Path(marker_name).name != marker_name:
                raise ValueError(f"非法 TXT 文件名：{marker_name}")
            marker_path = (destination / marker_name).resolve()
            planned[marker_path] = ""

        for shortcut in entry.get("source_shortcuts") or ():
            name = str(shortcut.get("name") or "")
            url = str(shortcut.get("url") or "")
            if (
                not name or Path(name).name != name
                or not name.casefold().endswith(".url")
                or not url.startswith(("http://", "https://"))
                or any(character in url for character in "\r\n")
            ):
                raise ValueError(f"非法来源快捷方式预览动作：{shortcut}")
            shortcut_path = (destination / name).resolve()
            planned[shortcut_path] = f"[InternetShortcut]\nURL={url}\n"

    relative_files: list[tuple[Path, str]] = []
    for path, content in planned.items():
        if not _inside(path, root):
            raise ValueError(f"目录预览文件动作超出扫描根：{path}")
        relative_files.append((path.relative_to(root), content))
    return sorted(relative_files, key=lambda item: str(item[0]).casefold())


def write_folder_structure_preview(
    manifest: Mapping[str, Any],
    destination: str | Path,
) -> Path:
    """原子创建模拟目录树与项目新增文件；已有目的地时拒绝覆盖。"""
    target = Path(destination).resolve()
    root = Path(str(manifest["root"])).resolve()
    if _inside(target, root):
        raise ValueError("纯目录预览必须位于扫描根目录之外")
    if target.exists():
        raise ValueError(f"纯目录预览目的地已存在：{target}")
    summary_path = target.parent / f"{target.name}.json"
    if summary_path.exists():
        raise ValueError(f"目录预览说明文件已存在：{summary_path}")
    relative_directories = simulate_folder_structure(manifest)
    relative_files = simulate_project_files(manifest)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(
        dir=target.parent, prefix=f".{target.name}.",
    )).resolve()
    try:
        for relative in relative_directories:
            (staging / relative).mkdir(parents=True, exist_ok=True)
        for relative, content in relative_files:
            file_path = staging / relative
            file_path.parent.mkdir(parents=True, exist_ok=True)
            with file_path.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
        try:
            _atomic_directory_replace(staging, target)
        except PermissionError:
            _transactional_directory_fallback(
                target, relative_directories, relative_files,
            )
            shutil.rmtree(staging, ignore_errors=True)
        summary = {
            "path": str(target),
            "directory_count": len(relative_directories),
            "file_count": len(relative_files),
            "files": [
                {
                    "path": relative.as_posix(),
                    "kind": (
                        "internet_shortcut"
                        if relative.suffix.casefold() == ".url"
                        else "marker_txt"
                    ),
                    "size_bytes": len(content.encode("utf-8")),
                }
                for relative, content in relative_files
            ],
        }
        summary_tmp = summary_path.with_name(f".{summary_path.name}.tmp")
        summary_tmp.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(summary_tmp, summary_path)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return target
