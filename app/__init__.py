"""应用工厂:组装 FastAPI app(静态目录挂到仓库根 static/)、建库、注册路由。"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import config


def create_app() -> FastAPI:
    app = FastAPI(title="PDF Tree QA", docs_url=None, redoc_url=None)

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
