"""Claude API backend: an agentic loop with a bash tool and an optional web_search tool."""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict, List, Optional

from ..config import (
    CLAUDE_API_MODEL,
    CLAUDE_API_TIMEOUT_SECONDS,
    CLAUDE_BASH_TIMEOUT_SECONDS,
    CLAUDE_MAX_TOOL_ITERATIONS,
    BASE_DIR,
    WORK_DIR,
    anthropic,
)
from ..db import get_files, get_highlight, get_node, update_node
from ..events import collect_tools_count, trim_events_for_storage
from ..prompts import build_prompt
from ..utils import now_iso
from ..workspace import record_workspace_changes, workspace_snapshot


def _bash_env() -> dict:
    venv_bin = BASE_DIR / ".venv" / "bin"
    env = os.environ.copy()
    if venv_bin.is_dir():
        env["PATH"] = f"{venv_bin}{os.pathsep}{env.get('PATH', '')}"
        env["VIRTUAL_ENV"] = str(BASE_DIR / ".venv")
        env.pop("PYTHONHOME", None)
    return env


async def _run_claude_bash_tool(command: str, restart: bool) -> str:
    if restart:
        return "(the bash tool has no persistent session; each call runs in a separate process; restart ignored)"
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(WORK_DIR),
            env=_bash_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=CLAUDE_BASH_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return f"[command timed out: not finished within {CLAUDE_BASH_TIMEOUT_SECONDS} seconds]"
        output = stdout.decode("utf-8", errors="replace")
        return output if output.strip() else "(command produced no output)"
    except Exception as exc:  # noqa: BLE001
        return f"[command execution failed: {exc}]"


def _claude_web_search_query(block: Any) -> str:
    tool_input = getattr(block, "input", None) or {}
    if isinstance(tool_input, dict):
        return str(tool_input.get("query") or "")
    return ""


# Web search tool variants to try, newest first; some gateways (e.g. Vertex-backed
# proxies) only recognize the older "basic" type.
WEB_SEARCH_VARIANTS: List[Dict[str, Any]] = [
    {"type": "web_search_20260209", "name": "web_search", "max_uses": 5},
    {"type": "web_search_20250305", "name": "web_search", "max_uses": 5},
]


def _claude_initial_tools(bash_tool: Dict[str, Any], enabled: bool) -> List[Dict[str, Any]]:
    """Include the first web_search variant when enabled, otherwise bash only (no failed round-trip)."""
    if enabled:
        return [bash_tool, WEB_SEARCH_VARIANTS[0]]
    return [bash_tool]


