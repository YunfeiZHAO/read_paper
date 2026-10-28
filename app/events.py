"""Parsing of the Codex/Claude event stream: token usage, tool counting, answer
extraction, JSONL parsing, and trimming for storage.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple


def collect_usage(events: List[Dict[str, Any]], prompt_text: str, answer: str) -> Dict[str, int]:
    prompt_tokens = 0
    completion_tokens = 0
    total_tokens = 0

    def visit(obj: Any) -> None:
        nonlocal prompt_tokens, completion_tokens, total_tokens
        if isinstance(obj, dict):
            lowered = {str(k).lower(): v for k, v in obj.items()}
            for key in ("prompt_tokens", "input_tokens"):
                value = lowered.get(key)
                if isinstance(value, (int, float)):
                    prompt_tokens = max(prompt_tokens, int(value))
            for key in ("completion_tokens", "output_tokens"):
                value = lowered.get(key)
                if isinstance(value, (int, float)):
                    completion_tokens = max(completion_tokens, int(value))
            value = lowered.get("total_tokens")
            if isinstance(value, (int, float)):
                total_tokens = max(total_tokens, int(value))
            for value in obj.values():
                if isinstance(value, (dict, list)):
                    visit(value)
        elif isinstance(obj, list):
            for value in obj:
                visit(value)

    visit(events)
    if not total_tokens and (prompt_tokens or completion_tokens):
        total_tokens = prompt_tokens + completion_tokens
    if not total_tokens:
        total_tokens = max(1, int((len(prompt_text) + len(answer)) / 3.5))
        prompt_tokens = max(1, int(len(prompt_text) / 3.5))
        completion_tokens = max(1, total_tokens - prompt_tokens)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }


def collect_tools_count(events: List[Dict[str, Any]]) -> int:
    tool_item_ids = set()
    for event in events:
        item = event.get("item")
        if isinstance(item, dict):
            item_type = str(item.get("type") or "")
            if item_type in {"command_execution", "web_search"}:
                tool_item_ids.add(str(item.get("id") or len(tool_item_ids)))
                continue
        text = json.dumps(event, ensure_ascii=False).lower()
        if "tool_call" in text or "tool_use" in text or "function_call" in text:
            tool_item_ids.add(str(event.get("id") or len(tool_item_ids)))
    return len(tool_item_ids)


def find_first_key(events: List[Dict[str, Any]], keys: Tuple[str, ...]) -> Optional[str]:
    lowered_keys = {key.lower() for key in keys}

    def visit(obj: Any) -> Optional[str]:
        if isinstance(obj, dict):
            for key, value in obj.items():
                if str(key).lower() in lowered_keys and isinstance(value, str) and value:
                    return value
            for value in obj.values():
                found = visit(value)
                if found:
                    return found
        elif isinstance(obj, list):
            for value in obj:
                found = visit(value)
                if found:
                    return found
        return None

    return visit(events)


def extract_answer_from_events(events: List[Dict[str, Any]]) -> str:
    candidates: List[str] = []
    preferred_types = (
        "agent_message",
        "assistant_message",
        "final_answer",
        "response.completed",
        "message",
    )

    for event in events:
        event_type = str(event.get("type") or event.get("event") or "").lower()
        if event_type and not any(kind in event_type for kind in preferred_types):
            continue
        for key in ("message", "content", "text", "answer", "final_answer"):
            value = event.get(key)
            if isinstance(value, str) and value.strip():
                candidates.append(value.strip())
        item = event.get("item")
        if isinstance(item, dict):
            for key in ("message", "content", "text"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    candidates.append(value.strip())
    return candidates[-1] if candidates else ""


def parse_jsonl_events(stdout_text: str) -> List[Dict[str, Any]]:
    events = []
    for line in stdout_text.splitlines():
        stripped = line.strip()
        if not stripped or not stripped.startswith("{"):
            continue
        try:
            events.append(json.loads(stripped))
        except json.JSONDecodeError:
            continue
    return events


def parse_jsonl_line(line: str) -> Optional[Dict[str, Any]]:
    stripped = line.strip()
    if not stripped or not stripped.startswith("{"):
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def trim_events_for_storage(events: List[Dict[str, Any]], max_events: int = 80) -> List[Dict[str, Any]]:
    trimmed = events[-max_events:]
    serialized = json.dumps(trimmed, ensure_ascii=False)
    if len(serialized) <= 120000:
        return trimmed
    return [{"type": "truncated", "message": "Events too large; truncated for storage", "events": len(events)}]
