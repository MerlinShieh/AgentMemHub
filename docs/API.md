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
| `PUT` | `/api/conversations/{source}/{cid}` | 改标题（置 `title_custom`，ingest 重写时保留）|

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

---

## 四、MCP 工具（Agent 侧，5 个）

| 工具 | 参数 | 说明 |
|---|---|---|
| `memory_search` | `query`（必填）、`topK`、`note` | 语义检索历史记忆 |
| `memory_save` | `content`（必填）、`importance`、`tags`、`note` | 写入一条记忆 |
| `memory_recent` | `limit`、`note` | 最近记忆时间线 |
| `memory_stats` | `note` | 引擎在线状态与记忆总量 |
| `memory_score` | `trace_id`、`polarity`、`note` | 给记忆打极性分（**仅用户明确说重要时才用**）|

**契约要点（Skill 侧强依赖，不得破坏）**：

- 五个工具的**名称与参数名**是 Skill 侧硬依赖
- `note` 是**可选**参数，用于记录调用意图（为什么），写进 `logs/mcp.log`
- **调用审计由服务端在唯一分发点自动写**，不依赖 Agent 手动记
- `importance` 档位：`high` 0.8 / `normal` 0.6 / `low` 0.4（不传即 `normal`）
- **写后不立即评分** —— 价值分由真实使用演化

**注册配置**见 `docs/mcp-register.example.json`。

---

## 五、CLI

见 README「命令行」章节的完整表格。与接口相关的三条运维入口：

```bash
python -m agentmemhub wiki --action failures --out DIR          # 查 wiki 失败清单
python -m agentmemhub wiki --action retry --out DIR --stage l2   # 定向补跑
python -m agentmemhub health_check                              # 记忆库一致性巡检
```

---

## 六、维护约定

- **改了接口必须同步本文与 docstring**：Swagger 的内容来自代码里的
  `summary` / `description`，函数不写描述，`/api/docs` 里就是空的（本文档诞生时
  29 个端点里多数没写）。
- **新增端点请补三样**：端点函数的 docstring、每个 Query 参数的 `description`、
  以及本文对应分组的表格行。
- **响应契约不得破坏**：MCP 五工具与面板网关的字段是 Skill / 前端强依赖
  （见 AGENTS.md「架构不变量」）。
