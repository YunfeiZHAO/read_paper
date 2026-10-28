"""应用工厂:组装 Flask app(模板/静态指向仓库根)、建库、注册路由。"""
from __future__ import annotations

from flask import Flask

from . import config


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=str(config.BASE_DIR / "templates"),
        static_folder=str(config.BASE_DIR / "static"),
    )
    app.config["MAX_CONTENT_LENGTH"] = config.MAX_CONTENT_LENGTH

    from .db import ensure_dirs, init_db

    ensure_dirs()
    init_db()

    from .routes import bp

    app.register_blueprint(bp)
    return app
