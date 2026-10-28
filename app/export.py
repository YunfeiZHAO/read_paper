"""Export a node's entire subtree as Markdown."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .db import DB_LOCK, db_connection, get_node, public_node


def export_subtree_markdown(root_id: str) -> str:
    root = get_node(root_id)
    if not root:
        raise ValueError("Node does not exist")
    with DB_LOCK, db_connection() as conn:
        rows = conn.execute("SELECT * FROM nodes ORDER BY created_at").fetchall()
    nodes = [public_node(row) for row in rows]
    children: Dict[Optional[str], List[Dict[str, Any]]] = {}
    for node in nodes:
        children.setdefault(node.get("parent_id"), []).append(node)

    lines = [f"# {root['title']}", ""]

    def walk(node: Dict[str, Any], depth: int) -> None:
        heading = "#" * min(depth + 2, 6)
        lines.extend(
            [
                f"{heading} {node['title']}",
                "",
                f"- Status: {node['status']}",
                f"- Token: {node.get('total_tokens') or 0}",
                "",
                "**User**",
                "",
                node["prompt"].strip(),
                "",
                "**Assistant**",
                "",
                (node.get("answer") or "").strip() or "(no answer)",
                "",
            ]
        )
        for child in children.get(node["id"], []):
            walk(child, depth + 1)

    walk(root, 0)
    return "\n".join(lines)
