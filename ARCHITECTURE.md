# AgentMemHub 架构

## 定位

AgentMemHub 从所有 Agent Harness（ZCode / OpenCode / Hermes / WorkBuddy / Qwen / QoderCN / DSH）读取完整对话历史
→ 归一化为**统一事件流**（user / assistant / tool / reasoning / patch / shell，含工具链、思维链、Shell 执行）
→ 存入本地 SQLite（可搜索）→ 导出（JSONL / Markdown）→ 桥接 MemOS 生成记忆。

```
各 Agent 原始数据                          AgentMemHub (agentmemhub/ 包)
┌──────────────────┐     ┌──────────────────────────────┐     ┌─────────────┐
│ ZCode db.sqlite  │     │  adapters/  (统一接口)         │     │ store.py     │
│ OpenCode state.db│───> │  ├ zcode / opencode (SQLite)  │───> │ SQLite       │
│ Hermes state.db  │     │  ├ hermes  (SQLite)           │     │ conversations│
│ WorkBuddy db     │     │  ├ qwen / qodercn (JSONL)     │     │ events       │
│ Qwen chats/*.jsonl│    │  └ dsh    (zstd JSONL)        │     │ events_fts   │
│ QoderCN *.jsonl  │     └──────────────────────────────┘     └──────┬──────┘
│ DSH *.zstd       │           统一事件流                            │
└──────────────────┘     models.py                                   │
                                                                     ├─> export.py   → JSONL / Markdown
                                                                     └─> memos.py    → MemOS bundle
```

## 目录结构

```
AgentMemHub/
├── agentmemhub/                # Python 包（整合封装）
│   ├── __init__.py             # 公共 API（Event / Store / load_all）
│   ├── __main__.py             # python -m agentmemhub 入口
│   ├── models.py               # 统一事件模型：Event 数据类 + 归一化/渲染工具
│   ├── schema.sql              # SQLite schema：conversations / events / events_fts (FTS5)
│   ├── store.py                # 存储层：读写 / 增量重建 / FTS 检索（CJK LIKE 兜底）
│   ├── export.py               # JSONL / Markdown 导出
│   ├── memos.py                # 生成 MemOS 导入 bundle + push
│   ├── cli.py                  # CLI 命令实现（ingest/list/show/search/export/memos/stats/adapters）
│   └── adapters/               # 各 Agent 适配器（统一接口 AgentAdapter）
│       ├── base.py             #   抽象基类：candidate_paths / locate / load
│       ├── sqlite_conversation.py  #   通用 SQLite 适配器（ZCode/OpenCode 共享）
│       ├── zcode.py / opencode.py  #   session/message/part 结构
│       ├── hermes.py           #   state.db sessions/messages
│       ├── qwen.py             #   chats/*.jsonl
│       ├── qodercn.py          #   JSONL
│       ├── workbuddy.py        #   sessions 元数据 + audit-log(Shell)
│       ├── dsh.py              #   zstd 解压 JSONL
│       └── __init__.py         #   adapter 注册表 + load_all
├── docs/                       # IMPLEMENTATION_REVIEW、EXAMPLES
├── scripts/                    # sensitive_scan / web_verify / js_check
├── agentmemhub/web/            # 可选 Web 子模块（feat/web-ui 分支）
│   ├── app.py                  #   FastAPI 路由：stats·facets·conversations(分页)·events·folders + DELETE/PATCH，绑定 127.0.0.1
│   ├── aggregates.py           #   统计聚合（SQL GROUP BY 下推）+ 进程内 TTL 缓存
│   └── static/
│       ├── index.html          #   仪表盘（WebsiteDesign 原型 API 化改造）
│       └── vendor/             #   tailwind browser / lucide / chart.js 本地化（离线可用）
├── tests/test_web_server.py    # Web API 冒烟测试（TestClient + 临时库）
├── exports/                    # 导出输出（gitignore）
├── README.md / LICENSE / SECURITY.md / ARCHITECTURE.md / .env.example / pyproject.toml
```

