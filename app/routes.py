"""All HTTP routes, attached to a single FastAPI APIRouter and registered by create_app()."""
from __future__ import annotations

import mimetypes
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from .config import (
    BACKENDS,
    BASE_DIR,
    ENABLE_WEB_SEARCH,
    WORKSPACE_TEXT_PREVIEW_BYTES,
    WORK_DIR,
)
from . import notes
from .db import (
    DB_LOCK,
    create_document,
    create_highlight,
    create_node,
    create_workspace,
    db_connection,
    delete_document,
    delete_highlight,
    delete_workspace,
    get_document,
    get_file,
    get_highlight,
    get_node,
    get_workspace,
    link_highlight_to_node,
    list_documents,
    list_files_for_workspace,
    list_highlights,
    list_workspaces,
    nodes_for_document,
    nodes_for_project,
    public_node,
    touch_document,
    touch_workspace,
    unlink_note_file,
    update_highlight,
    update_node,
    update_workspace,
)
from .export import export_subtree_markdown
from .runners import start_worker
from .uploads import store_upload
from .utils import compact_title, rows_to_dicts
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


@router.get("/api/tree")
def api_tree() -> Dict[str, Any]:
    """Return every node and file in the database (the frontend's initial tree load)."""
    with DB_LOCK, db_connection() as conn:
        node_rows = conn.execute("SELECT * FROM nodes ORDER BY created_at").fetchall()
        file_rows = conn.execute("SELECT * FROM files ORDER BY created_at DESC").fetchall()
    return {
        "nodes": [public_node(row) for row in node_rows],
        "files": rows_to_dicts(file_rows),
        "work_dir": str(WORK_DIR),
    }


@router.post("/api/ask")
async def api_ask(request: Request) -> JSONResponse:
    """Create a queued Q&A node and kick off a background worker to answer it.

    Returns 202 immediately with the created node; the answer streams in later and the
    frontend polls /api/tree to see the node move queued → running → done/failed.
    """
    payload = await _json_body(request)
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")
    parent_id = payload.get("parent_id") or None
    file_ids = payload.get("file_ids") or []
    if not isinstance(file_ids, list):
        raise HTTPException(status_code=400, detail="file_ids must be an array")
    workspace_refs_payload = payload.get("workspace_refs") or []
    if not isinstance(workspace_refs_payload, list):
        raise HTTPException(status_code=400, detail="workspace_refs must be an array")
    workspace_refs = normalize_workspace_refs(workspace_refs_payload)
    backend = str(payload.get("backend") or "claude").strip() or "claude"
    if backend not in BACKENDS:
        raise HTTPException(status_code=400, detail=f"backend must be one of {sorted(BACKENDS)}")
    enable_web_search = payload.get("enable_web_search")
    enable_web_search = ENABLE_WEB_SEARCH if enable_web_search is None else bool(enable_web_search)

    document_id = payload.get("document_id") or None
    source_highlight_id = payload.get("source_highlight_id") or None
    # Inherit document_id from the parent: a forked child still belongs to the same PDF.
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
async def api_upload(
    files: Optional[List[UploadFile]] = File(None),
    file: Optional[List[UploadFile]] = File(None),
    workspace_id: str = Form(""),
) -> JSONResponse:
    uploaded = files or file or []
    if not uploaded:
        raise HTTPException(status_code=400, detail="No file received")
    workspace_id = (workspace_id or "").strip() or None
    if workspace_id and not get_workspace(workspace_id):
        raise HTTPException(status_code=400, detail="Workspace does not exist")
    saved_files = []
    for item in uploaded:
        if not item.filename:
            continue
        data = await item.read()
        saved_files.append(store_upload(item.filename, data, item.content_type, workspace_id=workspace_id))
    return JSONResponse({"files": saved_files}, status_code=201)


@router.get("/api/files/{file_id}/download")
def api_download_file(file_id: str) -> Any:
    item = get_file(file_id)
    if not item:
        raise HTTPException(status_code=404, detail="File does not exist")
    return FileResponse(item["path"], filename=item["original_name"])


# ── workspaces (a user project: a group of PDFs + a shared file drawer) ───


@router.get("/api/workspaces")
def api_list_workspaces(include_archived: str = "") -> Dict[str, Any]:
    return {"workspaces": list_workspaces(include_archived=str(include_archived or "") == "1")}


