# 项目说明(新手上手)

> 面向刚接触这个代码库的人。读完能建立整体心智模型,知道每块代码在哪、为什么这么设计。

## 0. 这个仓库有两个独立部分

| 部分 | 位置 | 说明 |
|------|------|------|
| **Codex/Claude Tree QA**(本文主角) | 仓库根 `app.py` + `app/` + `templates/` + `static/` | 一个把问答组织成**树**的 Flask Web 应用 |
| **treeflow CLI** | `cli/` | 完全独立的命令行工具,浏览本地 Claude Code / Codex 历史会话。**与 Web 应用没有代码依赖**,刚上手可忽略 |

两者只共享 git 历史和"把会话当树"的思路,不互相 import。

---

## 1. 核心概念:把问答组织成「树」

普通聊天是**一条线**:问 → 答 → 再问 → 再答。

这个项目是**一棵树**:每个「提问 + 回答」是一个**节点(node)**,可以从**任意历史节点**分叉出新问题。

```
根问题:这篇论文讲什么?
├─ 它的方法有什么创新?        ← 从根分叉
│   └─ 和 XX 方法比呢?
└─ 实验设置是怎样的?          ← 又从根分叉(与上面并列)
    └─ 数据集多大?
```

**为什么这样设计**:读论文时常想「从摘要这个点分头深挖方法和实验」,各条探索路径互不干扰,都保留独立上下文。

**关键机制**:每次提问时,系统沿当前节点一路上溯到根,把所有祖先的「问 + 答 + 附件 + workspace 引用」拼成一个完整 prompt 发给 AI(见 `app/prompts.py` 的 `build_codex_prompt`)。所以每次调用 AI 都是**全自包含**的,不依赖任何"会话记忆"——这也是能从任意节点自由分叉的根本原因。

---

## 2. 一次提问的完整流转(最重要)

在网页上选后端、输入问题、点发送后:

```
1. 前端 POST /api/ask
        ↓  app/routes.py: api_ask()
2. create_node(...)          把节点写进 SQLite,状态 = queued
        ↓  app/db.py
3. start_worker(node_id)     开一个后台线程跑 AI
        ↓  app/runners/__init__.py
4. 按节点的 backend 字段选 runner:
     claude → app/runners/claude.py   (调 Anthropic API,带 bash + web_search 工具)
     codex  → app/runners/codex.py    (shell 出 codex exec 子进程)
        ↓
5. runner 内部:
     build_codex_prompt()           拼祖先上下文        (app/prompts.py)
     workspace_snapshot()           运行前拍文件快照     (app/workspace.py)
     循环调用 AI,期间:
       - 每来一批事件就 update_node() 写进 raw_events_json  ← 前端轮询可见"实时"进度
       - AI 要跑命令 → 在 instance/workspace/ 目录执行
     record_workspace_changes()     运行后对比快照,归因产出文件
     update_node(status=done, answer=...)
        ↓
6. 前端定期 GET /api/tree,看到状态变 done,渲染出回答
```

**务必记住**:后端**不是**等 AI 答完才返回。`/api/ask` 立即返回(HTTP 202),AI 在后台线程跑,前端靠轮询 `/api/tree` 看进展。

---

## 3. 两个后端

| | Codex | Claude |
|---|---|---|
| 怎么调 | shell 出 `codex exec` 命令行子进程 | Anthropic Python SDK |
| 需要什么 | `sub-pool-client` 包(租号) | `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` 或 `ANTHROPIC_API_KEY` |
| 代码 | `app/runners/codex.py` | `app/runners/claude.py` |
| 工具能力 | web search + 完整本机 exec | bash 工具 + (可选) web_search |

两者产出的"事件流"由 `app/events.py` 统一解析(token 统计、工具计数、答案提取),所以前端无需关心用了哪个后端。

> 注:`sub-pool-client` 未安装时,Codex 后端会返回友好错误并提示切到 Claude;Claude 后端不受影响。

---

## 4. 目录结构

