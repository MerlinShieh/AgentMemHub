# AgentMemHub 查询示例

数据库（默认 `~/.agentmemhub/agentmemhub.db`）的三张核心表：
- `conversations` — 会话元数据（source / id / title / cwd / model / 时间 / event_count）
- `events` — 全量事件流（role / content / tool / reasoning / patch / shell / raw_json）
- `events_fts` — FTS5 全文索引

下面每个场景给 **CLI / Python / SQL** 三种查询方式（SQL 用 `sqlite3 agentmemhub.db` 或 Python `sqlite3` 模块）。

---

## 1. 有哪些 Agent 被纳入了

```bash
# CLI
uv run python -m agentmemhub adapters
```

```python
# Python
from agentmemhub import all_adapters
for a in all_adapters():
    d = a.describe()
    print(d["source"], d["label"], d["path"])
```

```sql
-- SQL：哪些来源有数据
SELECT source, COUNT(*) AS sessions FROM conversations GROUP BY source ORDER BY sessions DESC;
```

---

## 2. 每个 Agent 有多少会话 / 事件

```bash
uv run python -m agentmemhub stats
```

```Python
from agentmemhub.store import Store
s = Store().stats()
print(s)   # {"conversations": 232, "events": 61453, "sources": [...]}
```

```sql
SELECT source, COUNT(*) AS sessions,
       SUM(event_count) AS events
FROM conversations GROUP BY source ORDER BY sessions DESC;
```

---

## 3. 某个 Agent 都有哪些会话

```bash
uv run python -m agentmemhub list --source hermes
```

```python
from agentmemhub.store import Store
store = Store()
for c in store.list_conversations("hermes"):
    print(c["id"], "|", c["title"], "|", c["event_count"], "事件")
```

```sql
SELECT id, title, cwd, event_count FROM conversations WHERE source = 'hermes' ORDER BY updated_at DESC;
```

---

## 4. 某个 Agent 下某个会话的完整聊天内容

```bash
uv run python -m agentmemhub show hermes 20260812_095807_9c3be0
```

```python
from agentmemhub.store import Store
store = Store()
for e in store.get_events("hermes", "20260812_095807_9c3be0"):
    print(e.seq, e.role, e.content or e.tool_name or "")
```

```sql
-- events 表按 seq 有序，role 区分消息类型
SELECT seq, role, content, tool_name, tool_output
FROM events WHERE source='hermes' AND conversation_id='20260812_095807_9c3be0'
ORDER BY seq;
```

---

## 5. 某文件夹下，不同 Agent 各有多少会话 ⭐

```bash
uv run python -m agentmemhub folders
uv run python -m agentmemhub folders --limit 50
```

```python
# Python：按 cwd 最后一段分组统计
from collections import defaultdict
from agentmemhub.store import Store
groups = defaultdict(lambda: defaultdict(int))
for c in Store().list_conversations():
    ws = (c["cwd"] or "(unknown)").rstrip("\\/").rsplit("\\",1)[-1].rsplit("/",1)[-1]
    groups[ws][c["source"]] += 1
for ws, srcs in sorted(groups.items(), key=lambda x: -sum(x[1].values())):
    print(sum(srcs.values()), ws, dict(srcs))
```

```sql
-- SQL：按文件夹名（cwd 去尾斜杠后取末段）统计
WITH f AS (
  SELECT source, cwd,
         REVERSE(SUBSTR(REVERSE(REPLACE(REPLACE(cwd,'\\','/'),' ','')), 1,
                INSTR(REVERSE(REPLACE(REPLACE(cwd,'\\','/'),' ','')),'/')-1)) AS folder
  FROM conversations
)
SELECT folder, source, COUNT(*) AS sessions
FROM f GROUP BY folder, source ORDER BY folder, sessions DESC;
```

---

## 6. 全文搜索会话内容

```bash
uv run python -m agentmemhub search "登录"                      # 全部来源
uv run python -m agentmemhub search "登录" --source zcode       # 指定来源
uv run python -m agentmemhub search "登录" --role reasoning     # 只搜思维链
```

```python
from agentmemhub.store import Store
for h in Store().search("登录", source="zcode", role="tool", limit=10):
    print(h["conversation_id"], h["role"], h["snippet"])
```

```sql
-- FTS5（英文/词组）；中文可用 LIKE
SELECT conversation_id, role, content FROM events_fts
WHERE events_fts MATCH '"登录"';
```

---

## 7. 会话内的角色分布（用户/思考/工具/补丁 各多少）

```python
from collections import Counter
from agentmemhub.store import Store
store = Store()
c = store.get_conversation("opencode", "<会话id>")
roles = Counter(e.role for e in store.get_events("opencode", c["id"]))
print(dict(roles))   # {'user': 3, 'reasoning': 8, 'tool': 10, 'assistant': 6}
```

```sql
SELECT role, COUNT(*) FROM events
WHERE source='opencode' AND conversation_id='<会话id>'
GROUP BY role ORDER BY 2 DESC;
```

---

## 8. 导出 / 供程序使用

```bash
uv run python -m agentmemhub export --format jsonl    --out exports/
uv run python -m agentmemhub export --format markdown --out exports_md/
uv run python -m agentmemhub memos --out exports/memos_bundle.json
```

```python
from agentmemhub.export import export_jsonl, export_markdown
from agentmemhub.store import Store
store = Store()
export_jsonl(store, "exports/")
```

---

## 常用命令速查

| 想要什么 | 命令 |
|---|---|
| 有哪些 Agent | `adapters` |
| 各 Agent 会话总数 | `stats` |
| 某 Agent 的会话列表 | `list --source x` |
| 某会话聊天全文 | `show <source> <id>` |
| 按文件夹跨 Agent 统计 | `folders [--source] [--limit]` |
| 全文搜索 | `search <词> [--source] [--role]` |
| 导出 JSONL/Markdown | `export --format jsonl\|markdown --out dir` |
| 生成 MemOS bundle | `memos --out x.json` |