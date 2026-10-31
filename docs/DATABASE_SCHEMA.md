# Database Schema — PDF Tree QA

The web app stores everything in one SQLite database:
`instance/qa.sqlite` (override the directory with `QA_DATA_DIR`, or the legacy `CODEX_QA_DATA_DIR`).
The schema is defined in [app/db.py](../app/db.py) (`init_db()`).

## Mental model

A **document** is one uploaded PDF — the top-level "project". It owns:

- **highlights** — saved selections drawn on the PDF.
- **nodes** — Q&A turns that form a *tree* (each node's `parent_id` points at
  the node it was forked from). A question can be asked directly from a
  highlight (`nodes.source_highlight_id`), and that highlight is linked back
  to the node it spawned (`highlights.node_id`).

Supporting tables: **files** (physical uploads on disk) and **node_workspace_files**
(files a run touched under the workspace scratch directory). The top-level **workspaces**
table groups PDFs into user projects.

## Entity-relationship diagram

```
                         ┌───────────────┐
                         │     files     │  physical uploads on disk
                         │───────────────│  (PDFs + attachments)
                         │ id (PK)       │
                         │ original_name │
                         │ stored_name   │
                         │ path, mime    │
                         │ size          │
                         │ text_excerpt  │
                         │ created_at    │
                         └───────┬───────┘
                                 │ file_id (1 PDF → 1 document)
                                 ▼
                         ┌───────────────┐
                         │   documents   │  one row per PDF ("project")
                         │───────────────│
                         │ id (PK)       │
                         │ file_id (FK)  │
                         │ title         │
                         │ num_pages     │
                         │ archived      │
                         │ last_opened_at│
                         └───┬───────┬───┘
            document_id      │       │   document_id
         ┌───────────────────┘       └───────────────────┐
         ▼                                                ▼
 ┌─────────────────┐                            ┌──────────────────────┐
 │   highlights    │                            │        nodes         │  Q&A tree
 │─────────────────│                            │──────────────────────│
 │ id (PK)         │   source_highlight_id      │ id (PK)              │
 │ document_id(FK) │◀───────────────────────────│ parent_id (FK→nodes) │─┐ self-ref
 │ page (0-based)  │                            │ document_id (FK)     │ │ (tree)
 │ rects_json      │   node_id                  │ source_highlight_id  │◀┘
 │ quote, color    │───────────────────────────▶│ prompt / answer      │
 │ node_id (FK)    │                            │ status, backend      │
 │ created_at      │                            │ model, tokens...     │
 └─────────────────┘                            │ note_md, raw_events  │
                                                └──────────┬───────────┘
                                                           │ node_id
                                                           ▼
                                               ┌──────────────────────────┐
                                               │  node_workspace_files    │
                                               │──────────────────────────│
                                               │ (node_id, rel_path) (PK) │
                                               │ path, size, mtime        │
                                               └──────────────────────────┘

 Top-level grouping:  workspaces (id PK) ──< documents.workspace_id, files.workspace_id
```

`highlights` ↔ `nodes` is a two-way link:
- `nodes.source_highlight_id` → the highlight a question was asked from.
- `highlights.node_id` → the node a highlight spawned.
Saving a highlight **never** creates a node; the link is only set when you ask
a question from it.

## Tables

### `documents` — one uploaded PDF (the top-level object)
| column | type | notes |
|---|---|---|
| `id` | TEXT PK | uuid hex |
| `workspace_id` | TEXT FK → `workspaces.id` | groups documents into workspaces (nullable) |
| `file_id` | TEXT FK → `files.id` | the stored PDF |
| `title` | TEXT | display title (defaults to original name) |
| `original_name` | TEXT | uploaded filename |
| `num_pages` | INTEGER | nullable; filled in once the PDF is rendered |
| `archived` | INTEGER | 0/1 soft-delete flag |
| `created_at` / `updated_at` | TEXT | ISO timestamps |
| `last_opened_at` | TEXT | drives recency sort in the document list |

Indexes: `idx_documents_workspace_id`.

### `highlights` — saved PDF selections
| column | type | notes |
|---|---|---|
| `id` | TEXT PK | uuid hex |
| `document_id` | TEXT FK → `documents.id` | owning PDF |
| `page` | INTEGER | **0-based** page index |
| `rects_json` | TEXT | JSON array of **normalized** `{x,y,w,h}` page-fraction rects, so anchors survive reload/zoom |
| `quote` | TEXT | selected text |
| `color` | TEXT | default `'yellow'` |
| `node_id` | TEXT FK → `nodes.id` | set when a question is asked from this highlight (nullable) |
| `created_at` / `updated_at` | TEXT | ISO timestamps |

Index: `idx_highlights_document_id`.

