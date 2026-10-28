# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository overview

This repo has two independent parts that share nothing but the git history:

1. **PDF Tree QA** (repo root: `app.py`, `templates/`, `static/`) — a **FastAPI** web app for reading PDFs and asking tree-structured questions about them. Each PDF is a top-level *document* that owns its own highlights and its own question tree. You read the PDF in a real viewer (PDF.js), highlight text, and ask the LLM (Codex CLI or Claude API) a question directly from a selection; each question is a node, and you can fork a new question from any prior node, so forks form a tree rather than a linear chat.
2. **`cli/` (treeflow)** — a separate, standalone, stdlib-only Python package (own `pyproject.toml`) that browses and resumes nodes in local Claude Code / Codex CLI conversation history (`~/.claude/projects/**/*.jsonl`, `~/.codex/sessions/**/*.jsonl`). It is not imported by the web app; the two projects are related in purpose (both model agent conversations as trees) but have no code dependency.

## Commands

### Web app (root)

Dependencies are managed with **uv** (`pyproject.toml` + `uv.lock`). The backend lives in the
`app/` package; root `app.py` is a thin entrypoint (`from app import create_app`) served by uvicorn.

```bash
uv sync                            # Claude-only deps (FastAPI, uvicorn, anthropic, python-dotenv)
uv sync --extra codex              # also install sub-pool-client for the Codex backend
uv run python app.py               # serves on http://127.0.0.1:8080 (0.0.0.0)
```

`sub-pool-client` is an optional extra (`[codex]`): the Codex backend fails gracefully with a
"switch to the Claude backend" message when it isn't installed.

Config via env vars (see `.env.example`, loaded automatically from `.env`):
- Claude backend: `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN` (or `ANTHROPIC_API_KEY`), `CLAUDE_API_MODEL` (default `claude-sonnet-4-6`).
- Codex backend: `CODEX_MODEL` (default `gpt-5.5`), `CODEX_TIMEOUT_SECONDS` (default 1200), `CODEX_REASONING_EFFORT`, `CODEX_SERVICE_TIER`, plus `SUB_POOL_URL` / `SUB_POOL_KEY` for the `sub-pool-client` package that leases a Codex account per request (see Architecture below).
- Server: `PORT`, `HOST`, `APP_RELOAD` (set `1` for uvicorn auto-reload). `ENABLE_WEB_SEARCH` is the default web-search toggle.

Tests: `uv run pytest` (pytest suite under `tests/`, config in `pyproject.toml`). No lint config.

### treeflow CLI (`cli/`)

```bash
cd cli
pip install -e .          # editable install; `treeflow` / `python -m treeflow` on PATH
treeflow                  # interactive: root picker -> tree browser
treeflow --codex          # same, but browse Codex CLI sessions instead of Claude Code
treeflow -p ~/some/repo roots --json   # non-interactive, scriptable (every subcommand supports --json)
```

Pure stdlib (Python 3.11+, no third-party deps — `tomllib` is why 3.11 is the floor). See `cli/README.md` for the full command/key reference; it's detailed and current, read it before changing CLI behavior.

## Architecture (web app)

The web layer is **FastAPI** (`app/__init__.py` builds the app, `app/routes.py` is one `APIRouter`,
`app.py` runs it under uvicorn). Request bodies are read with `await request.json()`; files come in as
`UploadFile`; responses are plain dicts or `FileResponse`/`JSONResponse`. The runner layer
(`app/runners/`, a daemon thread per node running an async Codex/Claude call) is framework-agnostic and
unchanged by the framework choice. `app/uploads.py` has its own `secure_filename` (no werkzeug).

