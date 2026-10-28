"""File-backed notes for nodes and highlights.

The Markdown body of every note lives on disk as ``NOTES_DIR/<note_id>.md``; the
``notes`` table (created in :func:`app.db.init_db`) only records the relationship
``(kind, target_id) -> file``. There is at most one note per target, so editing is an
upsert. Writes are file-first-then-row so a crash can only ever orphan a file (harmless),
never a row pointing at a missing file (``init_db`` reconciles those on startup anyway).

Kept deliberately thin and import-light: it depends on :mod:`app.db` only for the
connection helpers, so ``db`` never has to import this module at load time.
"""
from __future__ import annotations

import os
import uuid
from typing import Any, Dict, Optional

from .config import NOTES_DIR
from .db import DB_LOCK, db_connection, note_file_path, unlink_note_file
from .utils import now_iso

VALID_KINDS = {"node", "highlight"}


def _write_note_file(rel_path: str, md: str) -> None:
    """Atomically write a note's Markdown body (temp file + os.replace)."""
    path = note_file_path(rel_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(md, encoding="utf-8")
    os.replace(tmp, path)


def _read_note_file(rel_path: str) -> str:
    try:
        return note_file_path(rel_path).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return ""


def _public(row: Any, md: str) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "target_id": row["target_id"],
        "md": md,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def get_note(kind: str, target_id: str) -> Optional[Dict[str, Any]]:
    """Return the note for a target (with its Markdown body), or ``None`` if there is none."""
    with DB_LOCK, db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM notes WHERE kind = ? AND target_id = ?", (kind, target_id)
        ).fetchone()
    if not row:
        return None
    return _public(row, _read_note_file(row["rel_path"]))


def set_note(kind: str, target_id: str, md: str) -> Optional[Dict[str, Any]]:
    """Create or replace a target's note. Empty/whitespace ``md`` deletes the note instead."""
    md = md if isinstance(md, str) else str(md)
    if not md.strip():
        delete_note(kind, target_id)
        return None
    now = now_iso()
    with DB_LOCK, db_connection() as conn:
        row = conn.execute(
            "SELECT id, rel_path, created_at FROM notes WHERE kind = ? AND target_id = ?",
            (kind, target_id),
        ).fetchone()
        if row:
            note_id, rel_path, created_at = row["id"], row["rel_path"], row["created_at"]
            _write_note_file(rel_path, md)
            conn.execute("UPDATE notes SET updated_at = ? WHERE id = ?", (now, note_id))
        else:
            note_id = uuid.uuid4().hex
            rel_path = f"{note_id}.md"
            created_at = now
            _write_note_file(rel_path, md)  # file first; row second
            conn.execute(
                "INSERT INTO notes (id, kind, target_id, rel_path, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (note_id, kind, target_id, rel_path, created_at, now),
            )
    return {
        "id": note_id,
        "kind": kind,
        "target_id": target_id,
        "md": md,
        "created_at": created_at,
        "updated_at": now,
    }


def append_note(kind: str, target_id: str, text: str) -> Dict[str, Any]:
    """Append a block of Markdown to a target's note (creating the note if needed)."""
    addition = (text or "").strip()
    if not addition:
        raise ValueError("Content to append cannot be empty")
    current = get_note(kind, target_id)
    body = current["md"].rstrip() if current else ""
    combined = f"{body}\n\n{addition}" if body else addition
    note = set_note(kind, target_id, combined)
    assert note is not None  # combined is non-empty, so set_note never deletes here
    return note


def delete_note(kind: str, target_id: str) -> bool:
    with DB_LOCK, db_connection() as conn:
        row = conn.execute(
            "SELECT rel_path FROM notes WHERE kind = ? AND target_id = ?", (kind, target_id)
        ).fetchone()
        if not row:
            return False
        conn.execute("DELETE FROM notes WHERE kind = ? AND target_id = ?", (kind, target_id))
        rel_path = row["rel_path"]
    unlink_note_file(rel_path)
    return True
