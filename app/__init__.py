"""Application factory: build the FastAPI app, mount the repo-root static/ directory,
create the database, and register the routes.
"""
from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import config


def create_app() -> FastAPI:
    app = FastAPI(title="PDF Tree QA", docs_url=None, redoc_url=None)

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
    return app
