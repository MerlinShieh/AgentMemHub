# AgentMemHub 接口文档

> 适用版本：**v2.1**（2026-09-18）
> 交互式文档：服务运行时访问 **`/api/docs`**（Swagger UI）与 **`/openapi.json`**（OpenAPI 3 schema）。
> 本文是**人工契约文档** —— 补充 Swagger 里看不到的：分组语义、长任务约定、错误语义、
> 以及"哪些字段是 Skill / 面板强依赖的"。

## 一、三层对外接口

| 层 | 面向 | 入口 |
|---|---|---|
| **HTTP API** | 面板前端、外部程序 | `http://127.0.0.1:8086/api/*`（端口见 `web.port`）|
| **MCP 工具** | Agent（ZCode / OpenCode / DSH 等）| stdio 或 Streamable HTTP，见 `docs/mcp-register.example.json` |
| **CLI** | 人 / 运维脚本 | `python -m agentmemhub <子命令>`，见 README「命令行」 |

## 二、通用约定

- **基址**：`http://127.0.0.1:8086`，**只绑定回环地址**，不上传任何数据
- **编码**：UTF-8；请求与响应均为 JSON
- **时间**：Unix 秒（整数）
- **命名**：查询参数用 `camelCase`（`dateFrom`/`page_size` 混用是历史遗留，见各接口）
- **错误**：
  - `422` 参数校验失败（FastAPI 标准格式）
  - `400` 业务参数不合法（如第二级补跑缺 `src`）
  - `409` **已有长任务在运行**（单并发）
  - `503` 依赖不可用（记忆索引 / embedding 模型缺失）
  - 错误体统一为 `{"detail": "..."}`

### 长任务约定（重要）

`/api/admin/*` 与 `/api/wiki/retry` 走**后台任务**，模式统一：

```
POST /api/admin/xxx  →  立即返回 {"job": {"id", "name", "status", ...}}   （不阻塞）
                        若已有任务在跑 → 409
GET  /api/admin/job  →  轮询进度
```

- **单并发**：同时只允许一个长任务（避免争抢索引库）
- **完整输出**逐行落 `<程序根>/logs/tasks/<job_id>.log`，页面关掉也能追溯
- 操作记录写 `logs/web.log`

---

## 三、HTTP API

### 3.1 会话查询

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/stats` | 总览统计（会话/事件/来源计数）|
| `GET` | `/api/bootstrap` | 面板初始化：来源清单、文件夹、统计等一次拉齐 |
| `GET` | `/api/facets` | 筛选项取值（来源/角色/时间范围）|
| `GET` | `/api/folders` | 按文件夹统计各 Agent 会话数（`source` 可选）|
| `GET` | `/api/conversations` | 会话列表（分页）|
| `GET` | `/api/conversations/{source}/{cid}/events` | 某会话的事件流（`offset`/`limit`）|
| `DELETE` | `/api/conversations/{source}/{cid}` | 删除会话（**级联**：会话+事件+FTS+索引投影，并写删除墓碑）|
| `PATCH` | `/api/conversations/{source}/{cid}/title` | 改标题（置 `title_custom`，ingest 重写时保留）|

**`GET /api/conversations` 参数**

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `sources` | string | — | 逗号分隔多选 |
| `workspace` | string | — | 逗号分隔多选（文件夹名）|
| `q` | string | — | 关键字 |
| `days` | int | 0 | 近 N 天（0=不限，按 `createdAt`）|
| `dateFrom` / `dateTo` | float | — | 精确时间戳（Unix 秒）|
| `all` | bool | false | 返回全部匹配项（不分页）|
| `page` / `page_size` | int | 1 / 20 | 分页 |
| `sort` / `order` | string | updated / desc | 排序 |

### 3.2 记忆排除（不写入记忆）

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/exclusions/summary` | 排除概览（整会话 / 部分轮次计数）|
| `GET` | `/api/conversations/{source}/{cid}/memory-exclusion` | 查某会话的排除态 |
| `POST` | `/api/conversations/{source}/{cid}/memory-exclusion` | 加排除（`turn_key` 留空 = 整会话；**整会话排除会清掉该会话的轮次级排解**）|
| `DELETE` | `/api/conversations/{source}/{cid}/memory-exclusion` | 取消排除（`turn_key` 可选）|

> 排除是**意图真相源**，独立表、不随整源重建被清空；ingest / distill 都会跳过被排除项。

