"""Business-agnostic pure helpers: time, JSON, and text utilities. No internal deps."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value: Optional[str]) -> datetime:
    if not value:
        return datetime.fromtimestamp(0, timezone.utc)
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return datetime.fromtimestamp(0, timezone.utc)


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> List[Dict[str, Any]]:
    return [dict(row) for row in rows]


def safe_json_loads(value: str, fallback: Any) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def compact_title(text: str, limit: int = 44) -> str:
    normalized = re.sub(r"\s+", " ", text.strip())
    if not normalized:
        return "New question"
    return normalized if len(normalized) <= limit else normalized[: limit - 1] + "…"


def compose_node_title(quote: str, prompt: str, limit: int = 80) -> str:
    """Default node title combining the source highlight's quote with the question.

    Shown as `"<quote…>" — <question…>` so a node recalls both what was selected
    and what was asked. With no quote, falls back to the question-only title.
    """
    question = re.sub(r"\s+", " ", (prompt or "").strip())
    snippet = re.sub(r"\s+", " ", (quote or "").strip())
    if not snippet:
        return compact_title(question, limit)
    sep = " — "
    # Give the quote up to ~half the budget, the question gets the rest.
    quote_limit = max(16, (limit - len(sep)) // 2)
    quote_part = snippet if len(snippet) <= quote_limit else snippet[: quote_limit - 1] + "…"
    question_limit = max(1, limit - len(sep) - len(quote_part) - 2)  # -2 for the surrounding quotes
    question_part = question if len(question) <= question_limit else question[: question_limit - 1] + "…"
    title = f"“{quote_part}”{sep}{question_part}" if question_part else f"“{quote_part}”"
    return title


def indent_block(text: str, prefix: str) -> str:
    return "\n".join(prefix + line for line in text.splitlines())
