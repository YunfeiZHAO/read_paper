"""SQLite 数据层:连接、schema、以及 nodes/files/account_usage 的 CRUD。"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional

from .config import DATA_DIR, DB_PATH, RUN_DIR, UPLOAD_DIR, WORK_DIR
from .utils import compact_title, now_iso, rows_to_dicts, safe_json_loads

DB_LOCK = threading.RLock()


def ensure_dirs() -> None:
    for path in (DATA_DIR, WORK_DIR, UPLOAD_DIR, RUN_DIR):
        path.mkdir(parents=True, exist_ok=True)


def connect_db() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def db_connection() -> Iterable[sqlite3.Connection]:
    conn = connect_db()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    ensure_dirs()
    with DB_LOCK, db_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS nodes (
                id TEXT PRIMARY KEY,
                parent_id TEXT,
                title TEXT NOT NULL,
                prompt TEXT NOT NULL,
                answer TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'queued',
                backend TEXT NOT NULL DEFAULT 'codex',
                enable_web_search INTEGER NOT NULL DEFAULT 0,
                account_name TEXT,
                account_path TEXT,
                account_reason TEXT,
                codex_session_id TEXT,
                model TEXT,
                cwd TEXT,
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                tools_count INTEGER NOT NULL DEFAULT 0,
                attachments_json TEXT NOT NULL DEFAULT '[]',
                workspace_refs_json TEXT NOT NULL DEFAULT '[]',
                note_md TEXT NOT NULL DEFAULT '',
                raw_events_json TEXT NOT NULL DEFAULT '[]',
                error TEXT,
                archived INTEGER NOT NULL DEFAULT 0,
                bookmarked INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                FOREIGN KEY(parent_id) REFERENCES nodes(id)
            );

            CREATE TABLE IF NOT EXISTS files (
                id TEXT PRIMARY KEY,
                original_name TEXT NOT NULL,
                stored_name TEXT NOT NULL,
                path TEXT NOT NULL,
                mime TEXT,
                size INTEGER NOT NULL,
                text_excerpt TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS account_usage (
                name TEXT PRIMARY KEY,
                used_tokens INTEGER NOT NULL DEFAULT 0,
                last_node_id TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS account_probe (
                name TEXT PRIMARY KEY,
                auth_file TEXT NOT NULL,
                status TEXT NOT NULL,
                checked_at TEXT NOT NULL,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                answer TEXT
            );

            CREATE TABLE IF NOT EXISTS node_workspace_files (
                node_id TEXT NOT NULL,
                rel_path TEXT NOT NULL,
                path TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime REAL NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(node_id, rel_path),
                FOREIGN KEY(node_id) REFERENCES nodes(id)
            );

            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY,
                file_id TEXT NOT NULL,
                title TEXT NOT NULL,
                original_name TEXT NOT NULL,
                num_pages INTEGER,
                archived INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_opened_at TEXT,
                FOREIGN KEY(file_id) REFERENCES files(id)
            );

            CREATE TABLE IF NOT EXISTS highlights (
                id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                page INTEGER NOT NULL,
                rects_json TEXT NOT NULL DEFAULT '[]',
                quote TEXT NOT NULL DEFAULT '',
                color TEXT NOT NULL DEFAULT 'yellow',
                node_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(document_id) REFERENCES documents(id),
                FOREIGN KEY(node_id) REFERENCES nodes(id)
            );

            CREATE INDEX IF NOT EXISTS idx_nodes_parent_id ON nodes(parent_id);
            CREATE INDEX IF NOT EXISTS idx_nodes_status ON nodes(status);
            CREATE INDEX IF NOT EXISTS idx_files_created_at ON files(created_at);
            CREATE INDEX IF NOT EXISTS idx_node_workspace_files_node_id ON node_workspace_files(node_id);
            CREATE INDEX IF NOT EXISTS idx_highlights_document_id ON highlights(document_id);
            """
        )
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(nodes)").fetchall()}
        if "workspace_refs_json" not in columns:
            conn.execute("ALTER TABLE nodes ADD COLUMN workspace_refs_json TEXT NOT NULL DEFAULT '[]'")
        if "note_md" not in columns:
            conn.execute("ALTER TABLE nodes ADD COLUMN note_md TEXT NOT NULL DEFAULT ''")
        if "backend" not in columns:
            conn.execute("ALTER TABLE nodes ADD COLUMN backend TEXT NOT NULL DEFAULT 'codex'")
        if "enable_web_search" not in columns:
            conn.execute("ALTER TABLE nodes ADD COLUMN enable_web_search INTEGER NOT NULL DEFAULT 0")
        if "document_id" not in columns:
            conn.execute("ALTER TABLE nodes ADD COLUMN document_id TEXT")
        if "source_highlight_id" not in columns:
            conn.execute("ALTER TABLE nodes ADD COLUMN source_highlight_id TEXT")
        # document_id 由上面的 ALTER 补齐,索引必须在列存在之后再建(兼容旧库)。
        conn.execute("CREATE INDEX IF NOT EXISTS idx_nodes_document_id ON nodes(document_id)")


