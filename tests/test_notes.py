"""File-backed notes: CRUD, the has_note flag, cascades, migration, and the HTTP API."""
from __future__ import annotations

import sqlite3


def _document(fresh_db):
    """A workspace + stored PDF + document to hang nodes/highlights on."""
    from app import uploads

    ws = fresh_db.create_workspace("ws")
    pdf = uploads.store_upload("paper.pdf", b"%PDF-1.4", "application/pdf", workspace_id=ws["id"])
    doc = fresh_db.create_document(pdf["id"], "Paper", "paper.pdf", 1, workspace_id=ws["id"])
    return ws, doc


def _highlight(fresh_db, doc):
    return fresh_db.create_highlight(
        doc["id"], 0, [{"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.02}], "the quote", "yellow"
    )


def test_node_note_roundtrips_through_a_file(fresh_db):
    from app import notes

    node = fresh_db.create_node(None, "q", [], [])
    note = notes.set_note("node", node["id"], "# Heading\n\n$E=mc^2$")
    assert note is not None

    # The body lives on disk, and get_node surfaces it as note_md.
    assert fresh_db.note_file_path(f"{note['id']}.md").read_text(encoding="utf-8") == "# Heading\n\n$E=mc^2$"
    assert fresh_db.get_node(node["id"])["note_md"] == "# Heading\n\n$E=mc^2$"


def test_set_note_upserts_same_file(fresh_db):
    from app import notes

    node = fresh_db.create_node(None, "q", [], [])
    first = notes.set_note("node", node["id"], "one")
    second = notes.set_note("node", node["id"], "two")
    assert first["id"] == second["id"]  # same row/file, not a new one
    assert notes.get_note("node", node["id"])["md"] == "two"


def test_empty_md_deletes_note_and_file(fresh_db):
    from app import notes

    node = fresh_db.create_node(None, "q", [], [])
    note = notes.set_note("node", node["id"], "something")
    path = fresh_db.note_file_path(f"{note['id']}.md")
    assert path.exists()

    assert notes.set_note("node", node["id"], "   ") is None
    assert notes.get_note("node", node["id"]) is None
    assert not path.exists()


def test_append_note_concatenates(fresh_db):
    from app import notes

    node = fresh_db.create_node(None, "q", [], [])
    notes.set_note("node", node["id"], "first")
    notes.append_note("node", node["id"], "second")
    assert notes.get_note("node", node["id"])["md"] == "first\n\nsecond"


def test_highlight_note_without_any_node(fresh_db):
    """The headline feature: annotate a highlight with no LLM call / no node."""
    from app import notes

    _, doc = _document(fresh_db)
    hl = _highlight(fresh_db, doc)
    assert hl["has_note"] is False

    notes.set_note("highlight", hl["id"], "my annotation")
    assert fresh_db.get_highlight(hl["id"])["has_note"] is True
    assert [h["has_note"] for h in fresh_db.list_highlights(doc["id"])] == [True]
    assert notes.get_note("highlight", hl["id"])["md"] == "my annotation"


def test_delete_highlight_cascades_note(fresh_db):
    from app import notes

    _, doc = _document(fresh_db)
    hl = _highlight(fresh_db, doc)
    note = notes.set_note("highlight", hl["id"], "note")
    path = fresh_db.note_file_path(f"{note['id']}.md")
    assert path.exists()

    fresh_db.delete_highlight(hl["id"])
    assert notes.get_note("highlight", hl["id"]) is None
    assert not path.exists()


def test_delete_document_cascades_node_and_highlight_notes(fresh_db):
    from app import notes

    _, doc = _document(fresh_db)
    node = fresh_db.create_node(None, "q", [], [], document_id=doc["id"])
    hl = _highlight(fresh_db, doc)
    n1 = notes.set_note("node", node["id"], "node note")
    n2 = notes.set_note("highlight", hl["id"], "hl note")
    paths = [fresh_db.note_file_path(f"{n['id']}.md") for n in (n1, n2)]
    assert all(p.exists() for p in paths)

    fresh_db.delete_document(doc["id"])
    assert not any(p.exists() for p in paths)


def test_migration_moves_legacy_note_md_into_file(tmp_path, fresh_db):
    """init_db backfills an old inline nodes.note_md into a file-backed note."""
    node = fresh_db.create_node(None, "q", [], [])
    with fresh_db.db_connection() as conn:
        conn.execute("UPDATE nodes SET note_md = ? WHERE id = ?", ("legacy inline", node["id"]))

    fresh_db.init_db()  # idempotent; should backfill the legacy note
    assert fresh_db.get_node(node["id"])["note_md"] == "legacy inline"


def test_reconcile_drops_rows_whose_file_vanished(fresh_db):
    from app import notes

    node = fresh_db.create_node(None, "q", [], [])
    note = notes.set_note("node", node["id"], "body")
    fresh_db.note_file_path(f"{note['id']}.md").unlink()

    fresh_db.init_db()  # reconcile runs on init
    assert notes.get_note("node", node["id"]) is None


# ── HTTP API ────────────────────────────────────────────────────────────────


def test_api_note_crud_for_highlight(client, fresh_db):
    _, doc = _document(fresh_db)
    hl = _highlight(fresh_db, doc)

    assert client.get(f"/api/notes/highlight/{hl['id']}").json()["note"] is None

    put = client.put(f"/api/notes/highlight/{hl['id']}", json={"md": "hello **world**"})
    assert put.status_code == 200
    assert put.json()["note"]["md"] == "hello **world**"

    assert client.get(f"/api/notes/highlight/{hl['id']}").json()["note"]["md"] == "hello **world**"

    assert client.delete(f"/api/notes/highlight/{hl['id']}").json()["deleted"] is True
    assert client.get(f"/api/notes/highlight/{hl['id']}").json()["note"] is None


def test_api_note_unknown_kind_and_missing_target(client, fresh_db):
    assert client.get("/api/notes/bogus/x").status_code == 404
    assert client.get("/api/notes/highlight/does-not-exist").status_code == 404


def test_api_node_note_patch_still_works(client, fresh_db):
    node = fresh_db.create_node(None, "q", [], [])
    res = client.patch(f"/api/nodes/{node['id']}/note", json={"note_md": "patched"})
    assert res.status_code == 200
    assert res.json()["node"]["note_md"] == "patched"


def test_api_document_notes_aggregates_node_and_highlight_notes(client, fresh_db):
    from app import notes

    _, doc = _document(fresh_db)
    # A highlight with a standalone note, plus a question node anchored to another highlight.
    hl_note = _highlight(fresh_db, doc)
    notes.set_note("highlight", hl_note["id"], "highlight annotation")
    src = fresh_db.create_highlight(doc["id"], 2, [{"x": 0.1, "y": 0.3, "w": 0.2, "h": 0.02}], "src", "green")
    node = fresh_db.create_node(None, "q", [], [], document_id=doc["id"], source_highlight_id=src["id"])
    notes.set_note("node", node["id"], "node annotation")
    # A node without a note must not appear.
    fresh_db.create_node(None, "no note", [], [], document_id=doc["id"])

    data = client.get(f"/api/documents/{doc['id']}/notes").json()
    assert data["document"]["id"] == doc["id"]
    by_kind = {it["kind"]: it for it in data["notes"]}
    assert set(by_kind) == {"node", "highlight"}
    # Each note carries the highlight it anchors to, for jump-to-location.
    assert by_kind["highlight"]["highlight_id"] == hl_note["id"]
    assert by_kind["highlight"]["page"] == 0
    assert by_kind["node"]["highlight_id"] == src["id"]
    assert by_kind["node"]["page"] == 2
    assert by_kind["node"]["node_id"] == node["id"]
    # Reading order: page ascending.
    assert [it["page"] for it in data["notes"]] == [0, 2]


def test_api_document_notes_missing_document(client, fresh_db):
    assert client.get("/api/documents/nope/notes").status_code == 404