### 3.3 记忆查询与反馈

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/memories` | 记忆报表（筛选 + 分页 + 溯源）|
| `GET` | `/api/memos/search` | 语义检索（混合召回）|
| `POST` | `/api/memos/feedback` | 👍/👎 反馈（状态式，可撤销）|
| `POST` | `/api/memos/weight` | ⭐ 手动加权（锁定不衰减）|
| `GET` | `/api/memos/traces` | 近期记忆轨迹 |
| `GET` | `/api/memos/status` | 记忆引擎状态 |

**`GET /api/memories` 参数**

| 参数 | 默认 | 说明 |
|---|---|---|
| `source` | — | Agent 来源（逗号多选）|
| `type` | — | `decision,fact,preference,lesson,manual` |
| `status` | `active` | `active`(=new+similar) / `all` / 单值 / 逗号多值 |
| `conf` | — | `high,medium,low` |
| `q` | — | 关键字（内容/主题 LIKE）|
| `conversationId` | — | 按会话 id（会话→记忆联动）|
| `sessionUid` | — | 按全局会话 uid |
| `sort` | `time` | `time\|value\|type\|confidence\|status\|source\|conversation` |
| `page` / `page_size` | 1 / 20 | 分页 |

**`POST /api/memos/feedback` 参数**

| 参数 | 默认 | 说明 |
|---|---|---|
| `traceId` | 必填 | 记忆 id（`mcp_*` / `dst_*` / `msg:*` 等）|
| `polarity` | `neutral` | `positive` / `negative` / `neutral` |
| `magnitude` | 1.0 | 强度 |
| `channel` | `explicit` | 反馈渠道 |
| `state` | true | **状态式**：一个 unit 一条当前表态，反复点不叠加 |
| `revoke` | false | `state=true` 时取消该条反馈（干净回退初始分）|

### 3.4 健康与日志

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/health` | 记忆库一致性巡检（与 CLI `health_check` 同判据）|
| `POST` | `/api/health/reclaim` | 回收孤儿投影 |
| `GET` | `/api/logs` | 近期操作日志（`limit` 默认 100，上限 500）|

### 3.5 长任务（`/api/admin/*`）

| 方法 | 路径 | 参数 | 说明 |
|---|---|---|---|
| `POST` | `/api/admin/ingest` | `source`, `signature` | 采集入库（默认会话级增量）|
| `POST` | `/api/admin/push` | `source` | 向量化写入记忆索引 |
| `POST` | `/api/admin/distill` | `source`, `limit`, `dryRun` | 记忆蒸馏（幂等，失败可重跑）|
| `POST` | `/api/admin/score` | `limit`, `dryRun` | LLM 批量评分（**当前不生效**，入口已隐藏）|
| `POST` | `/api/admin/clean` | `source` | 删除系统注入事件 |
| `POST` | `/api/admin/exclude` | JSON body | 批量排除会话 |
| `POST` | `/api/admin/rebuild` | `mode`（`repair`/`rebuild`）| 补/重建向量 |
| `GET` | `/api/admin/job` | — | 查当前任务状态 |

