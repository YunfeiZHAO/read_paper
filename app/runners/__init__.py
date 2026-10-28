"""后端调度:按节点的 backend 字段选择 Codex 或 Claude runner,在后台线程里跑。"""
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
