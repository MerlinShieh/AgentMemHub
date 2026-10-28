> 覆盖模块：`agentmemhub/adapters/**`、`agentmemhub/store.py`、`models.py`、`watermarks.py` ·
> 最后核对：2026-10-23 · 表结构见 [data-architecture.md](../data-architecture.md)

# 采集与适配（ingest-adapters）

## 1. 职责边界

这一步把"各个 Agent 自己格式的数据"变成"一个统一的会话 + 事件模型"。
它只负责**读**，不负责理解内容（理解是蒸馏的事）。

**做什么**

| 职责 | 落点 |
|---|---|
| 一类 Agent 一个适配器：定位数据 → 读 → 归一化成统一 session | `agentmemhub/adapters/*.py`（10 个文件） |
| 统一事件模型 + 角色归一化 + 系统注入识别 | `agentmemhub/models.py`（`Event` / `to_event` / `normalize_role` / `is_system_inject`） |
| 落库：会话、事件、标题保护、删除墓碑、排除规则 | `agentmemhub/store.py`（755 行，`Store` 19 个公开方法） |
| 增量水位线：记到哪了、待评分队列、跨阶段消费 | `agentmemhub/watermarks.py`（171 行） |

**不做什么**

| 不做 | 当前语义 |
|---|---|
| **不写源数据** | 适配器只读 Agent 自己的库/日志。所有写入都落在本项目自己的库里 |
| 不做增量判断的最终裁决 | `only_ids` 只是**提示**：能廉价预过滤的适配器遵守它，不能的返回超集，由上游按 `updated_at` 幂等兜底（见 §3.3） |
| 不解析语义 | "这条消息重要吗"不在这里判断；系统注入过滤是**模式匹配**，不是理解 |
| 不保证每次都能定位到数据 | `locate()` 可能返回 `None`（用户没装那个 Agent），这是正常状态，不是错误 |

## 2. 对外入口

| 入口 | 说明 |
|---|---|
| `python -m agentmemhub adapters` | 列出所有适配器与定位状态（`discover` 的 CLI 面） |
| `python -m agentmemhub ingest [--source X] [--only id,...]` | 全量 / 增量采集入库 |
| `python -m agentmemhub sync` | 采集 + 清洗 + 向量化的编排入口 |
| `POST /api/admin/ingest` | 面板的采集长任务（走 `tasks`） |
| Python | `Store.replace_source()` / `upsert_sessions()`；`ingest.run(...)` |

## 3. 数据流与不变量

### 3.1 一个适配器要实现什么

`AgentAdapter`（`adapters/base.py`）只有两个**抽象**方法，其余是可选覆写：

| 方法 | 必须 | 语义 |
|---|---|---|
| `candidate_paths() -> list[Path]` | ✅ | 可能的数据路径，**有序**，第一个存在的优先 |
| `load(path, only_ids) -> list[dict]` | ✅ | 读出统一结构；`only_ids` 是可选的预过滤提示 |
| `locate() -> Path \| None` | 已实现 | 先看统一配置 `agents.<source>` 的显式覆盖，不存在才回退官方默认探测 |
| `list_sessions(path)` | 可选 | 轻量清单 `[{"id","updated_at"}]`，**不读事件正文**；返回 `None` 表示没有廉价清单能力 |
| `source_freshness(path)` | 可选 | 整源新鲜度（Unix 秒），兜住"会话清单看不见的变更" |
| `describe()` | 已实现 | 供 CLI / UI 展示的状态摘要 |

**统一结构**（`load` 的返回值，每个会话一条）：

```python
{"source","id","title","cwd","created_at","updated_at","model","meta",
 "events": [Event, ...]}
```

**已有的 8 个 Agent**：DSH（`dsh.py`，zstd 压缩流）、Hermes、OpenCode、Qoder CN、
Qwen、Trae、WorkBuddy（审计日志，只改 mtime）、ZCode。
`sqlite_conversation.py`（208 行）是 SQLite 型 Agent 的共用基类，OpenCode 与 ZCode 继承它。

### 3.2 统一事件模型

`models.Event` 是唯一的中间表示：`role`（user / assistant / system / tool）、`text`、
`thinking`、`tools`（调用与结果）、`patch`（代码补丁）、`ts`、`turn`（第几轮）。

- `normalize_role()` 把各家五花八门的角色名收敛到四类
- `message_text()` 处理 content 的多种形态（字符串 / 分块数组 / 嵌套对象）
- `is_system_inject()` 用**模式匹配**识别系统注入（工具说明、记忆注入段落），
  它们会在"清洗"步骤被删掉——删的是**系统注入**，不是用户内容
- `find_in_obj()` 在深层嵌套里找字段（不同 Agent 的 schema 差异都靠它抹平）

### 3.3 增量：三层信号，从便宜到贵

