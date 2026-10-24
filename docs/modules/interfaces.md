> 覆盖模块：`agentmemhub/mcp_server.py`（1095 行）、`agentmemhub/cli.py`（1861 行）、
> `agentmemhub/console.py`（466 行）、`agentmemhub/health.py`（116 行）·
> 最后核对：2026-10-23 · HTTP 路由清册见 [API.md](../API.md)

# 对外接口（interfaces）

## 1. 职责边界

同一套内部能力有**四个门**：给脚本的 CLI、给人点的控制台、给 Agent 调用的 MCP、
以及给前端页面用的 HTTP（HTTP 那层在 [panel.md](panel.md)）。

**做什么**

| 门 | 给谁 | 特征 |
|---|---|---|
| **CLI**（`python -m agentmemhub <cmd>`） | 运维脚本、CI、我自己 | 19 个子命令，参数全，可组合 |
| **控制台**（`python -m agentmemhub.console`） | 人 | 菜单驱动，只做高频动作 |
| **MCP**（`python -m agentmemhub mcp`） | 别的 Agent（Claude Code / Codex / DSH…） | 8 个工具，长驻，无人工确认 |
| **HTTP** | 本项目的 Web 页面 | 见 panel.md，不在本文档 |

**不做什么**

| 不做 | 说明 |
|---|---|
| 四个门**不各写一套逻辑** | 都调同一批 `store` / `distill` / `wiki` / `feed` 函数；CLI 与面板对同一动作语义一致 |
| MCP **不暴露运维动作** | 只给"写记忆 / 查记忆 / 喂素材"，不给删库、回滚、重编——那些是人的决定 |
| MCP **不做二次确认** | 调用方是 Agent，没人可确认；所以工具集本身就是经过删减的**安全子集** |

## 2. 对外入口

### 2.1 CLI 的 19 个子命令

| 分组 | 子命令 | 说明 |
|---|---|---|
| 采集 | `ingest` / `adapters` / `folders` | 入库；列适配器状态；按文件夹统计 |
| 查看 | `list` / `show` / `search` / `stats` / `export` | 会话清单 / 正文 / 全文检索 / 统计 / 导出 |
| 同步 | `sync` | **增量**：ingest 增量 → 清洗变更会话 → 向量化写入索引（日常就跑它） |
| 记忆 | `clean` / `score` / `rebuild` / `weight` / `distill` / `memos` | 删系统注入 / 批量评分 / 补向量 / 手动加权 / 蒸馏 / MemOS 回退包 |
| Wiki | `wiki` | 对齐审计 / 增量更新 / 失败清单 / 定向补跑（子命令再分） |
| 服务 | `serve` / `mcp` / `snapshot` | Web 页面 / MCP 网关 / 快照与回滚 |

`sync` 是**唯一**日常要跑的；其余是运维与排障。`rebuild` 在 rag 后端下几乎用不到
（写入即嵌入），留着是给老索引修漏。

### 2.2 MCP 的 8 个工具

| 工具 | 参数（全部可选） | 写入什么 |
|---|---|---|
| `memory_save` | `content`（必填）/`importance`/`type`/`tags`/`note` | 记忆引擎（**记住时调用**） |
| `memory_search` | `query`（必填）/`topK`/`origin`/`note` | 只读 |
| `memory_recent` | `limit`/`note` | 只读 |
| `memory_stats` | `note` | 只读（探活） |
| `memory_score` | `trace_id`（必填）/`polarity`/`note` | 只改该条价值分 |
| `feed_add` | `value`（必填）/`input`/`filename`/`encoding`/`kind`/`force`/`dry_run`/`note` | **素材库**（知识库页层） |
| `feed_list` | `limit`/`status`/`low_quality`/`note` | 只读 |
| `feed_status` | `doc_id`/`note` | 只读 |

**关键区分**：前 5 个动**记忆引擎**（会进蒸馏与 wiki 编译）；后 3 个只动**素材库**
（`origin=external`，不进蒸馏、不进 wiki 编译）。混用会导致"我喂的资料怎么被改写了"。

**传输**：默认 stdio（客户端拉起即用）；`--http` 转 Streamable HTTP 常驻，
面板/多客户端共用。两种传输**工具集完全相同**。

### 2.3 控制台与体检

- `console.run_console()`：菜单式入口，`env_snapshot()` 先打印环境；
  `action_ingest / search / dashboard / dashboard_stop / clean / score / distill / memos / status`
  都是对 CLI 同一函数的薄封装——**不是另一套实现**。
- `health.py` 三段：`collect(conn)` 采集指标 → `diagnose(m)` 出人话诊断 →
  `reclaim(conn)` 回收（VACUUM / 清理孤儿）。CLI `stats` 与面板健康卡都建立在 `collect()` 上。

