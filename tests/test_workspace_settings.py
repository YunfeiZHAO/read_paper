"""Per-workspace LLM settings: masking, fallback resolution, and the PATCH route."""
from __future__ import annotations

import os


def test_public_workspace_masks_key(fresh_db):
    ws = fresh_db.create_workspace("w")
    fresh_db.update_workspace(ws["id"], api_key="secret-token", api_base_url="https://gw.example")
    got = fresh_db.get_workspace(ws["id"])
    assert got["has_api_key"] is True
    assert "api_key" not in got  # the raw secret is never serialized
    assert got["api_base_url"] == "https://gw.example"


def test_settings_resolver_falls_back_to_defaults(fresh_db):
    ws = fresh_db.create_workspace("w")
    # Nothing set → all None (runner then uses env / CLAUDE_API_MODEL).
    settings = fresh_db.get_workspace_llm_settings(ws["id"])
    assert settings == {"base_url": None, "api_key": None, "auth_type": None, "model": None}

    fresh_db.update_workspace(ws["id"], api_model="claude-opus-4-8", api_auth_type="api_key", api_key="k")
    settings = fresh_db.get_workspace_llm_settings(ws["id"])
    assert settings["model"] == "claude-opus-4-8"
    assert settings["auth_type"] == "api_key"
    assert settings["api_key"] == "k"


def test_blank_value_clears_field(fresh_db):
    ws = fresh_db.create_workspace("w")
    fresh_db.update_workspace(ws["id"], api_model="claude-opus-4-8")
    assert fresh_db.get_workspace_llm_settings(ws["id"])["model"] == "claude-opus-4-8"
    fresh_db.update_workspace(ws["id"], api_model="   ")  # blank → NULL
    assert fresh_db.get_workspace_llm_settings(ws["id"])["model"] is None


def test_none_workspace_is_safe(fresh_db):
    assert fresh_db.get_workspace_llm_settings(None)["model"] is None


def test_patch_route_masks_key_and_keeps_it_on_blank(client):
    ws = client.post("/api/workspaces", json={"title": "w"}).json()["workspace"]
    wid = ws["id"]

    # Set a key via the route.
    resp = client.patch(f"/api/workspaces/{wid}", json={"api_key": "sk-test", "api_model": "claude-opus-4-8"})
    body = resp.json()["workspace"]
    assert body["has_api_key"] is True
    assert "api_key" not in body
    assert body["api_model"] == "claude-opus-4-8"

    # Editing only the model with a blank/absent key must NOT wipe the stored key.
    resp = client.patch(f"/api/workspaces/{wid}", json={"api_model": "claude-sonnet-4-6"})
    assert resp.json()["workspace"]["has_api_key"] is True

    # Explicit clear_api_key removes it.
    resp = client.patch(f"/api/workspaces/{wid}", json={"clear_api_key": True})
    assert resp.json()["workspace"]["has_api_key"] is False
