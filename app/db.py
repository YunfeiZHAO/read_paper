"""SQLite data layer: connection management, schema, and CRUD for
workspaces / documents / highlights / nodes / files.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .config import DATA_DIR, DB_PATH, NOTES_DIR, RUN_DIR, UPLOAD_DIR, WORK_DIR
from .utils import compose_node_title, now_iso, rows_to_dicts, safe_json_loads

DB_LOCK = threading.RLock()


def resolve_stored_path(stored: str) -> str:
    """Resolve a stored path to absolute.

    Stored paths may be relative (new style, relative to WORK_DIR) or absolute
    (legacy rows written before this change). Either way returns an absolute path
    string so callers don't have to care about which format a row uses.
    """
    p = Path(stored)
    if p.is_absolute():
        return stored
    return str(WORK_DIR / p)


def delete_file_from_disk(stored: str) -> None:
    """Remove a stored upload's on-disk directory (``uploads/<file_id>/``).

    Each upload lives in its own ``UPLOAD_DIR/<file_id>/`` directory, so we remove
    that whole directory rather than just the file. Guards against deleting anything
    outside UPLOAD_DIR, and never raises if the path is already gone.
    """
    try:
        resolved = Path(resolve_stored_path(stored)).resolve()
        upload_root = UPLOAD_DIR.resolve()
        file_dir = resolved.parent
        # Only remove the per-file directory when it sits directly under UPLOAD_DIR.
        if file_dir.parent == upload_root and file_dir != upload_root:
            shutil.rmtree(file_dir, ignore_errors=True)
        elif resolved.is_file():
            resolved.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


def ensure_dirs() -> None:
    for path in (DATA_DIR, WORK_DIR, UPLOAD_DIR, RUN_DIR, NOTES_DIR):
        path.mkdir(parents=True, exist_ok=True)


def note_file_path(rel_path: str) -> Path:
    """Absolute path of a note's Markdown file (rel_path is relative to NOTES_DIR)."""
    return NOTES_DIR / rel_path