async def _run_claude_api_for_node_async(node_id: str) -> None:
    node = get_node(node_id)
    if not node:
        return

    files = get_files([item["id"] for item in node.get("attachments", [])])
    quote = ""
    if node.get("source_highlight_id"):
        hl = get_highlight(node["source_highlight_id"])
        quote = (hl or {}).get("quote") or ""
    prompt_text = build_prompt(
        node.get("parent_id"), node["prompt"], files, node.get("workspace_refs") or [], quote=quote
    )
    before_workspace = workspace_snapshot()

    update_node(node_id, status="running", model=CLAUDE_API_MODEL)

    web_search_enabled = bool(node.get("enable_web_search"))

    client = anthropic.Anthropic()
    system_prompt = (
        "You are the Q&A backend (answered this time by the Claude API). The app organizes "
        "conversations as a tree; this request inherits the ancestor context of a node and "
        "produces a new child node. "
        f"You have a bash tool whose commands run with full permissions in the local working "
        f"directory {WORK_DIR} (no sandbox), which you can use to read/write files, run code, etc. "
        + ("You may also use the web_search tool to search the web. " if web_search_enabled else "")
        + "Answer based on the inherited path, the user's current question, and any uploaded attachments."
    )
    bash_tool = {"type": "bash_20250124", "name": "bash"}
    web_search_variants = WEB_SEARCH_VARIANTS
    web_search_tried: set[str] = set()
    tools: List[Dict[str, Any]] = _claude_initial_tools(bash_tool, web_search_enabled)

    messages: List[Dict[str, Any]] = [{"role": "user", "content": prompt_text}]
    events: List[Dict[str, Any]] = []
    prompt_tokens = 0
    completion_tokens = 0
    answer = ""
    timed_out = False
    error_message: Optional[str] = None

    async def run_loop() -> None:
        nonlocal answer, prompt_tokens, completion_tokens, error_message, tools

        response = None
        for _ in range(CLAUDE_MAX_TOOL_ITERATIONS):
            try:
                response = await asyncio.to_thread(
                    client.messages.create,
                    model=CLAUDE_API_MODEL,
                    max_tokens=16000,
                    system=system_prompt,
                    tools=tools,
                    messages=messages,
                )
            except anthropic.BadRequestError as exc:
                has_web_search = any(tool.get("name") == "web_search" for tool in tools)
                if has_web_search and "web_search" in str(exc).lower():
                    current_type = next(t["type"] for t in tools if t.get("name") == "web_search")
                    web_search_tried.add(current_type)
                    remaining = [v for v in web_search_variants if v["type"] not in web_search_tried]
                    if remaining:
                        tools = [bash_tool, remaining[0]]
                        note = f"The current gateway does not support {current_type}; trying {remaining[0]['type']} instead: {exc}"
                    else:
                        tools = [bash_tool]
                        note = f"The current gateway supports no web_search variant; fell back to bash only: {exc}"
                    events.append(
                        {
                            "type": "item.completed",
                            "item": {
                                "id": f"note-{len(events)}",
                                "type": "summary",
                                "text": note,
                                "status": "completed",
                            },
                        }
                    )
                    update_node(node_id, raw_events_json=json.dumps(trim_events_for_storage(events), ensure_ascii=False))
                    continue
                error_message = str(exc)
                return
            except Exception as exc:  # noqa: BLE001
                error_message = str(exc)
                return

            prompt_tokens = max(prompt_tokens, int(response.usage.input_tokens or 0))
            completion_tokens += int(response.usage.output_tokens or 0)

            # Server-side tools (web_search) show up as server_tool_use / *_tool_result
            # blocks even when the turn ends normally, so log them before checking stop_reason.
            for block in response.content:
                block_type = getattr(block, "type", "")
                if block_type == "server_tool_use" and getattr(block, "name", "") == "web_search":
                    query = _claude_web_search_query(block)
                    events.append(
                        {
                            "type": "item.completed",
                            "item": {"id": block.id, "type": "web_search", "query": query, "status": "completed"},
                        }
                    )
                elif block_type == "web_search_tool_result":
                    content = getattr(block, "content", None)
                    result_count = len(content) if isinstance(content, list) else 0
                    events.append(
                        {
                            "type": "item.completed",
                            "item": {
                                "id": getattr(block, "tool_use_id", f"note-{len(events)}"),
                                "type": "web_search",
                                "query": f"{result_count} results" if result_count else "",
                                "status": "completed",
                            },
                        }
                    )
            if any(getattr(b, "type", "") in {"server_tool_use", "web_search_tool_result"} for b in response.content):
                update_node(
                    node_id,
                    raw_events_json=json.dumps(trim_events_for_storage(events), ensure_ascii=False),
                    tools_count=collect_tools_count(events),
                )

            if response.stop_reason != "tool_use":
                answer = "\n\n".join(
                    block.text.strip() for block in response.content if getattr(block, "type", "") == "text" and block.text.strip()
                )
                return

            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if getattr(block, "type", "") != "tool_use":
                    continue
                if block.name == "bash":
                    command = str((block.input or {}).get("command") or "")
                    restart = bool((block.input or {}).get("restart"))
                    output = await _run_claude_bash_tool(command, restart)
                    events.append(
                        {
                            "type": "item.completed",
                            "item": {
                                "id": block.id,
                                "type": "command_execution",
                                "command": command or "(restart)",
                                "aggregated_output": output,
                                "status": "completed",
                            },
                        }
                    )
                    tool_results.append(
                        {"type": "tool_result", "tool_use_id": block.id, "content": output}
                    )
            update_node(
                node_id,
                raw_events_json=json.dumps(trim_events_for_storage(events), ensure_ascii=False),
                tools_count=collect_tools_count(events),
            )
            if tool_results:
                messages.append({"role": "user", "content": tool_results})

        error_message = error_message or f"Exceeded the maximum number of tool-call rounds ({CLAUDE_MAX_TOOL_ITERATIONS}) without finishing"

    try:
        await asyncio.wait_for(run_loop(), timeout=CLAUDE_API_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        timed_out = True

    record_workspace_changes(node_id, before_workspace)
    public_events = trim_events_for_storage(events)
    total_tokens = prompt_tokens + completion_tokens

    if timed_out:
        update_node(
            node_id,
            status="failed",
            error=f"Claude API timed out: not finished within {CLAUDE_API_TIMEOUT_SECONDS} seconds",
            raw_events_json=json.dumps(public_events, ensure_ascii=False),
            completed_at=now_iso(),
        )
    elif error_message or not answer:
        update_node(
            node_id,
            status="failed",
            answer=answer,
            error=(error_message or "Claude API returned no valid answer")[-8000:],
            raw_events_json=json.dumps(public_events, ensure_ascii=False),
            completed_at=now_iso(),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )
    else:
        update_node(
            node_id,
            status="done",
            answer=answer,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            tools_count=collect_tools_count(events),
            raw_events_json=json.dumps(public_events, ensure_ascii=False),
            completed_at=now_iso(),
            error=None,
        )


def run_claude_for_node(node_id: str) -> None:
    try:
        asyncio.run(_run_claude_api_for_node_async(node_id))
    except Exception as exc:  # noqa: BLE001
        update_node(node_id, status="failed", error=str(exc), completed_at=now_iso())
