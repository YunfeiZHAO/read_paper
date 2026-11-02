#!/usr/bin/env python3
"""Local Claude Code bridge for the PDF Tree QA web app.

Runs on the *user's* machine (not the remote app server). The web app's browser frontend,
when the "Local" backend is selected, POSTs a fully built prompt here; this helper shells out
to the local `claude` CLI (which has the user's key, VPN access, and Claude Code's native
tools) and streams the result back as newline-delimited JSON events shaped exactly like the
app's existing Codex/Claude event stream, so the app's tree/tool-activity UI renders it
unchanged. No API key ever leaves this machine.

Stdlib only (http.server); compatible with the system python3 (3.9+). Binds 127.0.0.1.

Wire protocol of POST /run (NDJSON, one JSON object per line):
  - app events: {"type": ..., "item": {...}}  -> the browser forwards these to the app server.
  - bridge control lines (NOT app events): {"bridge": "done"} at success, or
    {"bridge": "error", "message": "..."} on failure. The browser uses these to finalize the
    node (done/failed) and never forwards them as events.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

HOST = "127.0.0.1"
PORT = int(os.environ.get("READPAPER_BRIDGE_PORT", "8765"))
# Per-tool_result output kept small; the app trims the whole event list again for storage.
MAX_TOOL_OUTPUT = 8000
WORK_DIR = Path(os.environ.get("READPAPER_BRIDGE_WORKDIR", str(Path.home() / ".readpaper-bridge" / "work")))


def find_claude() -> Optional[str]:
    """Locate the `claude` binary. A LaunchAgent's PATH is minimal, so check common spots too."""
    override = os.environ.get("CLAUDE_BIN")
    if override and Path(override).exists():
        return override
    found = shutil.which("claude")
    if found:
        return found
    for candidate in (
        Path.home() / ".local" / "bin" / "claude",
        Path.home() / ".claude" / "local" / "claude",
        Path("/opt/homebrew/bin/claude"),
        Path("/usr/local/bin/claude"),
    ):
        if candidate.exists():
            return str(candidate)
    return None


def claude_version(claude_bin: str) -> Optional[str]:
    try:
        out = subprocess.run(
            [claude_bin, "--version"], capture_output=True, text=True, timeout=10
        )
        return (out.stdout or out.stderr or "").strip() or None
    except Exception:
        return None


def _flatten_tool_result(content: Any) -> str:
    """claude tool_result content is either a string or a list of {type:text, text:...} blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
                elif block.get("type") == "image":
                    parts.append("[image]")
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return ""


def _tool_kind(name: str) -> str:
    """Map a Claude Code tool name to the app's item.type vocabulary."""
    lowered = (name or "").lower()
    if lowered == "bash":
        return "command_execution"
    if lowered in ("websearch", "webfetch", "web_search"):
        return "web_search"
    return "tool_use"


