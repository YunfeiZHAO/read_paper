"""Time tracking: study-session lifecycle, MAX-based accrual, rollups, cascades, round-trip."""
from __future__ import annotations

import pytest

from app import uploads

PDF_BYTES = b"%PDF-1.4\n%%EOF\n"


def _make_doc(db, title="Paper"):
    ws = db.create_workspace("WS")
    f = uploads.store_upload("paper.pdf", PDF_BYTES, "application/pdf", workspace_id=ws["id"])
    doc = db.create_document(f["id"], title, "paper.pdf", 3, workspace_id=ws["id"])
    return ws, doc


# ── lifecycle & accrual ────────────────────────────────────────────────────

def test_session_lifecycle_and_max_accrual(fresh_db):
    _, doc = _make_doc(fresh_db)
    s = fresh_db.create_study_session(doc["id"], "passive")
    assert s["seconds"] == 0 and s["ended_at"] is None

    assert fresh_db.beat_study_session(s["id"], 30)["seconds"] == 30
    assert fresh_db.beat_study_session(s["id"], 90)["seconds"] == 90
    # A stale/out-of-order beat never lowers the stored total (MAX semantics).
    assert fresh_db.beat_study_session(s["id"], 45)["seconds"] == 90

    closed = fresh_db.stop_study_session(s["id"], 120)
    assert closed["seconds"] == 120 and closed["ended_at"] is not None


def test_unknown_kind_rejected(fresh_db):
    _, doc = _make_doc(fresh_db)
    with pytest.raises(ValueError):
        fresh_db.create_study_session(doc["id"], "break")


def test_session_requires_existing_document(fresh_db):
    with pytest.raises(ValueError):
        fresh_db.create_study_session("nope", "passive")


def test_beat_missing_session_returns_none(fresh_db):
    assert fresh_db.beat_study_session("ghost", 10) is None
    assert fresh_db.stop_study_session("ghost", 10) is None


def test_negative_and_garbage_active_seconds_coerced(fresh_db):
    _, doc = _make_doc(fresh_db)
    s = fresh_db.create_study_session(doc["id"], "pomodoro")
    assert fresh_db.beat_study_session(s["id"], -5)["seconds"] == 0
    assert fresh_db.beat_study_session(s["id"], "garbage")["seconds"] == 0


# ── rollups onto document / workspace cards ─────────────────────────────────

def test_document_and_workspace_totals(fresh_db):
    ws, doc = _make_doc(fresh_db)
    passive = fresh_db.create_study_session(doc["id"], "passive")
    pomo = fresh_db.create_study_session(doc["id"], "pomodoro")
    fresh_db.stop_study_session(passive["id"], 100)
    fresh_db.stop_study_session(pomo["id"], 60)

    assert fresh_db.get_document(doc["id"])["seconds_total"] == 160
    totals = {w["id"]: w["seconds_total"] for w in fresh_db.list_workspaces()}
    assert totals[ws["id"]] == 160


def test_zero_total_when_no_sessions(fresh_db):
    ws, doc = _make_doc(fresh_db)
    assert fresh_db.get_document(doc["id"])["seconds_total"] == 0
    assert {w["id"]: w["seconds_total"] for w in fresh_db.list_workspaces()}[ws["id"]] == 0


# ── cascading deletes ───────────────────────────────────────────────────────

def test_delete_document_removes_sessions(fresh_db):
    _, doc = _make_doc(fresh_db)
    s = fresh_db.create_study_session(doc["id"], "passive")
    fresh_db.beat_study_session(s["id"], 50)
    fresh_db.delete_document(doc["id"])
    assert fresh_db.beat_study_session(s["id"], 60) is None


def test_delete_workspace_removes_sessions(fresh_db):
    ws, doc = _make_doc(fresh_db)
    s = fresh_db.create_study_session(doc["id"], "passive")
    fresh_db.delete_workspace(ws["id"])
    assert fresh_db.beat_study_session(s["id"], 10) is None


# ── migration onto an old database ──────────────────────────────────────────

# ── HTTP routes ─────────────────────────────────────────────────────────────

def test_routes_start_beat_stop(client, fresh_db):
    _, doc = _make_doc(fresh_db)
    r = client.post(f"/api/documents/{doc['id']}/sessions", json={"kind": "passive"})
    assert r.status_code == 201
    sid = r.json()["session"]["id"]

    assert client.post(f"/api/sessions/{sid}/beat", json={"active_seconds": 40}).json()["session"]["seconds"] == 40
    stopped = client.post(f"/api/sessions/{sid}/stop", json={"active_seconds": 75}).json()["session"]
    assert stopped["seconds"] == 75 and stopped["ended_at"]
    assert client.get(f"/api/documents/{doc['id']}").json()["document"]["seconds_total"] == 75


def test_routes_404s(client, fresh_db):
    assert client.post("/api/documents/ghost/sessions", json={}).status_code == 400
    assert client.post("/api/sessions/ghost/beat", json={"active_seconds": 1}).status_code == 404
    assert client.post("/api/sessions/ghost/stop", json={"active_seconds": 1}).status_code == 404


# ── export / import round-trip ──────────────────────────────────────────────

def test_sessions_survive_export_import(fresh_db):
    from app import transfer

    ws, doc = _make_doc(fresh_db)
    s = fresh_db.create_study_session(doc["id"], "passive")
    fresh_db.stop_study_session(s["id"], 123)

    archive = transfer.export_workspace(ws["id"])
    try:
        imported = transfer.import_workspace(archive)
    finally:
        archive.unlink(missing_ok=True)

    totals = {w["id"]: w["seconds_total"] for w in fresh_db.list_workspaces()}
    assert totals[imported["id"]] == 123
    # The copy is independent: its session got a fresh id, remapped to the new workspace.
    new_doc = fresh_db.list_documents(imported["id"])[0]
    assert new_doc["seconds_total"] == 123


def test_migration_adds_table(fresh_db):
    """init_db (re)creates study_sessions on a database that predates the feature.

    Simulate an old DB by dropping the table from an otherwise-current schema, then
    re-run init_db and assert it comes back — exercising the CREATE TABLE IF NOT EXISTS path.
    """
    with fresh_db.db_connection() as c:
        c.execute("DROP TABLE study_sessions")
        gone = c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='study_sessions'"
        ).fetchone()
    assert gone is None

    fresh_db.init_db()
    with fresh_db.db_connection() as c:
        names = {r["name"] for r in c.execute("PRAGMA table_info(study_sessions)").fetchall()}
    assert {"id", "document_id", "workspace_id", "kind", "seconds"} <= names
