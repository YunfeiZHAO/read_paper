"""只读导入本地 Claude Code 会话历史(~/.claude/projects/<slug>/*.jsonl),
并支持把某条(可能被遗弃的)分支写成合成 session 以便 `claude --resume` 接上。

与 cli/treeflow 的 resume 技巧思路相同,但两者各自独立、互不导入。
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import CLAUDE_PROJECTS_DIR
from .utils import compact_title


def claude_code_slug(project_path: str) -> str:
    return re.sub(r"[/_]", "-", str(project_path).rstrip("/"))


def _user_text_blocks(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = str(block.get("text") or "").strip()
                if text:
                    parts.append(text)
        return "\n\n".join(parts)
    return ""


def _is_claude_user_prompt(msg: Dict[str, Any]) -> bool:
    if msg.get("type") != "user":
        return False
    content = (msg.get("message") or {}).get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                return False
    return bool(_user_text_blocks(content))


def _count_tool_uses(msg: Dict[str, Any]) -> int:
    content = (msg.get("message") or {}).get("content")
    if not isinstance(content, list):
        return 0
    return sum(1 for b in content if isinstance(b, dict) and b.get("type") == "tool_use")


def _assistant_usage(msg: Dict[str, Any]) -> Tuple[int, int]:
    usage = (msg.get("message") or {}).get("usage") or {}
    prompt = int(usage.get("input_tokens") or 0)
    prompt += int(usage.get("cache_creation_input_tokens") or 0)
    prompt += int(usage.get("cache_read_input_tokens") or 0)
    completion = int(usage.get("output_tokens") or 0)
    return prompt, completion


def read_claude_code_nodes(cc_dir: Path) -> List[Dict[str, Any]]:
    raw: Dict[str, Dict[str, Any]] = {}
    # For each session file, capture the LAST `last-prompt` event's leafUuid.
    # That is the message --resume <session_id> will land on.
    session_resume_leaf: Dict[str, str] = {}
    for jsonl_path in sorted(cc_dir.glob("*.jsonl")):
        session_id = jsonl_path.stem
        try:
            with jsonl_path.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line or not line.startswith("{"):
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if msg.get("type") == "last-prompt" and msg.get("leafUuid"):
                        session_resume_leaf[session_id] = msg["leafUuid"]
                        continue
                    uuid_ = msg.get("uuid")
                    if uuid_ and uuid_ not in raw:
                        raw[uuid_] = msg
        except OSError:
            continue

    def find_user_parent(uuid_: str) -> Optional[str]:
        seen = set()
        msg = raw.get(uuid_)
        if not msg:
            return None
        parent = msg.get("parentUuid")
        while parent and parent not in seen:
            seen.add(parent)
            p = raw.get(parent)
            if not p:
                return None
            if _is_claude_user_prompt(p):
                return parent
            parent = p.get("parentUuid")
        return None

    user_uuids = [u for u, m in raw.items() if _is_claude_user_prompt(m)]

    # session_id -> user_prompt uuid that --resume lands on (walk up from leafUuid until user prompt)
    resume_user_by_session: Dict[str, str] = {}
    # inverse: user_prompt uuid -> list of session_ids whose resume lands here
    sessions_resuming_to: Dict[str, List[str]] = {}
    for sid, leaf_uuid in session_resume_leaf.items():
        cursor = leaf_uuid
        seen: set = set()
        while cursor and cursor not in seen:
            seen.add(cursor)
            m = raw.get(cursor)
            if not m:
                break
            if _is_claude_user_prompt(m):
                resume_user_by_session[sid] = cursor
                sessions_resuming_to.setdefault(cursor, []).append(sid)
                break
            cursor = m.get("parentUuid")

    # children map among user prompts (for BFS to find branch-end resume target)
    user_children: Dict[Optional[str], List[str]] = {}
    for u in user_uuids:
        parent = find_user_parent(u)
        user_children.setdefault(parent, []).append(u)

    # For each user node, find the nearest descendant (BFS) that is an exact resume target.
    # If found, that descendant's session is the "branch-end resume" for this node.
    branch_end_session: Dict[str, str] = {}
    branch_end_node: Dict[str, str] = {}
    for u in user_uuids:
        if u in sessions_resuming_to:
            continue
        queue = list(user_children.get(u, []))
        visited = {u, *queue}
        while queue:
            cur = queue.pop(0)
            if cur in sessions_resuming_to:
                branch_end_session[u] = sessions_resuming_to[cur][-1]
                branch_end_node[u] = cur
                break
            for child in user_children.get(cur, []):
                if child not in visited:
                    visited.add(child)
                    queue.append(child)

    descendants_of: Dict[str, List[Dict[str, Any]]] = {u: [] for u in user_uuids}

    for uuid_, msg in raw.items():
        if _is_claude_user_prompt(msg):
            continue
        owner = find_user_parent(uuid_)
        if owner and owner in descendants_of:
            descendants_of[owner].append(msg)

    nodes: List[Dict[str, Any]] = []
    for uuid_ in user_uuids:
        user_msg = raw[uuid_]
        parent_user = find_user_parent(uuid_)
        prompt_text = _user_text_blocks((user_msg.get("message") or {}).get("content"))

        descendants = descendants_of.get(uuid_, [])
        descendants.sort(key=lambda m: m.get("timestamp") or "")

        answer_parts: List[str] = []
        tools_count = 0
        prompt_tokens = 0
        completion_tokens = 0
        model = ""
        for d in descendants:
            if d.get("type") != "assistant":
                continue
            msg = d.get("message") or {}
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = str(block.get("text") or "").strip()
                        if text:
                            answer_parts.append(text)
            elif isinstance(content, str) and content.strip():
                answer_parts.append(content.strip())
            tools_count += _count_tool_uses(d)
            pt, ct = _assistant_usage(d)
            prompt_tokens += pt
            completion_tokens += ct
            if not model and msg.get("model"):
                model = str(msg.get("model"))

        answer = "\n\n".join(answer_parts)
        last_ts = (descendants[-1].get("timestamp") if descendants else user_msg.get("timestamp")) or ""

        # With --resume-session-at we can always resume to this exact message.
        # The session arg is the file this user prompt lives in; the message arg is its uuid.
        own_session = user_msg.get("sessionId") or ""
        resume_sessions = sessions_resuming_to.get(uuid_, [])
        # Keep these informational fields for the UI to optionally surface.
        if resume_sessions:
            resume_kind = "exact"
            resume_descendant = ""
        elif uuid_ in branch_end_session:
            resume_kind = "branch_end"
            resume_descendant = branch_end_node[uuid_]
        else:
            resume_kind = "fallback"
            resume_descendant = ""
        primary_resume = own_session

        nodes.append({
            "id": f"cc:{uuid_}",
            "parent_id": f"cc:{parent_user}" if parent_user else None,
            "title": compact_title(prompt_text or "(empty message)"),
            "prompt": prompt_text,
            "answer": answer,
            "status": "done",
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "tools_count": tools_count,
            "attachments": [],
            "workspace_refs": [],
            "raw_events": [],
            "note_md": "",
            "model": model,
            "cwd": user_msg.get("cwd") or "",
            "created_at": user_msg.get("timestamp") or "",
            "completed_at": last_ts,
            "updated_at": last_ts,
            "archived": False,
            "bookmarked": False,
            "codex_session_id": primary_resume,
            "resume_sessions": resume_sessions,
            "resume_kind": resume_kind,
            "resume_descendant_id": f"cc:{resume_descendant}" if resume_descendant else "",
            "own_session_id": own_session,
            "resume_message_uuid": uuid_,
            "source": "claude_code",
        })

    return nodes


def resume_from_node(project_path: str, target_uuid: str) -> Tuple[Dict[str, Any], int]:
    """Build a synthetic session jsonl that pins the active leaf at the requested message.

    Returns (payload, http_status) so the route stays a thin wrapper.

    Why: Claude Code's --resume-session-at only matches uuids on the file's current active
    chain (walked back from the latest last-prompt event). Abandoned sibling branches can't
    be reached. So we copy just the chain we want into a new session file with a fresh
    last-prompt pointing at the target — then `claude --resume <new>` lands exactly there.
    """
    if not project_path or not target_uuid:
        return {"error": "path and message_uuid are both required"}, 400
    cc_dir = CLAUDE_PROJECTS_DIR / claude_code_slug(project_path)
    if not cc_dir.exists():
        return {"error": f"Claude Code project directory does not exist: {cc_dir}"}, 404

    # Collect all messages across all sessions for this project, keyed by uuid.
    # First-occurrence wins, so re-running this against a project that already has synthetic
    # files (whose entries are byte-for-byte copies of the originals) is idempotent.
    all_msgs: Dict[str, Tuple[Dict[str, Any], str]] = {}
    for jsonl_path in sorted(cc_dir.glob("*.jsonl")):
        try:
            with jsonl_path.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    raw_line = line.rstrip("\n")
                    stripped = raw_line.strip()
                    if not stripped or not stripped.startswith("{"):
                        continue
                    try:
                        d = json.loads(stripped)
                    except json.JSONDecodeError:
                        continue
                    u = d.get("uuid")
                    if u and u not in all_msgs:
                        all_msgs[u] = (d, raw_line)
        except OSError:
            continue

    if target_uuid not in all_msgs:
        return {"error": f"uuid={target_uuid} not found in this project"}, 404

    # Walk parentUuid chain from target back to root.
    back_chain: List[Tuple[Dict[str, Any], str]] = []
    cursor: Optional[str] = target_uuid
    seen: set = set()
    while cursor and cursor not in seen:
        seen.add(cursor)
        entry = all_msgs.get(cursor)
        if not entry:
            break
        back_chain.append(entry)
        cursor = entry[0].get("parentUuid")
    back_chain.reverse()  # root first, target last

    # Forward chain: target's assistant reply + tool_use/tool_result events.
    # Without this the resumed conversation ends at the user prompt with no
    # response visible — Claude's --resume only walks BACKWARD via parentUuid
    # from leafUuid, so reply events must be reachable from a descendant leaf.
    # BFS over non-user-prompt descendants; stopping at user-prompt children
    # means we don't pull in the next turn or sibling forks.
    children_index: Dict[str, List[str]] = {}
    for uid, (msg, _) in all_msgs.items():
        p = msg.get("parentUuid")
        if p:
            children_index.setdefault(p, []).append(uid)
    forward_uuids: List[str] = []
    visited_fwd: set = {target_uuid}
    bfs_queue: List[str] = [target_uuid]
    while bfs_queue:
        u = bfs_queue.pop(0)
        for child in children_index.get(u, []):
            if child in visited_fwd:
                continue
            entry = all_msgs.get(child)
            if not entry or _is_claude_user_prompt(entry[0]):
                continue
            visited_fwd.add(child)
            forward_uuids.append(child)
            bfs_queue.append(child)
    forward_uuids.sort(key=lambda u: all_msgs[u][0].get("timestamp") or "")
    # Leaf for last-prompt = end of the reply chain (or target if empty).
    leaf_uuid = forward_uuids[-1] if forward_uuids else target_uuid

    # Claude Code maps --resume <id> to <id>.jsonl with strict id↔filename match — any
    # prefix on the filename makes it report "No conversation found". So just use a plain
    # UUID for the session id and name the file <sid>.jsonl. Synthetic files remain
    # identifiable by content (single last-prompt event at the very end + no embedded events).
    new_sid = str(uuid.uuid4())
    new_path = cc_dir / f"{new_sid}.jsonl"

    # Extract target prompt text for the last-prompt event.
    target_msg = all_msgs[target_uuid][0]
    raw_content = (target_msg.get("message") or {}).get("content")
    target_text = ""
    if isinstance(raw_content, str):
        target_text = raw_content
    elif isinstance(raw_content, list):
        for block in raw_content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = str(block.get("text") or "").strip()
                if t:
                    target_text = t
                    break

    last_prompt_event = json.dumps(
        {
            "type": "last-prompt",
            "lastPrompt": target_text,
            "leafUuid": leaf_uuid,
            "sessionId": new_sid,
        },
        ensure_ascii=False,
    )

    try:
        with new_path.open("w", encoding="utf-8") as out:
            for _, raw_line in back_chain:
                out.write(raw_line + "\n")
            for fwd_uuid in forward_uuids:
                out.write(all_msgs[fwd_uuid][1] + "\n")
            out.write(last_prompt_event + "\n")
    except OSError as exc:
        return {"error": f"Write failed: {exc}"}, 500

    return {
        "session_id": new_sid,
        "file": str(new_path),
        "chain_length": len(back_chain) + len(forward_uuids),
        "command": f"claude --resume {new_sid}",
        "target_text_preview": target_text[:80],
    }, 200
