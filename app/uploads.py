"""User attachment uploads: write files under instance/workspace/uploads/ and cache a
text excerpt for text-like files.
"""
from __future__ import annotations

import mimetypes
import re
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from .config import TEXT_EXCERPT_BYTES, UPLOAD_DIR
from .db import DB_LOCK, db_connection, get_file
from .utils import now_iso


def secure_filename(name: str) -> str:
    """Reduce a user-supplied filename to a safe, single-segment name (our own stand-in
    for werkzeug.secure_filename).

    Strips path separators and control characters, collapses whitespace to underscores, and
    keeps only the basename; an empty result falls back to "" (the caller then uses "upload").
    """
    name = (name or "").replace("\\", "/").split("/")[-1]
    name = name.strip().replace("\x00", "")
    # Allow letters, digits, dot, underscore, hyphen; collapse everything else (incl. whitespace).
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    name = name.strip("._")
    return name


def is_text_like(filename: str, mime: Optional[str]) -> bool:
    if mime and (mime.startswith("text/") or mime in {"application/json", "application/xml"}):
        return True
    return Path(filename).suffix.lower() in {
        ".txt",
        ".md",
        ".rst",
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".json",
        ".toml",
        ".yaml",
        ".yml",
        ".csv",
        ".log",
        ".html",
        ".css",
        ".xml",
        ".tex",
    }


def store_upload(
    original_name: str,
    data: bytes,
    content_type: Optional[str] = None,
    workspace_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Write one uploaded file to disk, insert a `files` row, and return its public dict.

    ``original_name`` is the user's filename, ``data`` the bytes, ``content_type`` an optional
    MIME type, and ``workspace_id`` the owning workspace (None when unowned). This is decoupled
    from the web framework (works with a FastAPI UploadFile or any other source). The path is
    stored relative to WORK_DIR so the data directory stays portable.
    """
    from .config import WORK_DIR

    original_name = original_name or "upload"
    safe_name = secure_filename(original_name) or "upload"
    file_id = uuid.uuid4().hex
    target_dir = UPLOAD_DIR / file_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / safe_name
    target_path.write_bytes(data)

    mime = mimetypes.guess_type(original_name)[0] or content_type
    size = target_path.stat().st_size
    text_excerpt = ""
    if is_text_like(original_name, mime):
        text_excerpt = target_path.read_bytes()[:TEXT_EXCERPT_BYTES].decode(
            "utf-8", errors="replace"
        )

    # Store relative path from WORK_DIR for portability
    rel_path = target_path.relative_to(WORK_DIR).as_posix()

    created_at = now_iso()
    with DB_LOCK, db_connection() as conn:
        conn.execute(
            """
            INSERT INTO files (id, workspace_id, original_name, stored_name, path, mime, size, text_excerpt, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                file_id,
                workspace_id,
                original_name,
                safe_name,
                rel_path,
                mime,
                size,
                text_excerpt,
                created_at,
            ),
        )
    saved = get_file(file_id)
    if not saved:
        raise RuntimeError("Failed to save file")
    return saved
