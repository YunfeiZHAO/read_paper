"""用户附件上传:落盘到 instance/workspace/uploads/,缓存文本类文件的片段。"""
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
    """把用户文件名收敛成安全的单段文件名(替代 werkzeug.secure_filename)。

    去掉路径分隔符与控制字符,折叠空白为下划线,只保留基本名;空结果回退为空串
    (调用方再回退到 "upload")。
    """
    name = (name or "").replace("\\", "/").split("/")[-1]
    name = name.strip().replace("\x00", "")
    # 允许字母数字、点、下划线、连字符;其余(含空白)折叠为下划线。
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


def store_upload(original_name: str, data: bytes, content_type: Optional[str] = None) -> Dict[str, Any]:
    """落盘一个上传文件,写入 files 表并返回其公开表示。

    ``original_name`` 用户原始文件名,``data`` 文件字节,``content_type`` 可选 MIME。
    与具体 Web 框架解耦(FastAPI UploadFile / 任意来源皆可)。
    """
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
        raise RuntimeError("Failed to save file")
    return saved
