"""Central configuration: loads .env, imports optional dependencies, and defines
every constant and path the app uses.

This is the only module that reads environment variables or defines path constants;
every other module imports what it needs from here.
"""
from __future__ import annotations

import os
from pathlib import Path

# BASE_DIR points at the repository root (the parent of the app/ package), so the
# templates/static/.env/instance paths stay stable regardless of where Python is launched.
BASE_DIR = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")
except ImportError:
    pass

# sub-pool-client is only needed by the Codex backend; the Claude backend works without it.
try:
    from sub_pool_client import PooledCodexClient  # noqa: F401  -- re-exported for runners

    SUB_POOL_AVAILABLE = True
except ImportError:
    PooledCodexClient = None  # type: ignore[assignment,misc]
    SUB_POOL_AVAILABLE = False

import anthropic  # noqa: E402,F401  -- imported after .env load, re-exported for runners

# ── Paths ───────────────────────────────────────────────────────────────
DATA_DIR = Path(os.environ.get("CODEX_QA_DATA_DIR", BASE_DIR / "instance"))
DB_PATH = DATA_DIR / "codex_qa.sqlite"
WORK_DIR = Path(os.environ.get("CODEX_QA_WORKDIR", DATA_DIR / "workspace"))
UPLOAD_DIR = WORK_DIR / "uploads"
RUN_DIR = DATA_DIR / "runs"

# ── Codex backend ─────────────────────────────────────────────────────────
CODEX_TIMEOUT_SECONDS = int(os.environ.get("CODEX_TIMEOUT_SECONDS", "1200"))
CODEX_MODEL = os.environ.get("CODEX_MODEL", "gpt-5.5").strip() or "gpt-5.5"
CODEX_REASONING_EFFORT = os.environ.get("CODEX_REASONING_EFFORT", "high").strip() or "high"
CODEX_SERVICE_TIER = os.environ.get("CODEX_SERVICE_TIER", "fast").strip() or "fast"

# ── Claude API backend ──────────────────────────────────────────────────
CLAUDE_API_MODEL = os.environ.get("CLAUDE_API_MODEL", "claude-sonnet-4-6").strip() or "claude-sonnet-4-6"
CLAUDE_API_TIMEOUT_SECONDS = int(os.environ.get("CLAUDE_API_TIMEOUT_SECONDS", str(CODEX_TIMEOUT_SECONDS)))
CLAUDE_BASH_TIMEOUT_SECONDS = int(os.environ.get("CLAUDE_BASH_TIMEOUT_SECONDS", "120"))
CLAUDE_MAX_TOOL_ITERATIONS = int(os.environ.get("CLAUDE_MAX_TOOL_ITERATIONS", "40"))

# ── Shared ──────────────────────────────────────────────────────────────
BACKENDS = {"codex", "claude"}
# Default for the web_search tool (Codex's --search / Claude's web_search tool). The
# frontend can override it per request; this default only applies when it doesn't. Off by default.
ENABLE_WEB_SEARCH = os.environ.get("ENABLE_WEB_SEARCH", "false").strip().lower() in {"1", "true", "yes", "on"}
TEXT_EXCERPT_BYTES = int(os.environ.get("CODEX_QA_TEXT_EXCERPT_BYTES", "60000"))
WORKSPACE_TEXT_PREVIEW_BYTES = int(os.environ.get("CODEX_QA_WORKSPACE_TEXT_PREVIEW_BYTES", "524288"))
WORKSPACE_FILE_LIST_LIMIT = int(os.environ.get("CODEX_QA_WORKSPACE_FILE_LIST_LIMIT", "500"))

# File-type classification used by the workspace preview endpoints.
MARKDOWN_EXTENSIONS = {".md", ".markdown", ".mdown"}
TEXT_PREVIEW_EXTENSIONS = {
    ".csv",
    ".css",
    ".html",
    ".js",
    ".json",
    ".jsonl",
    ".log",
    ".py",
    ".rst",
    ".text",
    ".toml",
    ".ts",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
