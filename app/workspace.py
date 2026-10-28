"""共享 workspace 目录的文件追踪:快照、变更归因、对外视图。"""
from __future__ import annotations

import mimetypes
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .config import (
    MARKDOWN_EXTENSIONS,
    TEXT_PREVIEW_EXTENSIONS,
    WORKSPACE_FILE_LIST_LIMIT,
    WORK_DIR,
)
from .db import DB_LOCK, db_connection, path_to_node, resolve_stored_path
from .utils import now_iso, parse_iso


def path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def workspace_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    mime = mimetypes.guess_type(path.name)[0] or ""
    if suffix in MARKDOWN_EXTENSIONS:
        return "markdown"
    if suffix == ".pdf" or mime == "application/pdf":
        return "pdf"
    if mime.startswith("text/") or suffix in TEXT_PREVIEW_EXTENSIONS:
        return "text"
    return "file"


def public_workspace_file(path: Path, source_node: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    workspace_root = WORK_DIR.resolve()
    stat = path.stat()
    rel_path = path.resolve().relative_to(workspace_root).as_posix()
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    item = {
        "path": rel_path,
        "absolute_path": str(path.resolve()),
        "name": path.name,
        "dir": str(Path(rel_path).parent) if str(Path(rel_path).parent) != "." else "",
        "size": stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "mime": mime,
        "kind": workspace_kind(path),
    }
    if source_node:
        item["source_node_id"] = source_node.get("id")
        item["source_title"] = source_node.get("title") or "Node"
    return item


def public_workspace_ref(path: Path) -> Dict[str, Any]:
    item = public_workspace_file(path)
    return {
        "path": item["path"],
        "absolute_path": item["absolute_path"],
        "name": item["name"],
        "mime": item["mime"],
        "kind": item["kind"],
        "size": item["size"],
    }


def resolve_workspace_file(value: str) -> Path:
    """解析工作目录中的文件路径。

    支持相对路径(相对于WORK_DIR)和绝对路径,返回绝对路径。
    这提供了向后兼容性和向前的可移植性。
    """
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("File path cannot be empty")

    workspace_root = WORK_DIR.resolve()
    candidate = Path(raw)
    path = candidate.resolve() if candidate.is_absolute() else (workspace_root / raw).resolve()

    if not path_is_within(path, workspace_root):
        raise ValueError("File is not inside the workspace")
    if not path.exists() or not path.is_file():
        raise FileNotFoundError("File does not exist")
    return path


def relative_workspace_path(abs_path: Path) -> str:
    """转换绝对路径为相对于WORK_DIR的相对路径。方便迁移和备份。"""
    try:
        return abs_path.resolve().relative_to(WORK_DIR.resolve()).as_posix()
    except ValueError:
        raise ValueError(f"Path {abs_path} is not inside workspace {WORK_DIR}")


def normalize_workspace_refs(values: Iterable[Any]) -> List[Dict[str, Any]]:
    refs: List[Dict[str, Any]] = []
    seen = set()
    for value in values:
        raw = value.get("path") if isinstance(value, dict) else value
        try:
            path = resolve_workspace_file(str(raw or ""))
        except (FileNotFoundError, ValueError):
            continue
        rel_path = path.resolve().relative_to(WORK_DIR.resolve()).as_posix()
        if rel_path in seen:
            continue
        seen.add(rel_path)
        refs.append(public_workspace_ref(path))
    return refs


def workspace_snapshot() -> Dict[str, Tuple[Path, int, float]]:
    workspace_root = WORK_DIR.resolve()
    if not workspace_root.exists():
        return {}

    # Exclude tracked uploads (they have their own attachment chip in the UI), but include
    # everything else — including codex-created files that happen to land under uploads/.
    tracked_uploads: set = set()
    try:
        with DB_LOCK, db_connection() as conn:
            for row in conn.execute("SELECT path FROM files").fetchall():
                try:
                    tracked_uploads.add(Path(resolve_stored_path(row["path"])).resolve())
                except (OSError, ValueError):
                    continue
    except sqlite3.Error:
        pass

    files: Dict[str, Tuple[Path, int, float]] = {}
    for path in workspace_root.rglob("*"):
        if not path.is_file():
            continue
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in tracked_uploads:
            continue
        try:
            stat = resolved.stat()
            rel_path = resolved.relative_to(workspace_root).as_posix()
        except OSError:
            continue
        files[rel_path] = (resolved, stat.st_size, stat.st_mtime)
    return files


def record_workspace_changes(node_id: str, before: Dict[str, Tuple[Path, int, float]]) -> None:
    after = workspace_snapshot()
    changed: List[Tuple[str, str, str, int, float, str]] = []
    for rel_path, (path, size, mtime) in after.items():
        previous = before.get(rel_path)
        if previous and previous[1] == size and abs(previous[2] - mtime) < 0.001:
            continue
        # Store relative path for portability; resolve_workspace_file handles both rel and abs
        try:
            stored_path = path.resolve().relative_to(WORK_DIR.resolve()).as_posix()
        except ValueError:
            stored_path = str(path)
        changed.append((node_id, rel_path, stored_path, size, mtime, now_iso()))

    if not changed:
        return
    with DB_LOCK, db_connection() as conn:
        conn.executemany(
            """
            INSERT INTO node_workspace_files (node_id, rel_path, path, size, mtime, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(node_id, rel_path) DO UPDATE SET
                path = excluded.path,
                size = excluded.size,
                mtime = excluded.mtime,
                created_at = excluded.created_at
            """,
            changed,
        )


def inferred_workspace_files_for_nodes(nodes: List[Dict[str, Any]], known_paths: set[str]) -> List[Dict[str, Any]]:
    snapshot = workspace_snapshot()
    workspace_root = WORK_DIR.resolve()
    inferred: List[Dict[str, Any]] = []
    for rel_path, (path, _size, mtime) in snapshot.items():
        if rel_path in known_paths:
            continue
        absolute = str((workspace_root / rel_path).resolve())
        modified = datetime.fromtimestamp(mtime, timezone.utc)
        source_node = None
        for node in nodes:
            text = f"{node.get('prompt') or ''}\n{node.get('answer') or ''}\n{node.get('error') or ''}"
            if rel_path in text or absolute in text:
                source_node = node
            else:
                start = parse_iso(node.get("created_at"))
                end = parse_iso(node.get("completed_at") or node.get("updated_at"))
                if start <= modified <= end:
                    source_node = node
            if source_node:
                break
        if source_node:
            inferred.append(public_workspace_file(path, source_node))
            known_paths.add(rel_path)
    return inferred


def workspace_files_for_node(node_id: Optional[str]) -> List[Dict[str, Any]]:
    nodes = path_to_node(node_id)
    if not nodes:
        return []
    node_by_id = {node["id"]: node for node in nodes}
    node_ids = list(node_by_id.keys())
    placeholders = ",".join("?" for _ in node_ids)

    rows: List[sqlite3.Row] = []
    if placeholders:
        with DB_LOCK, db_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM node_workspace_files
                WHERE node_id IN ({placeholders})
                ORDER BY mtime DESC
                """,
                node_ids,
            ).fetchall()

    known_paths: set[str] = set()
    items: List[Dict[str, Any]] = []
    for row in rows:
        rel_path = str(row["rel_path"])
        if rel_path in known_paths:
            continue
        known_paths.add(rel_path)
        try:
            path = resolve_workspace_file(rel_path)
        except (FileNotFoundError, ValueError):
            continue
        items.append(public_workspace_file(path, node_by_id.get(row["node_id"])))

    items.extend(inferred_workspace_files_for_nodes(nodes, known_paths))
    items.sort(key=lambda item: item.get("modified_at") or "", reverse=True)
    return items[:WORKSPACE_FILE_LIST_LIMIT]
