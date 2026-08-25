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

## Roadmap

- [x] 统一事件模型 + SQLite 存储（FTS5）
- [x] 7 个 Agent Adapter
- [x] 全量检索 + JSONL/Markdown 导出
- [x] MemOS bundle 桥接
- [ ] Web UI（复用 ai-conversation-hub 前端外壳，增强 tool/reasoning 展示）
- [ ] 更多 Agent（Claude Code / Cursor / Gemini CLI / CodeBuddy）
- [ ] 记忆清洗规则（去注入元数据、压缩折叠会话）
