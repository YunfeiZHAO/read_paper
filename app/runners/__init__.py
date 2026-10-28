"""Backend dispatch: pick the Codex or Claude runner from a node's `backend` field
and run it in a background daemon thread.
"""
from __future__ import annotations

import threading

from ..db import get_node
from .claude import run_claude_for_node
from .codex import run_codex_for_node

__all__ = ["run_codex_for_node", "run_claude_for_node", "start_worker"]


def start_worker(node_id: str) -> None:
    node = get_node(node_id)
    target = run_claude_for_node if node and node.get("backend") == "claude" else run_codex_for_node
    thread = threading.Thread(target=target, args=(node_id,), daemon=True)
    thread.start()
