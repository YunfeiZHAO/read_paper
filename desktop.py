"""Desktop launcher: run the local FastAPI server and show it in a native window.

This is the entry point for the packaged desktop app (see packaging/readpaper.spec). It:
  1. points the writable data dir at a per-OS application-data location (so a frozen app
     never tries to write inside its read-only bundle),
  2. starts uvicorn on a free localhost port in a background thread,
  3. opens a pywebview window pointing at that server once it is accepting connections.

Run in development with:  uv run python desktop.py
Build a standalone app with: see packaging/build_<os>.sh / build_windows.ps1
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

APP_NAME = "ReadPaper"
WINDOW_TITLE = "Read Paper"


def _default_data_dir() -> Path:
    """Per-OS, user-writable directory for the SQLite DB, uploads, notes, etc."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    if os.name == "nt":
        base = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_NAME
    # Linux / other: follow the XDG base-directory spec.
    base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base) / APP_NAME


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_until_up(url: str, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1):  # noqa: S310 (localhost only)
                return True
        except Exception:  # noqa: BLE001 — server not ready yet
            time.sleep(0.15)
    return False


def main() -> int:
    # Must be set BEFORE importing the app: app.config reads QA_DATA_DIR at import time.
    # Support both QA_DATA_DIR (new) and CODEX_QA_DATA_DIR (legacy) for backward compatibility.
    if not os.environ.get("QA_DATA_DIR") and not os.environ.get("CODEX_QA_DATA_DIR"):
        data_dir = _default_data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        os.environ["QA_DATA_DIR"] = str(data_dir)

    import uvicorn
    import webview  # pywebview

    from app import create_app

    port = _free_port()
    url = f"http://127.0.0.1:{port}"

    app = create_app()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    if not _wait_until_up(url + "/"):
        print(f"Server did not start on {url}", file=sys.stderr)
        return 1

    # Blocks until the window is closed.
    webview.create_window(WINDOW_TITLE, url, width=1360, height=900, min_size=(900, 600))
    webview.start()

    # Window closed → ask uvicorn to shut down; the daemon thread exits with the process.
    server.should_exit = True
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
