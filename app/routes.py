"""所有 HTTP 路由,挂在一个 FastAPI APIRouter 上,由 create_app() 注册。"""
from __future__ import annotations

import mimetypes
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from .config import (
    BACKENDS,
    BASE_DIR,
    CLAUDE_PROJECTS_DIR,
    ENABLE_WEB_SEARCH,
    WORKSPACE_TEXT_PREVIEW_BYTES,
    WORK_DIR,
)
from .claude_history import claude_code_slug, read_claude_code_nodes, resume_from_node
from .db import (
    DB_LOCK,
    append_node_note,
    create_document,
    create_highlight,
    create_node,
    db_connection,
    delete_document,
    delete_highlight,
    get_document,
    get_file,
    get_highlight,
    get_node,
    link_highlight_to_node,
    list_documents,
    list_highlights,
    nodes_for_document,
    nodes_for_project,
    public_node,
    touch_document,
    update_highlight,
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

router = APIRouter()

INDEX_HTML = BASE_DIR / "templates" / "index.html"


@router.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(INDEX_HTML.read_text(encoding="utf-8"))


@router.get("/favicon.ico")
def favicon() -> Response:
    return Response(status_code=204)


@router.get("/api/health")
def health() -> Dict[str, Any]:
    return {"ok": True, "time": now_iso(), "auth_selection": "fixed_backend_auth"}


@router.get("/api/tree")
def api_tree() -> Dict[str, Any]:
    with DB_LOCK, db_connection() as conn:
        node_rows = conn.execute("SELECT * FROM nodes ORDER BY created_at").fetchall()
        file_rows = conn.execute("SELECT * FROM files ORDER BY created_at DESC").fetchall()
    return {
        "nodes": [public_node(row) for row in node_rows],
        "files": rows_to_dicts(file_rows),
        "work_dir": str(WORK_DIR),
    }


@router.get("/api/accounts")
def api_accounts() -> Dict[str, Any]:
    return {"managed_by_backend": True, "selection": "fixed"}


@router.post("/api/claude-code/resume-from-node")
async def api_claude_code_resume_from_node(request: Request) -> JSONResponse:
    payload = await _json_body(request)
    project_path = str(payload.get("path") or "").strip()
    target_uuid = str(payload.get("message_uuid") or "").strip()
    result, status = resume_from_node(project_path, target_uuid)
    return JSONResponse(result, status_code=status)


@router.get("/api/claude-code/tree")
def api_claude_code_tree(path: str = "") -> JSONResponse:
    project_path = (path or "").strip()
    if not project_path:
        raise HTTPException(status_code=400, detail="path 不能为空")
    slug = claude_code_slug(project_path)
    cc_dir = CLAUDE_PROJECTS_DIR / slug
    if not cc_dir.exists() or not cc_dir.is_dir():
        return JSONResponse(
            {"error": f"未找到 Claude Code 记录目录：{cc_dir}", "slug": slug}, status_code=404
        )
    try:
        nodes = read_claude_code_nodes(cc_dir)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": f"读取失败：{exc}"}, status_code=500)
    return JSONResponse(
        {"nodes": nodes, "source_path": project_path, "claude_dir": str(cc_dir)}
    )


@router.post("/api/ask")
async def api_ask(request: Request) -> JSONResponse:
    payload = await _json_body(request)
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt 不能为空")
    parent_id = payload.get("parent_id") or None
    file_ids = payload.get("file_ids") or []
    if not isinstance(file_ids, list):
        raise HTTPException(status_code=400, detail="file_ids 必须是数组")
    workspace_refs_payload = payload.get("workspace_refs") or []
    if not isinstance(workspace_refs_payload, list):
        raise HTTPException(status_code=400, detail="workspace_refs 必须是数组")
    workspace_refs = normalize_workspace_refs(workspace_refs_payload)
    backend = str(payload.get("backend") or "codex").strip() or "codex"
    if backend not in BACKENDS:
        raise HTTPException(status_code=400, detail=f"backend 必须是 {sorted(BACKENDS)} 之一")
    enable_web_search = payload.get("enable_web_search")
    enable_web_search = ENABLE_WEB_SEARCH if enable_web_search is None else bool(enable_web_search)

    document_id = payload.get("document_id") or None
    source_highlight_id = payload.get("source_highlight_id") or None
    # 从父节点继承 document_id(fork 出来的子节点仍属于同一篇 PDF)。
    if not document_id and parent_id:
        parent = get_node(parent_id)
        if parent:
            document_id = parent.get("document_id")

    try:
        node = create_node(
            parent_id,
            prompt,
            file_ids,
            workspace_refs,
            backend=backend,
            enable_web_search=enable_web_search,
            document_id=document_id,
            source_highlight_id=source_highlight_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if source_highlight_id:
        link_highlight_to_node(source_highlight_id, node["id"])
    start_worker(node["id"])
    return JSONResponse({"node": node}, status_code=202)


@router.post("/api/upload")
async def api_upload(files: Optional[List[UploadFile]] = File(None), file: Optional[List[UploadFile]] = File(None)) -> JSONResponse:
    uploaded = files or file or []
    if not uploaded:
        raise HTTPException(status_code=400, detail="没有收到文件")
    saved_files = []
    for item in uploaded:
        if not item.filename:
            continue
        data = await item.read()
        saved_files.append(store_upload(item.filename, data, item.content_type))
    return JSONResponse({"files": saved_files}, status_code=201)


@router.get("/api/files/{file_id}/download")
def api_download_file(file_id: str) -> Any:
    item = get_file(file_id)
    if not item:
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(item["path"], filename=item["original_name"])


# ── documents / highlights ──────────────────────────────────────────────


@router.post("/api/documents")
async def api_create_document(file: UploadFile = File(...), title: str = "", num_pages: Optional[int] = None) -> JSONResponse:
    if not file.filename:
        raise HTTPException(status_code=400, detail="没有收到文件")
    data = await file.read()
    saved = store_upload(file.filename, data, file.content_type)
    doc = create_document(saved["id"], title or saved["original_name"], saved["original_name"], num_pages)
    return JSONResponse({"document": doc}, status_code=201)


@router.get("/api/documents")
def api_list_documents() -> Dict[str, Any]:
    return {"documents": list_documents()}


@router.get("/api/documents/{document_id}")
def api_get_document(document_id: str) -> Dict[str, Any]:
    doc = get_document(document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    touch_document(document_id)
    return {
        "document": doc,
        "highlights": list_highlights(document_id),
        "nodes": nodes_for_document(document_id),
    }


@router.delete("/api/documents/{document_id}")
def api_delete_document(document_id: str) -> Dict[str, Any]:
    if not get_document(document_id):
        raise HTTPException(status_code=404, detail="文档不存在")
    result = delete_document(document_id)
    return {"deleted": True, **result}


@router.get("/api/documents/{document_id}/file")
def api_document_file(document_id: str) -> Any:
    doc = get_document(document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    item = get_file(doc["file_id"])
    if not item:
        raise HTTPException(status_code=404, detail="PDF 文件丢失")
    return FileResponse(
        item["path"],
        media_type=item.get("mime") or "application/pdf",
        filename=item["original_name"],
    )


@router.get("/api/documents/{document_id}/highlights")
def api_list_highlights(document_id: str) -> Dict[str, Any]:
    if not get_document(document_id):
        raise HTTPException(status_code=404, detail="文档不存在")
    return {"highlights": list_highlights(document_id)}


@router.post("/api/documents/{document_id}/highlights")
async def api_create_highlight(document_id: str, request: Request) -> JSONResponse:
    if not get_document(document_id):
        raise HTTPException(status_code=404, detail="文档不存在")
    payload = await _json_body(request)
    rects = payload.get("rects") or []
    if not isinstance(rects, list):
        raise HTTPException(status_code=400, detail="rects 必须是数组")
    try:
        hl = create_highlight(
            document_id,
            int(payload.get("page") or 0),
            rects,
            str(payload.get("quote") or ""),
            str(payload.get("color") or "yellow"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse({"highlight": hl}, status_code=201)


@router.patch("/api/highlights/{highlight_id}")
async def api_patch_highlight(highlight_id: str, request: Request) -> Dict[str, Any]:
    if not get_highlight(highlight_id):
        raise HTTPException(status_code=404, detail="高亮不存在")
    payload = await _json_body(request)
    fields: Dict[str, Any] = {}
    if "color" in payload:
        fields["color"] = str(payload["color"] or "yellow")
    if "quote" in payload:
        fields["quote"] = str(payload["quote"] or "")
    hl = update_highlight(highlight_id, **fields)
    return {"highlight": hl}


@router.delete("/api/highlights/{highlight_id}")
def api_delete_highlight(highlight_id: str) -> Dict[str, Any]:
    if not delete_highlight(highlight_id):
        raise HTTPException(status_code=404, detail="高亮不存在")
    return {"deleted": True}


# ── workspace (保留服务端能力;前端 PDF 模式已不主动调用) ──────────────────


@router.get("/api/workspace/files")
def api_workspace_files(node_id: str = "") -> Dict[str, Any]:
    node_id = (node_id or "").strip() or None
    return {"root": str(WORK_DIR), "files": workspace_files_for_node(node_id)}


@router.get("/api/workspace/content")
def api_workspace_content(path: str = "") -> Dict[str, Any]:
    try:
        resolved = resolve_workspace_file(str(path or ""))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    kind = workspace_kind(resolved)
    if kind not in {"markdown", "text"}:
        raise HTTPException(status_code=415, detail="该文件类型不支持文本预览")

    with resolved.open("rb") as file_obj:
        data = file_obj.read(WORKSPACE_TEXT_PREVIEW_BYTES + 1)
    truncated = len(data) > WORKSPACE_TEXT_PREVIEW_BYTES
    if truncated:
        data = data[:WORKSPACE_TEXT_PREVIEW_BYTES]
    text = data.decode("utf-8", errors="replace")
    return {"file": public_workspace_file(resolved), "content": text, "truncated": truncated}


@router.get("/api/workspace/file")
def api_workspace_file(path: str = "", download: str = "") -> Any:
    try:
        resolved = resolve_workspace_file(str(path or ""))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    is_download = str(download or "") == "1"
    return FileResponse(
        resolved,
        media_type=mimetypes.guess_type(resolved.name)[0] or None,
        filename=resolved.name if is_download else None,
    )


# ── notes / nodes ─────────────────────────────────────────────────────────


@router.patch("/api/nodes/{node_id}/note")
async def api_patch_node_note(node_id: str, request: Request) -> Dict[str, Any]:
    if not get_node(node_id):
        raise HTTPException(status_code=404, detail="节点不存在")
    payload = await _json_body(request)
    note_md = payload.get("note_md", "")
    if not isinstance(note_md, str):
        note_md = str(note_md)
    update_node(node_id, note_md=note_md)
    return {"node": get_node(node_id)}


@router.post("/api/nodes/{node_id}/note/append")
async def api_append_node_note(node_id: str, request: Request) -> Dict[str, Any]:
    payload = await _json_body(request)
    text = payload.get("text", "")
    if not isinstance(text, str):
        text = str(text)
    try:
        node = append_node_note(node_id, text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"node": node}


@router.get("/api/projects/{project_id}/notes")
def api_project_notes(project_id: str) -> Dict[str, Any]:
    project = get_node(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
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
    return {"project": {"id": project["id"], "title": project.get("title") or "项目"}, "notes": notes}


@router.patch("/api/nodes/{node_id}")
async def api_patch_node(node_id: str, request: Request) -> Dict[str, Any]:
    if not get_node(node_id):
        raise HTTPException(status_code=404, detail="节点不存在")
    payload = await _json_body(request)
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
    return {"node": get_node(node_id)}


@router.delete("/api/nodes/{node_id}")
def api_delete_node(node_id: str) -> Dict[str, Any]:
    node = get_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail="节点不存在")
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
    return {"deleted": deleted_count, "parent_id": node.get("parent_id")}


@router.get("/api/export/{node_id}")
def api_export(node_id: str) -> Response:
    try:
        markdown = export_subtree_markdown(node_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    filename = f"codex-tree-{node_id[:8]}.md"
    return Response(
        content=markdown,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


async def _json_body(request: Request) -> Dict[str, Any]:
    """宽容地解析 JSON 请求体,出错时回退为空 dict(对齐旧的 get_json(silent=True))。"""
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}
