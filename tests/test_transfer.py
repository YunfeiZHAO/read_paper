"""Whole-workspace export / import (app/transfer.py)."""
from __future__ import annotations

import io
import json
import uuid
import zipfile
from pathlib import Path

import pytest

from app import db, notes, transfer, uploads
from app.config import WORK_DIR

PDF_BYTES = b"%PDF-1.4\nfake pdf body\n%%EOF"
SHARED_BYTES = b"hello shared file"


def _build_workspace():
    """Create a workspace with a PDF, a shared file, a highlight, a 2-node tree, notes,
    and an AI scratch file. Returns a bag of ids plus the unique scratch rel-path."""
    ws = db.create_workspace("My Workspace", "a description")
    ws_id = ws["id"]
    db.update_workspace(
        ws_id,
        api_base_url="https://proxy.example",
        api_model="claude-test",
        api_auth_type="bearer",
        api_key="super-secret-key-123",
    )

    pdf = uploads.store_upload("paper.pdf", PDF_BYTES, "application/pdf", workspace_id=ws_id)
    doc = db.create_document(pdf["id"], "Paper", "paper.pdf", 3, workspace_id=ws_id)
    shared = uploads.store_upload("notes.txt", SHARED_BYTES, "text/plain", workspace_id=ws_id)

    hl = db.create_highlight(
        doc["id"], 0, [{"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.05}], "quoted text", "yellow"
    )

    parent = db.create_node(
        None, "What is X?", [], [], backend="claude",
        document_id=doc["id"], source_highlight_id=hl["id"],
    )
    db.link_highlight_to_node(hl["id"], parent["id"])
    db.update_node(
        parent["id"], answer="X is the answer.", status="done",
        total_tokens=42, raw_events_json='[{"type":"msg"}]',
    )
    child = db.create_node(
        parent["id"], "Follow up?", [], [], backend="claude", document_id=doc["id"]
    )

    notes.set_note("node", parent["id"], "my node note")
    notes.set_note("highlight", hl["id"], "my highlight note")

    # An AI scratch file + its tracking row (unique rel-path so tests don't collide).
    scratch_rel = f"scratch_out/{uuid.uuid4().hex}.md"
    scratch_abs = WORK_DIR / scratch_rel
    scratch_abs.parent.mkdir(parents=True, exist_ok=True)
    scratch_abs.write_text("generated content", encoding="utf-8")
    with db.DB_LOCK, db.db_connection() as conn:
        conn.execute(
            "INSERT INTO node_workspace_files (node_id, rel_path, path, size, mtime, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (child["id"], scratch_rel, scratch_rel, scratch_abs.stat().st_size,
             scratch_abs.stat().st_mtime, db.now_iso()),
        )

    return {
        "ws_id": ws_id,
        "doc_id": doc["id"],
        "pdf_file_id": pdf["id"],
        "shared_file_id": shared["id"],
        "hl_id": hl["id"],
        "parent_id": parent["id"],
        "child_id": child["id"],
        "scratch_rel": scratch_rel,
    }


def test_export_import_round_trip(fresh_db):
    bag = _build_workspace()

    zip_path = transfer.export_workspace(bag["ws_id"])
    assert zip_path.is_file()

    # The secret API key must never appear in the archive.
    with zipfile.ZipFile(zip_path) as zf:
        data_raw = zf.read("data.json").decode("utf-8")
    assert "super-secret-key-123" not in data_raw

    # Remove the on-disk scratch file so we can prove import restores it.
    (WORK_DIR / bag["scratch_rel"]).unlink()

    new_ws = transfer.import_workspace(zip_path)
    zip_path.unlink(missing_ok=True)
    new_ws_id = new_ws["id"]

    # Fresh identity, non-secret settings carried over, key stripped.
    assert new_ws_id != bag["ws_id"]
    assert new_ws["api_base_url"] == "https://proxy.example"
    assert new_ws["api_model"] == "claude-test"
    assert new_ws["has_api_key"] is False

    # Document + its PDF bytes.
    docs = db.list_documents(new_ws_id)
    assert len(docs) == 1
    new_doc = docs[0]
    assert new_doc["id"] != bag["doc_id"]
    assert new_doc["title"] == "Paper"
    pdf_file = db.get_file(new_doc["file_id"])
    assert Path(pdf_file["path"]).read_bytes() == PDF_BYTES

    # Shared file drawer (PDF + shared file both live in the workspace file set).
    files = db.list_files_for_workspace(new_ws_id)
    shared = next(f for f in files if f["original_name"] == "notes.txt")
    assert Path(shared["path"]).read_bytes() == SHARED_BYTES

    # Highlight remapped onto the new document and node.
    highlights = db.list_highlights(new_doc["id"])
    assert len(highlights) == 1
    new_hl = highlights[0]
    assert new_hl["quote"] == "quoted text"
    assert new_hl["rects"] == [{"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.05}]

    # The two-node tree, with FK remapping intact.
    nodes = db.nodes_for_document(new_doc["id"])
    assert len(nodes) == 2
    parent = next(n for n in nodes if n["parent_id"] is None)
    child = next(n for n in nodes if n["parent_id"] is not None)
    assert parent["answer"] == "X is the answer."
    assert parent["total_tokens"] == 42
    assert parent["raw_events"] == [{"type": "msg"}]
    assert child["parent_id"] == parent["id"]
    assert parent["source_highlight_id"] == new_hl["id"]
    assert new_hl["node_id"] == parent["id"]

    # Notes (bodies preserved, targets remapped).
    assert parent["note_md"] == "my node note"
    hl_note = notes.get_note("highlight", new_hl["id"])
    assert hl_note is not None and hl_note["md"] == "my highlight note"

    # Scratch file restored to the shared work dir, and its tracking row remapped.
    assert (WORK_DIR / bag["scratch_rel"]).read_text(encoding="utf-8") == "generated content"
    with db.DB_LOCK, db.db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM node_workspace_files WHERE node_id = ?", (child["id"],)
        ).fetchone()
    assert row is not None and row["rel_path"] == bag["scratch_rel"]


# ── safety ──────────────────────────────────────────────────────────────

def _zip_with(members: dict) -> Path:
    import tempfile
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp.close()
    path = Path(tmp.name)
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    return path


def test_rejects_non_zip():
    import tempfile
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp.write(b"definitely not a zip")
    tmp.close()
    path = Path(tmp.name)
    try:
        with pytest.raises(transfer.ImportError_):
            transfer.import_workspace(path)
    finally:
        path.unlink(missing_ok=True)


def test_rejects_wrong_format_version(fresh_db):
    path = _zip_with({
        "manifest.json": json.dumps({"format_version": 999}),
        "data.json": json.dumps({"format_version": 999, "workspace": {"id": "x", "title": "t"}}),
    })
    try:
        with pytest.raises(transfer.ImportError_) as exc:
            transfer.import_workspace(path)
        assert "version" in str(exc.value).lower()
    finally:
        path.unlink(missing_ok=True)


def test_rejects_oversized_archive(fresh_db, monkeypatch):
    monkeypatch.setattr(transfer, "MAX_TOTAL_UNCOMPRESSED_BYTES", 10)
    path = _zip_with({
        "manifest.json": json.dumps({"format_version": transfer.FORMAT_VERSION}),
        "data.json": json.dumps({"workspace": {"id": "x"}}) + " " * 100,
    })
    try:
        with pytest.raises(transfer.ImportError_) as exc:
            transfer.import_workspace(path)
        assert "too large" in str(exc.value).lower()
    finally:
        path.unlink(missing_ok=True)


def test_safe_rel_blocks_traversal_and_absolute():
    assert transfer._safe_rel("../evil.txt", WORK_DIR) is None
    assert transfer._safe_rel("/etc/passwd", WORK_DIR) is None
    assert transfer._safe_rel("a/b/ok.txt", WORK_DIR) is not None


def test_scratch_traversal_member_never_escapes(fresh_db):
    """A node_workspace_files row with a traversing rel-path must not write outside WORK_DIR."""
    data = {
        "format_version": transfer.FORMAT_VERSION,
        "workspace": {"id": "old", "title": "Evil", "description": "", "archived": 0,
                      "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"},
        "documents": [], "files": [], "highlights": [], "nodes": [],
        "notes": [],
        "node_workspace_files": [
            {"node_id": "n1", "rel_path": "../escaped.txt", "path": "../escaped.txt",
             "size": 4, "mtime": 0.0, "created_at": "2026-01-01T00:00:00Z"},
        ],
    }
    path = _zip_with({
        "manifest.json": json.dumps({"format_version": transfer.FORMAT_VERSION}),
        "data.json": json.dumps(data),
        "scratch/../escaped.txt": "evil",
    })
    try:
        transfer.import_workspace(path)  # the dangling node_id is skipped; no crash
        assert not (WORK_DIR.parent / "escaped.txt").exists()
    finally:
        path.unlink(missing_ok=True)


# ── route layer ───────────────────────────────────────────────────────────

def test_export_route_404_for_missing(client):
    r = client.get("/api/workspaces/does-not-exist/export")
    assert r.status_code == 404


def test_import_route_400_for_bad_file(client):
    r = client.post(
        "/api/workspaces/import",
        files={"file": ("x.zip", b"not a zip", "application/zip")},
    )
    assert r.status_code == 400


def test_export_import_round_trip_via_api(client):
    ws = client.post("/api/workspaces", json={"title": "API WS"}).json()["workspace"]
    client.post(
        "/api/documents",
        files={"file": ("paper.pdf", PDF_BYTES, "application/pdf")},
        data={"workspace_id": ws["id"], "title": "Paper"},
    )
    doc = client.get(f"/api/workspaces/{ws['id']}").json()["documents"][0]
    client.post(
        f"/api/documents/{doc['id']}/highlights",
        json={"page": 0, "rects": [{"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.05}], "quote": "q"},
    )

    export = client.get(f"/api/workspaces/{ws['id']}/export")
    assert export.status_code == 200
    assert "zip" in export.headers.get("content-type", "")

    imported = client.post(
        "/api/workspaces/import",
        files={"file": ("ws.zip", export.content, "application/zip")},
    )
    assert imported.status_code == 201
    new_ws = imported.json()["workspace"]
    assert new_ws["id"] != ws["id"]

    detail = client.get(f"/api/workspaces/{new_ws['id']}").json()
    assert len(detail["documents"]) == 1
    assert detail["documents"][0]["title"] == "Paper"
    assert len(detail["workspace"]) >= 1
    new_doc = detail["documents"][0]
    hls = client.get(f"/api/documents/{new_doc['id']}/highlights").json()["highlights"]
    assert len(hls) == 1 and hls[0]["quote"] == "q"