使用入口：`python -m agentmemhub <command>`（等价 `python -m agentmemhub.cli`）；Web 页面：`python -m agentmemhub serve`。

## Web 子模块设计要点

- **可选启用**：核心 CLI 不依赖 fastapi；仅 `serve` 子命令需要 `uv pip install -e ".[web]"`
- **性能模型（大库友好）**：
  - 引导数据 `/api/bootstrap` 只含会话元数据（不含事件流）；事件在打开详情抽屉时按需查询（命中主键索引），超长会话截断（前 88 + 后 12 条）并标注
  - 列表筛选/搜索下推后端（FTS5 + LIKE 兜底），服务端排序分页；conversations 元数据全量一次下发（行级轻量）
  - 统计聚合 SQL GROUP BY + 进程内 TTL 缓存（60s），删除/改标题即时失效
- **API**：GET stats · bootstrap · facets · folders · conversations（多值 sources/workspace、days 时间窗、q 搜索、all=1）· conversations/{src}/{id}/events；DELETE conversations/{src}/{id}（级联删事件+FTS）；PATCH …/title
- **前端**：原生 JS 单页（无构建链），静态快照原型改造——取数点从内联 JSON 改为 fetch API；vendor 离线可用；编辑/删除走真实 API（二次确认）
- **安全**：只绑 127.0.0.1；删除为显式接口；无外发通道

## 核心设计

### 1. 统一事件模型（models.py）

每个会话 = 元数据 + 有序事件流。事件类型：

| role | 含义 | 关键字段 |
|---|---|---|
| `user` | 用户输入 | content |
| `assistant` | Agent 文本回复 | content, model |
| `tool` | 工具调用/结果（含 Shell） | tool_name, tool_input, tool_output, tool_status |
| `reasoning` | 思维链 | content / reasoning |
| `patch` | 代码变更 | patch_file, patch_diff |
| `meta` | 事件边界/元信息 | content, model |

每个事件保存 `raw_json`（各 Agent 原始事件 JSON）实现**无损保底**——即使字段无法映射，原始数据也不丢失。

### 2. SQLite 存储（store.py · schema.sql）

- `conversations`：会话元数据 + `roles_json` + `signature`（增量指纹）
- `events`：全量事件流，PK `(source, conversation_id, seq)`，含 `raw_json`
- `events_fts`：FTS5 全文索引（英文/词组走 FTS5，中文子串走 LIKE 兜底）

### 3. Adapter 架构（adapters/）

统一接口 `AgentAdapter`：
```python
def candidate_paths(self) -> list[Path]   # 数据路径发现
def locate(self) -> Optional[Path]        # 找第一个存在路径
def load(self, path) -> list[dict]        # 返回统一会话列表
```
约定输出：`{source, id, title, cwd, created_at, updated_at, model, meta, events: [Event]}`。
- ZCode / OpenCode 共享 `SqliteConversationAdapter`（同 schema，自动适配 sequence 列差异）
- 新增一个 Agent 只需实现一个 adapter 类并注册进 `agentmemhub/adapters/__init__.py`

### 4. MemOS 桥接（memos.py）

粒度：每个会话 → 1 episode；每条 user 消息（含后续 assistant/tool/reasoning）→ 1 trace。
TraceDTO：`{id, episodeId, sessionId, ts, userText, agentText, summary, toolCalls, agentThinking}`。
通过 `POST /api/v1/import` 导入 MemOS（直接落 L1 trace 库，供后续检索召回）。

## 使用

```bash
python -m agentmemhub ingest            # 提取全部 adapter 并入库
python -m agentmemhub search "登录"     # 搜索
python -m agentmemhub show zcode <id>   # 查看会话 (Markdown)
python -m agentmemhub export --format jsonl --out exports/   # 全量导出
python -m agentmemhub memos --out bundle.json --push http://127.0.0.1:18800  # MemOS 导入
```