### `nodes` — one Q&A turn; the tree
| column | type | notes |
|---|---|---|
| `id` | TEXT PK | uuid hex |
| `parent_id` | TEXT FK → `nodes.id` | **self-reference** — forms the tree; null at a root |
| `document_id` | TEXT FK → `documents.id` | scopes the node to its PDF (nullable on legacy nodes) |
| `source_highlight_id` | TEXT | the highlight this question was asked from (nullable) |
| `title` | TEXT | compacted from the prompt |
| `prompt` | TEXT | the question |
| `answer` | TEXT | LLM answer (default `''`) |
| `status` | TEXT | `queued` → `running` → `done` / `failed` |
| `backend` | TEXT | `'codex'` or `'claude'` (default `'codex'`) |
| `enable_web_search` | INTEGER | 0/1 flag to enable web search (default `0`) |
| `codex_session_id` | TEXT | for resume with Codex CLI (nullable) |
| `model` | TEXT | model name used for the run (nullable) |
| `cwd` | TEXT | working directory for the run (nullable) |
| `prompt_tokens` / `completion_tokens` / `total_tokens` | INTEGER | token usage (default `0`) |
| `tools_count` | INTEGER | number of tool calls made (default `0`) |
| `attachments_json` | TEXT | JSON array of attached files (default `'[]'`) |
| `workspace_refs_json` | TEXT | JSON array of workspace file references (default `'[]'`) |
| `note_md` | TEXT | per-node freeform markdown notes (default `''`) |
| `raw_events_json` | TEXT | raw JSONL events from the run, trimmed for storage (default `'[]'`) |
| `error` | TEXT | failure message (nullable) |
| `archived` / `bookmarked` | INTEGER | 0/1 flags (default `0`) |
| `created_at` / `updated_at` | TEXT | ISO timestamps |
| `completed_at` | TEXT | when the run finished (nullable) |

Indexes: `idx_nodes_parent_id`, `idx_nodes_status`, `idx_nodes_document_id`.

### `files` — physical uploads
| column | type | notes |
|---|---|---|
| `id` | TEXT PK | uuid hex |
| `workspace_id` | TEXT FK → `workspaces.id` | groups files into workspaces (nullable) |
| `original_name` | TEXT | uploaded name |
| `stored_name` | TEXT | sanitized name on disk |
| `path` | TEXT | `instance/workspace/uploads/<file_id>/...` |
| `mime` | TEXT | nullable |
| `size` | INTEGER | bytes |
| `text_excerpt` | TEXT | extracted text preview (nullable) |
| `created_at` | TEXT | ISO timestamp |

Indexes: `idx_files_created_at`, `idx_files_workspace_id`.

### `node_workspace_files` — files a run changed (legacy plumbing)
| column | type | notes |
|---|---|---|
| `node_id` | TEXT FK → `nodes.id` | part of composite PK |
| `rel_path` | TEXT | part of composite PK |
| `path` | TEXT | absolute path |
| `size` | INTEGER | bytes |
| `mtime` | REAL | file mtime |
| `created_at` | TEXT | ISO timestamp |

PK: `(node_id, rel_path)`. Index: `idx_node_workspace_files_node_id`.
Populated by the workspace snapshot/diff in `app/workspace.py` and surfaced in the reader's
workspace-files side tab.

### `workspaces` — a user project (a group of PDFs + a shared file drawer)
| column | type | notes |
|---|---|---|
| `id` | TEXT PK | uuid hex |
| `title` | TEXT | display name |
| `description` | TEXT | optional; default `''` |
| `archived` | INTEGER | 0/1 soft-delete flag |
| `created_at` / `updated_at` | TEXT | ISO timestamps |
| `last_opened_at` | TEXT | drives recency sort on the home page; nullable |
| `api_base_url` | TEXT | per-workspace Claude API endpoint (nullable; falls back to env default) |
| `api_key` | TEXT | per-workspace API key (nullable; falls back to env default) |
| `api_auth_type` | TEXT | auth type for per-workspace settings (nullable; falls back to env default) |
| `api_model` | TEXT | per-workspace model override (nullable; falls back to env default) |

`documents.workspace_id` and `files.workspace_id` reference this table. Index:
`idx_workspaces_archived`.

### `notes` — file-backed notes for nodes and highlights
| column | type | notes |
|---|---|---|
| `id` | TEXT PK | uuid hex |
| `kind` | TEXT | `'node'` or `'highlight'` — the type of target |
| `target_id` | TEXT | `nodes.id` or `highlights.id` — the associated node or highlight |
| `rel_path` | TEXT | relative path to the `.md` file within `NOTES_DIR` |
| `created_at` / `updated_at` | TEXT | ISO timestamps |

PK: `id`. Unique constraint: `(kind, target_id)` — one note per target (editing upserts).
The Markdown body itself lives on disk at `NOTES_DIR/<rel_path>`; this table only tracks
which node/highlight a note belongs to. Index: `idx_notes_target`.

## Notes on migrations

`init_db()` is idempotent (`CREATE TABLE IF NOT EXISTS`) and additionally runs
`ALTER TABLE` guards for columns added after the original schema:

**On `nodes`:**
- `workspace_refs_json`, `note_md`, `backend`, `enable_web_search` (all added together)
- `document_id`, `source_highlight_id` (added together in a later migration)

**On `documents` and `files`:**
- `workspace_id` (both tables, added together)

**On `workspaces`:**
- `api_base_url`, `api_key`, `api_auth_type`, `api_model` (per-workspace LLM settings, added together)

Indexes that depend on an added column (e.g. `idx_nodes_document_id`,
`idx_documents_workspace_id`, `idx_files_workspace_id`) are created *after* the 
corresponding ALTER so old databases upgrade cleanly. 

Older databases may still carry retired columns (`account_name`/`account_path`/`account_reason`) 
or tables (`account_usage`/`account_probe`); they are simply ignored.
