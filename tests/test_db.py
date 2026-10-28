"""nodes.enable_web_search 的持久化、布尔化与迁移。"""
from __future__ import annotations

import sqlite3


def test_create_node_persists_flag(fresh_db):
    on = fresh_db.create_node(None, "开启", [], [], enable_web_search=True)
    off = fresh_db.create_node(None, "关闭", [], [], enable_web_search=False)

    assert fresh_db.get_node(on["id"])["enable_web_search"] is True
    assert fresh_db.get_node(off["id"])["enable_web_search"] is False


def test_default_flag_is_false(fresh_db):
    node = fresh_db.create_node(None, "默认", [], [])
    assert fresh_db.get_node(node["id"])["enable_web_search"] is False


def test_public_node_coerces_to_bool(fresh_db):
    node = fresh_db.create_node(None, "强制布尔", [], [], enable_web_search=True)
    value = fresh_db.get_node(node["id"])["enable_web_search"]
    assert value is True and isinstance(value, bool)


def test_migration_adds_column(tmp_path, fresh_db):
    """旧库缺列时 init_db 应补上,且旧行默认 0。"""
    legacy = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(legacy)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id TEXT PRIMARY KEY,
            parent_id TEXT,
            title TEXT NOT NULL,
            prompt TEXT NOT NULL,
            answer TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'queued',
            backend TEXT NOT NULL DEFAULT 'codex',
            archived INTEGER NOT NULL DEFAULT 0,
            bookmarked INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO nodes (id, title, prompt) VALUES ('old1', 't', 'p');
        """
    )
    conn.commit()
    conn.close()

    fresh_db.DB_PATH = legacy
    fresh_db.init_db()

    conn = sqlite3.connect(legacy)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(nodes)").fetchall()}
    assert "enable_web_search" in columns
    value = conn.execute("SELECT enable_web_search FROM nodes WHERE id = 'old1'").fetchone()[0]
    conn.close()
    assert value == 0
