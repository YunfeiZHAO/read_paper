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
    ENABLE_PAPER_SEARCH_FALLBACK,
    BASE_DIR,
    WORK_DIR,
    anthropic,
)
from ..paper_search import format_results_for_model, paper_search
from ..db import (
    get_document,
    get_files,
    get_highlight,
    get_node,
    get_workspace_llm_settings,
    update_node,
)
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


# Local academic-search tool offered ONLY as a fallback: when the request asked for web
# search but the gateway rejected every web_search variant. It is never added when the
# user turned web search off (that means "no web", so bash-only stays correct).
PAPER_SEARCH_TOOL: Dict[str, Any] = {
    "name": "paper_search",
    "description": (
        "Search academic literature across arXiv, Semantic Scholar, OpenAlex, DBLP, and "
        "OpenReview (ICLR/NeurIPS/EMNLP/ACL and other top venues). Returns de-duplicated "
        "papers with authors, year, venue, abstract, citation count, and URLs/PDF links. "
        "Use this for any paper, citation, author, or literature lookup. Native web search "
        "is unavailable in this environment."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search terms, e.g. a title, topic, or author."},
            "sources": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": ["arxiv", "semantic_scholar", "openalex", "dblp", "openreview"],
                },
                "description": "Optional subset of sources to query. Defaults to all.",
            },
            "limit": {"type": "integer", "description": "Max results to return (default 10)."},
            "year_from": {"type": "integer", "description": "Only papers published in or after this year."},
            "year_to": {"type": "integer", "description": "Only papers published in or before this year."},
            "venue": {"type": "string", "description": "Case-insensitive substring filter on venue, e.g. 'NeurIPS'."},
        },
        "required": ["query"],
    },
}


async def _run_claude_paper_search_tool(tool_input: Dict[str, Any]) -> str:
    payload = await asyncio.to_thread(
        paper_search,
        query=str(tool_input.get("query") or ""),
        sources=tool_input.get("sources"),
        limit=tool_input.get("limit"),
        year_from=tool_input.get("year_from"),
        year_to=tool_input.get("year_to"),
        venue=tool_input.get("venue"),
    )
    return format_results_for_model(payload)


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

    # Resolve the workspace's bring-your-own endpoint/key/model (node -> document -> workspace).
    # Any field left unset falls back to the env/config default: an unset base_url/key lets the
    # anthropic SDK read ANTHROPIC_* from the environment, and an unset model uses CLAUDE_API_MODEL.
    workspace_id = None
    if node.get("document_id"):
        doc = get_document(node["document_id"])
        workspace_id = (doc or {}).get("workspace_id")
    llm = get_workspace_llm_settings(workspace_id)
    model = llm["model"] or CLAUDE_API_MODEL

    update_node(node_id, status="running", model=model)

    web_search_enabled = bool(node.get("enable_web_search"))

    client_kwargs: Dict[str, Any] = {}
    if llm["base_url"]:
        client_kwargs["base_url"] = llm["base_url"]
    if llm["api_key"]:
        # Bearer for custom gateways (ANTHROPIC_AUTH_TOKEN-style), x-api-key for the official API.
        if (llm["auth_type"] or "bearer") == "api_key":
            client_kwargs["api_key"] = llm["api_key"]
        else:
            client_kwargs["auth_token"] = llm["api_key"]
    client = anthropic.Anthropic(**client_kwargs)
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
        nonlocal answer, prompt_tokens, completion_tokens, error_message, tools, system_prompt

        response = None
        for _ in range(CLAUDE_MAX_TOOL_ITERATIONS):
            try:
                response = await asyncio.to_thread(
                    client.messages.create,
                    model=model,
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
                    elif ENABLE_PAPER_SEARCH_FALLBACK:
                        # Web search was requested but the gateway forbids every web_search
                        # variant: fall back to the local academic paper_search tool.
                        tools = [bash_tool, PAPER_SEARCH_TOOL]
                        system_prompt += (
                            " Native web search is unavailable here; use the paper_search tool "
                            "for any paper, citation, author, or literature lookup."
                        )
                        note = f"The current gateway supports no web_search variant; using the local paper_search tool instead: {exc}"
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
                elif block.name == "paper_search":
                    tool_input = block.input or {}
                    output = await _run_claude_paper_search_tool(tool_input)
                    query = str(tool_input.get("query") or "")
                    # Log as a web_search item so the UI's existing search-activity
                    # rendering and collect_tools_count pick it up unchanged.
                    events.append(
                        {
                            "type": "item.completed",
                            "item": {
                                "id": block.id,
                                "type": "web_search",
                                "query": f"paper_search: {query}" if query else "paper_search",
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
