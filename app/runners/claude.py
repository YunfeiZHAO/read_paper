"""Claude API 后端:agentic 循环,带 bash 工具与(可选)web_search 工具。"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

from ..config import (
    CLAUDE_API_MODEL,
    CLAUDE_API_TIMEOUT_SECONDS,
    CLAUDE_BASH_TIMEOUT_SECONDS,
    CLAUDE_MAX_TOOL_ITERATIONS,
    WORK_DIR,
    anthropic,
)
from ..db import get_files, get_node, update_node
from ..events import collect_tools_count, trim_events_for_storage
from ..prompts import build_codex_prompt
from ..utils import now_iso
from ..workspace import record_workspace_changes, workspace_snapshot


async def _run_claude_bash_tool(command: str, restart: bool) -> str:
    if restart:
        return "(bash 工具没有持久会话，每次调用都在独立进程中执行；已忽略 restart)"
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(WORK_DIR),
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
            return f"[命令超时：超过 {CLAUDE_BASH_TIMEOUT_SECONDS} 秒未完成]"
        output = stdout.decode("utf-8", errors="replace")
        return output if output.strip() else "(命令无输出)"
    except Exception as exc:  # noqa: BLE001
        return f"[命令执行失败：{exc}]"


def _claude_web_search_query(block: Any) -> str:
    tool_input = getattr(block, "input", None) or {}
    if isinstance(tool_input, dict):
        return str(tool_input.get("query") or "")
    return ""


async def _run_claude_api_for_node_async(node_id: str) -> None:
    node = get_node(node_id)
    if not node:
        return

    files = get_files([item["id"] for item in node.get("attachments", [])])
    prompt_text = build_codex_prompt(node.get("parent_id"), node["prompt"], files, node.get("workspace_refs") or [])
    before_workspace = workspace_snapshot()

    update_node(node_id, status="running", model=CLAUDE_API_MODEL)

    client = anthropic.Anthropic()
    system_prompt = (
        "你是 Codex 问答后端（本次由 Claude API 回答）。当前应用把对话组织成树状结构；"
        "本次请求会从某个节点继承其祖先上下文，并产生一个新的子节点。"
        f"你有一个 bash 工具，命令会在本地工作目录 {WORK_DIR} 下以完整权限执行（无沙箱），"
        "可以用来读写文件、运行代码等；也可以使用 web_search 工具搜索网络信息。"
        "请基于继承路径、用户当前问题和上传附件作答。"
    )
    bash_tool = {"type": "bash_20250124", "name": "bash"}
    # Web search tool variants to try, newest first; some gateways (e.g. Vertex-backed
    # proxies) only recognize the older "basic" type.
    web_search_variants = [
        {"type": "web_search_20260209", "name": "web_search", "max_uses": 5},
        {"type": "web_search_20250305", "name": "web_search", "max_uses": 5},
    ]
    web_search_tried: set[str] = set()
    tools: List[Dict[str, Any]] = [bash_tool, web_search_variants[0]]

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
                        note = f"当前网关不支持 {current_type}，尝试改用 {remaining[0]['type']}：{exc}"
                    else:
                        tools = [bash_tool]
                        note = f"当前网关不支持任何 web_search 变体，已回退为仅使用 bash：{exc}"
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
                                "query": f"{result_count} 条结果" if result_count else "",
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

        error_message = error_message or f"超过最大工具调用轮数（{CLAUDE_MAX_TOOL_ITERATIONS}）仍未结束"

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
            error=f"Claude API 超时：超过 {CLAUDE_API_TIMEOUT_SECONDS} 秒未完成",
            raw_events_json=json.dumps(public_events, ensure_ascii=False),
            completed_at=now_iso(),
        )
    elif error_message or not answer:
        update_node(
            node_id,
            status="failed",
            answer=answer,
            error=(error_message or "Claude API 未返回有效回答")[-8000:],
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
