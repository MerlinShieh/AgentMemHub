# AgentMemHub

统一提取你电脑上所有 AI Agent Harness 的对话历史 → 归一为**全量事件流**（含工具链、思维链、Shell 执行、代码补丁）→ 本地 SQLite 存储可搜索 → 导出 JSONL / Markdown → 桥接 [MemOS Local Plugin](https://github.com/MemTensor/MemOS) 生成记忆。

**让任何 Agent 的会话经验，变成可检索、可迁移、可复用的统一记忆资产。**

## 支持的 Agent

| Agent | 数据来源 | 格式 |
|---|---|---|
| **ZCode** | `~/.zcode/cli/db/db.sqlite` | SQLite |
| **OpenCode** | `~/.local/share/opencode/opencode.db` | SQLite |
| **Hermes** | `%LOCALAPPDATA%\hermes\state.db` | SQLite |
| **WorkBuddy** | `~/.workbuddy/workbuddy.db` + audit-log | SQLite + JSONL |
| **Qwen** | `~/.qwen/projects/*/chats/*.jsonl` | JSONL |
| **QoderCN** | `~/.qoder-cn/.../*.jsonl` | JSONL |
| **DSH** | `~/.dsh/sessions/*/session.jsonl.zstd` | zstd JSONL |

## 快速开始

```bash
# 1. 提取所有 Agent 并入库
python agentmemhub.py ingest

# 2. 搜索（FTS5 英文 + 中文子串）
python agentmemhub.py search "登录"

# 3. 查看某个会话（Markdown，含思维链+工具）
python agentmemhub.py show zcode sess_xxxx

# 4. 导出全量（JSONL 每行一事件 / Markdown 可读）
python agentmemhub.py export --format jsonl --out exports/
python agentmemhub.py export --format markdown --out exports_md/

# 5. 生成 MemOS 导入 bundle
python agentmemhub.py memos --out exports/memos_bundle.json

# 6. 推送到运行中的 MemOS Local Plugin
python agentmemhub.py memos --push http://127.0.0.1:18800
```

## 查询示例

```bash
# ---- 关键词搜索 ----
# 中文自动走 LIKE 子串匹配，英文/词组走 FTS5 全文索引
python agentmemhub.py search "登录"                        # 全部 7 个来源
python agentmemhub.py search "登录" --source zcode         # 只搜某个来源
python agentmemhub.py search "登录" --role tool            # 只搜工具事件
python agentmemhub.py search "登录" --role reasoning       # 只搜思维链
python agentmemhub.py search "登录" --limit 50             # 条数限制（默认 20）

# ---- 查看单个会话（Markdown，含思维链/工具/补丁渲染）----
python agentmemhub.py show zcode sess_d8648672-3cc8-4bbc-8e4f-3e50afc6b032
python agentmemhub.py show opencode ses_0b10aad95ffe70V5

# ---- 列出会话 ----
python agentmemhub.py list                        # 全部来源
python agentmemhub.py list --source hermes        # 只列某个来源
```

## 导出示例

```bash
# 导出为 JSONL：每个会话一个 <source>__<session_id>.jsonl，每行一个事件
python agentmemhub.py export --format jsonl --out exports/
# → exports/zcode__sess_d86486....jsonl, exports/opencode__ses_0b10....jsonl, ...

# 导出为 Markdown：人类可读，含 👤用户/💭思考/🔧工具/📝修改 渲染
python agentmemhub.py export --format markdown --out exports_md/
# → exports_md/zcode__sess_d86486....md, ...

# 只导出某个来源、指定输出目录
python agentmemhub.py export --format markdown --source zcode --out exports_zcode/

# 导出后导入 MemOS（先启动 MemOS Local Plugin）
python agentmemhub.py memos --out exports/memos_bundle.json              # 生成 bundle
python agentmemhub.py memos --push http://127.0.0.1:18800              # 或直接推送
```

导出的 JSONL 每行就是一个标准事件：

```jsonc
{"role":"user","content":"帮我修复登录页面","time":1750000001}
{"role":"reasoning","content":"登录按钮没反应，先看代码","time":1750000002}
{"role":"tool","tool_name":"Bash","tool_input":{"command":"npm test"},"tool_output":"FAIL src/login.ts","tool_status":"completed","time":1750000003}
```

## 数据与文件存放位置

| 内容 | 默认位置 | 覆盖方式 |
|---|---|---|
| SQLite 数据库 | `~/.agentmemhub/agentmemhub.db` | 环境变量 `AGENTMEMHUB_DB` 或 `AGENTMEM_HUB_DATA_DIR` |
| 导出目录 | 项目下 `exports/` | `--out` 参数 |
| MemOS bundle | `exports/memos_bundle.json` | `--out` 参数 |

数据库三张核心表：

- `conversations` — 会话元数据（source / id / title / cwd / model / 时间）
- `events` — 全量事件流（role / content / tool / reasoning / patch / shell，含 `raw_json` 原始保底）
- `events_fts` — FTS5 全文索引（英文检索）

> `exports/` 已加入 `.gitignore`，含真实对话的导出不会进入仓库。

## 编程访问（Python）

核心存储层可直接编程调用：

```python
from store import Store

store = Store()                                  # 默认 ~/.agentmemhub/agentmemhub.db
convs = store.list_conversations("zcode")        # 列出 zcode 的会话
events = store.get_events("zcode", "<session-id>")  # 读取某会话事件流
hits = store.search("登录", role="tool")          # 搜索工具事件
```

## 统一事件流（全量保留）

不丢弃工具链、思维链、Shell 执行、代码补丁——每行一个 JSON 事件：

```jsonc
{"role":"user","content":"帮我修复登录页面","time":1750000001}
{"role":"reasoning","content":"登录按钮没反应，先看代码","time":1750000002}
{"role":"tool","tool_name":"Bash","tool_input":{"command":"npm test"},"tool_output":"FAIL src/login.ts","tool_status":"completed","time":1750000003}
{"role":"assistant","content":"是事件监听器问题","model":"claude-x","time":1750000004}
{"role":"patch","patch_file":"src/login.ts","patch_diff":"@@ -12,3 +12,5 @@","time":1750000005}
```

每个事件保留 `raw_json`（各 Agent 原始 JSON）实现**无损保底**。

## 命令行

| 命令 | 说明 |
|---|---|
| `ingest [--source x]` | 提取全部/指定 adapter 并入库 |
| `list [--source x]` | 列出会话 |
| `show <source> <id>` | 查看会话（Markdown）|
| `search <q> [--source] [--role] [--limit]` | 全文搜索事件正文 |
| `export --format jsonl\|markdown [--source] [--out dir]` | 导出 |
| `memos [--source] [--out] [--push url]` | 生成/推送 MemOS bundle |
| `stats` / `adapters` | 统计 / adapter 状态 |

## 数据模型

- **conversations**：会话元数据（source/id/title/cwd/model/时间/signature）
- **events**：全量事件流（role/content/tool/reasoning/patch/shell/raw_json）
- **events_fts**：FTS5 全文索引（英文走 FTS，中文子串走 LIKE 兜底）

详见 [ARCHITECTURE.md](./ARCHITECTURE.md)。

## 需求

- Python 3.10+
- `pip install zstandard`（仅 DSH 需要）

## 隐私与脱敏

本项目**本地运行、不上传任何数据**。推送到公开仓库的部分严格脱敏：

- 代码用 `Path.home()` / 环境变量在运行时发现路径，**不硬编码真实用户名或绝对路径**
- 敏感配置用示例文件给出：`.env.example`（环境变量占位）、`sources.example.json`（数据源示例）
- 真实会话导出默认为 `exports/`（已在 `.gitignore`，勿强制推送）
- 详细规则见 [SECURITY.md](./SECURITY.md)

## Roadmap

- [x] 统一事件模型 + SQLite 存储（FTS5）
- [x] 7 个 Agent Adapter
- [x] 全量检索 + JSONL/Markdown 导出
- [x] MemOS bundle 桥接
- [ ] Web UI（复用 ai-conversation-hub 前端外壳，增强 tool/reasoning 展示）
- [ ] 更多 Agent（Claude Code / Cursor / Gemini CLI / CodeBuddy）
- [ ] 记忆清洗规则（去注入元数据、压缩折叠会话）