@router.post("/api/workspaces")
async def api_create_workspace(request: Request) -> JSONResponse:
    payload = await _json_body(request)
    title = str(payload.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title is required")
    description = str(payload.get("description") or "")
    ws = create_workspace(title, description)
    return JSONResponse({"workspace": ws}, status_code=201)


@router.get("/api/workspaces/{workspace_id}")
def api_get_workspace(workspace_id: str) -> Dict[str, Any]:
    ws = get_workspace(workspace_id)
    if not ws:
        raise HTTPException(status_code=404, detail="Workspace does not exist")
    touch_workspace(workspace_id)
    return {
        "workspace": ws,
        "documents": list_documents(workspace_id),
        "files": list_files_for_workspace(workspace_id),
    }


@router.patch("/api/workspaces/{workspace_id}")
async def api_patch_workspace(workspace_id: str, request: Request) -> Dict[str, Any]:
    if not get_workspace(workspace_id):
        raise HTTPException(status_code=404, detail="Workspace does not exist")
    payload = await _json_body(request)
    fields: Dict[str, Any] = {}
    if "title" in payload:
        title = str(payload["title"] or "").strip()
        if not title:
            raise HTTPException(status_code=400, detail="title cannot be empty")
        fields["title"] = title
    if "description" in payload:
        fields["description"] = str(payload["description"] or "")
    if "archived" in payload:
        fields["archived"] = 1 if payload["archived"] else 0
    ws = update_workspace(workspace_id, **fields)
    return {"workspace": ws}


@router.delete("/api/workspaces/{workspace_id}")
def api_delete_workspace(workspace_id: str) -> Dict[str, Any]:
    if not get_workspace(workspace_id):
        raise HTTPException(status_code=404, detail="Workspace does not exist")
    result = delete_workspace(workspace_id)
    return {"deleted": True, **result}


# ── documents / highlights ──────────────────────────────────────────────


@router.post("/api/documents")
async def api_create_document(
    file: UploadFile = File(...),
    title: str = Form(""),
    num_pages: Optional[int] = Form(None),
    workspace_id: str = Form(""),
) -> JSONResponse:
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file received")
    workspace_id = (workspace_id or "").strip() or None
    if workspace_id and not get_workspace(workspace_id):
        raise HTTPException(status_code=400, detail="Workspace does not exist")
    data = await file.read()
    saved = store_upload(file.filename, data, file.content_type, workspace_id=workspace_id)
    try:
        doc = create_document(
            saved["id"], title or saved["original_name"], saved["original_name"], num_pages, workspace_id=workspace_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse({"document": doc}, status_code=201)


@router.get("/api/documents")
def api_list_documents(workspace_id: str = "") -> Dict[str, Any]:
    return {"documents": list_documents((workspace_id or "").strip() or None)}


@router.get("/api/documents/{document_id}")
def api_get_document(document_id: str) -> Dict[str, Any]:
    doc = get_document(document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document does not exist")
    touch_document(document_id)
    return {
        "document": doc,
        "highlights": list_highlights(document_id),
        "nodes": nodes_for_document(document_id),
    }


@router.delete("/api/documents/{document_id}")
def api_delete_document(document_id: str) -> Dict[str, Any]:
    if not get_document(document_id):
        raise HTTPException(status_code=404, detail="Document does not exist")
    result = delete_document(document_id)
    return {"deleted": True, **result}


@router.get("/api/documents/{document_id}/file")
def api_document_file(document_id: str) -> Any:
    doc = get_document(document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document does not exist")
    item = get_file(doc["file_id"])
    if not item:
        raise HTTPException(status_code=404, detail="PDF file is missing")
    return FileResponse(
        item["path"],
        media_type=item.get("mime") or "application/pdf",
        filename=item["original_name"],
    )


@router.get("/api/documents/{document_id}/highlights")
def api_list_highlights(document_id: str) -> Dict[str, Any]:
    if not get_document(document_id):
        raise HTTPException(status_code=404, detail="Document does not exist")
    return {"highlights": list_highlights(document_id)}


@router.post("/api/documents/{document_id}/highlights")
async def api_create_highlight(document_id: str, request: Request) -> JSONResponse:
    if not get_document(document_id):
        raise HTTPException(status_code=404, detail="Document does not exist")
    payload = await _json_body(request)
    rects = payload.get("rects") or []
    if not isinstance(rects, list):
        raise HTTPException(status_code=400, detail="rects must be an array")
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
        raise HTTPException(status_code=404, detail="Highlight does not exist")
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
        raise HTTPException(status_code=404, detail="Highlight does not exist")
    return {"deleted": True}


# ── workspace scratch dir (files the AI read/wrote under WORK_DIR; shown in the side tab) ──


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
        raise HTTPException(status_code=415, detail="This file type does not support text preview")

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
        raise HTTPException(status_code=404, detail="Node does not exist")
    payload = await _json_body(request)
    note_md = payload.get("note_md", "")
    if not isinstance(note_md, str):
        note_md = str(note_md)
    notes.set_note("node", node_id, note_md)
    return {"node": get_node(node_id)}


@router.post("/api/nodes/{node_id}/note/append")
async def api_append_node_note(node_id: str, request: Request) -> Dict[str, Any]:
    if not get_node(node_id):
        raise HTTPException(status_code=404, detail="Node does not exist")
    payload = await _json_body(request)
    text = payload.get("text", "")
    if not isinstance(text, str):
        text = str(text)
    try:
        notes.append_note("node", node_id, text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"node": get_node(node_id)}


# ── notes (file-backed Markdown; one per node/highlight) ────────────────────


def _require_note_target(kind: str, target_id: str) -> None:
    """Validate the note kind and that its target (node or highlight) exists."""
    if kind not in notes.VALID_KINDS:
        raise HTTPException(status_code=404, detail="Unknown note target type")
    exists = get_node(target_id) if kind == "node" else get_highlight(target_id)
    if not exists:
        raise HTTPException(status_code=404, detail=f"{kind.capitalize()} does not exist")


@router.get("/api/notes/{kind}/{target_id}")
def api_get_note(kind: str, target_id: str) -> Dict[str, Any]:
    _require_note_target(kind, target_id)
    return {"note": notes.get_note(kind, target_id)}


@router.put("/api/notes/{kind}/{target_id}")
async def api_put_note(kind: str, target_id: str, request: Request) -> Dict[str, Any]:
    _require_note_target(kind, target_id)
    payload = await _json_body(request)
    md = payload.get("md", "")
    if not isinstance(md, str):
        md = str(md)
    return {"note": notes.set_note(kind, target_id, md)}


@router.delete("/api/notes/{kind}/{target_id}")
def api_delete_note(kind: str, target_id: str) -> Dict[str, Any]:
    _require_note_target(kind, target_id)
    return {"deleted": notes.delete_note(kind, target_id)}


@router.get("/api/projects/{project_id}/notes")
def api_project_notes(project_id: str) -> Dict[str, Any]:
    project = get_node(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project does not exist")
    notes = [
        {
            "id": node["id"],
            "title": node.get("title") or "Node",
            "note_md": node.get("note_md") or "",
            "created_at": node.get("created_at"),
            "updated_at": node.get("updated_at"),
        }
        for node in nodes_for_project(project_id)
        if str(node.get("note_md") or "").strip()
    ]
    return {"project": {"id": project["id"], "title": project.get("title") or "Project"}, "notes": notes}


@router.get("/api/documents/{document_id}/notes")
def api_document_notes(document_id: str) -> Dict[str, Any]:
    """Every marked part of a PDF — node notes, highlight notes, and the bare highlights
    with no note yet — each carrying the highlight it anchors to (a node note anchors to
    the node's source highlight) so the reader can jump the PDF viewer to where it was
    marked. Items without a note carry an empty ``md`` and ``has_note=False`` so the list
    reads as the full record of interesting parts marked in the paper. Ordered by page,
    then time."""
    document = get_document(document_id)
    if not document:
        raise HTTPException(status_code=404, detail="Document does not exist")

    highlights_by_id = {hl["id"]: hl for hl in list_highlights(document_id)}

    def _loc(hl: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Page/rect anchor for a highlight (page None sorts such notes last)."""
        if not hl:
            return {"highlight_id": None, "page": None, "quote": None, "color": None}
        return {
            "highlight_id": hl["id"],
            "page": hl.get("page"),
            "quote": hl.get("quote") or "",
            "color": hl.get("color"),
        }

    items: List[Dict[str, Any]] = []
    # Node notes. A node's source highlight is "represented" by its node row, so we don't
    # also emit a bare highlight row for it below.
    covered_highlight_ids: set = set()
    for node in nodes_for_document(document_id):
        md = str(node.get("note_md") or "").strip()
        hl = highlights_by_id.get(node.get("source_highlight_id"))
        if hl:
            covered_highlight_ids.add(hl["id"])
        # A node with neither a note nor a source highlight is just a bare question with
        # nothing to anchor to; skip it (its highlight, if any, is listed on its own).
        if not md and not hl:
            continue
        items.append({
            "kind": "node",
            "target_id": node["id"],
            "node_id": node["id"],
            "title": node.get("title") or "Node",
            "md": md,
            "has_note": bool(md),
            "updated_at": node.get("updated_at") or node.get("created_at"),
            **_loc(hl),
        })
    # Every highlight that isn't already shown via its node row — with its note if it has
    # one, otherwise as a bare marked part (empty md, has_note=False).
    for hl in highlights_by_id.values():
        if hl["id"] in covered_highlight_ids:
            continue
        md = ""
        updated_at = hl.get("created_at")
        if hl.get("has_note"):
            note = notes.get_note("highlight", hl["id"])
            md = str((note or {}).get("md") or "").strip()
            updated_at = (note or {}).get("updated_at") or updated_at
        items.append({
            "kind": "highlight",
            "target_id": hl["id"],
            "node_id": hl.get("node_id"),
            "title": (hl.get("quote") or "Highlight").strip() or "Highlight",
            "md": md,
            "has_note": bool(md),
            "updated_at": updated_at,
            **_loc(hl),
        })

    # Reading order: page ascending (anchorless notes last), then by time.
    items.sort(key=lambda it: (it["page"] is None, it["page"] or 0, str(it.get("updated_at") or "")))
    return {
        "document": {"id": document["id"], "title": document.get("title") or "Document"},
        "notes": items,
    }


@router.patch("/api/nodes/{node_id}")
async def api_patch_node(node_id: str, request: Request) -> Dict[str, Any]:
    if not get_node(node_id):
        raise HTTPException(status_code=404, detail="Node does not exist")
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
        raise HTTPException(status_code=404, detail="Node does not exist")
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
        # Collect the doomed subtree's node-note files, drop their rows, unlink after commit.
        note_rows = conn.execute(
            """
            WITH RECURSIVE doomed(id) AS (
                SELECT ?
                UNION ALL
                SELECT nodes.id FROM nodes JOIN doomed ON nodes.parent_id = doomed.id
            )
            SELECT rel_path FROM notes
            WHERE kind = 'node' AND target_id IN (SELECT id FROM doomed)
            """,
            (node_id,),
        ).fetchall()
        note_paths = [row["rel_path"] for row in note_rows]
        conn.execute(
            """
            WITH RECURSIVE doomed(id) AS (
                SELECT ?
                UNION ALL
                SELECT nodes.id FROM nodes JOIN doomed ON nodes.parent_id = doomed.id
            )
            DELETE FROM notes WHERE kind = 'node' AND target_id IN (SELECT id FROM doomed)
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
            DELETE FROM node_workspace_files WHERE node_id IN (SELECT id FROM doomed)
            """,
            (node_id,),
        )
        # Clear the back-link from any highlight that pointed at a doomed node, so the
        # highlight survives but loses its stale "has-node" marker.
        conn.execute(
            """
            WITH RECURSIVE doomed(id) AS (
                SELECT ?
                UNION ALL
                SELECT nodes.id FROM nodes JOIN doomed ON nodes.parent_id = doomed.id
            )
            UPDATE highlights SET node_id = NULL WHERE node_id IN (SELECT id FROM doomed)
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
    for rel in note_paths:
        unlink_note_file(rel)
    return {"deleted": deleted_count, "parent_id": node.get("parent_id")}


@router.get("/api/export/{node_id}")
def api_export(node_id: str) -> Response:
    try:
        markdown = export_subtree_markdown(node_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    filename = f"qa-tree-{node_id[:8]}.md"
    return Response(
        content=markdown,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


async def _json_body(request: Request) -> Dict[str, Any]:
    """Parse the JSON request body leniently, falling back to an empty dict on error."""
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}
