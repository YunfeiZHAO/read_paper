"""All HTTP routes, attached to a single FastAPI APIRouter and registered by create_app()."""
from __future__ import annotations

import json
import mimetypes
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Cookie, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.background import BackgroundTask

from .auth import AUTH_ENABLED, create_session_token, verify_credentials, verify_session_token
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
    create_category,
    create_document,
    create_highlight,
    create_node,
    create_workspace,
    create_workspace_category,
    db_connection,
    delete_category,
    delete_document,
    delete_highlight,
    delete_workspace,
    delete_workspace_category,
    get_category,
    get_document,
    get_file,
    get_highlight,
    get_node,
    get_workspace,
    get_workspace_category,
    link_highlight_to_node,
    list_categories,
    list_documents,
    list_files_for_workspace,
    list_highlights,
    list_workspace_categories,
    list_workspaces,
    nodes_for_document,
    nodes_for_project,
    public_node,
    set_document_placement,
    set_workspace_placement,
    touch_document,
    touch_workspace,
    unlink_note_file,
    update_category,
    update_document,
    update_highlight,
    update_node,
    update_workspace,
    update_workspace_category,
)
from .events import (
    collect_tools_count,
    collect_usage,
    extract_answer_from_events,
    find_first_key,
    trim_events_for_storage,
)
from .export import export_subtree_markdown
from .prompts import build_prompt_for_node
from .runners import start_worker
from .transfer import ImportError_, export_workspace, import_workspace
from .uploads import secure_filename, store_upload
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


def _require_auth(request: Request) -> None:
    """Check if authentication is enabled and verify the session token."""
    if not AUTH_ENABLED:
        return
    
    auth_token = request.cookies.get("auth_token")
    if not auth_token:
        raise HTTPException(status_code=401, detail="Authentication required")
    
    email = verify_session_token(auth_token)
    if not email:
        raise HTTPException(status_code=401, detail="Invalid or expired session")


# ── Authentication endpoints ────────────────────────────────────────────

@router.get("/api/auth/status")
def api_auth_status() -> Dict[str, Any]:
    """Return authentication status."""
    return {"auth_enabled": AUTH_ENABLED}


@router.post("/api/auth/login")
async def api_auth_login(request: Request) -> JSONResponse:
    """Login endpoint: verify credentials and return session token."""
    if not AUTH_ENABLED:
        raise HTTPException(status_code=403, detail="Authentication is not enabled")
    
    payload = await _json_body(request)
    email = str(payload.get("email") or "").strip()
    password = str(payload.get("password") or "").strip()
    
    if not email or not password:
        raise HTTPException(status_code=400, detail="Email and password are required")
    
    if not verify_credentials(email, password):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    
    token = create_session_token(email)
    response = JSONResponse({"message": "Login successful", "email": email}, status_code=200)
    response.set_cookie(
        key="auth_token",
        value=token,
        httponly=True,
        secure=False,  # Set to True in production with HTTPS
        samesite="lax",
        max_age=86400,  # 24 hours
    )
    return response


@router.post("/api/auth/logout")
def api_auth_logout() -> JSONResponse:
    """Logout endpoint: clear session token."""
    response = JSONResponse({"message": "Logout successful"})
    response.delete_cookie(key="auth_token")
    return response


@router.get("/api/auth/verify")
def api_auth_verify(request: Request) -> Dict[str, Any]:
    """Verify if the current session is valid."""
    if not AUTH_ENABLED:
        return {"authenticated": True}
    
    auth_token = request.cookies.get("auth_token")
    if not auth_token:
        return {"authenticated": False}
    
    email = verify_session_token(auth_token)
    if email:
        return {"authenticated": True, "email": email}
    
    return {"authenticated": False}


@router.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(INDEX_HTML.read_text(encoding="utf-8"))


@router.get("/favicon.ico")
def favicon() -> Response:
    return Response(status_code=204)


