"""Parsing of the ENABLE_WEB_SEARCH environment variable."""
from __future__ import annotations

import importlib


def _reload_config_with(monkeypatch, value):
    import app.config as config

    if value is None:
        monkeypatch.delenv("ENABLE_WEB_SEARCH", raising=False)
    else:
        monkeypatch.setenv("ENABLE_WEB_SEARCH", value)
    return importlib.reload(config)


def test_default_is_false(monkeypatch):
    config = _reload_config_with(monkeypatch, None)
    assert config.ENABLE_WEB_SEARCH is False


def test_truthy_values(monkeypatch):
    for value in ("true", "True", "1", "on", "YES", " on "):
        config = _reload_config_with(monkeypatch, value)
        assert config.ENABLE_WEB_SEARCH is True, value


def test_falsy_values(monkeypatch):
    for value in ("false", "0", "off", "no", ""):
        config = _reload_config_with(monkeypatch, value)
        assert config.ENABLE_WEB_SEARCH is False, value
