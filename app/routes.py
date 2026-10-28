"""所有 HTTP 路由,挂在一个 blueprint 上,由 create_app() 注册。"""
from __future__ import annotations

import mimetypes
from typing import Any, Dict

from flask import Blueprint, Response, jsonify, render_template, request, send_file

from .config import (
    BACKENDS,
    CLAUDE_PROJECTS_DIR,
    WORKSPACE_TEXT_PREVIEW_BYTES,
    WORK_DIR,
)
from .claude_history import claude_code_slug, read_claude_code_nodes, resume_from_node
from .db import (
    DB_LOCK,
    append_node_note,
    create_node,
    db_connection,
    get_file,
    get_node,
    nodes_for_project,
    public_node,
    update_node,
)
from .export import export_subtree_markdown
from .runners import start_worker
from .uploads import store_upload
from .utils import compact_title, now_iso, rows_to_dicts
from .workspace import (
    normalize_workspace_refs,
    public_workspace_file,
    resolve_workspace_file,
    workspace_files_for_node,
    workspace_kind,
)

bp = Blueprint("main", __name__)


@bp.get("/")
def index() -> str:
    return render_template("index.html")


@bp.get("/favicon.ico")
def favicon() -> Response:
    return Response(status=204)


@bp.get("/api/health")
def health() -> Response:
    return jsonify({"ok": True, "time": now_iso(), "auth_selection": "fixed_backend_auth"})


@bp.get("/api/tree")
def api_tree() -> Response:
    with DB_LOCK, db_connection() as conn:
        node_rows = conn.execute("SELECT * FROM nodes ORDER BY created_at").fetchall()
        file_rows = conn.execute("SELECT * FROM files ORDER BY created_at DESC").fetchall()
    return jsonify(
        {
            "nodes": [public_node(row) for row in node_rows],
            "files": rows_to_dicts(file_rows),
            "work_dir": str(WORK_DIR),
        }
    )


@bp.get("/api/accounts")
def api_accounts() -> Response:
    return jsonify({"managed_by_backend": True, "selection": "fixed"})


@bp.post("/api/claude-code/resume-from-node")
def api_claude_code_resume_from_node() -> Response:
    payload = request.get_json(silent=True) or {}
    project_path = str(payload.get("path") or "").strip()
    target_uuid = str(payload.get("message_uuid") or "").strip()
    result, status = resume_from_node(project_path, target_uuid)
    return jsonify(result), status


@bp.get("/api/claude-code/tree")
def api_claude_code_tree() -> Response:
    project_path = (request.args.get("path") or "").strip()
    if not project_path:
        return jsonify({"error": "path 不能为空"}), 400
    slug = claude_code_slug(project_path)
    cc_dir = CLAUDE_PROJECTS_DIR / slug
    if not cc_dir.exists() or not cc_dir.is_dir():
        return jsonify({
            "error": f"未找到 Claude Code 记录目录：{cc_dir}",
            "slug": slug,
        }), 404
    try:
        nodes = read_claude_code_nodes(cc_dir)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"读取失败：{exc}"}), 500
    return jsonify({
        "nodes": nodes,
        "source_path": project_path,
        "claude_dir": str(cc_dir),
    })


@bp.post("/api/ask")
def api_ask() -> Response:
    payload = request.get_json(silent=True) or {}
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        return jsonify({"error": "prompt 不能为空"}), 400
    parent_id = payload.get("parent_id") or None
    file_ids = payload.get("file_ids") or []
    if not isinstance(file_ids, list):
        return jsonify({"error": "file_ids 必须是数组"}), 400
    workspace_refs_payload = payload.get("workspace_refs") or []
    if not isinstance(workspace_refs_payload, list):
        return jsonify({"error": "workspace_refs 必须是数组"}), 400
    workspace_refs = normalize_workspace_refs(workspace_refs_payload)
    backend = str(payload.get("backend") or "codex").strip() or "codex"
    if backend not in BACKENDS:
        return jsonify({"error": f"backend 必须是 {sorted(BACKENDS)} 之一"}), 400
    try:
        node = create_node(parent_id, prompt, file_ids, workspace_refs, backend=backend)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    start_worker(node["id"])
    return jsonify({"node": node}), 202


@bp.post("/api/upload")
def api_upload() -> Response:
    uploaded = request.files.getlist("files")
    if not uploaded:
        uploaded = request.files.getlist("file")
    if not uploaded:
        return jsonify({"error": "没有收到文件"}), 400
    saved_files = [store_upload(item) for item in uploaded if item.filename]
    return jsonify({"files": saved_files}), 201


@bp.get("/api/files/<file_id>/download")
def api_download_file(file_id: str) -> Any:
    item = get_file(file_id)
    if not item:
        return jsonify({"error": "文件不存在"}), 404
    return send_file(item["path"], as_attachment=True, download_name=item["original_name"])


@bp.get("/api/workspace/files")
def api_workspace_files() -> Response:
    node_id = str(request.args.get("node_id") or "").strip() or None
    return jsonify({"root": str(WORK_DIR), "files": workspace_files_for_node(node_id)})