### 3.6 LLM Wiki（v2.1 新增）

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/wiki/failures` | 失败清单摘要（`out` 必填；`stage` 可选 `l1`/`l2`）|
| `POST` | `/api/wiki/retry` | **定向补跑**失败项（`out`、`stage`、`src`、`workers`）|
| `GET` | `/api/wiki/align` | 对齐审计（只读）：**脏多少 + 覆盖缺口**（`out` 必填，`stage`/`db` 可省）|
| `POST` | `/api/wiki/update` | **增量更新**（长任务）：脏会话重编 L1 + 受影响域重编 L2 |
| `GET` | `/api/wiki/triggers` | 触发器配置与状态（规则、今日已触发档位、上次更新时间、产出目录）|
| `PUT` | `/api/wiki/triggers` | 编辑触发规则（运行时 overrides，**不改 yaml**）|
| `POST` | `/api/wiki/trigger/run` | 手动执行一次检测（`force=` 绕过规则强制更新）|
| `GET` | `/api/wiki/pending` | **待更新记忆明细**（`out` 可省=用配置；`limit` 0=全部）—— 带来源/类型/时间/正文摘要 |
| `POST` | `/api/wiki/pending/drop` | **删除待更新记忆**：body `{ids, mode, confirm, out}`；`soft`=不进 wiki，`hard`=真删（需 `confirm`）|
| `GET` | `/api/wiki/pending/ignored` | 已软删除（不进 wiki）的记忆 —— 供取消忽略 |
| `POST` | `/api/wiki/pending/restore` | **取消软删除**：body `{ids, out}` |
| `GET` | `/api/wiki/pages` | **知识库页面清单**（面板「wiki知识库」页）：`origin`（native/external）、`domain`、`q`、`page`/`page_size` —— **以磁盘为准**，不查投影表 |
| `GET` | `/api/wiki/page` | **读一页正文**：`path` 必须在页面根内（越界 400）；返回 frontmatter + markdown + **渲染后的 HTML** |

**知识库浏览（`/api/wiki/pages`、`/api/wiki/page`）**

面板第三个标签页「wiki知识库」的数据源（2026-10-18 新增）。两个决定：

- **以磁盘为准，不查投影表**：投影（`wiki --action index-pages`）是运维动作、会滞后 ——
  实测磁盘 424 页 / 投影 399 行。面板要看的是**知识库本身**，不该随"跑没跑过投影"起落；
  "能不能被召回"是另一件事（由投影决定）。
- **读页面只允许在 `page_roots()` 之内**：`path` 会 `resolve()` 后比对根，越界直接 400 ——
  面板是本地服务，但 `?path=C:\Windows\win.ini` 这种口子不该存在。

```jsonc
// GET /api/wiki/pages?origin=native&page_size=2
{
  "total": 424, "page": 1, "pageSize": 120, "pageCount": 4,
  "originCounts": {"native": 418, "external": 6},
  "domains": [{"name": "01-ADBController-与-Android-自动化",
               "origin": "native", "count": 17}],
  "items": [{"title": "…", "domain": "01-…", "rel": "01-…/xxx.md",
             "type": "analysis", "compiled_at": "2026-10-04 17:14:11",
             "sources": "5", "path": "D:\\…\\out_l2_mimo\\01-…\\xxx.md",
             "origin": "native"}],
  "roots": [{"path": "D:\\…\\out_l2_mimo", "origin": "native", "exists": true}]
}
```

`page` 返回 `{path, rel, origin, domain, title, frontmatter, markdown, html, chars}`；
`html` 由 `agentmemhub/mdmini.py` 渲染（**先转义再套结构**，内容里的 `<script>`
只会显示成文本；链接只放行 http/https）。

**待更新记忆的查看与删除（`/api/wiki/pending*`）**

与 `align` 的分工：`align` 回答"**差多少**"（只有 id 列表、且会截断），这一组回答
"**具体是哪几条、内容是什么**"，让人能判断该不该删。

```jsonc
// GET /api/wiki/pending
{
  "count": 3, "added_total": 3, "changed_total": 0,
  "dirty_sessions": {"mcp/direct": {"added": 3, "changed": 0, "removed": 0}},
  "items": [{"id": 4388, "source": "mcp", "conversation_id": "direct",
             "type": "fact", "status": "new", "confidence": "medium",
             "chars": 492, "created_at": 1758000421, "summary": "…"}]
}
```

**两种删除语义**（这是核心区别，别混）：

| 模式 | 效果 | 可恢复 |
|---|---|---|
| **`soft`** | 写 `wiki_ignore_at` 标记：记忆**仍然存在、仍然能被召回**，只是**不再参与 wiki 编译** | ✅ `restore` 可取消 |
| **`hard`** | 真删：蒸馏表行 + `units` 投影 + 向量（FTS 由触发器同步） | ❌ 不可恢复，需 `confirm: true` |

**⚠️ 只允许删"待更新"的记忆**（尚未进 wiki 的那些）：

- 它们**没有被任何页面引用**，所以删除**不产生死链** —— 这是最简单的场景
- **已进 wiki** 的记忆会被直接**拒绝**（返回 `rejected`）：硬删会让页面正文里的
  `[m<id>]` 变成死引用，那属于 [`memory-deletion.md`](memory-deletion.md) 的范畴
  （需要墓碑表 + 引用重映射/占位），本接口刻意不承担
- **软删除一条待更新的记忆不触发任何重编**：它本来就不在 manifest 基线里，
  编译输入去掉它，diff 依然是零
- **硬删除后会自动重建 manifest**：否则 `align` 会把这批 id 报成"失去输入"，
  明明页面里引用不到，却触发一次毫无意义的重编

**`GET /api/wiki/failures` 响应要点**

```json
{
  "out": "...", "log": ".../failures.jsonl", "exists": true,
  "total": 3,
  "by_kind": {"transient": 2, "format": 1},
  "fatal": [],                    // quota/auth/model —— 重试无意义
  "needs_manual": false,          // true 时调用方应提示用户先处理，而非继续补跑
  "stages": {"l1": 3, "l2-plan": 0, "l2-compile": 0},
  "items": [{"stage": "l1", "target": "qwen/xxx", "kind": "transient",
             "attempts": 1, "error": "...", "ts": 1789000000}]
}
```

**`GET /api/wiki/align` 响应要点**

```json
{
  "needs_recompile": true,        // 只看 manifest diff（"wiki 是不是脏的"）
  "stages": {"l1": {"added_total": 101, "changed_total": 0, "removed_total": 0,
                    "dirty_sessions": {"mcp/direct": {"added": 101, ...}},
                    "dirty_session_total": 1}},
  "invalid_refs": {"missing_total": 0, "not_input_total": 0},   // 死链
  "coverage": {"current_inputs": 1781, "covered": 1720,
               "uncovered": 61,   // ← 没被**任何**页面引用（diff 视角看不见）
               "uncovered_ids": [428, 527, ...], "ratio": 0.9657},
  "message": "l1: 新增 101 / 变更 0 / 失去输入 0（脏会话 1 个）；失效引用 0 个；⚠️ 未覆盖 61 条输入"
}
```

`needs_recompile` 与 `coverage.uncovered` 是**两个视角**：前者回答"要不要重编"，
后者暴露"页面里根本没有的输入"。`uncovered` **只告警、不触发重编** —— 否则会陷入
"每次都触发、却因脏会话为空而什么都不做"（详见 `data-architecture.md` §7.4）。

---

## 四、MCP 工具（Agent 侧，8 个）

**分两组**：五个**记忆工具**（依赖记忆引擎）+ 三个 **feed 工具**（外部投喂，
**不依赖引擎**）。feed 是后加的**独立**工具 —— 原有五个的名称、参数与返回格式
一字未改（Skill 侧强依赖）。

| 工具 | 参数 | 说明 |
|---|---|---|
| `memory_search` | `query`（必填）、`topK`、`origin`、`note` | 语义检索历史记忆（六路通道融合，见 `data-architecture.md` §4） |
| `memory_save` | `content`（必填）、`importance`、`tags`、`note` | 写入一条记忆 |
| `memory_recent` | `limit`、`note` | 最近记忆时间线 |
| `memory_stats` | `note` | 引擎在线状态与记忆总量 |
| `memory_score` | `trace_id`、`polarity`、`note` | 给记忆打极性分（**仅用户明确说重要时才用**）|
| `feed_add` | `input`、`value`（必填）、`filename`、`encoding`、`kind`、`force`、`dry_run`、`note` | 投喂外部素材（`path` / `url` / `data` / `text` 四形态）：转译成统一 md，发布到与自有 wiki **并列**的知识库页面层 |
| `feed_list` | `status`、`low_quality`、`limit`、`note` | 列已投喂素材 —— 回答「我投过什么、哪些要复核」 |
| `feed_status` | `doc_id`、`note` | 素材库健康快照（状态计数 / 格式白名单 / 产物体检）；给 `doc_id` 看**单条**详情 |

**契约要点（Skill 侧强依赖，不得破坏）**：

- 五个**记忆工具**的**名称与参数名**是 Skill 侧硬依赖；feed 三个是**新增**，不许改前者
- `note` 是**可选**参数，用于记录调用意图（为什么），写进 `logs/mcp.log`
- **调用审计由服务端在唯一分发点自动写**，不依赖 Agent 手动记（feed 工具同样被覆盖）
- `importance` 档位：`high` 0.8 / `normal` 0.6 / `low` 0.4（不传即 `normal`）
- **写后不立即评分** —— 价值分由真实使用演化

**feed 三个工具的三条约定**：

- **不依赖记忆引擎**：索引没建好、引擎没起来，投喂照样能用（feed 是本地文件处理，
  与索引无关）；反之记忆工具此时会返回"引擎未运行"的指引；
- **只给"写入 + 查看"，不给删除**：`retract` / `forget` 仍在 CLI —— 后者的 `--purge`
  会删**不可再生**的原件。低门槛写入、高门槛删除；
- **过程日志进返回文本，不写 stdout**：MCP 的 stdout 是 JSON-RPC 协议流，
  而 `feed/service.py` 的 `log` 默认值正是 `print`（CLI 场景）—— 直连会把
  `[+] 原件入 raw：…` 混进协议里，客户端解析直接失败。

**`memory_search` 的返回形态**：

- 每条带**层级**与**来源**标注：`[知识页·自有]` / `[记忆·自有]` / `[对话·投喂]`…
  —— `kind` 区分 page / memory / message（聚合答案 / 具体结论 / 原始细节），
  `origin` 区分 native（自有沉淀）/ external（外部投喂）
- 知识页额外给**摘要**与 `wikiPath`（两阶段召回：需要细节时按路径读整页）
- `origin` 参数可只召回某一来源（`native` / `external`），留空 = 全部
- `topK` 只控制**列出多少条**；引擎可能返回更多（curate 只做相关度截断、不硬砍
  条数），此时头部会注明"引擎共返回 N 条，按相关度取前 M 条"

**召回严格度**由配置 `rag.retrieval.recall_level`（1 最严格 … 5 最宽松，
**默认 3**）统一控制，见 `data-architecture.md` §4.3。

**注册配置**见 `docs/mcp-register.example.json`。

---

## 五、CLI

见 README「命令行」章节的完整表格。与接口相关的几条运维入口：

```bash
python -m agentmemhub wiki --action failures --out DIR          # 查 wiki 失败清单
python -m agentmemhub wiki --action retry --out DIR --stage l2   # 定向补跑
python scripts/health_check.py                                   # 记忆库一致性巡检
python -m agentmemhub memory-log                                # 记忆操作事实流（写了/读了什么）
  [--event write|read] [--path mcp|http|distill|cli] [--grep 关键词] [--limit N] [--json]
