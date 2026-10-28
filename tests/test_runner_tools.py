"""runner 侧的「是否带联网搜索工具」决策(纯函数,不触网)。"""
from __future__ import annotations

from app.runners.claude import WEB_SEARCH_VARIANTS, _claude_initial_tools
from app.runners.codex import _codex_search_args


def test_codex_search_args_enabled():
    assert _codex_search_args(True) == ["--search"]


def test_codex_search_args_disabled():
    assert _codex_search_args(False) == []


def test_claude_tools_disabled_is_bash_only():
    bash = {"type": "bash_20250124", "name": "bash"}
    tools = _claude_initial_tools(bash, False)
    assert tools == [bash]
    assert not any(t.get("name") == "web_search" for t in tools)


def test_claude_tools_enabled_includes_web_search():
    bash = {"type": "bash_20250124", "name": "bash"}
    tools = _claude_initial_tools(bash, True)
    assert bash in tools
    assert tools[-1] == WEB_SEARCH_VARIANTS[0]
    assert any(t.get("name") == "web_search" for t in tools)