@router.get("/api/tree")
def api_tree(request: Request) -> Dict[str, Any]:
    """Return every node and file in the database (the frontend's initial tree load)."""
    _require_auth(request)
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
    _require_auth(request)
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
    if backend == "local":
        # Browser-driven: the user's local Claude Code answers this via the localhost bridge, so
        # no server-side worker runs. Hand back the fully built prompt (the browser can't walk the
        # ancestor tree itself) and let the browser POST results to /api/nodes/{id}/bridge-events.
        return JSONResponse({"node": node, "prompt": build_prompt_for_node(node)}, status_code=201)
    start_worker(node["id"])
    return JSONResponse({"node": node}, status_code=202)


@router.post("/api/nodes/{node_id}/bridge-events")
async def api_bridge_events(node_id: str, request: Request) -> Dict[str, Any]:
    """Persist events/answer produced by the browser-driven local Claude Code bridge.

    The browser runs the model on the user's machine and POSTs here periodically while running
    and once at the end. No key or LLM call touches the server; this only writes the result into
    the normal node lifecycle (reusing the same event→answer/tokens/tools derivation the runners
    use), so the tree, polling, export, and tool-activity UI all work unchanged.
    """
    _require_auth(request)
    node = get_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node does not exist")
    if node.get("backend") != "local":
        raise HTTPException(status_code=400, detail="Node is not a local-backend node")
    if node.get("status") in {"done", "failed"}:
        raise HTTPException(status_code=409, detail="Node is already finalized")

    payload = await _json_body(request)
    events = payload.get("events")
    if not isinstance(events, list):
        raise HTTPException(status_code=400, detail="events must be an array")
    status = str(payload.get("status") or "running")
    if status not in {"running", "done", "failed"}:
        raise HTTPException(status_code=400, detail="status must be running, done, or failed")

    fields: Dict[str, Any] = {
        "status": status,
        "raw_events_json": json.dumps(trim_events_for_storage(events), ensure_ascii=False),
        "tools_count": collect_tools_count(events),
    }
    session_id = find_first_key(events, ("thread_id", "session_id", "sessionId", "conversation_id"))
    if session_id:
        fields["codex_session_id"] = session_id
    model = payload.get("model")
    if isinstance(model, str) and model.strip():
        fields["model"] = model.strip()

    if status in {"running"}:
        update_node(node_id, **fields)
        return {"node": get_node(node_id)}

    # Terminal: derive the answer + token usage from the event stream, same as the runners.
    answer = extract_answer_from_events(events)
    usage = collect_usage(events, node.get("prompt") or "", answer)
    fields.update(
        answer=answer,
        prompt_tokens=usage["prompt_tokens"],
        completion_tokens=usage["completion_tokens"],
        total_tokens=usage["total_tokens"],
        completed_at=now_iso(),
    )
    if status == "failed":
        fields["error"] = str(payload.get("error") or "Local Claude Code bridge reported a failure")[-8000:]
    elif not answer:
        fields["status"] = "failed"
        fields["error"] = "The local bridge finished but produced no answer"
    else:
        fields["error"] = None

    update_node(node_id, **fields)
    return {"node": get_node(node_id)}


@router.post("/api/upload")
async def api_upload(
    request: Request,
    files: Optional[List[UploadFile]] = File(None),
    file: Optional[List[UploadFile]] = File(None),
    workspace_id: str = Form(""),
) -> JSONResponse:
    _require_auth(request)
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
def api_download_file(request: Request, file_id: str) -> Any:
    _require_auth(request)
    item = get_file(file_id)
    if not item:
        raise HTTPException(status_code=404, detail="File does not exist")
    return FileResponse(item["path"], filename=item["original_name"])


# ── workspaces (a user project: a group of PDFs + a shared file drawer) ───


@router.get("/api/workspaces")
def api_list_workspaces(request: Request, include_archived: str = "") -> Dict[str, Any]:
    _require_auth(request)
    return {
        "workspaces": list_workspaces(include_archived=str(include_archived or "") == "1"),
        "categories": list_workspace_categories(),
    }