python -m agentmemhub llm-config [--json]                       # 各 LLM 使用点**最终生效**的配置
                                                                # （标出每项是继承还是覆盖）
```

---

## 六、接口使用现状（面板未调用的接口）

**2026-10-23 实测**：后端 **62 个 HTTP 路由**，面板前端调用了 **51 个**，
**11 个未接**。复跑一条命令即可对账：

```powershell
python scripts/audit_frontend_api.py        # 或 --used 连已用的一起列 / --json 给机器读
```

| 域 | 状态 | 说明 |
|---|---|---|
| **Wiki** | ✅ **全部接上** | 浏览（页面清单/正文）+ 运维（对齐审计 / 待更新记忆 / 失败补跑 / 触发器热改 / 离线批次 / 增量更新），2026-10-18~19 |
| **外部投喂** | ✅ **全部接上** | 投喂视图：清单 / 详情 / 四形态投喂 / 上传 / 扫描批量 / 重转 / 撤下 / 删除 / 体检 / 格式白名单，2026-10-22 |
| **快照** | ✅ **全部接上** | 运维页「快照与回滚」卡：列表 / 立即快照 / 分范围回滚（执行前自动存保护快照），2026-10-23 |
| 会话 + 记忆排除 | 部分未接 | 记忆排除 UI 已下线（整会话 / 部分轮次 + `exclusions/summary`）；面板改标题等仍在用 |
| 运维 | 部分未接 | `admin/score`（入口已隐藏）/ `admin/exclude` / `admin/rebuild` |
| 记忆 | 部分未接 | `GET /api/memos/traces`（时间线明细） |
| 引导与统计 | 部分未接 | 面板走 `/api/bootstrap`（**已含统计**），`stats`/`facets`/`folders` 供外部调用 |

> 逐域的路由数**不写死**（写死就一定会漂 —— 这份表 2026-10-16 那版的结构数字
> 到 10-22 已经全错）。要精确分组时跑审计脚本的 `--json`。

未接的 11 个**不是缺陷**，而是设计选择（或"等前端统一重做"）—— 但必须写清楚，
否则下次排查又会当成"未完成"。

> ⚠️ **静态扫描一定有漏网**：URL 写在变量里（`` const u = `/api/…`; fetch(u) ``）、
> 前端用模板串拼路径（`` `/api/conversations/${encodeURIComponent(x)}/…` ``）都可能匹配不到。
> 2026-10-16 实测：脚本第一版没先归一 `${...}`，把会话详情 / 删除 / 改标题三个
> **其实在用**的接口报成了未调用 —— 所以**关键接口必须再用关键字 grep 交叉验证**。

### 6.1 保留**供外部调用**（面板走 `/api/bootstrap`）

| 接口 | 现状与保留理由 |
|---|---|
| `GET /api/stats` | 面板用 `/api/bootstrap`（其中已含统计）。外部程序只需要统计时，不必拉整个引导包 |
| `GET /api/facets` | 同上：面板筛选项来自 bootstrap；外部可单独取 |
| `GET /api/folders` | 同上 |

### 6.2 保留但**对应 UI 已下线**

| 接口 | 现状 |
|---|---|
| `POST /api/admin/score` | 入口已隐藏（实测评为 neutral 居多，质量把关改由蒸馏置信度 + 面板 👍/👎 承担） |
| `POST /api/admin/exclude`、`GET /api/exclusions/summary`、`GET/POST/DELETE /api/conversations/{source}/{cid}/memory-exclusion` | **记忆排除 UI 已下线**（2026-09 起），后端能力完整保留 |

**为什么后端能力保留**：排除机制是**摄取与蒸馏的硬依赖**（`memory_exclusions`
表被 `ingest` 与 `distill` 读取，用于跳过不写入记忆的会话/轮次）。
**UI 下线 ≠ 能力下线**，且它随时可能重新上架。

### 6.3 曾经**整块未接的三个域**（2026-10-22/23 已全部接上）

这三块曾经**一个接口都没被前端调用**（后端 + CLI + 部分 MCP 都完整）。
留在这里是为了说明"后端先做、前端后补"的那段历史 —— **别再照着它判断现状**。

| 域 | 接口 | 能做什么 | 现状 |
|---|---|---|---|
| **外部投喂** `/api/feed/*` | 12 个（见 §3.7） | 投喂文件 / 链接 / 正文、批量扫描与投喂、格式白名单热改、素材列表与详情、重转 / 撤下 / 删除、产物体检 | ✅ 投喂子视图（2026-10-22） |
| **LLM Wiki** `/api/wiki/*` | 16 个 | 失败清单与定向补跑、待更新记忆的查看 / 丢弃 / 恢复、触发器状态**热改**、对齐审计、增量更新、离线批次 | ✅ 运维子视图（2026-10-18~19） |
| **快照** `/api/snapshots*` | 3 个 | 列出 / 创建 / 回滚（索引库 + wiki 产物整包） | ✅ 运维子视图「快照与回滚」（2026-10-23） |

### 6.3.1 v2.1 之后新增接口的接入情况（逐个）

| 接口 | 说明 | 状态 |
|---|---|---|
| `GET /api/wiki/failures`、`POST /api/wiki/retry` | 编译失败清单与**定向补跑**（只跑失败的，不全量重来） | ✅ 运维·失败补跑 |
| `GET /api/wiki/pending`、`POST /api/wiki/pending/drop`、`GET /api/wiki/pending/ignored`、`POST /api/wiki/pending/restore` | **待更新记忆**的查看 / 丢弃 / 恢复 —— 丢弃分 soft（不进 wiki、仍可召回）与 hard（真删，需 `--confirm` 语义） | ✅ 运维·待更新记忆 |
| `GET`/`PUT /api/wiki/triggers`、`POST /api/wiki/trigger/run` | 触发器状态查询 / **热改**（写 overrides，不用重启）/ 手动触发一次 | ✅ 运维·更新触发器 |
| `GET /api/wiki/align` | 对齐审计（只读）：库里变了但 wiki 没跟上的输入 | ✅ 运维·对齐审计 |
| `POST /api/wiki/update` | 增量更新 wiki | ✅ 运维·增量更新 |
| `GET /api/wiki/batch/status`、`run`、`cancel`、`resubmit`、`checks` | 离线批处理（Batch API，官方价 5 折）的推进与预检 | ✅ 运维·离线批处理 |
| `GET /api/snapshots`、`POST /api/snapshots/create`、`POST /api/snapshots/restore` | 快照与回滚（列表 / 立即快照 / 分范围回滚） | ✅ 运维·快照与回滚 |
| `GET /api/memos/traces` | 记忆**时间线明细**（原始的逐条轨迹）。面板"记忆报表"目前走 `/api/memories` + 搜索，没用它 | ⬜ 仍未接（见 §6.1） |

---

### 3.7 外部投喂（`/api/feed/*`）

把外部素材（文件 / 二进制 / 网页）转译进知识库页面层。**装配与动作都在
`agentmemhub/feed/service.py`（与 CLI 共用）**，网关只做 HTTP 语义与渲染。

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/feed/formats` | 当前**允许投喂的格式**（看板渲染勾选项用）|
| `PUT` | `/api/feed/formats` | 调整允许的格式（写**运行时覆盖**，不改 yaml）|
| `POST` | `/api/feed/add` | 投喂一项（**四种输入形态**，见下）|
| `POST` | `/api/feed/upload` | 投喂：直接上传**文件字节**（multipart）|
| `POST` | `/api/feed/scan` | 扫描目录列出可投喂项（**只读，不写盘**）|
| `POST` | `/api/feed/batch` | 批量投喂（**长任务**，轮询 `/api/admin/job`）|
| `GET` | `/api/feed/list` | 素材列表（`status` / `low_quality` / `limit`）|
| `GET` | `/api/feed/show/{doc_id}` | 单条详情（404 = 没有这条）|
| `POST` | `/api/feed/retry` | 重转（来源不可达时**自动回落 `raw/` 原件**）|
| `POST` | `/api/feed/retract` | 撤下（可恢复；**原件保留**）|
| `POST` | `/api/feed/forget` | 清除登记（`purge=true` 才连原件一起删）|
| `GET` | `/api/feed/lint` | 确定性体检（`P0>0` 表示数据不一致）|

#### 四种输入形态（`POST /api/feed/add`）

用户 2026-10-15 明确要求区分"传输的是二进制、还是路径、还是链接"；
2026-10-16 补上第四种 `text`（与 CLI 的 `--stdin`、MCP 的 `input="text"` 等价）。

| `input` | `value` 是什么 | 必填 | 说明 |
|---|---|---|---|
| `path` | **服务器本机**文件路径 | — | 转换器自己去 `open/read` |
| `data` | **二进制内容**（调用方已读好） | `filename` | JSON 装不下裸字节 ⇒ 用 `encoding="base64"`；或用 `/api/feed/upload` |
| `url` | 网页链接 | — | 取回层出网抓（走代理） |
| `text` | **正文本身** | — | `filename` 兼作标题；省掉"字符串 → 字节 → base64"的绕路 |

```jsonc
// ① 路径
{"input": "path", "value": "D:/docs/报告.docx"}

// ② 二进制（客户端 open(p,"rb").read() 之后 base64）
{"input": "data", "value": "<base64>", "encoding": "base64", "filename": "报告.docx"}
//    ⚠️ filename 必填：扩展名是判格式与认领转换器的依据，裸字节没有名字

// ③ 链接
{"input": "url", "value": "https://mp.weixin.qq.com/s/xxxx"}

// ④ 正文（浏览器另存失败 / 反爬 / 只拿到文字时最省事的通路）
{"input": "text", "value": "# 会议纪要\n\n……", "filename": "会议纪要"}
```

> **失败返回 200 + `ok=false`**（不是 4xx）—— 投喂失败是**业务结果**而不是传输错误，
> 调用方要看的是 `error_kind`（它决定处置：`unsupported_format` 要换输入、
> `needs_ocr` 等 T4 通路、`convert` 可重试）。详见 `docs/modules/feed.md` §9。

#### 格式白名单（`GET`/`PUT /api/feed/formats`）

用户要求"以不同格式文件后缀控制输入（**大小写不强制**），后续可调整哪些格式支持投喂"。

- **后缀驱动、大小写不敏感**：`.PDF` ≡ `.pdf`；
- **默认跟随转换器注册表**：不配 = 支持当前全部格式（加转换器自动生效，不必回来改清单）；
- **无后缀放行**：URL 与工具导出的无扩展名报告没有后缀可查，硬拦会把它们全挡在门外 ——
  交给认领机制按内容判型，判不出来照旧**显式报错**（不静默跳过）；
- 优先级：**运行时覆盖 > yaml `feed.accept.exts` > 注册表默认**。

```jsonc
// 只放开三种
PUT /api/feed/formats  {"exts": [".pdf", ".docx", ".xmind"]}
// 清除覆盖，回到 yaml / 默认（不传 exts 即可）
PUT /api/feed/formats  {}
```

**为什么运行时覆盖是独立文件**（`logs/feed_formats.json`）：看板要能直接点选，
但**不该反过来去改用户的 yaml**。

#### 批量投喂（`POST /api/feed/batch`）

长任务（几百个文件是分钟级），与 `/api/admin/*` 同约定：立即返回 `{"job": {...}}`、
已有任务在跑时 `409`、进度用 `tasks.set_progress` 回填、逐行输出落
`logs/tasks/<job_id>.log`。请求体：

```jsonc
{"roots": ["D:/TCL/文档"], "recursive": true, "dry_run": false,
 "force": false, "limit": 0}
```

**先扫描再投喂**（用户强调"先筛选再落库"）：`POST /api/feed/scan` 是**只读**的，
返回"可投喂 N / 被格式挡掉 M + 按后缀分布 + 被挡原因"，人确认后再开 `batch`。

---

### 6.4 已知的**前端缺口**（后端没问题）

| 接口 | 说明 |
|---|---|
| `POST /api/admin/rebuild` | 后端可用，**面板没有对应按钮** —— 补向量目前只能走 CLI `rebuild` 或直接调接口。属**未做**，非不做 |

### 6.5 复查手段

**入库脚本** `scripts/audit_frontend_api.py`（2026-10-16 由"工作区一次性脚本"正规化）：

```powershell
python scripts/audit_frontend_api.py          # 按域分组列"面板未用"的接口
python scripts/audit_frontend_api.py --used   # 连已用的一起列
python scripts/audit_frontend_api.py --json   # 机器读（给看板 / CI）
```

**什么时候跑**：改完前端就跑一次 —— 它会立刻告诉你"哪些刚接上、哪些还空着"。
它的边界（漏网情形）写在脚本 docstring 里，**别把它的结论当唯一真相**。

> ⚠️ **静态扫描前端调用一定有漏网**：URL 若写在变量里
> （如 `` const u = `/api/memos/feedback?...`; fetch(u) ``），正则匹配不到。
> 本次就误报过一次"feedback 没被调用"——**关键接口必须交叉验证**
> （用关键字 grep 确认），不能只信脚本结论。

### 6.6 **离线批处理（Batch API）** —— 底层、HTTP、自动收割与自动发起**均已就绪**

wiki 第二级编译可以走厂商**离线批处理**（价格是实时的 50%，闲时调度）。
截至 2026-09-29 的进度（详见 [batch-offline-plan.md](batch-offline-plan.md)）：

| 层 | 状态 |
|---|---|
| 客户端 `agentmemhub/batch_client.py`（上传 / 建任务 / 查状态 / 列表对账 / 取消 / 下载） | ✅ 就绪 |
| 编排 `agentmemhub/wiki_batch.py`（查状态 → 决策 → 取结果 → 作废回池 + 通知） | ✅ 就绪 |
| 提交轮 `scripts/wiki_aggregate._submit_offline()`（**整体一次提交**） | ✅ 就绪 |
| 回读 + 第二段提交（`merge_plan_results()` / `submit_offline_compile()`） | ✅ 就绪 |
| 落盘轮 `scripts/wiki_aggregate.write_offline_stage()`（组装页面 → 写盘） | ✅ 就绪 |
| 状态机编排（`wiki.update(offline_client=, offline_runner=)` 按在飞任务推进一段） | ✅ 就绪 |
| **CLI**：`wiki --action batch-status`（查状态）· `batch-run`（推进一段）· `batch-cancel`（取消作废）· `batch-resubmit`（失败后用原输入文件重提）· **`full-run [--offline]`**（全量重跑，见下） | ✅ 就绪（见 §五） |
| **HTTP**（挂在**面板服务**上，不新增常驻进程）：`GET /api/wiki/batch/status`（只读）· `POST /api/wiki/batch/run`（长任务）· `POST /api/wiki/batch/cancel` · `POST /api/wiki/batch/resubmit` · `GET /api/wiki/batch/checks`（收割流水） | ✅ 就绪（2026-10-01） |
| **自动收割**（事件驱动，无轮询）：MCP 启动 · 写入记忆 · 蒸馏收尾 · **任意 CLI 命令** → 顺手收割在飞任务 | ✅ 就绪（2026-10-01） |
| **自动发起**（离线触发器）：`wiki.batch.enabled` + 规则命中 → 提交离线批次；判定顺序 **离线优先 → 实时兜底** | ✅ 就绪（2026-10-04，见 [modules/batch-offline.md](modules/batch-offline.md) §3.5） |
| **全量重跑**：`wiki --action full-run [--offline]`（重新归域 + 全部域重编 + 覆盖写盘；通道由 `wiki.full.mode` 决定） | ✅ 就绪（2026-10-04） |
| `wiki.update_mode` 偏好开关（显式选实时/离线） | ⏳ **未做** —— 但"队列非空 ⇒ 不提交实时"这条**安全规则已就绪**，两条通道已互不打架 |

> **现在怎么用**（2026-10-04 起**离线可自动**，也可手动）：
>
> ```bash
> agentmemhub wiki --action batch-status     # 查在飞任务（只读）
> agentmemhub wiki --action batch-run        # 推进一段（提交 / 交编译段 / 落盘，状态机自判）
> agentmemhub wiki --action batch-cancel     # 取消并作废所有在飞任务
> agentmemhub wiki --action batch-resubmit   # 平台侧 validation_failed 后用原输入文件重提
> agentmemhub wiki --action full-run         # 全量重跑（重新归域 + 全部域重编 + 覆盖写盘）
> agentmemhub wiki --action full-run --offline   # 同上，但编译段走离线批次
> agentmemhub wiki --action triggers --set-batch on|off   # 热改离线触发器总闸（不用重启）
> ```
>
> ⚠️ `batch-run` **要跑两次**（第一次提交、第二次落盘）；不过**收割已经自动**
> （开 DSH / 写记忆 / 跑任意命令都会顺手推进一次），所以通常只需要等。
> **发起**也可以自动：`wiki.batch.enabled` 打开后，规则命中即提交离线批次
> （判定顺序"**离线优先 → 实时兜底**"）；通道由 `wiki.full.mode` 决定全量走哪条。
> 详见 [modules/batch-offline.md](modules/batch-offline.md) §3.4 / §3.5。
>
> 直连厂商（OpenAI Batch 兼容，需自备 API Key；`region` 段是 `-cn`）：
>
> ```bash
> curl -H "Authorization: Bearer $ARK_API_KEY" \
>   https://batch-api-cn.xiaomimimo.com/v1/batches/<batch_id>
> curl -H "Authorization: Bearer $ARK_API_KEY" \
>   "https://batch-api-cn.xiaomimimo.com/v1/batches?limit=10"   # 列出最近任务（文档未写、实测有）
> ```
>
> 代码接口：`batch_client.MiMoBatchClient.retrieve/list_batches/find_batch`、
> `wiki_batch.poll/import_results`、`wiki.update(offline_client=, offline_runner=)`。
> 事实流：`logs/wiki.log` 的 `batch_*` 事件（见 [logging.md](logging.md) §5.1）。

## 七、维护约定

- **改了接口必须同步本文与 docstring**：Swagger 的内容来自代码里的
  `summary` / `description` —— 端点不写描述，`/api/docs` 里就只剩 `Api Xxx`
  这种自动名（本文档诞生时 31 个端点里多数如此，现已全部补齐 `summary`）。
- **新增端点请补三样**：装饰器上的 `summary`、每个 Query 参数的 `description`、
  以及本文对应分组的表格行。
- **响应契约不得破坏**：MCP 五工具与面板网关的字段是 Skill / 前端强依赖
  （见 AGENTS.md「架构不变量」）。
