"""How /api/ask resolves enable_web_search: fall back to config when omitted, pass through when explicit."""
from __future__ import annotations

import app.routes as routes


def _capture(monkeypatch):
    captured = {}

    def fake_create_node(
        parent_id,
        prompt,
        file_ids,
        workspace_refs,
        backend="codex",
        enable_web_search=False,
        document_id=None,
        source_highlight_id=None,
    ):
        captured["enable_web_search"] = enable_web_search
        return {"id": "n1", "prompt": prompt, "backend": backend}

    monkeypatch.setattr(routes, "create_node", fake_create_node)
    monkeypatch.setattr(routes, "start_worker", lambda node_id: None)
    return captured


def test_defaults_to_config_false(client, monkeypatch):
    captured = _capture(monkeypatch)
    monkeypatch.setattr(routes, "ENABLE_WEB_SEARCH", False)

    resp = client.post("/api/ask", json={"prompt": "hi"})
    assert resp.status_code == 202
    assert captured["enable_web_search"] is False


def test_defaults_to_config_true(client, monkeypatch):
    captured = _capture(monkeypatch)
    monkeypatch.setattr(routes, "ENABLE_WEB_SEARCH", True)

    resp = client.post("/api/ask", json={"prompt": "hi"})
    assert resp.status_code == 202
    assert captured["enable_web_search"] is True


def test_explicit_true_overrides(client, monkeypatch):
    captured = _capture(monkeypatch)
    monkeypatch.setattr(routes, "ENABLE_WEB_SEARCH", False)

    resp = client.post("/api/ask", json={"prompt": "hi", "enable_web_search": True})
    assert resp.status_code == 202
    assert captured["enable_web_search"] is True


def test_explicit_false_overrides(client, monkeypatch):
    captured = _capture(monkeypatch)
    monkeypatch.setattr(routes, "ENABLE_WEB_SEARCH", True)

    resp = client.post("/api/ask", json={"prompt": "hi", "enable_web_search": False})
    assert resp.status_code == 202
    assert captured["enable_web_search"] is False
