"""Whole-workspace export / import.

A *workspace* (the ``workspaces`` table — a user project: a group of PDFs, a shared file
drawer, highlights, Q&A trees, and notes) can be exported to a single ``.zip`` and imported
on another machine. Export packs every row the workspace owns plus the backing binaries
(PDFs, shared files, note Markdown bodies, AI scratch files); import recreates the workspace
with freshly generated IDs so it never collides with anything already in the target database.

This is distinct from :mod:`app.export`, which only renders one node subtree as Markdown.

Archive layout::

    manifest.json                      # format_version, counts, metadata
    data.json                          # all relational rows (raw table columns)
    files/<old_file_id>/<stored_name>  # PDF + shared-file binaries
    notes/<old_note_rel_path>          # note Markdown bodies
    scratch/<rel_path>                 # node_workspace_files + node workspace_refs content

The imported zip is **untrusted input**: we cap member count and uncompressed size (zip-bomb
guard), only ever write to paths derived from freshly generated IDs or rel-paths validated to
stay inside WORK_DIR (zip-slip guard), and never trust column names from the archive (every
insert is filtered to a fixed per-table column whitelist).
"""
from __future__ import annotations

import json
import shutil
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import NOTES_DIR, UPLOAD_DIR, WORK_DIR
from .db import (
    DB_LOCK,
    db_connection,
    get_workspace,
    note_file_path,
    resolve_stored_path,
)
from .utils import now_iso, safe_json_loads

FORMAT_VERSION = 1

# ── Import safety caps (defense against malicious archives) ────────────────
MAX_MEMBERS = 100_000
MAX_TOTAL_UNCOMPRESSED_BYTES = 5 * 1024**3   # 5 GiB total across the archive
MAX_MEMBER_UNCOMPRESSED_BYTES = 2 * 1024**3  # 2 GiB for any single member
MAX_DATA_JSON_BYTES = 256 * 1024**2          # 256 MiB of relational JSON

# Fixed column whitelists: an insert only ever uses these keys, never column names
# read from the (untrusted) archive. Must match the schema in app/db.py:init_db.
_WORKSPACE_COLS = [
    "id", "title", "description", "archived", "created_at", "updated_at",
    "last_opened_at", "api_base_url", "api_key", "api_auth_type", "api_model",
]
_DOCUMENT_COLS = [
    "id", "workspace_id", "file_id", "title", "original_name", "num_pages",
    "category_id", "position", "archived", "created_at", "updated_at", "last_opened_at",
]
_CATEGORY_COLS = [
    "id", "workspace_id", "title", "position", "created_at", "updated_at",
]
_FILE_COLS = [
    "id", "workspace_id", "original_name", "stored_name", "path", "mime",
    "size", "text_excerpt", "created_at",
]
_HIGHLIGHT_COLS = [
    "id", "document_id", "page", "rects_json", "quote", "color", "node_id",
    "created_at", "updated_at",
]
_NODE_COLS = [
    "id", "parent_id", "title", "prompt", "answer", "status", "backend",
    "enable_web_search", "codex_session_id", "model", "cwd", "prompt_tokens",
    "completion_tokens", "total_tokens", "tools_count", "attachments_json",
    "workspace_refs_json", "note_md", "raw_events_json", "error", "archived",
    "bookmarked", "created_at", "updated_at", "completed_at", "document_id",
    "source_highlight_id",
]
_NOTE_COLS = ["id", "kind", "target_id", "rel_path", "created_at", "updated_at"]
_NWF_COLS = ["node_id", "rel_path", "path", "size", "mtime", "created_at"]
_SESSION_COLS = [
    "id", "document_id", "workspace_id", "kind", "seconds",
    "started_at", "ended_at", "created_at", "updated_at",
]


class ImportError_(ValueError):
    """Raised when an uploaded archive is malformed or unsafe. The message is user-facing."""


# ── path helpers ───────────────────────────────────────────────────────────

def _safe_rel(rel_path: str, root: Path) -> Optional[Path]:
    """Resolve ``root/rel_path`` and return it only if it stays inside ``root``.

    Rejects absolute paths and ``..`` escapes. Returns None when unsafe, so callers can skip.
    """
    rel = str(rel_path or "").strip()
    if not rel:
        return None
    candidate = Path(rel)
    if candidate.is_absolute():
        return None
    root_res = root.resolve()
    target = (root_res / candidate).resolve()
    try:
        target.relative_to(root_res)
    except ValueError:
        return None
    return target


