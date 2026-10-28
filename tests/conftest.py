"""Shared fixtures: point the data directory at a temp dir so tests never touch the real instance/.

Important: config reads CODEX_QA_DATA_DIR at import time and fixes the DB/work-dir paths then,
so the env var must be set **before any app module is imported**. We pin a process-level temp
directory before importing app, and the fixtures rebuild a clean database as needed.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# Pin the data directory to a temp location before importing app.
_TMP_DATA_DIR = tempfile.mkdtemp(prefix="treeqa-tests-")
os.environ["CODEX_QA_DATA_DIR"] = _TMP_DATA_DIR
# Tests default to web search off (matching production); individual tests monkeypatch as needed.
os.environ.setdefault("ENABLE_WEB_SEARCH", "false")


@pytest.fixture()
def fresh_db(tmp_path: Path):
    """A clean SQLite database per test."""
    from app import db

    db_path = tmp_path / "test.sqlite"
    # Redirect DB_PATH to a per-test file and initialize it.
    original = db.DB_PATH
    db.DB_PATH = db_path
    try:
        db.ensure_dirs()
        db.init_db()
        yield db
    finally:
        db.DB_PATH = original


@pytest.fixture()
def client(fresh_db):
    """A FastAPI test client backed by the fresh_db database."""
    from fastapi.testclient import TestClient

    from app import create_app

    app = create_app()
    with TestClient(app) as test_client:
        yield test_client
