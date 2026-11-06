from pathlib import Path

import pytest

from app import notes, routes, uploads

PDF = b"%PDF-1.7\noriginal\n%%EOF"


@pytest.fixture(autouse=True)
def no_auth(monkeypatch):
    monkeypatch.setattr(routes, "AUTH_ENABLED", False)


def make_document(db, workspace_id=None, filename="paper.pdf", category_id=None):
    saved = uploads.store_upload(filename, PDF, "application/pdf", workspace_id=workspace_id)
    return db.create_document(
        saved["id"], filename, filename, 1, workspace_id=workspace_id, category_id=category_id
    )


def test_inbox_survives_restart_and_excludes_attachments(client, fresh_db):
    ws = fresh_db.create_workspace("Existing")
    assigned = make_document(fresh_db, ws["id"], "assigned.pdf")
    unassigned = make_document(fresh_db)
    attachment = uploads.store_upload("attachment.pdf", PDF, "application/pdf")
    fresh_db.init_db()
    inbox = client.get("/api/documents?unassigned=true")
    assert inbox.status_code == 200
    assert [doc["id"] for doc in inbox.json()["documents"]] == [unassigned["id"]]
    assert fresh_db.get_file(attachment["id"])["workspace_id"] is None
    assert fresh_db.get_document(assigned["id"])["workspace_id"] == ws["id"]
    assert len(fresh_db.list_workspaces()) == 1
    assert len(client.get("/api/documents").json()["documents"]) == 2
    assert client.get(f"/api/documents?unassigned=true&workspace_id={ws['id']}").status_code == 400


def test_move_preserves_history_and_resets_placement(client, fresh_db):
    source = fresh_db.create_workspace("Source")
    destination = fresh_db.create_workspace("Destination")
    cat = fresh_db.create_category(source["id"], "Reading")
    doc = make_document(fresh_db, source["id"], category_id=cat["id"])
    node = fresh_db.create_node(None, "Question", [], [], document_id=doc["id"])
    highlight = fresh_db.create_highlight(doc["id"], 0, [], "Quote", "yellow")
    notes.set_note("node", node["id"], "Question note")
    notes.set_note("highlight", highlight["id"], "Highlight note")
    session = fresh_db.create_study_session(doc["id"], "passive")
    fresh_db.stop_study_session(session["id"], 60)
    fresh_db.update_document_schedule(doc["id"], studied=True, deadline="2026-12-01")
    original_path = fresh_db.get_file(doc["file_id"])["path"]
    before = fresh_db.get_document(doc["id"])
    for workspace_id in (destination["id"], None, source["id"]):
        response = client.post(
            f"/api/documents/{doc['id']}/move", json={"workspace_id": workspace_id}
        )
        assert response.status_code == 200
        moved = response.json()["document"]
        assert moved["workspace_id"] == workspace_id
        assert moved["category_id"] is None
        assert moved["id"] == doc["id"]
        assert moved["seconds_total"] == 60
        assert moved["studied_at"] == before["studied_at"]
        assert moved["deadline"] == before["deadline"]
        assert fresh_db.get_node(node["id"])["document_id"] == doc["id"]
        assert fresh_db.get_node(node["id"])["note_md"] == "Question note"
        assert notes.get_note("highlight", highlight["id"])["md"] == "Highlight note"
        assert fresh_db.list_highlights(doc["id"])[0]["id"] == highlight["id"]
        stored = fresh_db.get_file(doc["file_id"])
        assert stored["workspace_id"] == workspace_id
        assert stored["path"] == original_path
        assert Path(stored["path"]).read_bytes() == PDF
        totals = {ws["id"]: ws["seconds_total"] for ws in fresh_db.list_workspaces()}
        assert totals[source["id"]] == (60 if workspace_id == source["id"] else 0)
        assert totals[destination["id"]] == (60 if workspace_id == destination["id"] else 0)
    fresh_db.delete_workspace(destination["id"])
    assert fresh_db.get_document(doc["id"]) is not None
    assert Path(original_path).exists()


