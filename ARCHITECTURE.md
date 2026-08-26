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
├── docs/                       # IMPLEMENTATION_REVIEW 等
├── scripts/sensitive_scan.py   # 推送前敏感信息扫描
├── exports/                    # 导出输出（gitignore）
├── README.md / LICENSE / SECURITY.md / ARCHITECTURE.md / .env.example
```

使用入口：`python -m agentmemhub <command>`（等价 `python -m agentmemhub.cli`）。

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
