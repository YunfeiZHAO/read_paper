"""Session auth: the same JWT works as an `auth_token` cookie (browser) or a Bearer header
(native iOS client, scripts) on every protected route."""
from __future__ import annotations

import pytest

from app import auth, routes

SECRET = "test-secret-key-for-auth-tests-0123456789"


@pytest.fixture()
def auth_on(monkeypatch):
    monkeypatch.setattr(routes, "AUTH_ENABLED", True)
    monkeypatch.setattr(auth, "AUTH_SECRET_KEY", SECRET)
    return auth.create_session_token("reader@example.com")


def test_no_credentials_is_401(client, auth_on):
    assert client.get("/api/workspaces").status_code == 401


def test_bearer_token_accepted(client, auth_on):
    response = client.get("/api/workspaces", headers={"Authorization": f"Bearer {auth_on}"})
    assert response.status_code == 200


def test_bearer_scheme_is_case_insensitive(client, auth_on):
    response = client.get("/api/workspaces", headers={"Authorization": f"bearer {auth_on}"})
    assert response.status_code == 200


def test_invalid_bearer_token_is_401(client, auth_on):
    response = client.get("/api/workspaces", headers={"Authorization": "Bearer not-a-jwt"})
    assert response.status_code == 401


def test_bearer_takes_precedence_over_cookie(client, auth_on):
    client.cookies.set("auth_token", auth_on)
    response = client.get("/api/workspaces", headers={"Authorization": "Bearer forged"})
    assert response.status_code == 401


def test_cookie_still_accepted(client, auth_on):
    client.cookies.set("auth_token", auth_on)
    assert client.get("/api/workspaces").status_code == 200


def test_verify_accepts_bearer(client, auth_on):
    response = client.get("/api/auth/verify", headers={"Authorization": f"Bearer {auth_on}"})
    assert response.json() == {"authenticated": True, "email": "reader@example.com"}
    assert client.get("/api/auth/verify").json() == {"authenticated": False}


def test_export_and_import_require_auth(client, auth_on):
    assert client.get("/api/workspaces/whatever/export").status_code == 401
    response = client.post(
        "/api/workspaces/import", files={"file": ("ws.zip", b"PK", "application/zip")}
    )
    assert response.status_code == 401


def test_batch_upload_accepts_bearer(client, auth_on):
    response = client.post(
        "/api/batch-upload",
        headers={"Authorization": f"Bearer {auth_on}"},
        files={"files": ("paper.pdf", b"%PDF-1.4\n%%EOF\n", "application/pdf")},
    )
    assert response.status_code == 207