@bp.get("/api/workspace/content")
def api_workspace_content() -> Response:
    try:
        path = resolve_workspace_file(str(request.args.get("path") or ""))
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    kind = workspace_kind(path)
    if kind not in {"markdown", "text"}:
        return jsonify({"error": "该文件类型不支持文本预览"}), 415

    with path.open("rb") as file_obj:
        data = file_obj.read(WORKSPACE_TEXT_PREVIEW_BYTES + 1)
    truncated = len(data) > WORKSPACE_TEXT_PREVIEW_BYTES
    if truncated:
        data = data[:WORKSPACE_TEXT_PREVIEW_BYTES]
    text = data.decode("utf-8", errors="replace")
    return jsonify({"file": public_workspace_file(path), "content": text, "truncated": truncated})


@bp.get("/api/workspace/file")
def api_workspace_file() -> Any:
    try:
        path = resolve_workspace_file(str(request.args.get("path") or ""))
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    download = str(request.args.get("download") or "") == "1"
    return send_file(
        path,
        as_attachment=download,
        download_name=path.name,
        mimetype=mimetypes.guess_type(path.name)[0] or None,
        conditional=True,
    )


@bp.patch("/api/nodes/<node_id>/note")
def api_patch_node_note(node_id: str) -> Response:
    if not get_node(node_id):
        return jsonify({"error": "节点不存在"}), 404
    payload = request.get_json(silent=True) or {}
    note_md = payload.get("note_md", "")
    if not isinstance(note_md, str):
        note_md = str(note_md)
    update_node(node_id, note_md=note_md)
    return jsonify({"node": get_node(node_id)})


@bp.post("/api/nodes/<node_id>/note/append")
def api_append_node_note(node_id: str) -> Response:
    payload = request.get_json(silent=True) or {}
    text = payload.get("text", "")
    if not isinstance(text, str):
        text = str(text)
    try:
        node = append_node_note(node_id, text)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"node": node})


@bp.get("/api/projects/<project_id>/notes")
def api_project_notes(project_id: str) -> Response:
    project = get_node(project_id)
    if not project:
        return jsonify({"error": "项目不存在"}), 404
    notes = [
        {
            "id": node["id"],
            "title": node.get("title") or "节点",
            "note_md": node.get("note_md") or "",
            "created_at": node.get("created_at"),
            "updated_at": node.get("updated_at"),
        }
        for node in nodes_for_project(project_id)
        if str(node.get("note_md") or "").strip()
    ]
    return jsonify({"project": {"id": project["id"], "title": project.get("title") or "项目"}, "notes": notes})


@bp.patch("/api/nodes/<node_id>")
def api_patch_node(node_id: str) -> Response:
    if not get_node(node_id):
        return jsonify({"error": "节点不存在"}), 404
    payload = request.get_json(silent=True) or {}
    updates: Dict[str, Any] = {}
    if "title" in payload:
        title = compact_title(str(payload["title"]), 80)
        if title:
            updates["title"] = title
    if "archived" in payload:
        updates["archived"] = 1 if bool(payload["archived"]) else 0
    if "bookmarked" in payload:
        updates["bookmarked"] = 1 if bool(payload["bookmarked"]) else 0
    update_node(node_id, **updates)
    return jsonify({"node": get_node(node_id)})


@bp.delete("/api/nodes/<node_id>")
def api_delete_node(node_id: str) -> Response:
    node = get_node(node_id)
    if not node:
        return jsonify({"error": "节点不存在"}), 404
    with DB_LOCK, db_connection() as conn:
        count_row = conn.execute(
            """
            WITH RECURSIVE doomed(id) AS (
                SELECT ?
                UNION ALL
                SELECT nodes.id FROM nodes JOIN doomed ON nodes.parent_id = doomed.id
            )
            SELECT COUNT(*) AS count FROM doomed
            """,
            (node_id,),
        ).fetchone()
        deleted_count = int(count_row["count"] if count_row else 0)
        conn.execute(
            """
            WITH RECURSIVE doomed(id) AS (
                SELECT ?
                UNION ALL
                SELECT nodes.id FROM nodes JOIN doomed ON nodes.parent_id = doomed.id
            )
            DELETE FROM node_workspace_files WHERE node_id IN (SELECT id FROM doomed)
            """,
            (node_id,),
        )
        conn.execute(
            """
            WITH RECURSIVE doomed(id) AS (
                SELECT ?
                UNION ALL
                SELECT nodes.id FROM nodes JOIN doomed ON nodes.parent_id = doomed.id
            )
            DELETE FROM nodes WHERE id IN (SELECT id FROM doomed)
            """,
            (node_id,),
        )
    return jsonify({"deleted": deleted_count, "parent_id": node.get("parent_id")})


@bp.get("/api/export/<node_id>")
def api_export(node_id: str) -> Any:
    try:
        markdown = export_subtree_markdown(node_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 404
    filename = f"codex-tree-{node_id[:8]}.md"
    return Response(
        markdown,
        mimetype="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