## 3. 数据流与不变量

### 3.1 MCP 一次写入走哪条路

```
memory_save(content)
  → distill.save_direct_memory()   # source=mcp, conversation_id=direct-YYYY-MM-DD
  → content_hash 查重（同 hash 不重复写）
  → units 投影 + 向量（rag 后端写入即嵌入）
  → 标记该会话为脏 → 下次 wiki 增量才重编 L1/L2
```

**分桶是关键**：`mcp` 直写记忆按**天**分会话（`direct-2026-10-22`），
不再全部堆在 `direct` 一个会话里。否则 200 多条记忆共用一个会话，
L1 重编就是整会话重做（详见 [distill-llm.md](distill-llm.md) §5）。

### 3.2 四条硬不变量

1. **长任务不阻塞调用方**。CLI 与 HTTP 的长动作（ingest / distill / wiki 重编 / 快照回滚）
   都走 `tasks.submit`，返回 job 句柄；MCP 工具都是**秒级**短动作，不做长任务。
2. **同一动作只有一个实现**。面板按钮、CLI 子命令、控制台菜单指向同一个函数；
   改语义只改一处。
3. **MCP 是长驻进程**。改了 `agentmemhub/**` 的代码，**必须重启 MCP 客户端**
   才生效（代码在启动时载入）——这是"改了没反应"的第一排查项。
4. **写工具幂等**。`memory_save` 同内容重复调用不产生两条（`content_hash` 查重）；
   `feed_add` 同内容重复投喂返回"已投喂过"。

### 3.3 工具集为什么是这个边界

| 给了 | 没给 | 理由 |
|---|---|---|
| `memory_save` / `feed_add` | 删除、回滚、重编、改配置 | 写入是**增量且可逆**（能查到、能撤下）；删除与重编会**破坏性改变历史**，必须人来按 |
| `memory_score` 只能改一条的分 | 批量评分 | 批量评分会一次改写几百条排序，影响面不可控，留 CLI |
| `feed_list` / `feed_status` | `feed` 的 purge | 同上：purge 是真删文件 |

## 4. 配置项

| 键 | 作用 |
|---|---|
| `mcp.*` | MCP 网关相关（传输、暴露工具集等） |
| `rag.*` | 记忆后端（`memory_*` 工具写入的落地方式） |
| `distillation.direct_bucket` | `mcp` 直写记忆的会话分桶粒度（`daily` / `none`） |

## 5. 调试与排障

| 现象 | 排查路径 |
|---|---|
| **改了代码 MCP 没反应** | MCP 长驻，**重启客户端**（最常见，不是 bug） |
| MCP 工具列表变了但客户端没变 | 客户端缓存了 tool list，重启即刷新 |
| `memory_save` 返回成功但搜不到 | rag 写入即嵌入，正常立刻可搜；搜不到先看 `memory_stats` 是否在线，再看 `origin` 过滤是否把 `native` 滤掉了 |
| `memory_stats` 报离线 | 引擎内置、进程内直调，**没有"启动引擎"这个动作**；报离线通常是索引库不可写或依赖缺失 → 看错误原文 |
| `feed_add` 报格式不支持 | 返回里带当前白名单；面板设置页可改白名单（热生效） |
| CLI 与面板结果不一致 | 两者共用同一函数与同一库，不一致通常是一边在跑长任务、读到了中间态（面板有"更新前快照"提示） |
| `sync` 跑完会话数没变 | `sync` 是增量；要全量用 `ingest --source X` 或 `--only`，并注意 `list_sessions` 给的 `updated_at` |

## 6. 测试在哪

| 文件 | 守住什么 |
|---|---|
| `tests/test_mcp_server.py`（449 行） | 8 工具契约（**superset 断言**：工具数只许多不许少）、参数校验、返回结构 |
| `tests/test_mcp_http.py` | HTTP 传输下同一工具集、同一返回契约 |
| `tests/test_mcp_extra.py` | 边界参数（空 query、超大 topK、非法 origin） |
| `tests/test_cli_*.py` / `tests/test_console*.py` | CLI 子命令分发、控制台动作装配 |
| `tests/test_health.py` | `collect/diagnose/reclaim` 三段不抛、指标字段齐 |
| `tests/test_feed_mcp.py` | feed 三工具：不触碰蒸馏与 wiki（**守边界**） |

**改 MCP 工具时的自检顺序**：改实现 → 跑 `test_mcp_server.py`（契约）→
跑 `test_mcp_http.py`（传输）→ **重启客户端**实测一次 → 再动文档。
