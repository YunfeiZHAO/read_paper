"""入口文件。后端代码已拆分到 app/ 包,这里只负责创建并启动应用。

`python app.py` 以 __main__ 运行,其中 `from app import create_app` 解析到 app/ 包
(同名下包优先于模块),两者不冲突。
"""
from __future__ import annotations

import os

from app import create_app

app = create_app()


if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host=host, port=port, debug=debug, use_reloader=False)
