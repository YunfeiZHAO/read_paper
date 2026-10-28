"""Codex 后端:通过 sub-pool 租号,shell 出 `codex exec --json` 并流式读取事件。"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

from ..config import (
    BASE_DIR,
    CODEX_MODEL,
    CODEX_REASONING_EFFORT,
    CODEX_SERVICE_TIER,
    CODEX_TIMEOUT_SECONDS,
    RUN_DIR,
    SUB_POOL_AVAILABLE,
    WORK_DIR,
    PooledCodexClient,
)
from ..db import add_account_usage, get_files, get_highlight, get_node, update_node
from ..events import (
    collect_tools_count,
    collect_usage,
    extract_answer_from_events,
    find_first_key,
    parse_jsonl_line,
    trim_events_for_storage,
)
from ..prompts import build_codex_prompt
from ..utils import now_iso
from ..workspace import record_workspace_changes, workspace_snapshot


def _codex_search_args(enabled: bool) -> List[str]:
    """开启联网搜索时返回 ["--search"],否则返回空(不给 codex 传该 flag)。"""
    return ["--search"] if enabled else []


async def _run_codex_for_node_async(node_id: str) -> None:
    if not SUB_POOL_AVAILABLE:
        update_node(
            node_id,
            status="failed",
            error="Codex backend unavailable: sub-pool-client is not installed. Please switch to the Claude backend.",
            completed_at=now_iso(),
        )
        return

    node = get_node(node_id)
    if not node:
        return

    files = get_files([item["id"] for item in node.get("attachments", [])])
    quote = ""
    if node.get("source_highlight_id"):
        hl = get_highlight(node["source_highlight_id"])
        quote = (hl or {}).get("quote") or ""
    prompt_text = build_codex_prompt(
        node.get("parent_id"), node["prompt"], files, node.get("workspace_refs") or [], quote=quote
    )
    run_path = RUN_DIR / node_id
    run_path.mkdir(parents=True, exist_ok=True)
    final_message_path = run_path / "final.md"
    before_workspace = workspace_snapshot()

    stdout_lines: List[str] = []
    events: List[Dict[str, Any]] = []
    stderr_text = ""
    returncode: Optional[int] = None
    timed_out = False
    account_name: Optional[str] = None
    lease_id: Optional[str] = None
    rate_limited = False

    try:
        async with PooledCodexClient(required_model=CODEX_MODEL or None) as client:
            account_name = client.account
            lease_id = client.lease_id
            update_node(
                node_id,
                status="running",
                account_name=account_name,
                account_path=f"sub-pool lease {lease_id or ''}".strip(),
                account_reason=f"sub-pool auto-assigned (model={CODEX_MODEL})",
                model=CODEX_MODEL or None,
            )

            cmd = [
                "codex",
                *_codex_search_args(bool(node.get("enable_web_search"))),
                "-c",
                f'model_reasoning_effort="{CODEX_REASONING_EFFORT}"',
                "-c",
                f'service_tier="{CODEX_SERVICE_TIER}"',
                "exec",
                "--json",
                "--skip-git-repo-check",
                "--ignore-rules",
                "--dangerously-bypass-approvals-and-sandbox",
                "-C",
                str(WORK_DIR),
                "-o",
                str(final_message_path),
                "-m",
                CODEX_MODEL,
                "-",
            ]

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(BASE_DIR),
                env=client.env,
                # Codex --json can emit single events well over the 64KB default
                # (e.g. an item with a huge aggregated_output). Raise the per-line
                # limit so the StreamReader doesn't raise "Separator is found, but
                # chunk is longer than limit".
                limit=64 * 1024 * 1024,
            )

            async def pump_stdout() -> None:
                assert proc.stdout is not None
                while True:
                    try:
                        raw = await proc.stdout.readline()
                    except ValueError:
                        # A line exceeded `limit` despite our generous cap, or contained
                        # an unexpected sequence. Drain the offending segment(s) and skip.
                        skipped = []
                        while True:
                            try:
                                chunk = await proc.stdout.read(1024 * 1024)
                            except ValueError:
                                continue
                            if not chunk:
                                break
                            skipped.append(chunk)
                            if b"\n" in chunk:
                                break
                        continue
                    if not raw:
                        break
                    line = raw.decode("utf-8", errors="replace")
                    stdout_lines.append(line)
                    event = parse_jsonl_line(line)
                    if not event:
                        continue
                    events.append(event)
                    public_events = trim_events_for_storage(events)
                    update_node(
                        node_id,
                        raw_events_json=json.dumps(public_events, ensure_ascii=False),
                        tools_count=collect_tools_count(events),
                        codex_session_id=find_first_key(events, ("thread_id", "session_id", "sessionId", "conversation_id")),
                    )

            try:
                if proc.stdin is not None:
                    proc.stdin.write(prompt_text.encode("utf-8"))
                    await proc.stdin.drain()
                    proc.stdin.close()
                await asyncio.wait_for(pump_stdout(), timeout=CODEX_TIMEOUT_SECONDS)
                returncode = await asyncio.wait_for(proc.wait(), timeout=30)
                stderr_bytes = await proc.stderr.read() if proc.stderr else b""
                stderr_text = stderr_bytes.decode("utf-8", errors="replace")
            except asyncio.TimeoutError:
                timed_out = True
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    returncode = await asyncio.wait_for(proc.wait(), timeout=10)
                except asyncio.TimeoutError:
                    returncode = -1
                if proc.stderr:
                    try:
                        stderr_bytes = await asyncio.wait_for(proc.stderr.read(), timeout=5)
                        stderr_text = stderr_bytes.decode("utf-8", errors="replace")
                    except asyncio.TimeoutError:
                        pass

            stdout_text = "".join(stdout_lines)
            combined = f"{stderr_text}\n{stdout_text}".lower()
            if returncode and returncode != 0 and ("429" in combined or "rate limit" in combined or "rate_limit" in combined):
                rate_limited = True
                try:
                    await client.report_error("429", (stderr_text or stdout_text)[-500:])
                except Exception:
                    pass
    except Exception as exc:  # noqa: BLE001
        update_node(node_id, status="failed", error=f"sub-pool call failed: {exc}", completed_at=now_iso())
        return

    stdout_text = "".join(stdout_lines)
    answer = ""
    if final_message_path.exists():
        answer = final_message_path.read_text(encoding="utf-8", errors="replace").strip()
    if not answer:
        answer = extract_answer_from_events(events).strip()

    record_workspace_changes(node_id, before_workspace)
    usage = collect_usage(events, prompt_text, answer or stderr_text)
    session_id = find_first_key(events, ("session_id", "sessionId", "conversation_id"))
    tools_count = collect_tools_count(events)
    public_events = trim_events_for_storage(events)

    if timed_out:
        update_node(
            node_id,
            status="failed",
            error=f"Codex timed out: not finished within {CODEX_TIMEOUT_SECONDS} seconds",
            raw_events_json=json.dumps(public_events, ensure_ascii=False),
            completed_at=now_iso(),
        )
    elif returncode == 0 and answer:
        update_node(
            node_id,
            status="done",
            answer=answer,
            codex_session_id=session_id,
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
            total_tokens=usage["total_tokens"],
            tools_count=tools_count,
            raw_events_json=json.dumps(public_events, ensure_ascii=False),
            completed_at=now_iso(),
            error=None,
        )
        if account_name:
            add_account_usage(account_name, node_id, usage["total_tokens"])
    else:
        error = stderr_text.strip() or stdout_text.strip() or "Codex execution failed"
        if rate_limited:
            error = f"[reported to sub-pool; account put on cooldown] {error}"
        update_node(
            node_id,
            status="failed",
            answer=answer,
            raw_events_json=json.dumps(public_events, ensure_ascii=False),
            error=error[-8000:],
            completed_at=now_iso(),
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
            total_tokens=usage["total_tokens"],
        )
        if account_name:
            add_account_usage(account_name, node_id, usage["total_tokens"])


def run_codex_for_node(node_id: str) -> None:
    try:
        asyncio.run(_run_codex_for_node_async(node_id))
    except Exception as exc:  # noqa: BLE001
        update_node(node_id, status="failed", error=str(exc), completed_at=now_iso())