@router.post("/api/workspaces")
async def api_create_workspace(request: Request) -> JSONResponse:
    _require_auth(request)
    payload = await _json_body(request)
    title = str(payload.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title is required")
    description = str(payload.get("description") or "")
    ws = create_workspace(title, description)
    return JSONResponse({"workspace": ws}, status_code=201)


@router.get("/api/workspaces/{workspace_id}")
def api_get_workspace(request: Request, workspace_id: str) -> Dict[str, Any]:
    _require_auth(request)
    ws = get_workspace(workspace_id)
    if not ws:
        raise HTTPException(status_code=404, detail="Workspace does not exist")
    touch_workspace(workspace_id)
    return {
        "workspace": ws,
        "documents": list_documents(workspace_id),
        "categories": list_categories(workspace_id),
        "files": list_files_for_workspace(workspace_id),
    }


@router.patch("/api/workspaces/{workspace_id}")
async def api_patch_workspace(workspace_id: str, request: Request) -> Dict[str, Any]:
    _require_auth(request)
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
    # Per-workspace LLM settings (bring-your-own endpoint/key/model for the Claude backend).
    # A blank value clears the field (→ env default). api_key is special: a blank/absent value
    # leaves the stored key untouched, so editing only the model never wipes an existing key.
    if "api_base_url" in payload:
        fields["api_base_url"] = str(payload["api_base_url"] or "").strip()
    if "api_model" in payload:
        fields["api_model"] = str(payload["api_model"] or "").strip()
    if "api_auth_type" in payload:
        auth_type = str(payload["api_auth_type"] or "").strip().lower()
        fields["api_auth_type"] = auth_type if auth_type in {"bearer", "api_key"} else ""
    if str(payload.get("api_key") or "").strip():
        fields["api_key"] = str(payload["api_key"]).strip()
    elif payload.get("clear_api_key"):
        fields["api_key"] = ""

    # Home-dashboard placement (drag/reorder): applied when either key is present. Kept out
    # of `fields`/update_workspace so it never collides with the title/LLM-settings logic.
    if "category_id" in payload or "position" in payload:
        category_id = payload.get("category_id")
        category_id = str(category_id).strip() if category_id else None
        if category_id and not get_workspace_category(category_id):
            raise HTTPException(status_code=400, detail="Category does not exist")
        try:
            position = float(payload.get("position"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="position must be a number")
        set_workspace_placement(workspace_id, category_id, position)

    if fields:
        update_workspace(workspace_id, **fields)
    return {"workspace": get_workspace(workspace_id)}


@router.post("/api/workspace-categories")
async def api_create_workspace_category(request: Request) -> JSONResponse:
    _require_auth(request)
    payload = await _json_body(request)
    title = str(payload.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title is required")
    return JSONResponse({"category": create_workspace_category(title)}, status_code=201)


@router.patch("/api/workspace-categories/{category_id}")
async def api_update_workspace_category(category_id: str, request: Request) -> Dict[str, Any]:
    _require_auth(request)
    if not get_workspace_category(category_id):
        raise HTTPException(status_code=404, detail="Category does not exist")
    payload = await _json_body(request)
    title = None
    if "title" in payload:
        title = str(payload["title"] or "").strip()
        if not title:
            raise HTTPException(status_code=400, detail="title cannot be empty")
    position = None
    if "position" in payload:
        try:
            position = float(payload["position"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="position must be a number")
    return {"category": update_workspace_category(category_id, title=title, position=position)}


@router.delete("/api/workspace-categories/{category_id}")
def api_delete_workspace_category(request: Request, category_id: str) -> Dict[str, Any]:
    _require_auth(request)
    if not get_workspace_category(category_id):
        raise HTTPException(status_code=404, detail="Category does not exist")
    return {**delete_workspace_category(category_id)}


@router.delete("/api/workspaces/{workspace_id}")
def api_delete_workspace(request: Request, workspace_id: str) -> Dict[str, Any]:
    _require_auth(request)
    if not get_workspace(workspace_id):
        raise HTTPException(status_code=404, detail="Workspace does not exist")
    result = delete_workspace(workspace_id)
    return {"deleted": True, **result}


@router.get("/api/workspaces/{workspace_id}/export")
def api_export_workspace(workspace_id: str) -> Any:
    """Download the whole workspace (PDFs, shared files, highlights, chat tree, notes,
    AI scratch) as a single portable .zip. The secret API key is never included."""
    ws = get_workspace(workspace_id)
    if not ws:
        raise HTTPException(status_code=404, detail="Workspace does not exist")
    try:
        zip_path = export_workspace(workspace_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    safe_title = secure_filename(ws.get("title") or "") or "workspace"
    filename = f"workspace-{safe_title}-{now_iso()[:10]}.zip"
    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=filename,
        background=BackgroundTask(Path(zip_path).unlink, missing_ok=True),
    )


@router.post("/api/workspaces/import")
async def api_import_workspace(file: UploadFile = File(...)) -> JSONResponse:
    """Import a workspace .zip exported by this app, recreating it with fresh IDs."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file received")
    tmp = tempfile.NamedTemporaryFile(prefix="ws-import-", suffix=".zip", delete=False)
    tmp_path = Path(tmp.name)
    try:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            tmp.write(chunk)
        tmp.close()
        workspace = import_workspace(tmp_path)
    except ImportError_ as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except (OSError, PermissionError, IOError) as exc:
        raise HTTPException(
            status_code=500,
            detail=f"File system error during import: {str(exc)}"
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Import failed: {str(exc)}"
        )
    finally:
        try:
            tmp.close()
        except Exception:
            pass
        tmp_path.unlink(missing_ok=True)
    return JSONResponse({"workspace": workspace}, status_code=201)


# ── documents / highlights ──────────────────────────────────────────────


@router.post("/api/documents")
async def api_create_document(
    request: Request,
    file: UploadFile = File(...),
    title: str = Form(""),
    num_pages: Optional[int] = Form(None),
    workspace_id: str = Form(""),
) -> JSONResponse:
    _require_auth(request)
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
def api_list_documents(request: Request, workspace_id: str = "") -> Dict[str, Any]:
    _require_auth(request)
    return {"documents": list_documents((workspace_id or "").strip() or None)}


@router.get("/api/documents/{document_id}")
def api_get_document(request: Request, document_id: str) -> Dict[str, Any]:
    _require_auth(request)
    doc = get_document(document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document does not exist")
    touch_document(document_id)
    return {
        "document": doc,
        "highlights": list_highlights(document_id),
        "nodes": nodes_for_document(document_id),
    }


@router.patch("/api/documents/{document_id}")
async def api_update_document(document_id: str, request: Request) -> Dict[str, Any]:
    """Partial update of a document. A `title` key renames it (and the file on disk);
    `category_id`/`position` keys move it between kanban columns (a drag) without
    touching the file."""
    _require_auth(request)
    if not get_document(document_id):
        raise HTTPException(status_code=404, detail="Document does not exist")

    body = await request.json()

    # Placement update (drag/reorder): applied when either key is present.
    if "category_id" in body or "position" in body:
        category_id = body.get("category_id")
        category_id = str(category_id).strip() if category_id else None
        if category_id:
            cat = get_category(category_id)
            if not cat:
                raise HTTPException(status_code=400, detail="Category does not exist")
        try:
            position = float(body.get("position"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="position must be a number")
        set_document_placement(document_id, category_id, position)

    # Title update (rename): only when a title key is actually sent.
    if "title" in body:
        title = (body.get("title") or "").strip()
        if not title:
            raise HTTPException(status_code=400, detail="Title cannot be empty")
        try:
            update_document(document_id, title)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    return {"document": get_document(document_id)}


@router.post("/api/workspaces/{workspace_id}/categories")
async def api_create_category(workspace_id: str, request: Request) -> JSONResponse:
    _require_auth(request)
    if not get_workspace(workspace_id):
        raise HTTPException(status_code=404, detail="Workspace does not exist")
    payload = await _json_body(request)
    title = str(payload.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title is required")
    cat = create_category(workspace_id, title)
    return JSONResponse({"category": cat}, status_code=201)


@router.patch("/api/categories/{category_id}")
async def api_update_category(category_id: str, request: Request) -> Dict[str, Any]:
    _require_auth(request)
    if not get_category(category_id):
        raise HTTPException(status_code=404, detail="Category does not exist")
    payload = await _json_body(request)
    title = None
    if "title" in payload:
        title = str(payload["title"] or "").strip()
        if not title:
            raise HTTPException(status_code=400, detail="title cannot be empty")
    position = None
    if "position" in payload:
        try:
            position = float(payload["position"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="position must be a number")
    return {"category": update_category(category_id, title=title, position=position)}


@router.delete("/api/categories/{category_id}")
def api_delete_category(request: Request, category_id: str) -> Dict[str, Any]:
    _require_auth(request)
    if not get_category(category_id):
        raise HTTPException(status_code=404, detail="Category does not exist")
    result = delete_category(category_id)
    return {**result}


@router.delete("/api/documents/{document_id}")
def api_delete_document(request: Request, document_id: str) -> Dict[str, Any]:
    _require_auth(request)
    if not get_document(document_id):
        raise HTTPException(status_code=404, detail="Document does not exist")
    result = delete_document(document_id)
    return {"deleted": True, **result}


@router.get("/api/documents/{document_id}/file")
def api_document_file(request: Request, document_id: str) -> Any:
    _require_auth(request)
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
def api_list_highlights(request: Request, document_id: str) -> Dict[str, Any]:
    _require_auth(request)
    if not get_document(document_id):
        raise HTTPException(status_code=404, detail="Document does not exist")
    return {"highlights": list_highlights(document_id)}


@router.post("/api/documents/{document_id}/highlights")
async def api_create_highlight(document_id: str, request: Request) -> JSONResponse:
    _require_auth(request)
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
    _require_auth(request)
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
def api_delete_highlight(request: Request, highlight_id: str) -> Dict[str, Any]:
    _require_auth(request)
    if not delete_highlight(highlight_id):
        raise HTTPException(status_code=404, detail="Highlight does not exist")
    return {"deleted": True}


# ── workspace scratch dir (files the AI read/wrote under WORK_DIR; shown in the side tab) ──


@router.get("/api/workspace/files")
def api_workspace_files(request: Request, node_id: str = "") -> Dict[str, Any]:
    _require_auth(request)
    node_id = (node_id or "").strip() or None
    return {"root": str(WORK_DIR), "files": workspace_files_for_node(node_id)}


@router.get("/api/workspace/content")
def api_workspace_content(request: Request, path: str = "") -> Dict[str, Any]:
    _require_auth(request)
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
def api_workspace_file(request: Request, path: str = "", download: str = "") -> Any:
    _require_auth(request)
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
    _require_auth(request)
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
    _require_auth(request)
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
def api_get_note(request: Request, kind: str, target_id: str) -> Dict[str, Any]:
    _require_auth(request)
    _require_note_target(kind, target_id)
    return {"note": notes.get_note(kind, target_id)}


@router.put("/api/notes/{kind}/{target_id}")
async def api_put_note(kind: str, target_id: str, request: Request) -> Dict[str, Any]:
    _require_auth(request)
    _require_note_target(kind, target_id)
    payload = await _json_body(request)
    md = payload.get("md", "")
    if not isinstance(md, str):
        md = str(md)
    return {"note": notes.set_note(kind, target_id, md)}


@router.delete("/api/notes/{kind}/{target_id}")
def api_delete_note(request: Request, kind: str, target_id: str) -> Dict[str, Any]:
    _require_auth(request)
    _require_note_target(kind, target_id)
    return {"deleted": notes.delete_note(kind, target_id)}


@router.get("/api/projects/{project_id}/notes")
def api_project_notes(request: Request, project_id: str) -> Dict[str, Any]:
    _require_auth(request)
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
def api_document_notes(request: Request, document_id: str) -> Dict[str, Any]:
    """Every marked part of a PDF — node notes, highlight notes, and the bare highlights
    with no note yet — each carrying the highlight it anchors to (a node note anchors to
    the node's source highlight) so the reader can jump the PDF viewer to where it was
    marked. Items without a note carry an empty ``md`` and ``has_note=False`` so the list
    reads as the full record of interesting parts marked in the paper. Ordered by page,
    then time."""
    _require_auth(request)
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
    _require_auth(request)
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
def api_delete_node(request: Request, node_id: str) -> Dict[str, Any]:
    _require_auth(request)
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
def api_export(request: Request, node_id: str) -> Response:
    _require_auth(request)
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
