"""用户附件上传:落盘到 instance/workspace/uploads/,缓存文本类文件的片段。"""
from __future__ import annotations

import mimetypes
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from werkzeug.utils import secure_filename

from .config import TEXT_EXCERPT_BYTES, UPLOAD_DIR
from .db import DB_LOCK, db_connection, get_file
from .utils import now_iso


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


def store_upload(file_storage: Any) -> Dict[str, Any]:
    original_name = file_storage.filename or "upload"
    safe_name = secure_filename(original_name) or "upload"
    file_id = uuid.uuid4().hex
    target_dir = UPLOAD_DIR / file_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / safe_name
    file_storage.save(target_path)

    mime = mimetypes.guess_type(original_name)[0] or file_storage.mimetype
    size = target_path.stat().st_size
    text_excerpt = ""
    if is_text_like(original_name, mime):
        text_excerpt = target_path.read_bytes()[:TEXT_EXCERPT_BYTES].decode(
            "utf-8", errors="replace"
        )

    created_at = now_iso()
    with DB_LOCK, db_connection() as conn:
        conn.execute(
            """
            INSERT INTO files (id, original_name, stored_name, path, mime, size, text_excerpt, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                file_id,
                original_name,
                safe_name,
                str(target_path),
                mime,
                size,
                text_excerpt,
                created_at,
            ),
        )
    saved = get_file(file_id)
    if not saved:
        raise RuntimeError("文件保存失败")
    return saved
