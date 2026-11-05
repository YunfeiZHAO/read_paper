"""Persistence, boolean coercion, and migration of nodes.enable_web_search."""
from __future__ import annotations

import sqlite3


def test_create_node_persists_flag(fresh_db):
    on = fresh_db.create_node(None, "on", [], [], enable_web_search=True)
    off = fresh_db.create_node(None, "off", [], [], enable_web_search=False)

    assert fresh_db.get_node(on["id"])["enable_web_search"] is True
    assert fresh_db.get_node(off["id"])["enable_web_search"] is False


def test_default_flag_is_false(fresh_db):
    node = fresh_db.create_node(None, "default", [], [])
    assert fresh_db.get_node(node["id"])["enable_web_search"] is False


def test_public_node_coerces_to_bool(fresh_db):
    node = fresh_db.create_node(None, "coerce", [], [], enable_web_search=True)
    value = fresh_db.get_node(node["id"])["enable_web_search"]
    assert value is True and isinstance(value, bool)


def test_migration_adds_column(tmp_path, fresh_db):
    """init_db should add the missing column to an old database, defaulting old rows to 0."""
    legacy = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(legacy)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id TEXT PRIMARY KEY,
            parent_id TEXT,
            title TEXT NOT NULL,
            prompt TEXT NOT NULL,
            answer TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'queued',
            backend TEXT NOT NULL DEFAULT 'codex',
            archived INTEGER NOT NULL DEFAULT 0,
            bookmarked INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO nodes (id, title, prompt) VALUES ('old1', 't', 'p');
        """
    )
    conn.commit()
    conn.close()

    fresh_db.DB_PATH = legacy
    fresh_db.init_db()

    conn = sqlite3.connect(legacy)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(nodes)").fetchall()}
    assert "enable_web_search" in columns
    value = conn.execute("SELECT enable_web_search FROM nodes WHERE id = 'old1'").fetchone()[0]
    conn.close()
    assert value == 0


def test_delete_document_removes_file_from_disk(fresh_db):
    from pathlib import Path

    from app import uploads

    ws = fresh_db.create_workspace("ws")
    saved = uploads.store_upload("paper.pdf", b"%PDF-1.4 data", "application/pdf", workspace_id=ws["id"])
    doc = fresh_db.create_document(saved["id"], "Paper", "paper.pdf", 1, workspace_id=ws["id"])

    on_disk = Path(fresh_db.resolve_stored_path(
        fresh_db.get_file(saved["id"])["path"]
    ))
    assert on_disk.exists()

    fresh_db.delete_document(doc["id"])

    assert fresh_db.get_file(saved["id"]) is None
    assert not on_disk.exists()
    assert not on_disk.parent.exists()


def test_delete_workspace_removes_files_from_disk(fresh_db):
    from pathlib import Path

    from app import uploads

    ws = fresh_db.create_workspace("ws")
    pdf = uploads.store_upload("paper.pdf", b"%PDF-1.4", "application/pdf", workspace_id=ws["id"])
    shared = uploads.store_upload("notes.txt", b"hello", "text/plain", workspace_id=ws["id"])
    fresh_db.create_document(pdf["id"], "Paper", "paper.pdf", 1, workspace_id=ws["id"])

    pdf_dir = Path(fresh_db.resolve_stored_path(pdf["path"])).parent
    shared_dir = Path(fresh_db.resolve_stored_path(shared["path"])).parent
    assert pdf_dir.exists() and shared_dir.exists()

    fresh_db.delete_workspace(ws["id"])

    assert fresh_db.get_file(pdf["id"]) is None
    assert fresh_db.get_file(shared["id"]) is None
    assert not pdf_dir.exists()
    assert not shared_dir.exists()


def _make_doc(fresh_db, ws_id, name="paper.pdf"):
    from app import uploads

    saved = uploads.store_upload(name, b"%PDF-1.4 data", "application/pdf", workspace_id=ws_id)
    return fresh_db.create_document(saved["id"], name, name, 1, workspace_id=ws_id)


def test_update_document_schedule_sets_and_clears(fresh_db):
    ws = fresh_db.create_workspace("ws")
    doc = _make_doc(fresh_db, ws["id"])

    # Fresh document: no deadline, not studied.
    assert doc["deadline"] is None
    assert doc["studied"] is False

    # Set a deadline and mark studied.
    fresh_db.update_document_schedule(doc["id"], deadline="2099-01-15", studied=True)
    got = fresh_db.get_document(doc["id"])
    assert got["deadline"] == "2099-01-15"
    assert got["studied"] is True
    assert got["studied_at"]  # a timestamp was stamped

    # Clearing deadline and unmarking studied; omitted args stay put.
    fresh_db.update_document_schedule(doc["id"], deadline=None, studied=False)
    got = fresh_db.get_document(doc["id"])
    assert got["deadline"] is None
    assert got["studied"] is False
    assert got["studied_at"] is None


def test_update_document_schedule_partial(fresh_db):
    ws = fresh_db.create_workspace("ws")
    doc = _make_doc(fresh_db, ws["id"])
    fresh_db.update_document_schedule(doc["id"], deadline="2099-02-01")
    # Studied untouched (still False) when only deadline is passed.
    got = fresh_db.get_document(doc["id"])
    assert got["deadline"] == "2099-02-01"
    assert got["studied"] is False


def test_list_workspaces_urgent_deadline(fresh_db):
    ws = fresh_db.create_workspace("ws")
    d1 = _make_doc(fresh_db, ws["id"], "a.pdf")
    d2 = _make_doc(fresh_db, ws["id"], "b.pdf")
    d3 = _make_doc(fresh_db, ws["id"], "c.pdf")

    fresh_db.update_document_schedule(d1["id"], deadline="2099-05-01")
    fresh_db.update_document_schedule(d2["id"], deadline="2099-03-01")  # soonest unstudied
    # A sooner deadline that is already studied must be ignored.
    fresh_db.update_document_schedule(d3["id"], deadline="2099-01-01", studied=True)

    rows = {w["id"]: w for w in fresh_db.list_workspaces()}
    assert rows[ws["id"]]["urgent_deadline"] == "2099-03-01"


def test_patch_document_schedule_route(client):
    from app import uploads

    ws = client.post("/api/workspaces", json={"title": "ws"}).json()["workspace"]
    saved = uploads.store_upload("p.pdf", b"%PDF-1.4", "application/pdf", workspace_id=ws["id"])
    from app import db

    doc = db.create_document(saved["id"], "P", "p.pdf", 1, workspace_id=ws["id"])

    res = client.patch(f"/api/documents/{doc['id']}", json={"deadline": "2099-07-04", "studied": True})
    assert res.status_code == 200
    out = res.json()["document"]
    assert out["deadline"] == "2099-07-04"
    assert out["studied"] is True

    # Bad date is rejected.
    bad = client.patch(f"/api/documents/{doc['id']}", json={"deadline": "not-a-date"})
    assert bad.status_code == 400


def test_create_document_into_category_and_size(fresh_db):
    from app import uploads

    ws = fresh_db.create_workspace("ws")
    cat = fresh_db.create_category(ws["id"], "Machine Learning")
    saved = uploads.store_upload("paper.pdf", b"%PDF-1.4 some bytes", "application/pdf", workspace_id=ws["id"])
    doc = fresh_db.create_document(
        saved["id"], "Paper", "paper.pdf", 3, workspace_id=ws["id"], category_id=cat["id"]
    )
    assert doc["category_id"] == cat["id"]
    # File size is joined into the public document.
    assert doc["size"] == len(b"%PDF-1.4 some bytes")

    # Listing also carries category + size.
    listed = fresh_db.list_documents(ws["id"])[0]
    assert listed["category_id"] == cat["id"]
    assert listed["size"] == len(b"%PDF-1.4 some bytes")


def test_create_document_rejects_bad_category(fresh_db):
    import pytest

    from app import uploads

    ws = fresh_db.create_workspace("ws")
    saved = uploads.store_upload("p.pdf", b"%PDF", "application/pdf", workspace_id=ws["id"])
    with pytest.raises(ValueError):
        fresh_db.create_document(
            saved["id"], "P", "p.pdf", 1, workspace_id=ws["id"], category_id="nope"
        )