```
① 会话清单（list_sessions）   → 按 updated_at 对比，只重读变化的会话
② 整源新鲜度（source_freshness）→ 兜住"清单看不见的变更"（如只追加日志、改 mtime）
③ 水位线（watermarks）        → 记录这次同步到哪个时间点，下次从这里开始
```

**为什么三层都要**：只靠会话清单会漏掉 WorkBuddy 那种"审计日志追加只改文件 mtime、
`sessions` 表不动"的变更；只靠整源 mtime 又会把"库里所有会话都重读"变成常态。
三层叠加 = 便宜的先筛，筛不掉的兜底。

**`only_ids` 的契约**：它只是**提示**。能廉价预过滤的实现把过滤下推到读事件之前；
做不到的返回超集也合法——上游按 `updated_at` 与内容哈希做**幂等**，重复读不会产生重复行。

### 3.4 三条硬不变量

1. **源只读**。适配器不写 Agent 的库，也不改它的文件。
2. **标题修改与删除不被 ingest 覆盖**。靠全局递增的 `session_uid` + 用户修改保护 +
   删除墓碑（`store.delete_conversation` 打墓碑）：下次采集把同一会话读回来，
   也不会把标题改回去、不会让删掉的会话复活。
3. **`replace_source` 是幂等的**。同一批数据跑两次，结果一致；事件有稳定主键
   （`source + conversation_id + idx`），不是靠自增 id 对齐。

### 3.5 水位线的语义

`watermarks.py` 是**状态机**，不是一个数字：

| 函数 | 作用 |
|---|---|
| `load_state` / `save_state` | 读写 `logs/watermarks.json`（或 `data_dir` 下同名文件） |
| `record_ingest(st, ts=...)` | 记录本次采集时间 |
| `pending_for(st, stage)` | 取"上游已完成、下游还没消费"的一批任务 |
| `mark_consumed(st, stage, ts=...)` | 标记下游已消费 |
| `add_pending_score` / `take_pending_score` | 待评分队列的入队与出队 |

**跨阶段消费**是这套设计的核心：采集完成后不是直接调评分，而是**记一笔 pending**，
评分阶段自己去取——这样任一阶段失败都不会丢任务，重跑也不会重复处理。

## 4. 配置项

| 键 | 作用 |
|---|---|
| `agents.<source>` | 该 Agent 数据的**显式路径覆盖**；不配就回退官方默认探测（`locate()` 先看这里） |
| `data_dir` | 水位线状态文件、数据库所在目录 |

支持的 Agent 与各自默认路径见 README「支持的 Agent」一节。改完配置要重启进程
（`config()` 单例）。

## 5. 调试与排障

| 现象 | 排查路径 |
|---|---|
| `adapters` 里某源显示 `located: false` | 该 Agent 没装、或数据在非默认路径 → 配 `agents.<source>` 显式路径 |
| 会话数没涨但源里明明有新会话 | 看是不是走了增量且 `list_sessions` 返回了旧 `updated_at`；临时用 `--only` 全量重扫对照 |
| 采集了但内容为空 | 该源的 `message_text` 没覆盖它的 content 形态；用 `show` 看事件原文 |
| 删掉的会话又回来了 | 看墓碑是否被清（`replace_source` 的 force 路径）；墓碑是删除的保护，不该被覆盖 |
| 改过的标题被打回 | 同上，`update_title` 有用户修改标记，ingest 不该覆盖 |
| 同步卡在某一阶段 | 看水位线状态文件里的 `pending` 段：pending 堆积 = 下游没消费，不是上游没产 |

## 6. 测试在哪

| 文件 | 守住什么 |
|---|---|
| `tests/test_incremental.py`（503 行） | 增量三层信号：清单对比、新鲜度兜底、水位线推进后的增删一致 |
| `tests/test_store_lifecycle.py`（128 行） | 库生命周期：替换源、标题保护、删除墓碑 |
| `tests/test_trae.py`（136 行） | 单个适配器的端到端读取（Trae 是 JSONL 型代表） |
| `tests/test_clean.py`（266 行） | 清洗：系统注入识别与删除，删后不影响用户内容 |
| `tests/test_sanitize.py`（155 行） | 文本消毒（控制字符、超长截断） |
| `tests/test_memory_exclusion.py` / `test_exclusion_api.py` | 排除规则：整会话 / 部分轮次的"不写入记忆" |

**新增一个 Agent 适配器的最小流程**：

1. 在 `adapters/` 下建 `xxx.py`，实现 `candidate_paths()` + `load()`（SQLite 型就直接继承
   `SqliteConversationAdapter`，只需覆写会话查询的 SQL）
2. 在注册表里登记（`adapters/__init__.py` 的工厂）
3. 加一条 `test_<name>.py`：真实样本 → `load()` → 断言会话字段与事件角色
4. 跑 `.venv\Scripts\python.exe -m agentmemhub adapters` 确认 `located: true`
