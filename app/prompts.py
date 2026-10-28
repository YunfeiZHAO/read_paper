"""Stitch the ancestor chain + attachments + workspace refs into one self-contained
prompt for the LLM backend (provider-agnostic).

This is what makes the "tree" work without a stateful chat history: every request walks
from the node up to the root and inlines each ancestor's question, answer, and references,
so any node can be forked and still carry full context in a single fresh LLM call.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from .db import get_files, path_to_node
from .utils import indent_block


def make_attachment_context(files: List[Dict[str, Any]]) -> str:
    if not files:
        return "None"
    blocks = []
    for item in files:
        excerpt = (item.get("text_excerpt") or "").strip()
        block = [
            f"- Filename: {item['original_name']}",
            f"  Path: {item['path']}",
            f"  MIME: {item.get('mime') or 'unknown'}",
            f"  Size: {item['size']} bytes",
        ]
        if excerpt:
            block.append("  Excerpt:")
            block.append(indent_block(excerpt[:12000], "    "))
        blocks.append("\n".join(block))
    return "\n\n".join(blocks)


def make_workspace_ref_context(refs: List[Dict[str, Any]]) -> str:
    if not refs:
        return "None"
    blocks = []
    for item in refs:
        blocks.append(
            "\n".join(
                [
                    f"- Filename: {item.get('name') or Path(item.get('path') or '').name}",
                    f"  Workspace-relative path: {item.get('path') or ''}",
                    f"  Local absolute path: {item.get('absolute_path') or ''}",
                    f"  MIME: {item.get('mime') or 'unknown'}",
                    f"  Type: {item.get('kind') or 'file'}",
                ]
            )
        )
    return "\n\n".join(blocks)


def inherited_attachment_files(nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    file_ids: List[str] = []
    seen = set()
    for node in nodes:
        for attachment in node.get("attachments", []):
            file_id = str(attachment.get("id") or "").strip()
            if file_id and file_id not in seen:
                seen.add(file_id)
                file_ids.append(file_id)
    return get_files(file_ids)


def build_prompt(
    parent_id: Optional[str],
    prompt: str,
    files: List[Dict[str, Any]],
    workspace_refs: List[Dict[str, Any]],
    quote: str = "",
) -> str:
    inherited = path_to_node(parent_id)
    inherited_files = inherited_attachment_files(inherited)
    lines = [
        "You are the Q&A backend. The app organizes conversations as a tree; this request inherits the ancestor context of a node and produces a new child node.",
        "Answer based on the inherited path, the user's current question, and any uploaded attachments. When referencing uploaded files, prefer the given local paths and excerpts.",
        "",
        "## Inherited path",
    ]
    if inherited:
        for index, node in enumerate(inherited, 1):
            lines.extend(
                [
                    f"### Node {index}: {node['title']}",
                    "User:",
                    node["prompt"].strip(),
                    "",
                    "Assistant:",
                    (node.get("answer") or "").strip() or "(this node has no completed answer yet)",
                    "",
                    "Workspace references:",
                    make_workspace_ref_context(node.get("workspace_refs") or []),
                    "",
                ]
            )
    else:
        lines.append("None — this is a root-node question.")

    lines.extend(
        [
            "",
            "## Inherited attachments (uploaded on ancestor nodes)",
            make_attachment_context(inherited_files),
            "",
            "## Attachments uploaded this time",
            make_attachment_context(files),
            "",
            "## Workspace references this time",
            make_workspace_ref_context(workspace_refs),
        ]
    )
    if quote.strip():
        lines.extend(
            [
                "",
                "## PDF text selected by the user",
                "(The user selected the following passage from the paper they're reading and asked about it)",
                "",
                "> " + quote.strip().replace("\n", "\n> "),
            ]
        )
    lines.extend(
        [
            "",
            "## Current user question",
            prompt.strip(),
        ]
    )
    return "\n".join(lines)