### Data model
Everything lives in one SQLite DB (`instance/codex_qa.sqlite`, path overridable via `CODEX_QA_DATA_DIR`):
- `documents` — one row per uploaded PDF (the top-level object / "project"). Links to its stored `files` row via `file_id`; a document owns its highlights and its question tree (nodes carry `document_id`).
- `highlights` — one row per saved PDF highlight: `document_id`, `page` (0-based), `rects_json` (array of **normalized** `{x,y,w,h}` page-fraction rects, so anchors survive reload/zoom), `quote`, `color`, and a nullable `node_id` (set when a question is asked from that highlight). Saving a highlight never creates a node.
- `nodes` — one row per Q&A turn. Self-referencing `parent_id` forms the tree; `document_id` scopes a node to its PDF and `source_highlight_id` links back to the highlight a question was asked from. Holds prompt, answer, status (`queued` → `running` → `done`/`failed`), token usage, tool-call count, raw JSONL events (`raw_events_json`, trimmed for storage), and a per-node freeform `note_md`.
- `files` — stored uploads (PDFs and any attachments) under `instance/workspace/uploads/<file_id>/`.
- `node_workspace_files` — tracks workspace scratch-dir files changed as a side effect of a node run; surfaced in the reader's workspace-files side tab.
- `workspaces` — a top-level user project grouping a set of PDFs (`documents.workspace_id`) and shared files.

### Prompt construction and tree semantics
`build_codex_prompt()` in `app/prompts.py` is the core of the "tree" behavior: when a new node is created under a parent, it walks the full ancestor chain (`path_to_node`) and stitches every ancestor's prompt/answer/workspace-refs into one prompt block, then appends the current node's attachments, the **selected PDF quote** (from its source highlight, if any), and the question. This is what lets a user fork from any node and get full context without a literal chat history API — every backend invocation is a fresh, fully self-contained call.

### Running Codex
`_run_codex_for_node_async()` shells out to `codex exec --json ... -m <model>` via `asyncio.create_subprocess_exec`, streaming stdin (the built prompt) and reading stdout as newline-delimited JSON events line-by-line (raised `limit=64MB` on the stream reader because Codex can emit very large single events). Events are persisted incrementally to the node's `raw_events_json` as they arrive so the frontend can poll and show live tool/tool-call/search activity. `--dangerously-bypass-approvals-and-sandbox` and `--search` are always passed — Codex has full local exec + web search for every request.

Account/auth is handled entirely by an external package, `sub-pool-client` (`PooledCodexClient`), which leases a Codex account for the duration of one run and reports 429s back to the pool for cooldown; `app.py` does not manage `auth.json` files or account selection directly. The frontend never sees or picks accounts.

### Workspace vs. uploads
- Uploads (via `/api/upload`) are content the user explicitly attaches to a question; stored under `instance/workspace/uploads/`, referenced by node via `attachments_json`.
- The rest of `instance/workspace/` is a shared scratch dir Codex can read/write freely (via `-C <WORK_DIR>`); `workspace_snapshot()`/`record_workspace_changes()` diff this directory before/after each run to attribute new/changed files to the node that produced them, and `inferred_workspace_files_for_nodes()` retroactively attributes orphaned files by scanning prompt/answer text and creation-time windows when no explicit tracking exists.

### Frontend
`static/app.js` (vanilla JS, no build step) drives a **three-pane layout — PDF │ tree │ chat** — each pane collapsible to a rail (`state.paneOpen`, persisted in `localStorage`) and the PDF/chat widths resizable. Navigation is hash-routed through three views: home (workspace cards) → workspace (PDF list) → reader (the three-pane view); see `parseRoute()`/`applyRoute()`. The middle pane renders the question tree as an SVG canvas (pan/zoom, `layout`/`nodeById`/`childrenByParent` maps in `state`); the right pane is conversation + notes. The left pane is a **PDF.js** viewer (vendored UMD build at `static/vendor/pdfjs/`): each page is a canvas + a selectable text layer + a highlight overlay. Selecting text pops a floating toolbar (highlight / ask); highlights are stored as normalized rects and redrawn by `drawHighlightsForPage()`. `templates/index.html` is the single-page shell (served at `/`, static mounted at `/static`); `styles.css` has the styling. All state lives in the top-level `state` object; new UI features are functions operating on that shared object plus direct DOM manipulation.
