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
