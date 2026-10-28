"""Entry point. The backend lives in the app/ package; this module only creates
and launches the application.

Running `python app.py` executes this as __main__, where `from app import create_app`
resolves to the app/ package (a sibling package shadows the same-named module, and the
two don't conflict). uvicorn then serves the FastAPI app over ASGI.
"""
from __future__ import annotations

import os

from app import create_app

app = create_app()


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    # APP_RELOAD=1 enables uvicorn's auto-reload (handy during development).
    reload = os.environ.get("APP_RELOAD", "0") == "1"
    if reload:
        uvicorn.run("app:create_app", factory=True, host=host, port=port, reload=True)
    else:
        uvicorn.run(app, host=host, port=port)
