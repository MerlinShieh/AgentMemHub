# 离线批处理（Batch API）接入设计

> 状态：**接缝已就位，上传/轮询/下载未实现**
> 2026-09-27 起接通小米 MiMo Batch API：计划、决策记录（D1–D6）与里程碑追踪见
> [`batch-offline-plan.md`](batch-offline-plan.md)，本文档是其接缝前置。
> 代码：`agentmemhub/llm_batch.py`（编解码 + 运行器协议）、
> `scripts/wiki_aggregate.py` 的 `stage_plan(..., runner=...)`
> 日期：2026-09-25

---

## 1. 为什么做这件事

部分模型厂商提供**离线批处理**：把一批 API 调用写成 JSONL 上传到云平台，
平台在**空闲时段**逐条执行，之后把结果 JSONL 交回。相对实时调用：

| | 实时（当前默认） | 离线批处理 |
|---|---|---|
| 价格 | 常规 | 通常明显更低 |
| 延迟 | 秒级 | 分钟 ~ 24 小时 |
| 并发/限流 | 受 RPM/TPM 约束，会撞 429 | 平台排队，不受我们的并发限制 |
| 适用 | 用户等结果 | **不需要当场拿结果**的批量环节 |

本项目里最匹配的是 **wiki 第二级的「域内细分」**：

- 它天然是**一批互相独立的小请求**（一个域 / 一个分批一条），彼此无依赖；
- 它对延迟不敏感（wiki 产物本来就是滞后一个更新周期的）；
- **它正是压垮上游的那一环**：细分是一次调用，prompt 随域内页数线性增长 ——
  实测 2026-09-25 一个 114 成员的域必撞 `HTTP 524`，而当时的兜底会把整域压成
  一页（43 页 → 1 页，两次丢页事故的直接放大器）。

---

## 2. 接缝设计：为什么是 `runner`

离线批处理的本质是**异步 + 跨进程**：请求在一次运行里攒出来，结果要在**另一次
运行**里读回来。所以调用层不能只有「当场调用」这一种形态。

`stage_plan` 因此长成：

```python
stage_plan(client, domain, members, *, batch=0, runner=None, log=print)
```

- **`runner=None`** → `SyncRunner`，当场逐条执行（**当前生产路径**）
- **`runner=<离线运行器>`** → 把请求写进 JSONL、抛 `PendingError` 保留现场

协议只有一个方法：

```python
class Runner(Protocol):
    def run(self, requests: list[BatchRequest]) -> list[dict]: ...
```

**编号映射与流程完全共用** —— 实时与离线只在「谁执行、什么时候执行」上分叉，
切分、prompt、批内编号 → 全局编号的映射、遗漏成员兜底，全部同一段代码。

---

## 3. 已完成（都不需要厂商参与，已测）

| 组件 | 位置 | 说明 |
|---|---|---|
| `BatchRequest` | `agentmemhub/llm_batch.py` | 一次待执行调用的最小单位，含 `custom_id` |
| `batch_to_jsonl()` | 同上 | OpenAI Batch 兼容格式（`custom_id` / `method` / `url` / `body`） |
| `load_batch_results()` | 同上 | 结果 JSONL → `{custom_id: {"ok", "data"\|"error"}}`，**逐行容错** |
| `SyncRunner` | 同上 | 当场执行；**单条失败记空结果**，不拖垮整批 |
| `PendingError` | 同上 | 「结果还没回来」≠「失败」，必须原样抛出、由上层保留现场 |
| `BatchJob` | 同上 | **「已交给厂商」这个标记 + 现场**：`batch_id`、custom_id↔域/批号、**成员快照**、已回读结果 |
| `save_state()` / `load_state()` | 同上 | 标记落盘/读回（`<out_l2>/batch_state.json`），原子替换、坏文件不抛 |
| `decide()` | 同上 | **处置决策**：`CONSUME` / `WAIT` / `PARTIAL` / `FALLBACK`（纯函数） |
| 分批 | `wiki_aggregate.plan_requests()` | 成员按 `L2_PLAN_BATCH_MAX`（配置 `wiki.l2.plan_batch_max`，默认 40）切批 |

`custom_id` 设计成**自带定位信息**而非裸序号：

```
l2plan|<domain_key>|<batch_no>      例：l2plan|DSH-Hermes|3
```

因为上传与回读可能隔几小时、甚至在不同机器上，凭据必须能独立定位到
「哪个阶段、哪个域、第几批」。

### 3.1 「标记」为什么必须是**域粒度**且**带成员快照**

标记的对象是「**已经加入离线任务、需要 wiki 更新的那批数据**」。粒度选**域**，
因为它与 wiki 的更新单位一致：`manifest` / `align` 判定"哪些域脏了"，
脏域再按批提交 —— 所以"这个域已交给厂商"就是最小的、需要记住的事实。

`members` 快照**必须原样保留**：回读时要用**全局编号**取成员，而分批编号是
相对批内的 —— 少了这份快照，结果拿回来也拼不回原来的成员。

### 3.2 处置决策：等不及了就改走实时

`decide(job, now=..., deadline_s=...)` 把「**还在等厂商的那部分也可以选择立即更新**」
这条规则固定成纯函数：