class ClaudeTranslator:
    """Turn a stream of claude stream-json events into app-shaped events.

    Emits an item.started when a tool is invoked and an item.completed when its result arrives,
    keyed by the tool_use id, plus one final agent_message carrying the answer + token usage.
    """

    def __init__(self) -> None:
        self.session_id: Optional[str] = None
        self.model: Optional[str] = None
        self.last_text: str = ""
        self.pending: Dict[str, Dict[str, Any]] = {}

    def handle(self, event: Dict[str, Any]) -> List[Dict[str, Any]]:
        etype = event.get("type")
        if etype == "system":
            if isinstance(event.get("session_id"), str):
                self.session_id = event["session_id"]
            if isinstance(event.get("model"), str):
                self.model = event["model"]
            return []
        if etype == "assistant":
            return self._handle_assistant(event)
        if etype == "user":
            return self._handle_user(event)
        if etype == "result":
            return self._handle_result(event)
        return []

    def _handle_assistant(self, event: Dict[str, Any]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        message = event.get("message") or {}
        if isinstance(event.get("session_id"), str):
            self.session_id = event["session_id"]
        if isinstance(message.get("model"), str):
            self.model = message["model"]
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    self.last_text = text.strip()
            elif btype == "tool_use":
                tool_id = str(block.get("id") or "")
                name = str(block.get("name") or "tool")
                kind = _tool_kind(name)
                tool_input = block.get("input") or {}
                item: Dict[str, Any] = {"id": tool_id, "type": kind, "status": "in_progress"}
                if kind == "command_execution":
                    item["command"] = str(tool_input.get("command") or json.dumps(tool_input, ensure_ascii=False))
                elif kind == "web_search":
                    item["query"] = str(tool_input.get("query") or tool_input.get("url") or "")
                else:
                    item["name"] = name
                    item["command"] = json.dumps(tool_input, ensure_ascii=False)[:MAX_TOOL_OUTPUT]
                self.pending[tool_id] = item
                out.append({"type": "item.started", "item": dict(item)})
        return out

    def _handle_user(self, event: Dict[str, Any]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        message = event.get("message") or {}
        for block in message.get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tool_id = str(block.get("tool_use_id") or "")
            text = _flatten_tool_result(block.get("content"))
            if len(text) > MAX_TOOL_OUTPUT:
                text = text[:MAX_TOOL_OUTPUT] + "\n…(truncated)"
            item = dict(self.pending.get(tool_id) or {"id": tool_id, "type": "command_execution"})
            item["status"] = "completed"
            item["aggregated_output"] = text
            out.append({"type": "item.completed", "item": item})
        return out

    def _handle_result(self, event: Dict[str, Any]) -> List[Dict[str, Any]]:
        if isinstance(event.get("session_id"), str):
            self.session_id = event["session_id"]
        result_text = event.get("result")
        answer = result_text if isinstance(result_text, str) and result_text.strip() else self.last_text
        usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
        final: Dict[str, Any] = {"type": "agent_message", "text": answer or ""}
        if usage:
            final["usage"] = {
                "input_tokens": usage.get("input_tokens") or usage.get("prompt_tokens") or 0,
                "output_tokens": usage.get("output_tokens") or usage.get("completion_tokens") or 0,
            }
        if self.session_id:
            final["session_id"] = self.session_id
        if self.model:
            final["model"] = self.model
        return [final]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"  # connection closes at end → client reads the stream to EOF

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter default logging
        sys.stderr.write("[bridge] " + (fmt % args) + "\n")

    # ── CORS ────────────────────────────────────────────────────────────
    def _cors_headers(self) -> None:
        origin = self.headers.get("Origin") or "*"
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "content-type")
        # Chrome Private Network Access: a public (https) page calling localhost needs this.
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Max-Age", "600")

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_json(self, code: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self._cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] != "/health":
            self._send_json(404, {"error": "not found"})
            return
        claude_bin = find_claude()
        self._send_json(200, {
            "ok": claude_bin is not None,
            "claude": claude_version(claude_bin) if claude_bin else None,
        })

    def do_POST(self) -> None:
        if self.path.split("?", 1)[0] != "/run":
            self._send_json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception as exc:  # noqa: BLE001
            self._send_json(400, {"error": "bad JSON body: %s" % exc})
            return
        prompt = str((payload or {}).get("prompt") or "")
        if not prompt.strip():
            self._send_json(400, {"error": "prompt is required"})
            return
        model = (payload or {}).get("model")
        cwd = str((payload or {}).get("cwd") or WORK_DIR)
        self._run_claude(prompt, model if isinstance(model, str) and model.strip() else None, cwd)

    # ── /run streaming ──────────────────────────────────────────────────
    def _begin_stream(self) -> None:
        self.send_response(200)
        self._cors_headers()
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _emit(self, obj: Dict[str, Any]) -> None:
        self.wfile.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        self.wfile.flush()

    def _run_claude(self, prompt: str, model: Optional[str], cwd: str) -> None:
        claude_bin = find_claude()
        if not claude_bin:
            self._begin_stream()
            self._emit({"bridge": "error", "message": "claude CLI not found on this machine. Install Claude Code or set CLAUDE_BIN."})
            return
        try:
            Path(cwd).mkdir(parents=True, exist_ok=True)
        except Exception:
            cwd = str(Path.home())

        cmd = [claude_bin, "-p", "--output-format", "stream-json", "--verbose", "--dangerously-skip-permissions"]
        if model:
            cmd += ["--model", model]

        self._begin_stream()
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except Exception as exc:  # noqa: BLE001
            self._emit({"bridge": "error", "message": "failed to launch claude: %s" % exc})
            return

        stderr_chunks: List[str] = []

        def drain_stderr() -> None:
            try:
                for line in proc.stderr:  # type: ignore[union-attr]
                    stderr_chunks.append(line)
            except Exception:
                pass

        threading.Thread(target=drain_stderr, daemon=True).start()

        try:
            proc.stdin.write(prompt)  # type: ignore[union-attr]
            proc.stdin.close()  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001
            self._emit({"bridge": "error", "message": "failed to send prompt: %s" % exc})
            try:
                proc.kill()
            except Exception:
                pass
            return

        translator = ClaudeTranslator()
        client_gone = False
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                line = line.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for app_event in translator.handle(event):
                    self._emit(app_event)
        except (BrokenPipeError, ConnectionResetError):
            client_gone = True  # browser navigated away; stop quietly
        finally:
            code = proc.wait()
            if client_gone:
                return
            if code == 0:
                self._emit({"bridge": "done"})
            else:
                tail = "".join(stderr_chunks)[-1000:].strip()
                self._emit({"bridge": "error", "message": "claude exited with code %s%s" % (code, (": " + tail) if tail else "")})


def main() -> None:
    claude_bin = find_claude()
    status = ("found: " + (claude_version(claude_bin) or claude_bin)) if claude_bin else "NOT FOUND (set CLAUDE_BIN)"
    sys.stderr.write("[bridge] claude %s\n" % status)
    sys.stderr.write("[bridge] listening on http://%s:%d\n" % (HOST, PORT))
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