# ── export ─────────────────────────────────────────────────────────────────

def export_workspace(workspace_id: str) -> Path:
    """Build a portable ``.zip`` of one workspace and return the temp-file path.

    The caller is responsible for deleting the returned file after sending it (the route
    streams it with a background cleanup task). Raises ``ValueError`` if the workspace is gone.
    """
    with DB_LOCK, db_connection() as conn:
        ws_row = conn.execute(
            "SELECT * FROM workspaces WHERE id = ?", (workspace_id,)
        ).fetchone()
        if not ws_row:
            raise ValueError("Workspace does not exist")
        workspace = dict(ws_row)
        workspace.pop("api_key", None)  # never export the secret key

        doc_rows = conn.execute(
            "SELECT * FROM documents WHERE workspace_id = ?", (workspace_id,)
        ).fetchall()
        documents = [dict(r) for r in doc_rows]
        doc_ids = [d["id"] for d in documents]

        categories = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM categories WHERE workspace_id = ?", (workspace_id,)
            ).fetchall()
        ]

        # Mirror delete_workspace()'s file set: the drawer plus every document's PDF.
        file_rows = conn.execute(
            """
            SELECT * FROM files
            WHERE workspace_id = ?
               OR id IN (SELECT file_id FROM documents WHERE workspace_id = ?)
            """,
            (workspace_id, workspace_id),
        ).fetchall()
        files = [dict(r) for r in file_rows]

        highlights: List[Dict[str, Any]] = []
        nodes: List[Dict[str, Any]] = []
        if doc_ids:
            placeholders = ",".join("?" for _ in doc_ids)
            highlights = [
                dict(r)
                for r in conn.execute(
                    f"SELECT * FROM highlights WHERE document_id IN ({placeholders})", doc_ids
                ).fetchall()
            ]
            nodes = [
                dict(r)
                for r in conn.execute(
                    f"SELECT * FROM nodes WHERE document_id IN ({placeholders})", doc_ids
                ).fetchall()
            ]

        node_ids = [n["id"] for n in nodes]
        highlight_ids = [h["id"] for h in highlights]

        notes: List[Dict[str, Any]] = []
        if node_ids:
            ph = ",".join("?" for _ in node_ids)
            notes += [
                dict(r)
                for r in conn.execute(
                    f"SELECT * FROM notes WHERE kind = 'node' AND target_id IN ({ph})", node_ids
                ).fetchall()
            ]
        if highlight_ids:
            ph = ",".join("?" for _ in highlight_ids)
            notes += [
                dict(r)
                for r in conn.execute(
                    f"SELECT * FROM notes WHERE kind = 'highlight' AND target_id IN ({ph})",
                    highlight_ids,
                ).fetchall()
            ]

        node_workspace_files: List[Dict[str, Any]] = []
        if node_ids:
            ph = ",".join("?" for _ in node_ids)
            node_workspace_files = [
                dict(r)
                for r in conn.execute(
                    f"SELECT * FROM node_workspace_files WHERE node_id IN ({ph})", node_ids
                ).fetchall()
            ]

        study_sessions = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM study_sessions WHERE workspace_id = ?", (workspace_id,)
            ).fetchall()
        ]

    data = {
        "format_version": FORMAT_VERSION,
        "workspace": workspace,
        "documents": documents,
        "categories": categories,
        "files": files,
        "highlights": highlights,
        "nodes": nodes,
        "notes": notes,
        "node_workspace_files": node_workspace_files,
        "study_sessions": study_sessions,
    }
    manifest = {
        "format_version": FORMAT_VERSION,
        "app": "pdf-tree-qa",
        "exported_at": now_iso(),
        "workspace_title": workspace.get("title") or "",
        "counts": {
            "documents": len(documents),
            "files": len(files),
            "highlights": len(highlights),
            "nodes": len(nodes),
            "notes": len(notes),
            "scratch_files": len(node_workspace_files),
            "study_sessions": len(study_sessions),
        },
    }

    # Scratch files to bundle: every node_workspace_files row, plus every path referenced
    # by a node's workspace_refs_json (dedup by rel-path).
    scratch_rels: set[str] = {str(r.get("rel_path") or "") for r in node_workspace_files}
    for node in nodes:
        for ref in safe_json_loads(node.get("workspace_refs_json") or "[]", []):
            if isinstance(ref, dict) and ref.get("path"):
                scratch_rels.add(str(ref["path"]))

    tmp = tempfile.NamedTemporaryFile(prefix="ws-export-", suffix=".zip", delete=False)
    tmp.close()
    tmp_path = Path(tmp.name)
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            zf.writestr("data.json", json.dumps(data, ensure_ascii=False))
            # File binaries.
            for item in files:
                src = Path(resolve_stored_path(item["path"]))
                if src.is_file():
                    zf.write(src, f"files/{item['id']}/{item['stored_name']}")
            # Note Markdown bodies.
            for note in notes:
                src = note_file_path(note["rel_path"])
                if src.is_file():
                    zf.write(src, f"notes/{note['rel_path']}")
            # AI scratch files.
            for rel in sorted(scratch_rels):
                src = _safe_rel(rel, WORK_DIR)
                if src and src.is_file():
                    zf.write(src, f"scratch/{rel}")
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return tmp_path