def public_node(row: sqlite3.Row) -> Dict[str, Any]:
    node = dict(row)
    node["attachments"] = safe_json_loads(node.pop("attachments_json", "[]"), [])
    node["workspace_refs"] = safe_json_loads(node.pop("workspace_refs_json", "[]"), [])
    node["raw_events"] = safe_json_loads(node.pop("raw_events_json", "[]"), [])
    for internal_key in ("account_name", "account_path", "account_reason"):
        node.pop(internal_key, None)
    node["archived"] = bool(node["archived"])
    node["bookmarked"] = bool(node["bookmarked"])
    node["enable_web_search"] = bool(node.get("enable_web_search"))
    return node


def get_node(node_id: str) -> Optional[Dict[str, Any]]:
    with DB_LOCK, db_connection() as conn:
        row = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
    return public_node(row) if row else None


def get_file(file_id: str) -> Optional[Dict[str, Any]]:
    with DB_LOCK, db_connection() as conn:
        row = conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
    return dict(row) if row else None


def get_files(file_ids: Iterable[str]) -> List[Dict[str, Any]]:
    ids = [str(file_id) for file_id in file_ids if str(file_id).strip()]
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    with DB_LOCK, db_connection() as conn:
        rows = conn.execute(
            f"SELECT * FROM files WHERE id IN ({placeholders}) ORDER BY created_at",
            ids,
        ).fetchall()
    return rows_to_dicts(rows)


