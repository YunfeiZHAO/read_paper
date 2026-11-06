"""Application factory: build the FastAPI app, mount the repo-root static/ directory,
create the database, and register the routes.
"""
from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.staticfiles import StaticFiles

from . import config


def _normalize_swagger_upload_schema(schema: dict[str, Any]) -> None:
    components = schema.get("components", {}).get("schemas", {})
    for name, body_schema in components.items():
        if not name.startswith("Body_"):
            continue
        for field_schema in body_schema.get("properties", {}).values():
            _normalize_upload_field(field_schema)
        properties = body_schema.get("properties", {})
        if "files" in properties and "file" in properties:
            # Keep the legacy alias accepted at runtime, not as a second Swagger input.
            properties.pop("file")


def _normalize_upload_field(field_schema: dict[str, Any]) -> None:
    variants = field_schema.get("anyOf")
    if variants:
        upload_variants = [
            variant
            for variant in variants
            if variant.get("type") != "null" and _contains_content_media_type(variant)
        ]
        if len(upload_variants) == 1:
            metadata = {key: value for key, value in field_schema.items() if key != "anyOf"}
            field_schema.clear()
            field_schema.update(upload_variants[0])
            field_schema.update(metadata)

    if "contentMediaType" in field_schema:
        field_schema.pop("contentMediaType")
        field_schema["format"] = "binary"

    items = field_schema.get("items")
    if isinstance(items, dict):
        _normalize_upload_field(items)


def _contains_content_media_type(schema: dict[str, Any]) -> bool:
    if "contentMediaType" in schema:
        return True
    items = schema.get("items")
    if isinstance(items, dict) and _contains_content_media_type(items):
        return True
    return any(
        _contains_content_media_type(variant)
        for variant in schema.get("anyOf", [])
        if isinstance(variant, dict)
    )


def create_app() -> FastAPI:
    openapi_tags = [
        {"name": "Authentication", "description": "Login and session status."},
        {"name": "Questions", "description": "Question tree, answers, and node management."},
        {"name": "Workspaces", "description": "Workspace and category management."},
        {"name": "Documents", "description": "PDF document management."},
        {"name": "Highlights", "description": "PDF highlights and annotations."},
        {"name": "Study Sessions", "description": "Document reading-time tracking."},
        {
            "name": "Files",
            "description": (
                "Upload question attachments with `/api/upload`, or import PDFs as "
                "workspace documents with `/api/batch-upload`."
            ),
        },
        {"name": "Workspace Files", "description": "Browse and preview AI workspace files."},
        {"name": "Notes", "description": "Read and update notes."},
        {"name": "Import & Export", "description": "Import and export workspaces or question trees."},
        {"name": "Application", "description": "Web application shell and static entry points."},
    ]
    app = FastAPI(
        title="PDF Tree QA",
        openapi_tags=openapi_tags,
    )

    allowed_origins_env = os.environ.get("CORS_ALLOWED_ORIGINS", "")
    allowed_origins = [o.strip() for o in allowed_origins_env.split(",") if o.strip()]
    if allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=allowed_origins,
            allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type"],
        )

    from .db import ensure_dirs, init_db

    ensure_dirs()
    init_db()

    app.mount(
        "/static",
        StaticFiles(directory=str(config.BASE_DIR / "static")),
        name="static",
    )

    from .routes import router

    app.include_router(router)

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            routes=app.routes,
            tags=openapi_tags,
            openapi_version="3.0.3",
        )
        _normalize_swagger_upload_schema(schema)
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi
    return app