# ── import ───────────────────────────────────────────────────────────────

def _filtered(src: Dict[str, Any], cols: List[str], overrides: Dict[str, Any]) -> Dict[str, Any]:
    """Build an insert row from the whitelisted ``cols`` only, then apply ``overrides``."""
    row = {c: src.get(c) for c in cols}
    row.update(overrides)
    return row


def _insert(conn: Any, table: str, cols: List[str], row: Dict[str, Any]) -> None:
    """Insert ``row`` into ``table``. ``table``/``cols`` are fixed literals, never archive data."""
    collist = ",".join(cols)
    placeholders = ",".join("?" for _ in cols)
    conn.execute(
        f"INSERT INTO {table} ({collist}) VALUES ({placeholders})",
        [row.get(c) for c in cols],
    )


def _validate_archive(zf: zipfile.ZipFile) -> None:
    """Enforce member-count and uncompressed-size caps before reading anything."""
    infos = zf.infolist()
    if len(infos) > MAX_MEMBERS:
        raise ImportError_("Archive has too many entries")
    total = 0
    for info in infos:
        if info.file_size > MAX_MEMBER_UNCOMPRESSED_BYTES:
            raise ImportError_("Archive contains an entry that is too large")
        total += info.file_size
    if total > MAX_TOTAL_UNCOMPRESSED_BYTES:
        raise ImportError_("Archive is too large to import")