def unlink_note_file(rel_path: str) -> None:
    """Remove a note's backing .md file; never raises if it's already gone."""
    if not rel_path:
        return
    try:
        note_file_path(rel_path).unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


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
                workspace_id TEXT,
                original_name TEXT NOT NULL,
                stored_name TEXT NOT NULL,
                path TEXT NOT NULL,
                mime TEXT,
                size INTEGER NOT NULL,
                text_excerpt TEXT,
                created_at TEXT NOT NULL
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

            -- "workspace" in this table means a user project (a group of PDFs + a shared
            -- file drawer). This is distinct from WORK_DIR / app/workspace.py / /api/workspace/*,
            -- which is the AI's scratch directory.
            CREATE TABLE IF NOT EXISTS workspaces (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                archived INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_opened_at TEXT
            );

            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY,
                workspace_id TEXT,
                file_id TEXT NOT NULL,
                title TEXT NOT NULL,
                original_name TEXT NOT NULL,
                num_pages INTEGER,
                archived INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_opened_at TEXT,
                FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
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

            -- File-backed notes. The Markdown body lives on disk (NOTES_DIR/<rel_path>);
            -- this table only records which node/highlight a note file belongs to. One
            -- note per target (UNIQUE), so editing upserts rather than piling up files.
            CREATE TABLE IF NOT EXISTS notes (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,            -- 'node' | 'highlight'
                target_id TEXT NOT NULL,       -- nodes.id or highlights.id
                rel_path TEXT NOT NULL,        -- path to the .md file, relative to NOTES_DIR
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(kind, target_id)
            );

            CREATE INDEX IF NOT EXISTS idx_nodes_parent_id ON nodes(parent_id);
            CREATE INDEX IF NOT EXISTS idx_nodes_status ON nodes(status);
            CREATE INDEX IF NOT EXISTS idx_files_created_at ON files(created_at);
            CREATE INDEX IF NOT EXISTS idx_node_workspace_files_node_id ON node_workspace_files(node_id);
            CREATE INDEX IF NOT EXISTS idx_highlights_document_id ON highlights(document_id);
            CREATE INDEX IF NOT EXISTS idx_notes_target ON notes(kind, target_id);
            CREATE INDEX IF NOT EXISTS idx_workspaces_archived ON workspaces(archived);
            -- Indexes on documents/files workspace_id are created further below, AFTER the
            -- ALTER migration that guarantees the column exists on old databases.
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
        # document_id is added by the ALTER above; build its index only after the column
        # exists (keeps old databases upgrading cleanly).
        conn.execute("CREATE INDEX IF NOT EXISTS idx_nodes_document_id ON nodes(document_id)")

        # Backfill the workspace_id column (old databases lacked it on documents/files).
        doc_columns = {row["name"] for row in conn.execute("PRAGMA table_info(documents)").fetchall()}
        if "workspace_id" not in doc_columns:
            conn.execute("ALTER TABLE documents ADD COLUMN workspace_id TEXT")
        file_columns = {row["name"] for row in conn.execute("PRAGMA table_info(files)").fetchall()}
        if "workspace_id" not in file_columns:
            conn.execute("ALTER TABLE files ADD COLUMN workspace_id TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_workspace_id ON documents(workspace_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_files_workspace_id ON files(workspace_id)")

        _migrate_node_notes_to_files(conn)
        _reconcile_notes(conn)

        # Attach any orphaned documents/files (no workspace yet) to a "Default" workspace, so
        # existing PDFs survive the introduction of the workspace layer and the home page has content.
        orphan_doc = conn.execute(
            "SELECT 1 FROM documents WHERE workspace_id IS NULL LIMIT 1"
        ).fetchone()
        orphan_file = conn.execute(
            "SELECT 1 FROM files WHERE workspace_id IS NULL LIMIT 1"
        ).fetchone()
        if orphan_doc or orphan_file:
            default_id = _ensure_default_workspace(conn)
            conn.execute(
                "UPDATE documents SET workspace_id = ? WHERE workspace_id IS NULL",
                (default_id,),
            )
            conn.execute(
                "UPDATE files SET workspace_id = ? WHERE workspace_id IS NULL",
                (default_id,),
            )


def _migrate_node_notes_to_files(conn: sqlite3.Connection) -> None:
    """One-time backfill: move any legacy inline ``nodes.note_md`` into a file-backed note.

    The ``note_md`` column is left in place (unread afterwards) so the upgrade is
    non-destructive; new writes go through the notes layer / ``notes`` table.
    """
    rows = conn.execute(
        "SELECT id, note_md FROM nodes WHERE note_md IS NOT NULL AND TRIM(note_md) != ''"
    ).fetchall()
    for row in rows:
        node_id = row["id"]
        already = conn.execute(
            "SELECT 1 FROM notes WHERE kind = 'node' AND target_id = ?", (node_id,)
        ).fetchone()
        if already:
            continue
        note_id = uuid.uuid4().hex
        rel_path = f"{note_id}.md"
        try:
            note_file_path(rel_path).write_text(str(row["note_md"]), encoding="utf-8")
        except OSError:
            continue
        created_at = now_iso()
        conn.execute(
            "INSERT INTO notes (id, kind, target_id, rel_path, created_at, updated_at) "
            "VALUES (?, 'node', ?, ?, ?, ?)",
            (note_id, node_id, rel_path, created_at, created_at),
        )


def _reconcile_notes(conn: sqlite3.Connection) -> None:
    """Drop note rows whose backing file has vanished (e.g. a crash between the two writes)."""
    for row in conn.execute("SELECT id, rel_path FROM notes").fetchall():
        if not note_file_path(row["rel_path"]).exists():
            conn.execute("DELETE FROM notes WHERE id = ?", (row["id"],))


def _ensure_default_workspace(conn: sqlite3.Connection) -> str:
    """Return a default workspace id, creating one if none exists (used during migration/backfill)."""
    row = conn.execute(
        "SELECT id FROM workspaces ORDER BY created_at LIMIT 1"
    ).fetchone()
    if row:
        return str(row["id"])
    ws_id = uuid.uuid4().hex
    created_at = now_iso()
    conn.execute(
        """
        INSERT INTO workspaces (id, title, description, created_at, updated_at, last_opened_at)
        VALUES (?, ?, '', ?, ?, ?)
        """,
        (ws_id, "Default", created_at, created_at, created_at),
    )
    return ws_id


def public_node(row: sqlite3.Row) -> Dict[str, Any]:
    """Turn a nodes row into the JSON-friendly dict the API returns.

    JSON text columns are parsed into real lists, and the integer flags become booleans.
    Legacy account_* columns (if present on an old database) are dropped defensively.
    """
    node = dict(row)
    node["attachments"] = safe_json_loads(node.pop("attachments_json", "[]"), [])
    node["workspace_refs"] = safe_json_loads(node.pop("workspace_refs_json", "[]"), [])
    node["raw_events"] = safe_json_loads(node.pop("raw_events_json", "[]"), [])
    for legacy_key in ("account_name", "account_path", "account_reason"):
        node.pop(legacy_key, None)
    node["archived"] = bool(node["archived"])
    node["bookmarked"] = bool(node["bookmarked"])
    node["enable_web_search"] = bool(node.get("enable_web_search"))
    return node


def _read_note_md(rel_path: Optional[str]) -> str:
    if not rel_path:
        return ""
    try:
        return note_file_path(rel_path).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return ""


def _attach_node_notes(conn: sqlite3.Connection, nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Fill each node's ``note_md`` from its file-backed note (empty string when none).

    Done in one batched query over the ``notes`` table rather than per node, so a big
    tree costs a single round-trip plus one small file read per annotated node.
    """
    if not nodes:
        return nodes
    rel_by_target: Dict[str, str] = {}
    ids = [n["id"] for n in nodes]
    chunk = 500
    for start in range(0, len(ids), chunk):
        part = ids[start : start + chunk]
        placeholders = ",".join("?" * len(part))
        rows = conn.execute(
            f"SELECT target_id, rel_path FROM notes WHERE kind = 'node' AND target_id IN ({placeholders})",
            part,
        ).fetchall()
        for row in rows:
            rel_by_target[row["target_id"]] = row["rel_path"]
    for node in nodes:
        node["note_md"] = _read_note_md(rel_by_target.get(node["id"]))
    return nodes


def get_node(node_id: str) -> Optional[Dict[str, Any]]:
    with DB_LOCK, db_connection() as conn:
        row = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if not row:
            return None
        node = public_node(row)
        _attach_node_notes(conn, [node])
    return node


def _resolve_file_row(row: sqlite3.Row) -> Dict[str, Any]:
    """Convert a files row to a dict, resolving the stored path to absolute."""
    item = dict(row)
    item["path"] = resolve_stored_path(item["path"])
    return item


def get_file(file_id: str) -> Optional[Dict[str, Any]]:
    with DB_LOCK, db_connection() as conn:
        row = conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
    return _resolve_file_row(row) if row else None


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
    return [_resolve_file_row(row) for row in rows]


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

    quote = ""
    if source_highlight_id:
        hl = get_highlight(source_highlight_id)
        quote = (hl or {}).get("quote") or ""

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
                compose_node_title(quote, prompt),
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
        "codex_session_id",
        "model",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "tools_count",
        "raw_events_json",
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
        return _attach_node_notes(conn, [public_node(row) for row in rows])


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


# ── workspaces (a user project: a group of PDFs + a shared file drawer) ────

def public_workspace(row: sqlite3.Row) -> Dict[str, Any]:
    ws = dict(row)
    ws["archived"] = bool(ws.get("archived"))
    return ws


def create_workspace(title: str, description: str = "") -> Dict[str, Any]:
    ws_id = uuid.uuid4().hex
    created_at = now_iso()
    with DB_LOCK, db_connection() as conn:
        conn.execute(
            """
            INSERT INTO workspaces (id, title, description, created_at, updated_at, last_opened_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (ws_id, (title or "Untitled workspace").strip(), description or "", created_at, created_at, created_at),
        )
    ws = get_workspace(ws_id)
    if not ws:
        raise RuntimeError("Failed to create workspace")
    return ws


def get_workspace(workspace_id: str) -> Optional[Dict[str, Any]]:
    with DB_LOCK, db_connection() as conn:
        row = conn.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
    return public_workspace(row) if row else None


def list_workspaces(include_archived: bool = False) -> List[Dict[str, Any]]:
    """List workspaces ordered by most-recently-opened, each annotated with its PDF count."""
    where = "" if include_archived else "WHERE w.archived = 0"
    with DB_LOCK, db_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT w.*,
                   (SELECT COUNT(*) FROM documents d
                     WHERE d.workspace_id = w.id AND d.archived = 0) AS document_count
            FROM workspaces w
            {where}
            ORDER BY COALESCE(w.last_opened_at, w.created_at) DESC
            """
        ).fetchall()
    out = []
    for row in rows:
        ws = public_workspace(row)
        ws["document_count"] = int(ws.get("document_count") or 0)
        out.append(ws)
    return out


def update_workspace(workspace_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
    allowed = {"title", "description", "archived"}
    payload = {key: value for key, value in fields.items() if key in allowed}
    if not payload:
        return get_workspace(workspace_id)
    payload["updated_at"] = now_iso()
    assignments = ", ".join(f"{key} = ?" for key in payload.keys())
    values = list(payload.values()) + [workspace_id]
    with DB_LOCK, db_connection() as conn:
        conn.execute(f"UPDATE workspaces SET {assignments} WHERE id = ?", values)
    return get_workspace(workspace_id)


def touch_workspace(workspace_id: str) -> None:
    with DB_LOCK, db_connection() as conn:
        conn.execute(
            "UPDATE workspaces SET last_opened_at = ?, updated_at = ? WHERE id = ?",
            (now_iso(), now_iso(), workspace_id),
        )


def delete_workspace(workspace_id: str) -> Dict[str, Any]:
    """Delete a workspace and all its PDFs (their trees, highlights, attachment rows) and shared files."""
    with DB_LOCK, db_connection() as conn:
        doc_ids = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM documents WHERE workspace_id = ?", (workspace_id,)
            ).fetchall()
        ]
        # Collect stored paths for every file the workspace owns (shared files and the
        # PDFs its documents point at) so we can remove them from disk after the commit.
        file_paths = [
            r["path"]
            for r in conn.execute(
                """
                SELECT path FROM files
                WHERE workspace_id = ?
                   OR id IN (SELECT file_id FROM documents WHERE workspace_id = ?)
                """,
                (workspace_id, workspace_id),
            ).fetchall()
        ]
        note_paths: List[str] = []
        node_count = 0
        for doc_id in doc_ids:
            node_count += conn.execute(
                "SELECT COUNT(*) AS c FROM nodes WHERE document_id = ?", (doc_id,)
            ).fetchone()["c"]
            note_paths += _purge_notes(
                conn, "node", "SELECT id FROM nodes WHERE document_id = ?", (doc_id,)
            )
            note_paths += _purge_notes(
                conn, "highlight", "SELECT id FROM highlights WHERE document_id = ?", (doc_id,)
            )
            conn.execute("DELETE FROM highlights WHERE document_id = ?", (doc_id,))
            conn.execute(
                "DELETE FROM node_workspace_files WHERE node_id IN (SELECT id FROM nodes WHERE document_id = ?)",
                (doc_id,),
            )
            conn.execute("DELETE FROM nodes WHERE document_id = ?", (doc_id,))
        conn.execute(
            "DELETE FROM files WHERE workspace_id = ? OR id IN (SELECT file_id FROM documents WHERE workspace_id = ?)",
            (workspace_id, workspace_id),
        )
        conn.execute("DELETE FROM documents WHERE workspace_id = ?", (workspace_id,))
        conn.execute("DELETE FROM workspaces WHERE id = ?", (workspace_id,))
    for stored in file_paths:
        delete_file_from_disk(stored)
    for rel in note_paths:
        unlink_note_file(rel)
    return {"deleted_documents": len(doc_ids), "deleted_nodes": int(node_count)}


def list_files_for_workspace(workspace_id: str) -> List[Dict[str, Any]]:
    with DB_LOCK, db_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM files WHERE workspace_id = ? ORDER BY created_at DESC",
            (workspace_id,),
        ).fetchall()
    return [_resolve_file_row(row) for row in rows]


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


def create_document(
    file_id: str,
    title: str,
    original_name: str,
    num_pages: Optional[int],
    workspace_id: Optional[str] = None,
) -> Dict[str, Any]:
    if not get_file(file_id):
        raise ValueError("File does not exist")
    if workspace_id and not get_workspace(workspace_id):
        raise ValueError("Workspace does not exist")
    doc_id = uuid.uuid4().hex
    created_at = now_iso()
    with DB_LOCK, db_connection() as conn:
        conn.execute(
            """
            INSERT INTO documents (id, workspace_id, file_id, title, original_name, num_pages, created_at, updated_at, last_opened_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (doc_id, workspace_id, file_id, title or original_name, original_name, num_pages, created_at, created_at, created_at),
        )
    doc = get_document(doc_id)
    if not doc:
        raise RuntimeError("Failed to create document")
    return doc


def get_document(document_id: str) -> Optional[Dict[str, Any]]:
    with DB_LOCK, db_connection() as conn:
        row = conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
    return public_document(row) if row else None


def list_documents(workspace_id: Optional[str] = None) -> List[Dict[str, Any]]:
    with DB_LOCK, db_connection() as conn:
        if workspace_id:
            rows = conn.execute(
                "SELECT * FROM documents WHERE archived = 0 AND workspace_id = ? "
                "ORDER BY COALESCE(last_opened_at, created_at) DESC",
                (workspace_id,),
            ).fetchall()
        else:
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
    file_paths: List[str] = []
    with DB_LOCK, db_connection() as conn:
        doc = conn.execute(
            "SELECT file_id FROM documents WHERE id = ?", (document_id,)
        ).fetchone()
        note_paths = _purge_notes(
            conn, "node", "SELECT id FROM nodes WHERE document_id = ?", (document_id,)
        )
        note_paths += _purge_notes(
            conn, "highlight", "SELECT id FROM highlights WHERE document_id = ?", (document_id,)
        )
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
        # Remove the backing PDF file too, unless some other document still points at it.
        file_id = doc["file_id"] if doc else None
        if file_id:
            still_used = conn.execute(
                "SELECT 1 FROM documents WHERE file_id = ? LIMIT 1", (file_id,)
            ).fetchone()
            if not still_used:
                file_paths = [
                    r["path"]
                    for r in conn.execute(
                        "SELECT path FROM files WHERE id = ?", (file_id,)
                    ).fetchall()
                ]
                conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
    for stored in file_paths:
        delete_file_from_disk(stored)
    for rel in note_paths:
        unlink_note_file(rel)
    return {"deleted_nodes": int(node_count)}


def nodes_for_document(document_id: str) -> List[Dict[str, Any]]:
    with DB_LOCK, db_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM nodes WHERE document_id = ? ORDER BY created_at", (document_id,)
        ).fetchall()
        return _attach_node_notes(conn, [public_node(row) for row in rows])


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
        if not row:
            return None
        hl = public_highlight(row)
        hl["has_note"] = (
            conn.execute(
                "SELECT 1 FROM notes WHERE kind = 'highlight' AND target_id = ?", (highlight_id,)
            ).fetchone()
            is not None
        )
    return hl


def list_highlights(document_id: str) -> List[Dict[str, Any]]:
    with DB_LOCK, db_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM highlights WHERE document_id = ? ORDER BY page, created_at", (document_id,)
        ).fetchall()
        hls = [public_highlight(row) for row in rows]
        annotated = {
            row["target_id"]
            for row in conn.execute(
                """
                SELECT target_id FROM notes
                WHERE kind = 'highlight'
                  AND target_id IN (SELECT id FROM highlights WHERE document_id = ?)
                """,
                (document_id,),
            ).fetchall()
        }
    for hl in hls:
        hl["has_note"] = hl["id"] in annotated
    return hls


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


def _purge_notes(conn: sqlite3.Connection, kind: str, target_subquery: str, params: tuple) -> List[str]:
    """Within an open transaction, delete note rows whose target matches ``target_subquery``
    and return their file paths (unlink them from disk after the commit)."""
    rows = conn.execute(
        f"SELECT rel_path FROM notes WHERE kind = ? AND target_id IN ({target_subquery})",
        (kind, *params),
    ).fetchall()
    rel_paths = [row["rel_path"] for row in rows]
    if rel_paths:
        conn.execute(
            f"DELETE FROM notes WHERE kind = ? AND target_id IN ({target_subquery})",
            (kind, *params),
        )
    return rel_paths


def delete_highlight(highlight_id: str) -> bool:
    with DB_LOCK, db_connection() as conn:
        note_paths = _purge_notes(conn, "highlight", "?", (highlight_id,))
        # Drop the forward link from any node that was asked from this highlight so it
        # doesn't dangle (the node itself is independent and stays).
        conn.execute(
            "UPDATE nodes SET source_highlight_id = NULL WHERE source_highlight_id = ?",
            (highlight_id,),
        )
        cur = conn.execute("DELETE FROM highlights WHERE id = ?", (highlight_id,))
        deleted = cur.rowcount > 0
    for rel in note_paths:
        unlink_note_file(rel)
    return deleted
