# Project guide (for newcomers)

> For anyone new to this codebase. After reading it you'll have a mental model of the whole
> thing: where each piece lives and why it's built the way it is.

## 0. This repo has two independent parts

| Part | Location | What it is |
|------|----------|------------|
| **PDF Tree QA** (this guide's subject) | repo root: `app.py` + `app/` + `templates/` + `static/` | A FastAPI web app that organizes Q&A as a **tree** |
| **treeflow CLI** | `cli/` | A fully standalone command-line tool for browsing local Claude Code / Codex session history. **No code dependency on the web app**; ignore it when getting started |

They share only git history and the "model conversations as a tree" idea; they never import
each other.

---

## 1. Core idea: Q&A organized as a *tree*

A normal chat is **one line**: ask → answer → ask → answer.

This project is a **tree**: each "question + answer" is a **node**, and you can fork a new
question from **any earlier node**.

```
Root: what is this paper about?
├─ what's novel about its method?     ← forked from the root
│   └─ how does it compare to XX?
└─ what's the experimental setup?     ← also forked from the root (parallel branch)
    └─ how big is the dataset?
```

**Why build it this way**: when reading a paper you often want to branch off one point in the
abstract and dig into the method and the experiments separately — each path keeps its own
independent context and doesn't interfere with the others.

**Key mechanism**: on every question, the system walks from the current node up to the root and
stitches every ancestor's "question + answer + attachments + workspace references" into one
complete prompt for the LLM (see `build_codex_prompt` in `app/prompts.py`). So every LLM call is
**fully self-contained** and relies on no server-side "chat memory" — which is exactly what lets
you fork freely from any node.

---

## 2. The full flow of one question (most important)

After you pick a backend, type a question, and hit send:

```
1. Frontend POSTs /api/ask
        ↓  app/routes.py: api_ask()
2. create_node(...)          write the node to SQLite, status = queued
        ↓  app/db.py
3. start_worker(node_id)     spawn a background thread to run the LLM
        ↓  app/runners/__init__.py
4. pick a runner from the node's `backend` field:
     claude → app/runners/claude.py   (Anthropic API, bash + web_search tools)
     codex  → app/runners/codex.py    (shells out to `codex exec`)
        ↓
5. inside the runner:
     build_codex_prompt()           stitch the ancestor context     (app/prompts.py)
     workspace_snapshot()           snapshot files before the run   (app/workspace.py)
     loop the LLM, meanwhile:
       - each batch of events → update_node() into raw_events_json  ← frontend polling sees "live" progress
       - the LLM runs commands → executed in instance/workspace/
     record_workspace_changes()     diff against the snapshot, attribute produced files
     update_node(status=done, answer=...)
        ↓
6. the frontend periodically GETs /api/tree, sees status flip to done, and renders the answer
```

**Remember**: the backend does **not** wait for the LLM to finish before responding.
`/api/ask` returns immediately (HTTP 202); the LLM runs in a background thread and the frontend
polls `/api/tree` for progress.

---

## 3. The two backends

| | Codex | Claude |
|---|---|---|
| How it's called | shells out to the `codex exec` CLI subprocess | Anthropic Python SDK |
| Requires | the `sub-pool-client` package (leases an account) | `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN`, or `ANTHROPIC_API_KEY` |
| Code | `app/runners/codex.py` | `app/runners/claude.py` |
| Tools | web search + full local exec | bash tool + (optional) web_search |

Both produce an "event stream" parsed uniformly by `app/events.py` (token usage, tool counting,
answer extraction), so the frontend doesn't care which backend ran.

> Note: when `sub-pool-client` isn't installed the Codex backend returns a friendly error and
> suggests switching to Claude; the Claude backend is unaffected.

---

## 4. Directory layout

```
read_paper/
├── app.py                  # thin entry point: app = create_app(); `python app.py` to launch
├── app/                    # backend package
│   ├── __init__.py         # create_app() factory: build FastAPI, create DB, register routes
│   ├── config.py           # .env loading, optional imports, all constants and paths
│   ├── utils.py            # pure helpers: time / JSON / text
│   ├── db.py               # SQLite: connection, schema, CRUD (the data backbone)
│   ├── workspace.py        # tracking files in the shared workspace scratch dir
│   ├── prompts.py          # stitch ancestor chain + attachments into a self-contained prompt (the "soul" of the tree)
│   ├── events.py           # event-stream parsing: tokens, tool count, answer, JSONL
│   ├── runners/
│   │   ├── __init__.py     # start_worker: pick a runner by backend, run in a background thread
│   │   ├── codex.py        # Codex backend
│   │   └── claude.py       # Claude API backend (agentic loop)
│   ├── uploads.py          # persist user attachments + cache a text excerpt
│   ├── export.py           # export a node's subtree as Markdown
│   └── routes.py           # all HTTP routes (one FastAPI APIRouter)
├── templates/index.html    # single-page shell
├── static/
│   ├── app.js              # all frontend logic (vanilla JS, SVG tree, no framework)
│   └── styles.css
├── instance/               # runtime data (SQLite, workspace, uploads)
│   ├── qa.sqlite
│   └── workspace/          # shared scratch dir the AI can read/write
│       └── uploads/        # user-uploaded attachments
└── cli/                    # standalone treeflow CLI (unrelated to the above)
```

### Module dependency direction (acyclic)

```
config  ← every module
utils   ← db / workspace / prompts / uploads
db      ← workspace / prompts / export / uploads / runners / routes
workspace / events / prompts ← runners / routes
runners / uploads / export ← routes
routes  ← __init__ (create_app registers the router)
```

---

## 5. Data model (SQLite; see `init_db` in `app/db.py`)

| Table | Role |
|-------|------|
| `workspaces` | a user project: a group of PDFs + a shared file drawer |
| `documents` | one uploaded PDF; owns its highlights and its question tree |
| `highlights` | saved PDF selections (normalized rects), optionally the source of a question |
| `nodes` | one Q&A turn. `parent_id` self-reference forms the tree. Stores prompt, answer, status (`queued`→`running`→`done`/`failed`), token usage, tool-call count, raw event JSON, per-node notes, backend, etc. |
| `files` | uploaded attachments; text-like files cache an excerpt |
| `node_workspace_files` | which files a node's run created/changed under the workspace dir |

**The tree is just `nodes.parent_id` self-referencing.** A node with `parent_id` NULL is a root.
See [docs/DATABASE_SCHEMA.md](docs/DATABASE_SCHEMA.md) for the full schema.

---

## 6. Two easily-confused "file" concepts

| | Uploaded attachment | Workspace file |
|---|---|---|
| Produced by | the user, attached to a question | the AI reading/writing under the workspace dir at run time |
| Stored at | `instance/workspace/uploads/<file_id>/` | anywhere under `instance/workspace/` |
| Code | `app/uploads.py` | `app/workspace.py` |
| Attribution | explicit, in the node's `attachments_json` | before/after snapshot diff, written to `node_workspace_files` |

---

## 7. How to run it

Dependencies are managed with **uv** (`pyproject.toml` + `uv.lock`):

```bash
uv sync                 # install deps for the Claude backend
uv sync --extra codex   # add this when you need the Codex backend (installs sub-pool-client)
uv run python app.py    # default http://127.0.0.1:8080
```

Configure Claude credentials in `.env` to use the Claude backend:

```bash
ANTHROPIC_BASE_URL=...      # custom gateway; or use ANTHROPIC_API_KEY for the official API
ANTHROPIC_AUTH_TOKEN=...
CLAUDE_API_MODEL=claude-sonnet-4-6
```

Open the page, pick **Claude** with the `Codex | Claude` toggle in the top bar, then upload a
paper PDF and start asking.

> You can also launch it from VSCode with F5 (see `.vscode/launch.json`); it reads `.env`
> automatically.

---

## 8. Suggested reading order

1. `init_db()` in `app/db.py` — read the schema first to understand the `nodes` table (the tree's data backbone)
2. `app/routes.py` — every API entry point; focus on `api_ask` to see what the frontend can do
3. `build_codex_prompt` in `app/prompts.py` — the "soul" of the tree structure
4. `run_loop()` in `app/runners/claude.py` — the agentic loop (call LLM → run tools → call again)
5. the rest: `workspace.py` / `events.py` / `uploads.py` / `export.py` as needed
6. the frontend `static/app.js` — draws the tree with SVG; all state lives in the top-level `state` object