def _read_json_member(zf: zipfile.ZipFile, name: str, cap: int) -> Any:
    try:
        info = zf.getinfo(name)
    except KeyError:
        raise ImportError_(f"Archive is missing {name}")
    if info.file_size > cap:
        raise ImportError_(f"{name} is too large")
    try:
        return json.loads(zf.read(name).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ImportError_(f"{name} is not valid JSON")


def _extract_member_to(zf: zipfile.ZipFile, name: str, dest: Path) -> bool:
    """Stream one archive member to ``dest`` (creating parents). Returns False if absent."""
    try:
        info = zf.getinfo(name)
    except KeyError:
        return False
    if info.file_size > MAX_MEMBER_UNCOMPRESSED_BYTES:
        raise ImportError_("Archive contains an entry that is too large")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zf.open(info) as src, dest.open("wb") as out:
        shutil.copyfileobj(src, out)
    return True


def import_workspace(zip_path: Path) -> Dict[str, Any]:
    """Import a workspace archive, creating a fresh workspace with remapped IDs.

    Returns the new workspace's public dict. Raises :class:`ImportError_` (a ``ValueError``)
    with a user-facing message for any malformed or unsafe archive.
    """
    if not zipfile.is_zipfile(zip_path):
        raise ImportError_("File is not a valid .zip archive")

    with zipfile.ZipFile(zip_path) as zf:
        _validate_archive(zf)
        manifest = _read_json_member(zf, "manifest.json", 1 * 1024**2)
        fmt = manifest.get("format_version") if isinstance(manifest, dict) else None
        if fmt != FORMAT_VERSION:
            raise ImportError_(
                f"Unsupported archive version {fmt!r}; this app reads version {FORMAT_VERSION}"
            )
        data = _read_json_member(zf, "data.json", MAX_DATA_JSON_BYTES)
        if not isinstance(data, dict) or not isinstance(data.get("workspace"), dict):
            raise ImportError_("Archive data is malformed")

        def _rows(key: str) -> List[Dict[str, Any]]:
            value = data.get(key) or []
            if not isinstance(value, list):
                raise ImportError_(f"Archive field {key!r} is malformed")
            return [r for r in value if isinstance(r, dict)]

        ws_src = data["workspace"]
        documents = _rows("documents")
        categories = _rows("categories")
        files = _rows("files")
        highlights = _rows("highlights")
        nodes = _rows("nodes")
        notes = _rows("notes")
        nwfs = _rows("node_workspace_files")
        study_sessions = _rows("study_sessions")

        # Pre-generate every new id so the node<->highlight cycle resolves cleanly.
        new_ws_id = uuid.uuid4().hex
        file_map = {f["id"]: uuid.uuid4().hex for f in files if f.get("id")}
        doc_map = {d["id"]: uuid.uuid4().hex for d in documents if d.get("id")}
        cat_map = {c["id"]: uuid.uuid4().hex for c in categories if c.get("id")}
        node_map = {n["id"]: uuid.uuid4().hex for n in nodes if n.get("id")}
        hl_map = {h["id"]: uuid.uuid4().hex for h in highlights if h.get("id")}

        def _map(mapping: Dict[str, str], old: Any) -> Any:
            return mapping.get(old) if old else None

        # 1) Copy file binaries to their new UPLOAD_DIR homes; track new abs paths.
        new_file_abs: Dict[str, str] = {}  # old file id -> new absolute path
        new_file_rel: Dict[str, str] = {}  # old file id -> new rel path (stored in DB)
        for f in files:
            old_id = f.get("id")
            if not old_id:
                continue
            new_id = file_map[old_id]
            stored_name = str(f.get("stored_name") or "file")
            # stored_name comes from secure_filename on export, so it is a single safe segment;
            # strip any path parts defensively anyway.
            stored_name = Path(stored_name).name or "file"
            dest = UPLOAD_DIR / new_id / stored_name
            _extract_member_to(zf, f"files/{old_id}/{stored_name}", dest)
            new_file_abs[old_id] = str(dest.resolve())
            new_file_rel[old_id] = dest.resolve().relative_to(WORK_DIR.resolve()).as_posix()

        # 2) Copy note bodies to fresh <new_note_id>.md files.
        note_new: Dict[str, str] = {}  # old note id -> new note id
        note_rel: Dict[str, str] = {}  # old note id -> new rel path
        for note in notes:
            old_id = note.get("id")
            if not old_id:
                continue
            new_id = uuid.uuid4().hex
            new_rel = f"{new_id}.md"
            note_new[old_id] = new_id
            note_rel[old_id] = new_rel
            _extract_member_to(zf, f"notes/{note.get('rel_path')}", note_file_path(new_rel))

        # 3) Restore AI scratch files (best-effort; never clobber an existing file).
        scratch_rels: set[str] = {str(r.get("rel_path") or "") for r in nwfs}
        for node in nodes:
            for ref in safe_json_loads(node.get("workspace_refs_json") or "[]", []):
                if isinstance(ref, dict) and ref.get("path"):
                    scratch_rels.add(str(ref["path"]))
        for rel in scratch_rels:
            dest = _safe_rel(rel, WORK_DIR)
            if dest is None or dest.exists():
                continue
            _extract_member_to(zf, f"scratch/{rel}", dest)

    # 4) Insert every row in one transaction (DB FK enforcement is off, so order is free).
    created_at = ws_src.get("created_at") or now_iso()
    with DB_LOCK, db_connection() as conn:
        _insert(conn, "workspaces", _WORKSPACE_COLS, _filtered(ws_src, _WORKSPACE_COLS, {
            "id": new_ws_id,
            "created_at": created_at,
            "updated_at": now_iso(),
            "last_opened_at": now_iso(),
            "api_key": None,  # never restore a secret key, even if present in the archive
        }))

        for f in files:
            old_id = f.get("id")
            if not old_id:
                continue
            _insert(conn, "files", _FILE_COLS, _filtered(f, _FILE_COLS, {
                "id": file_map[old_id],
                "workspace_id": new_ws_id,
                "path": new_file_rel.get(old_id, f.get("path")),
            }))

        for c in categories:
            old_id = c.get("id")
            if not old_id:
                continue
            _insert(conn, "categories", _CATEGORY_COLS, _filtered(c, _CATEGORY_COLS, {
                "id": cat_map[old_id],
                "workspace_id": new_ws_id,
            }))

        for d in documents:
            old_id = d.get("id")
            if not old_id:
                continue
            _insert(conn, "documents", _DOCUMENT_COLS, _filtered(d, _DOCUMENT_COLS, {
                "id": doc_map[old_id],
                "workspace_id": new_ws_id,
                "file_id": _map(file_map, d.get("file_id")),
                "category_id": _map(cat_map, d.get("category_id")),
            }))

        for n in nodes:
            old_id = n.get("id")
            if not old_id:
                continue
            attachments = safe_json_loads(n.get("attachments_json") or "[]", [])
            if isinstance(attachments, list):
                for att in attachments:
                    if isinstance(att, dict) and att.get("id") in file_map:
                        old_fid = att["id"]
                        att["id"] = file_map[old_fid]
                        if old_fid in new_file_abs:
                            att["path"] = new_file_abs[old_fid]
            refs = safe_json_loads(n.get("workspace_refs_json") or "[]", [])
            if isinstance(refs, list):
                for ref in refs:
                    if isinstance(ref, dict) and ref.get("path"):
                        ref["absolute_path"] = str((WORK_DIR / ref["path"]).resolve())
            _insert(conn, "nodes", _NODE_COLS, _filtered(n, _NODE_COLS, {
                "id": node_map[old_id],
                "parent_id": _map(node_map, n.get("parent_id")),
                "document_id": _map(doc_map, n.get("document_id")),
                "source_highlight_id": _map(hl_map, n.get("source_highlight_id")),
                "cwd": str(WORK_DIR),
                "attachments_json": json.dumps(attachments, ensure_ascii=False),
                "workspace_refs_json": json.dumps(refs, ensure_ascii=False),
            }))

        for h in highlights:
            old_id = h.get("id")
            if not old_id:
                continue
            _insert(conn, "highlights", _HIGHLIGHT_COLS, _filtered(h, _HIGHLIGHT_COLS, {
                "id": hl_map[old_id],
                "document_id": _map(doc_map, h.get("document_id")),
                "node_id": _map(node_map, h.get("node_id")),
            }))

        for note in notes:
            old_id = note.get("id")
            if not old_id or old_id not in note_new:
                continue
            kind = note.get("kind")
            target_map = node_map if kind == "node" else hl_map
            new_target = _map(target_map, note.get("target_id"))
            if not new_target:
                continue  # orphaned note target; skip
            _insert(conn, "notes", _NOTE_COLS, _filtered(note, _NOTE_COLS, {
                "id": note_new[old_id],
                "target_id": new_target,
                "rel_path": note_rel[old_id],
            }))

        for r in nwfs:
            new_node = _map(node_map, r.get("node_id"))
            if not new_node:
                continue
            _insert(conn, "node_workspace_files", _NWF_COLS, _filtered(r, _NWF_COLS, {
                "node_id": new_node,
                "path": r.get("rel_path"),  # keep portable (rel to WORK_DIR)
            }))

        for s in study_sessions:
            new_doc = _map(doc_map, s.get("document_id"))
            if not new_doc:
                continue  # session for a document that didn't import; skip
            _insert(conn, "study_sessions", _SESSION_COLS, _filtered(s, _SESSION_COLS, {
                "id": uuid.uuid4().hex,
                "document_id": new_doc,
                "workspace_id": new_ws_id,
            }))

    workspace = get_workspace(new_ws_id)
    if not workspace:
        raise RuntimeError("Failed to import workspace")
    return workspace
