# Database Schema — PDF Tree QA

The web app stores everything in one SQLite database:
`instance/codex_qa.sqlite` (override the directory with `CODEX_QA_DATA_DIR`).
The schema is defined in [app/db.py](../app/db.py) (`init_db()`).

## Mental model

A **document** is one uploaded PDF — the top-level "project". It owns:

- **highlights** — saved selections drawn on the PDF.
- **nodes** — Q&A turns that form a *tree* (each node's `parent_id` points at
  the node it was forked from). A question can be asked directly from a
  highlight (`nodes.source_highlight_id`), and that highlight is linked back
  to the node it spawned (`highlights.node_id`).

Supporting tables: **files** (physical uploads on disk), **node_workspace_files**
(files a Codex run touched), and the legacy accounting tables
**account_usage** / **account_probe**.

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

 Legacy / standalone:   account_usage (name PK)     account_probe (name PK)
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
| `file_id` | TEXT FK → `files.id` | the stored PDF |
| `title` | TEXT | display title (defaults to original name) |
| `original_name` | TEXT | uploaded filename |
| `num_pages` | INTEGER | nullable; filled in once the PDF is rendered |
| `archived` | INTEGER | 0/1 soft-delete flag |
| `created_at` / `updated_at` | TEXT | ISO timestamps |
| `last_opened_at` | TEXT | drives recency sort in the document list |

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
| `document_id` | TEXT FK → `documents.id` | scopes the node to its PDF |
| `source_highlight_id` | TEXT | the highlight this question was asked from (nullable) |
| `title` | TEXT | compacted from the prompt |
| `prompt` | TEXT | the question |
| `answer` | TEXT | LLM answer (default `''`) |
| `status` | TEXT | `queued` → `running` → `done` / `failed` |
| `backend` | TEXT | `'codex'` or `'claude'` |
| `enable_web_search` | INTEGER | 0/1 |
| `account_name` / `account_path` / `account_reason` | TEXT | internal; stripped from API responses |
| `codex_session_id` | TEXT | for resume |
| `model`, `cwd` | TEXT | |
| `prompt_tokens` / `completion_tokens` / `total_tokens` | INTEGER | usage |
| `tools_count` | INTEGER | number of tool calls |
| `attachments_json` | TEXT | JSON array of attached files |
| `workspace_refs_json` | TEXT | JSON array of workspace references |
| `note_md` | TEXT | per-node freeform markdown notes |
| `raw_events_json` | TEXT | raw JSONL events from the run (trimmed for storage) |
| `error` | TEXT | failure message |
| `archived` / `bookmarked` | INTEGER | 0/1 flags |
| `created_at` / `updated_at` | TEXT | ISO timestamps |
| `completed_at` | TEXT | nullable |

Indexes: `idx_nodes_parent_id`, `idx_nodes_status`, `idx_nodes_document_id`.

### `files` — physical uploads
| column | type | notes |
|---|---|---|
| `id` | TEXT PK | uuid hex |
| `original_name` | TEXT | uploaded name |
| `stored_name` | TEXT | sanitized name on disk |
| `path` | TEXT | `instance/workspace/uploads/<file_id>/...` |
| `mime` | TEXT | nullable |
| `size` | INTEGER | bytes |
| `text_excerpt` | TEXT | extracted text preview (nullable) |
| `created_at` | TEXT | ISO timestamp |

Index: `idx_files_created_at`.

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
The PDF UI no longer surfaces the workspace drawer, but the backend tracking remains.

### `account_usage` — legacy per-account token accounting
| column | type | notes |
|---|---|---|
| `name` | TEXT PK | account name |
| `used_tokens` | INTEGER | running total |
| `last_node_id` | TEXT | last node charged |
| `updated_at` | TEXT | ISO timestamp |

### `account_probe` — legacy account health checks
| column | type | notes |
|---|---|---|
| `name` | TEXT PK | account name |
| `auth_file` | TEXT | path to auth.json |
| `status` | TEXT | probe result |
| `checked_at` | TEXT | ISO timestamp |
| `duration_ms` | INTEGER | probe latency |
| `error` / `answer` | TEXT | nullable |

> `account_usage` / `account_probe` are largely superseded by the external
> `sub-pool-client`, which leases a Codex account per request.

## Notes on migrations

`init_db()` is idempotent (`CREATE TABLE IF NOT EXISTS`) and additionally runs
`ALTER TABLE` guards for columns added after the original schema
(`workspace_refs_json`, `note_md`, `backend`, `enable_web_search`,
`document_id`, `source_highlight_id`). The `idx_nodes_document_id` index is
created *after* the `document_id` ALTER so old databases upgrade cleanly.