def create_node(
    parent_id: Optional[str],
    prompt: str,
    file_ids: List[str],
    workspace_refs: List[Dict[str, Any]],
    backend: str = "codex",
    enable_web_search: bool = False,
    document_id: Optional[str] = None,
    source_highlight_id: Optional[str] = None,
) -> Dict[str, Any]:
    if parent_id and not get_node(parent_id):
        raise ValueError("Parent node does not exist")

    files = get_files(file_ids)
    attachments = [
        {
            "id": item["id"],
            "name": item["original_name"],
            "path": item["path"],
            "mime": item.get("mime"),
            "size": item["size"],
        }
        for item in files
    ]

    node_id = uuid.uuid4().hex
    created_at = now_iso()
    with DB_LOCK, db_connection() as conn:
        conn.execute(
            """
            INSERT INTO nodes (
                id, parent_id, title, prompt, status, backend, enable_web_search,
                attachments_json, workspace_refs_json, cwd,
                document_id, source_highlight_id,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                node_id,
                parent_id,
                compact_title(prompt),
                prompt,
                backend,
                1 if enable_web_search else 0,
                json.dumps(attachments, ensure_ascii=False),
                json.dumps(workspace_refs, ensure_ascii=False),
                str(WORK_DIR),
                document_id,
                source_highlight_id,
                created_at,
                created_at,
            ),
        )
    node = get_node(node_id)
    if not node:
        raise RuntimeError("Failed to create node")
    return node


def update_node(node_id: str, **fields: Any) -> None:
    allowed = {
        "title",
        "answer",
        "status",
        "account_name",
        "account_path",
        "account_reason",
        "codex_session_id",
        "model",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "tools_count",
        "raw_events_json",
        "note_md",
        "error",
        "archived",
        "bookmarked",
        "completed_at",
        "updated_at",
    }
    payload = {key: value for key, value in fields.items() if key in allowed}
    payload["updated_at"] = payload.get("updated_at") or now_iso()
    if not payload:
        return
    assignments = ", ".join(f"{key} = ?" for key in payload.keys())
    values = list(payload.values()) + [node_id]
    with DB_LOCK, db_connection() as conn:
        conn.execute(f"UPDATE nodes SET {assignments} WHERE id = ?", values)


def nodes_for_project(root_id: str) -> List[Dict[str, Any]]:
    with DB_LOCK, db_connection() as conn:
        rows = conn.execute(
            """
            WITH RECURSIVE project_nodes(id) AS (
                SELECT ?
                UNION ALL
                SELECT nodes.id FROM nodes JOIN project_nodes ON nodes.parent_id = project_nodes.id
            )
            SELECT nodes.* FROM nodes
            JOIN project_nodes ON nodes.id = project_nodes.id
            ORDER BY nodes.created_at
            """,
            (root_id,),
        ).fetchall()
    return [public_node(row) for row in rows]


def path_to_node(node_id: Optional[str]) -> List[Dict[str, Any]]:
    if not node_id:
        return []

    seen = set()
    path: List[Dict[str, Any]] = []
    current = node_id
    with DB_LOCK, db_connection() as conn:
        while current:
            if current in seen:
                break
            seen.add(current)
            row = conn.execute("SELECT * FROM nodes WHERE id = ?", (current,)).fetchone()
            if not row:
                break
            node = public_node(row)
            path.append(node)
            current = node.get("parent_id")
    path.reverse()
    return path


def append_node_note(node_id: str, text: str) -> Dict[str, Any]:
    addition = text.strip()
    if not addition:
        raise ValueError("Content to append cannot be empty")
    with DB_LOCK, db_connection() as conn:
        row = conn.execute("SELECT note_md FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if not row:
            raise ValueError("Node does not exist")
        current = str(row["note_md"] or "").rstrip()
        next_note = f"{current}\n\n{addition}" if current else addition
        conn.execute(
            "UPDATE nodes SET note_md = ?, updated_at = ? WHERE id = ?",
            (next_note, now_iso(), node_id),
        )
    node = get_node(node_id)
    if not node:
        raise ValueError("Node does not exist")
    return node


# ── documents / highlights ──────────────────────────────────────────────

def public_document(row: sqlite3.Row) -> Dict[str, Any]:
    doc = dict(row)
    doc["archived"] = bool(doc.get("archived"))
    return doc


def public_highlight(row: sqlite3.Row) -> Dict[str, Any]:
    item = dict(row)
    item["rects"] = safe_json_loads(item.pop("rects_json", "[]"), [])
    item["page"] = int(item.get("page") or 0)
    return item


def create_document(file_id: str, title: str, original_name: str, num_pages: Optional[int]) -> Dict[str, Any]:
    if not get_file(file_id):
        raise ValueError("File does not exist")
    doc_id = uuid.uuid4().hex
    created_at = now_iso()
    with DB_LOCK, db_connection() as conn:
        conn.execute(
            """
            INSERT INTO documents (id, file_id, title, original_name, num_pages, created_at, updated_at, last_opened_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (doc_id, file_id, title or original_name, original_name, num_pages, created_at, created_at, created_at),
        )
    doc = get_document(doc_id)
    if not doc:
        raise RuntimeError("Failed to create document")
    return doc


def get_document(document_id: str) -> Optional[Dict[str, Any]]:
    with DB_LOCK, db_connection() as conn:
        row = conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
    return public_document(row) if row else None


def list_documents() -> List[Dict[str, Any]]:
    with DB_LOCK, db_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM documents WHERE archived = 0 ORDER BY COALESCE(last_opened_at, created_at) DESC"
        ).fetchall()
    return [public_document(row) for row in rows]


def touch_document(document_id: str, num_pages: Optional[int] = None) -> None:
    fields = {"last_opened_at": now_iso(), "updated_at": now_iso()}
    with DB_LOCK, db_connection() as conn:
        if num_pages is not None:
            conn.execute(
                "UPDATE documents SET num_pages = ?, last_opened_at = ?, updated_at = ? WHERE id = ?",
                (int(num_pages), fields["last_opened_at"], fields["updated_at"], document_id),
            )
        else:
            conn.execute(
                "UPDATE documents SET last_opened_at = ?, updated_at = ? WHERE id = ?",
                (fields["last_opened_at"], fields["updated_at"], document_id),
            )


