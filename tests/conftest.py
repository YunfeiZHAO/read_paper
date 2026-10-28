"""共享 fixture:把数据目录指到临时目录,避免污染真实 instance/。

关键:config 在 import 时就读取 CODEX_QA_DATA_DIR 并定下 DB/工作目录路径,
因此必须在**任何 app 模块被 import 之前**设置好环境变量。这里用一个进程级
临时目录在 import app 之前落定,fixture 再按需重建干净的库。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# 在 import app 之前就把数据目录钉到临时位置。
_TMP_DATA_DIR = tempfile.mkdtemp(prefix="treeqa-tests-")
os.environ["CODEX_QA_DATA_DIR"] = _TMP_DATA_DIR
# 测试默认不开联网搜索(与生产默认一致);个别用例自行 monkeypatch。
os.environ.setdefault("ENABLE_WEB_SEARCH", "false")


@pytest.fixture()
def fresh_db(tmp_path: Path):
    """每个用例一个干净的 SQLite 库。"""
    from app import db

    db_path = tmp_path / "test.sqlite"
    # 重定向 DB_PATH 到用例专属文件并初始化。
    original = db.DB_PATH
    db.DB_PATH = db_path
    try:
        db.ensure_dirs()
        db.init_db()
        yield db
    finally:
        db.DB_PATH = original


@pytest.fixture()
def client(fresh_db):
    """FastAPI 测试客户端,复用 fresh_db 的库。"""
    from fastapi.testclient import TestClient

    from app import create_app

    app = create_app()
    with TestClient(app) as test_client:
        yield test_client
