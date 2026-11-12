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

import base64
import hmac
import ipaddress
import json
import os
import shutil
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

HOST = "127.0.0.1"
_CONFIG_FILE = Path(__file__).parent.parent / "static" / "bridge-config.json"
def _load_config_port() -> int:
    try:
        return int(json.loads(_CONFIG_FILE.read_text()).get("port", 8767))
    except Exception:
        return 8767
PORT = int(os.environ.get("READPAPER_BRIDGE_PORT", "") or _load_config_port())
# Per-tool_result output kept small; the app trims the whole event list again for storage.
MAX_TOOL_OUTPUT = 8000
WORK_DIR = Path(os.environ.get("READPAPER_BRIDGE_WORKDIR", str(Path.home() / ".readpaper-bridge" / "work")))
# Optional pre-built venv: if it exists its bin/ is prepended to PATH so claude's bash tool
# can import packages without having to `pip install` them on each run.
VENV_DIR = Path(os.environ.get("READPAPER_BRIDGE_VENV", str(WORK_DIR.parent / "venv")))

# ── Security ──────────────────────────────────────────────────────────────
# The bridge runs the local `claude` with --dangerously-skip-permissions, so it must only
# serve the app's own frontend. Two gates:
#   1. Origin allowlist — browsers set the Origin header themselves and page JS cannot forge
#      it on a cross-origin fetch, so this blocks any other web page from calling the bridge.
#   2. Optional shared token (READPAPER_BRIDGE_TOKEN) — defense in depth against non-browser
#      local callers, which *can* forge Origin. When set, /run also needs a matching header.
def _parse_origins(raw: str) -> set:
    return {o.strip().rstrip("/") for o in raw.split(",") if o.strip()}

def _is_local_origin(origin: str) -> bool:
    """True for a page served from this machine (loopback/localhost or a private LAN address),
    on any scheme/port. Used so local development on any port is accepted without listing each
    one. Safe because the bridge binds HOST only, so such a page is in a browser on this machine.
    """
    try:
        host = urllib.parse.urlsplit(origin).hostname or ""
    except Exception:
        return False
    if not host:
        return False
    if host in ("localhost",) or host.endswith(".localhost") or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified

# The bridge is dedicated to this user's own app at reader.yunfeizhao.com; no other site may
# drive the local claude. The production origin is the built-in default (localhost entries are
# kept only for local development). Override with READPAPER_BRIDGE_ORIGINS if needed.
ALLOWED_ORIGINS = _parse_origins(os.environ.get(
    "READPAPER_BRIDGE_ORIGINS",
    "https://reader.yunfeizhao.com,http://localhost,http://127.0.0.1",
))
BRIDGE_TOKEN = os.environ.get("READPAPER_BRIDGE_TOKEN", "").strip()


def _bridge_env() -> Dict[str, str]:
    """Return an env for the claude subprocess, injecting the pre-built venv when present."""
    env = os.environ.copy()
    venv_bin = VENV_DIR / "bin"
    if venv_bin.is_dir():
        env["VIRTUAL_ENV"] = str(VENV_DIR)
        env["PATH"] = str(venv_bin) + os.pathsep + env.get("PATH", "")
        env.pop("PYTHONHOME", None)
    return env


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

    # ── CORS / authorization ──────────────────────────────────────────────
    def _origin_allowed(self) -> bool:
        origin = (self.headers.get("Origin") or "").rstrip("/")
        if not origin:
            return False
        if any(origin == o or origin.startswith(o + ":") for o in ALLOWED_ORIGINS):
            return True
        # Local development: accept any loopback/LAN origin on any port. This is safe because the
        # bridge binds HOST (127.0.0.1) only, so a page can reach it solely from this same machine
        # — a remote site has a public origin and never matches here.
        return _is_local_origin(origin)

    def _authorized(self) -> bool:
        """A request may run only from an allowed Origin AND, when a token is configured,
        with a matching X-Bridge-Token header (constant-time comparison)."""
        if not self._origin_allowed():
            return False
        if BRIDGE_TOKEN:
            provided = self.headers.get("X-Bridge-Token") or ""
            return hmac.compare_digest(provided, BRIDGE_TOKEN)
        return True

    def _cors_headers(self) -> None:
        # Only reflect Access-Control-Allow-Origin for origins on the allowlist; without it the
        # browser refuses to expose the response to the calling page.
        origin = (self.headers.get("Origin") or "").rstrip("/")
        if origin and self._origin_allowed():
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            # Chrome Private Network Access: a public (https) page calling localhost needs this.
            self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "content-type, x-bridge-token")
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
            "project": "readpaper",
            "claude": claude_version(claude_bin) if claude_bin else None,
        })

    def do_POST(self) -> None:
        if self.path.split("?", 1)[0] != "/run":
            self._send_json(404, {"error": "not found"})
            return
        if not self._authorized():
            self._send_json(403, {"error": "forbidden: origin not allowed or missing/invalid bridge token"})
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
        # The working directory is fixed to WORK_DIR — never taken from the request, so a
        # caller cannot point the local claude process at an arbitrary directory on disk.
        cwd = str(WORK_DIR)
        # Write any files the browser fetched (e.g. the source PDF) into the work dir so
        # the local claude process can read them without making further network requests.
        for f in (payload or {}).get("files") or []:
            name = str(f.get("name") or "file").replace("/", "_").replace("..", "_")
            data_b64 = f.get("data") or ""
            try:
                Path(cwd).mkdir(parents=True, exist_ok=True)
                (Path(cwd) / name).write_bytes(base64.b64decode(data_b64))
            except Exception as exc:
                sys.stderr.write("[bridge] could not write file %s: %s\n" % (name, exc))
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
                env=_bridge_env(),
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