def delete_document(document_id: str) -> Dict[str, Any]:
    with DB_LOCK, db_connection() as conn:
        conn.execute("DELETE FROM highlights WHERE document_id = ?", (document_id,))
        conn.execute(
            "DELETE FROM node_workspace_files WHERE node_id IN (SELECT id FROM nodes WHERE document_id = ?)",
            (document_id,),
        )
        node_count = conn.execute(
            "SELECT COUNT(*) AS c FROM nodes WHERE document_id = ?", (document_id,)
        ).fetchone()["c"]
        conn.execute("DELETE FROM nodes WHERE document_id = ?", (document_id,))
        conn.execute("DELETE FROM documents WHERE id = ?", (document_id,))
    return {"deleted_nodes": int(node_count)}


def nodes_for_document(document_id: str) -> List[Dict[str, Any]]:
    with DB_LOCK, db_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM nodes WHERE document_id = ? ORDER BY created_at", (document_id,)
        ).fetchall()
    return [public_node(row) for row in rows]


def create_highlight(
    document_id: str,
    page: int,
    rects: List[Dict[str, Any]],
    quote: str,
    color: str = "yellow",
    node_id: Optional[str] = None,
) -> Dict[str, Any]:
    if not get_document(document_id):
        raise ValueError("Document does not exist")
    hl_id = uuid.uuid4().hex
    created_at = now_iso()
    with DB_LOCK, db_connection() as conn:
        conn.execute(
            """
            INSERT INTO highlights (id, document_id, page, rects_json, quote, color, node_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                hl_id,
                document_id,
                int(page),
                json.dumps(rects, ensure_ascii=False),
                quote or "",
                color or "yellow",
                node_id,
                created_at,
                created_at,
            ),
        )
    return get_highlight(hl_id)  # type: ignore[return-value]


def get_highlight(highlight_id: str) -> Optional[Dict[str, Any]]:
    with DB_LOCK, db_connection() as conn:
        row = conn.execute("SELECT * FROM highlights WHERE id = ?", (highlight_id,)).fetchone()
    return public_highlight(row) if row else None


def list_highlights(document_id: str) -> List[Dict[str, Any]]:
    with DB_LOCK, db_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM highlights WHERE document_id = ? ORDER BY page, created_at", (document_id,)
        ).fetchall()
    return [public_highlight(row) for row in rows]


def update_highlight(highlight_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
    allowed = {"color", "quote", "node_id", "rects_json"}
    payload = {key: value for key, value in fields.items() if key in allowed}
    if not payload:
        return get_highlight(highlight_id)
    payload["updated_at"] = now_iso()
    assignments = ", ".join(f"{key} = ?" for key in payload.keys())
    values = list(payload.values()) + [highlight_id]
    with DB_LOCK, db_connection() as conn:
        conn.execute(f"UPDATE highlights SET {assignments} WHERE id = ?", values)
    return get_highlight(highlight_id)


def link_highlight_to_node(highlight_id: str, node_id: str) -> None:
    with DB_LOCK, db_connection() as conn:
        conn.execute(
            "UPDATE highlights SET node_id = ?, updated_at = ? WHERE id = ?",
            (node_id, now_iso(), highlight_id),
        )


def delete_highlight(highlight_id: str) -> bool:
    with DB_LOCK, db_connection() as conn:
        cur = conn.execute("DELETE FROM highlights WHERE id = ?", (highlight_id,))
        return cur.rowcount > 0


def add_account_usage(account_name: str, node_id: str, tokens: int) -> None:
    if not account_name or tokens <= 0:
        return
    with DB_LOCK, db_connection() as conn:
        conn.execute(
            """
            INSERT INTO account_usage (name, used_tokens, last_node_id, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                used_tokens = used_tokens + excluded.used_tokens,
                last_node_id = excluded.last_node_id,
                updated_at = excluded.updated_at
            """,
            (account_name, int(tokens), node_id, now_iso()),
        )


def account_usage_map() -> Dict[str, int]:
    with DB_LOCK, db_connection() as conn:
        rows = conn.execute("SELECT name, used_tokens FROM account_usage").fetchall()
    return {row["name"]: int(row["used_tokens"]) for row in rows}
