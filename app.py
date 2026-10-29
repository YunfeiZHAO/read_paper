"""Entry point. The backend lives in the app/ package; this module only creates
and launches the application.

Running `python app.py` executes this as __main__, where `from app import create_app`
resolves to the app/ package (a sibling package shadows the same-named module, and the
two don't conflict). uvicorn then serves the FastAPI app over ASGI.
"""
from __future__ import annotations

import os
import socket

from app import create_app

app = create_app()


def find_available_port(host: str, preferred_port: int) -> int:
    """Find an available port, starting with the preferred port."""
    port = preferred_port
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
                return port
            except OSError:
                port += 1


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("HOST", "0.0.0.0")
    preferred_port = int(os.environ.get("PORT", "8080"))
    port = find_available_port(host if host != "0.0.0.0" else "127.0.0.1", preferred_port)
    if port != preferred_port:
        print(f"Port {preferred_port} is in use, using port {port} instead")
    # APP_RELOAD=1 enables uvicorn's auto-reload (handy during development).
    reload = os.environ.get("APP_RELOAD", "0") == "1"
    if reload:
        uvicorn.run("app:create_app", factory=True, host=host, port=port, reload=True)
    else:
        uvicorn.run(app, host=host, port=port)
