# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository overview

This repo has two independent parts that share nothing but the git history:

1. **Codex Tree QA** (repo root: `app.py`, `templates/`, `static/`) — a Flask web app for tree-structured Q&A against the Codex CLI. Each `user -> assistant` turn is a node; you can branch (fork) a new question from any prior node, and forks form a tree rather than a linear chat.
2. **`cli/` (treeflow)** — a separate, standalone, stdlib-only Python package (own `pyproject.toml`) that browses and resumes nodes in local Claude Code / Codex CLI conversation history (`~/.claude/projects/**/*.jsonl`, `~/.codex/sessions/**/*.jsonl`). It is not imported by the Flask app; the two projects are related in purpose (both model agent conversations as trees) but have no code dependency.

## Commands

### Flask app (root)

Dependencies are managed with **uv** (`pyproject.toml` + `uv.lock`). The backend lives in the
`app/` package; root `app.py` is a thin entrypoint (`from app import create_app`).

```bash
uv sync                            # Claude-only deps (Flask, anthropic, python-dotenv)
uv sync --extra codex              # also install sub-pool-client for the Codex backend
uv run python app.py               # serves on http://127.0.0.1:8080 (0.0.0.0)
```

`sub-pool-client` is an optional extra (`[codex]`): the Codex backend fails gracefully with a
"切换到 Claude 后端" message when it isn't installed.

Config via env vars (see `.env.example`, loaded automatically from `.env`):
- `CODEX_AUTH_FILE`, `CODEX_MODEL` (default `gpt-5.5`), `CODEX_TIMEOUT_SECONDS` (default 1200), `CODEX_REASONING_EFFORT`, `CODEX_SERVICE_TIER`, `PORT`, `HOST`, `FLASK_DEBUG`.
- `SUB_POOL_URL` / `SUB_POOL_KEY` — credentials for the `sub-pool-client` package that leases a Codex account per request (see Architecture below).

There is no test suite and no lint config in this repo currently.

### treeflow CLI (`cli/`)

```bash
cd cli
pip install -e .          # editable install; `treeflow` / `python -m treeflow` on PATH
treeflow                  # interactive: root picker -> tree browser
treeflow --codex          # same, but browse Codex CLI sessions instead of Claude Code
treeflow -p ~/some/repo roots --json   # non-interactive, scriptable (every subcommand supports --json)
```

Pure stdlib (Python 3.11+, no third-party deps — `tomllib` is why 3.11 is the floor). See `cli/README.md` for the full command/key reference; it's detailed and current, read it before changing CLI behavior.

## Architecture (Flask app)

### Data model
Everything lives in one SQLite DB (`instance/codex_qa.sqlite`, path overridable via `CODEX_QA_DATA_DIR`):
- `nodes` — one row per Q&A turn. Self-referencing `parent_id` forms the tree. Holds prompt, answer, status (`queued` → `running` → `done`/`failed`), token usage, tool-call count, raw Codex JSONL events (`raw_events_json`, trimmed/truncated for storage), a per-node freeform `note_md`, and workspace file references.
- `files` — uploaded attachments (stored under `instance/workspace/uploads/<file_id>/`), with a text excerpt cached for text-like files.
- `node_workspace_files` — tracks which files in the shared workspace directory changed as a side effect of running a given node (diffed via `workspace_snapshot()` before/after each Codex run).
- `account_usage` / `account_probe` — legacy per-account token accounting; largely superseded by the sub-pool client (below).

### Prompt construction and tree semantics
`build_codex_prompt()` in `app.py` is the core of the "tree" behavior: when a new node is created under a parent, it walks the full ancestor chain (`path_to_node`), and stitches every ancestor's prompt/answer/workspace-refs plus inherited attachments into one prompt block, followed by the current node's new attachments and question. This is what lets a user fork from any node and get full context without a literal chat history API — every Codex invocation is a fresh, fully self-contained `codex exec` call.

### Running Codex
`_run_codex_for_node_async()` shells out to `codex exec --json ... -m <model>` via `asyncio.create_subprocess_exec`, streaming stdin (the built prompt) and reading stdout as newline-delimited JSON events line-by-line (raised `limit=64MB` on the stream reader because Codex can emit very large single events). Events are persisted incrementally to the node's `raw_events_json` as they arrive so the frontend can poll and show live tool/tool-call/search activity. `--dangerously-bypass-approvals-and-sandbox` and `--search` are always passed — Codex has full local exec + web search for every request.

Account/auth is handled entirely by an external package, `sub-pool-client` (`PooledCodexClient`), which leases a Codex account for the duration of one run and reports 429s back to the pool for cooldown; `app.py` does not manage `auth.json` files or account selection directly. Frontend never sees or picks accounts (`/api/accounts` always reports `fixed`).

### Workspace vs. uploads
- Uploads (via `/api/upload`) are content the user explicitly attaches to a question; stored under `instance/workspace/uploads/`, referenced by node via `attachments_json`.
- The rest of `instance/workspace/` is a shared scratch dir Codex can read/write freely (via `-C <WORK_DIR>`); `workspace_snapshot()`/`record_workspace_changes()` diff this directory before/after each run to attribute new/changed files to the node that produced them, and `inferred_workspace_files_for_nodes()` retroactively attributes orphaned files by scanning prompt/answer text and creation-time windows when no explicit tracking exists.

### Claude Code history import (unrelated to Codex runs)
A separate read-only feature (`/api/claude-code/tree`, `read_claude_code_nodes()`) parses `~/.claude/projects/<slug>/*.jsonl` files directly to reconstruct a Claude Code session tree for viewing inside the same tree UI (toggled via the "历史" mode button in the frontend). `/api/claude-code/resume-from-node` writes a synthetic session JSONL (mirroring the technique used independently in `cli/treeflow`) so an abandoned branch can be resumed with `claude --resume <sid>` even though stock Claude Code can't normally reach it. This logic is copy-adjacent to, but independent from, the `treeflow` CLI package — changes to one do not need to be mirrored in the other unless you're deliberately keeping them in sync.

### Frontend
`static/app.js` (~2600 lines, vanilla JS, no build step) renders the tree as an SVG canvas (pan/zoom, node layout in `layout`/`nodeById`/`childrenByParent` maps in `state`), plus a side panel for conversation/notes and a workspace file drawer. `templates/index.html` is a single-page shell; `styles.css` has the visual styling. All state lives in the top-level `state` object in `app.js`; there's no framework or module bundler, so new UI features are added as functions operating on that shared object plus direct DOM manipulation.
