"""把祖先链 + 附件 + workspace 引用拼成 Codex/Claude 的自包含 prompt。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from .db import get_files, path_to_node
from .utils import indent_block


def make_attachment_context(files: List[Dict[str, Any]]) -> str:
    if not files:
        return "无"
    blocks = []
    for item in files:
        excerpt = (item.get("text_excerpt") or "").strip()
        block = [
            f"- 文件名: {item['original_name']}",
            f"  路径: {item['path']}",
            f"  MIME: {item.get('mime') or 'unknown'}",
            f"  大小: {item['size']} bytes",
        ]
        if excerpt:
            block.append("  摘要片段:")
            block.append(indent_block(excerpt[:12000], "    "))
        blocks.append("\n".join(block))
    return "\n\n".join(blocks)


def make_workspace_ref_context(refs: List[Dict[str, Any]]) -> str:
    if not refs:
        return "无"
    blocks = []
    for item in refs:
        blocks.append(
            "\n".join(
                [
                    f"- 文件名: {item.get('name') or Path(item.get('path') or '').name}",
                    f"  Workspace 相对路径: {item.get('path') or ''}",
                    f"  本地绝对路径: {item.get('absolute_path') or ''}",
                    f"  MIME: {item.get('mime') or 'unknown'}",
                    f"  类型: {item.get('kind') or 'file'}",
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


def build_codex_prompt(
    parent_id: Optional[str],
    prompt: str,
    files: List[Dict[str, Any]],
    workspace_refs: List[Dict[str, Any]],
    quote: str = "",
) -> str:
    inherited = path_to_node(parent_id)
    inherited_files = inherited_attachment_files(inherited)
    lines = [
        "你是 Codex 问答后端。当前应用把对话组织成树状结构；本次请求会从某个节点继承其祖先上下文，并产生一个新的子节点。",
        "请基于继承路径、用户当前问题和上传附件作答。若需要引用上传文件，优先使用给出的本地路径和片段。",
        "",
        "## 继承路径",
    ]
    if inherited:
        for index, node in enumerate(inherited, 1):
            lines.extend(
                [
                    f"### 节点 {index}: {node['title']}",
                    "User:",
                    node["prompt"].strip(),
                    "",
                    "Assistant:",
                    (node.get("answer") or "").strip() or "(该节点尚无完成回答)",
                    "",
                    "Workspace 引用:",
                    make_workspace_ref_context(node.get("workspace_refs") or []),
                    "",
                ]
            )
    else:
        lines.append("无，当前是根节点问题。")

    lines.extend(
        [
            "",
            "## 继承附件（祖先节点上传）",
            make_attachment_context(inherited_files),
            "",
            "## 本次上传附件",
            make_attachment_context(files),
            "",
            "## 本次 Workspace 引用",
            make_workspace_ref_context(workspace_refs),
        ]
    )
    if quote.strip():
        lines.extend(
            [
                "",
                "## 用户选中的 PDF 原文",
                "（用户从正在阅读的论文里选中了下面这段文字,并就其提问）",
                "",
                "> " + quote.strip().replace("\n", "\n> "),
            ]
        )
    lines.extend(
        [
            "",
            "## 当前用户问题",
            prompt.strip(),
        ]
    )
    return "\n".join(lines)