| 返回 | 含义 |
|---|---|
| `CONSUME` | 结果齐了 → 直接用，不必再花钱 |
| `WAIT` | 还没好、且在预算内 → 继续等下一轮 |
| `PARTIAL` | 部分到位但已超预算 → **先用已到的，剩下的改走实时** |
| `FALLBACK` | 一条都没到且已超预算 → **整批改走实时** |

`deadline_s <= 0` 表示**不限时**（一直等）—— 离线批处理本来就慢，默认不该催它；
限时是"我这次就想拿到结果"时临时给的。

---

## 4. 未完成（接通厂商时要做的）

### 4.1 三个 HTTP 动作

`agentmemhub/llm_batch.py` 里预留的落点（**各家端点与鉴权不同，且此处无凭据可测，
所以只定义形状、不写死实现**）：

```python
class BatchUploader(Protocol):
    def upload(self, jsonl_text: str) -> str: ...          # -> provider batch id
    def poll(self, batch_id: str) -> str: ...              # -> "running" | "done" | "failed"
    def download(self, batch_id: str) -> str: ...          # -> 结果 JSONL 文本
```

### 4.2 断点续跑（关键，也最容易被低估）

离线模式下**一次运行无法闭环**，必须持久化「现场」：

```
run(online) ──collect──▶ batch.jsonl ──upload──▶ 厂商
                                                   │ 空闲时段逐条执行
              ingest ◀── download ◀── poll ◀────────┘
                 │
                 └─▶ 回填结果 → 继续 ③ 编译
```

现场至少要有：

- `batch_id`（厂商返回）
- 每个 `custom_id` ↔ `(domain, batch_no)` 的映射
- **每个域那批成员的完整列表**（因为回读时要按全局编号取成员 —— 分批的编号是
  相对批内的）

**这三样已经由 `BatchJob` + `save_state()` / `load_state()` 落地**
（`<out_l2>/batch_state.json`，与 `manifest_l2.json`、`failures.jsonl` 同级；
都在产物目录里，快照会一并覆盖）：

```
BatchJob
  batch_id      厂商给的任务 id
  provider      哪家（端点/语义不同）
  stage         "l2plan"
  submitted_at  提交时刻（用于 decide 的超时判定）
  items         {custom_id: {domain, batch_no}}   ← 「已交给厂商」的标记
  members       {域名: [成员快照]}                 ← 回读时还原全局编号用
  results       {custom_id: {ok, data|error}}      ← 回读后填
```

于是「这批数据已经加入离线任务」这件事**跨进程可查**：`load_state()` 读出所有
在飞的 `BatchJob`，`job.domains()` 就是"已被离线任务覆盖、暂不该重复提交"的域。

### 4.3 接入清单

1. 实现 `BatchUploader`（上传/轮询/下载），放 `agentmemhub/llm_batch.py`
2. 实现 `OfflineRunner`：build 请求 → 建 `BatchJob` → `save_state()` → upload
   → 抛 `PendingError`（标记已落盘，现场不会丢）
3. 加 CLI 入口：`agentmemhub wiki --action batch-status | batch-export | batch-import`
   - `batch-status`：读 `batch_state.json` + 问厂商状态，按 `decide()` 报告
     `CONSUME` / `WAIT` / `PARTIAL` / `FALLBACK`（**含"哪些域还在等、等了多久"**）
   - `batch-import`：回读结果 → 按 `decide()` 决定"消费 / 剩余走实时"
4. **此时才加配置键** `wiki.l2.plan_mode: online | offline`（以及可选的
   `plan_wait_deadline_s`）
5. 测试：用**假 uploader** 打通 `export → ingest` 闭环（不需要真厂商）

---

## 5. ⚠️ 为什么现在**不加**配置开关

本项目已经**三次**踩过「假开关」：配置项没有任何读取点、默认值恰好等于代码里
硬编码的值 —— 于是既不报错、也不让测试变红，能潜伏数月
（`wiki.single_shot_max`、`wiki.workers`、`wiki.enabled`）。

所以这里的顺序是**反过来的**：先做「不需要厂商就能做对」的那一半并测好
（编解码、运行器协议、分批），**等真接通了再加开关**。在那之前，
`wiki.l2.plan_batch_max` 是唯一新增的键 —— 它**有真实消费点**
（`_l2_cfg("plan_batch_max", ...)`），`scripts/check_config_keys.py` 审计通过。

---

## 6. 与实时路径的关系

**不是二选一，而是同一套代码的两种执行方式**：

- 实时是**默认**，也是唯一在生产跑的路径；
- 离线是**可选**的省钱/错峰通道，接通后按域粒度切换（甚至可混用：小域实时、
  大域离线）；
- 两条路的产物**完全一致** —— 因为编号映射与合并逻辑是同一份代码。

---

## 7. 顺带修掉的根因（本次一并做了）

`stage_plan` 原本**一次调用**处理整个域，失败则兜底「退化为整域一页」。
本次改了两处，缺一不可：

1. **分批**：成员按上限切批，prompt 不再随域无限增长 → 大域不再必然超时；
2. **兜底语义改为「成员各自成页」**：即使某批仍然失败，也只影响那一批覆盖的成员，
   **不再把整个域压成一页**（配合 `SyncRunner` 的单条失败隔离）。

再叠加写盘侧的三道闸（域级原子写 / 页数崩塌拒写 / 整轮降级中止），
「降级」的破坏力被限制在**质量**层面，不再触及**数据完整性**。