def test_delete_source_workspace_does_not_delete_moved_history(client, fresh_db):
    source = fresh_db.create_workspace("Source")
    doc = make_document(fresh_db, source["id"])
    session = fresh_db.create_study_session(doc["id"], "passive")
    fresh_db.stop_study_session(session["id"], 45)
    response = client.post(f"/api/documents/{doc['id']}/move", json={"workspace_id": None})
    assert response.status_code == 200
    fresh_db.delete_workspace(source["id"])
    assert fresh_db.get_document(doc["id"])["seconds_total"] == 45
    assert Path(fresh_db.get_file(doc["file_id"])["path"]).read_bytes() == PDF


@pytest.mark.parametrize("target_inbox", [False, True])
def test_filename_conflict_is_atomic(client, fresh_db, target_inbox):
    source = fresh_db.create_workspace("Source")
    target = None if target_inbox else fresh_db.create_workspace("Target")["id"]
    doc = make_document(fresh_db, source["id"])
    make_document(fresh_db, target)
    response = client.post(f"/api/documents/{doc['id']}/move", json={"workspace_id": target})
    assert response.status_code == 409
    assert fresh_db.get_document(doc["id"]) == doc
    assert fresh_db.get_file(doc["file_id"])["workspace_id"] == source["id"]


@pytest.mark.parametrize("body", [{}, {"workspace_id": ""}, {"workspace_id": 1}, [], {"workspace_id": "missing"}])
def test_invalid_move_leaves_document_unchanged(client, fresh_db, body):
    doc = make_document(fresh_db)
    response = client.post(f"/api/documents/{doc['id']}/move", json=body)
    assert response.status_code == 400
    assert fresh_db.get_document(doc["id"]) == doc


def test_unknown_document_and_same_workspace(client, fresh_db):
    assert client.post("/api/documents/missing/move", json={"workspace_id": None}).status_code == 404
    doc = make_document(fresh_db)
    response = client.post(f"/api/documents/{doc['id']}/move", json={"workspace_id": None})
    assert response.status_code == 200
    assert response.json()["document"] == doc


def test_move_rejects_malformed_json(client, fresh_db):
    doc = make_document(fresh_db)
    response = client.post(
        f"/api/documents/{doc['id']}/move",
        content="{",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert fresh_db.get_document(doc["id"]) == doc


def test_move_requires_authentication(client, fresh_db, monkeypatch):
    doc = make_document(fresh_db)
    monkeypatch.setattr(routes, "AUTH_ENABLED", True)
    assert client.post(
        f"/api/documents/{doc['id']}/move", json={"workspace_id": None}
    ).status_code == 401


def test_batch_validation_and_replace(client, fresh_db):
    response = client.post(
        "/api/batch-upload",
        files=[
            ("files", ("paper.pdf", PDF, "application/pdf")),
            ("files", ("not-a-pdf.txt", b"hello", "text/plain")),
            ("files", ("fake.pdf", b"not a PDF", "application/pdf")),
        ],
    )
    results = response.json()["results"]
    assert [result["status"] for result in results] == ["created", "failed", "failed"]
    doc = results[0]["document"]
    replacement = PDF + b"\nreplacement"
    response = client.post(
        "/api/batch-upload",
        data={"on_conflict": "replace"},
        files={"files": ("paper.pdf", replacement, "application/pdf")},
    )
    result = response.json()["results"][0]
    assert result["status"] == "replaced"
    assert result["document"]["id"] == doc["id"]
    assert Path(fresh_db.get_file(result["document"]["file_id"])["path"]).read_bytes() == replacement
    assert fresh_db.list_workspaces() == []


def test_category_must_belong_to_upload_workspace(client, fresh_db):
    ws = fresh_db.create_workspace("Workspace")
    category = fresh_db.create_category(ws["id"], "Reading")
    response = client.post(
        "/api/batch-upload",
        data={"category_id": category["id"]},
        files={"files": ("paper.pdf", PDF, "application/pdf")},
    )
    assert response.status_code == 400
    assert fresh_db.list_documents() == []