```
read_paper/
├── app.py                  # 瘦入口:app = create_app(); python app.py 启动
├── app/                    # 后端包
│   ├── __init__.py         # create_app() 应用工厂:建 Flask、建库、注册路由
│   ├── config.py           # .env 加载、可选依赖导入、所有常量与路径
│   ├── utils.py            # 纯函数:时间 / JSON / 文本辅助
│   ├── db.py               # SQLite:连接、建表、nodes/files CRUD(数据骨架)
│   ├── workspace.py        # 共享 workspace 目录的文件追踪与归因
│   ├── prompts.py          # 把祖先链 + 附件拼成自包含 prompt(树的"灵魂")
│   ├── events.py           # 事件流解析:token、工具数、答案、JSONL
│   ├── runners/
│   │   ├── __init__.py     # start_worker:按 backend 选 runner,后台线程执行
│   │   ├── codex.py        # Codex 后端
│   │   └── claude.py       # Claude API 后端(agentic 循环)
│   ├── uploads.py          # 用户上传附件落盘 + 文本片段缓存
│   ├── export.py           # 把某节点子树导出为 Markdown
│   ├── claude_history.py   # 只读导入本地 Claude Code 历史 + 合成 resume 会话
│   └── routes.py           # 所有 HTTP 路由(Flask Blueprint)
├── templates/index.html    # 单页外壳
├── static/
│   ├── app.js              # 前端全部逻辑(~2600 行原生 JS,SVG 画树,无框架)
│   └── styles.css
├── instance/               # 运行时数据(SQLite、workspace、上传)
│   ├── codex_qa.sqlite
│   └── workspace/          # AI 可读写的共享临时目录
│       └── uploads/        # 用户上传的附件
└── cli/                    # 独立的 treeflow CLI(与上面无关)
```

### 模块依赖方向(无环)

```
config  ← 所有模块
utils   ← db / workspace / prompts / uploads / claude_history
db      ← workspace / prompts / export / uploads / runners / routes
workspace / events / prompts ← runners / routes
runners / uploads / export / claude_history ← routes
routes  ← __init__(create_app 注册 blueprint)
```

---

## 5. 数据模型(SQLite,见 `app/db.py` 的 `init_db`)

| 表 | 作用 |
|----|------|
| `nodes` | 一行一个问答节点。`parent_id` 自引用形成树。存 prompt、answer、status(`queued`→`running`→`done`/`failed`)、token 用量、工具调用数、原始事件 JSON、每节点笔记、backend 等 |
| `files` | 上传的附件,文本类缓存一段摘要片段 |
| `node_workspace_files` | 记录每个节点运行时在 workspace 里新建/改动了哪些文件 |
| `account_usage` / `account_probe` | 旧的按账号 token 记账,基本被 sub-pool 取代 |

**树就靠 `nodes.parent_id` 自引用**。`parent_id` 为 NULL 的是根节点。

---

## 6. 两个容易混淆的"文件"概念

| | 上传附件 | workspace 文件 |
|---|---|---|
| 谁产生 | 用户主动上传给某个问题 | AI 运行时在 workspace 目录读写 |
| 存哪 | `instance/workspace/uploads/<file_id>/` | `instance/workspace/` 下任意位置 |
| 代码 | `app/uploads.py` | `app/workspace.py` |
| 怎么归因 | 显式挂在节点的 `attachments_json` | 运行前后快照对比,写入 `node_workspace_files` |

---

## 7. 一个独立的只读功能:Claude Code 历史导入

`app/claude_history.py` + 路由 `/api/claude-code/tree` 能直接解析本地
`~/.claude/projects/<slug>/*.jsonl`,把一段 Claude Code 会话重建成树,显示在同一个 UI 里(前端"历史"模式)。

`/api/claude-code/resume-from-node` 更进一步:它把某条(可能被遗弃的)分支写成一个**合成 session 文件**,让 `claude --resume <sid>` 能精确跳回那个点——这是 stock Claude Code 正常够不到的分支。此逻辑与 `cli/treeflow` 思路一致但各自独立。

---

## 8. 怎么跑起来

依赖用 **uv** 管理(`pyproject.toml` + `uv.lock`):

```bash
uv sync                 # 安装 Claude 后端所需依赖
uv sync --extra codex   # 需要 Codex 后端时再加这个(装 sub-pool-client)
uv run python app.py    # 默认 http://127.0.0.1:8080
```

只需在 `.env` 里配 Claude 凭证即可用 Claude 后端:

```bash
ANTHROPIC_BASE_URL=...      # 自建网关;或用官方 API 时改成 ANTHROPIC_API_KEY
ANTHROPIC_AUTH_TOKEN=...
CLAUDE_API_MODEL=claude-sonnet-4-6
```

打开网页后,顶部 `Codex | Claude` 切换按钮选 **Claude**,就能上传论文 PDF/文本提问了。

> VSCode 里直接按 F5(见 `.vscode/launch.json`)也能启动,会自动读 `.env`。

---

## 9. 建议的代码阅读顺序

1. `app/db.py` 的 `init_db()` — 先看建表 SQL,理解 `nodes` 表字段(树的数据骨架)
2. `app/routes.py` — 所有 API 入口,重点 `api_ask`,看一遍就知道前端能干什么
3. `app/prompts.py` 的 `build_codex_prompt` — 树结构的"灵魂"
4. `app/runners/claude.py` 的 `run_loop()` — agentic 循环(调 AI → 跑工具 → 再调)
5. 其余 `workspace.py` / `events.py` / `uploads.py` / `export.py` / `claude_history.py` 用到再看
6. 前端 `static/app.js` — 用 SVG 画树,状态都在顶层 `state` 对象里
