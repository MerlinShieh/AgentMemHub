# 离线批处理（Batch API）：接缝设计 → 实施记录

> 状态：**✅ 已落地并实测四轮**（2026-10-04）。本文档记录**从接缝到落地**的全过程：
> §1–§7 是 2026-09-25 写的**接缝设计**（原样保留，便于回看当初的判断），
> 每节末尾补了「**后来怎样了**」；**§8 是实施记录**（最终形态、实测数据、决策变更、踩坑）。
>
> **现在该看哪份**：
> · **怎么用 / 怎么排查** → [`modules/batch-offline.md`](modules/batch-offline.md)（模块文档，权威）
> · **为什么这么设计 / 里程碑与风险** → [`batch-offline-plan.md`](batch-offline-plan.md)
> · **当初为什么先做接缝、后来怎么长成现在这样** → 本文档
>
> 代码：`agentmemhub/llm_batch.py`（编解码 + 运行器）、`agentmemhub/batch_client.py`（厂商客户端）、
> `agentmemhub/wiki_batch.py`（回读 + 状态机）、`agentmemhub/task_queue.py`（通用惰性收割）、
> `agentmemhub/batch_ops.py`（CLI/HTTP 共用动作层）、`scripts/wiki_aggregate.py` 的
> `stage_plan(..., runner=)` 与离线提交/落盘分支
> 日期：接缝 2026-09-25 · 落地 2026-09-29 ~ 2026-10-04

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

## 4. 当时列出的待办（**现已全部完成**，保留原文便于对照）

> **后来怎样了（2026-10-04 补）**：这一节当初列的"接通厂商时要做的"**全部做完了**，
> 而实现形态与当初设想**有几处不同**（详见 §8）：`BatchUploader` 的
> `poll/download` 合并成了 `retrieve/download` 六个动作、`PendingError` 换成了
> `OfflineRunner` 的"落盘后抛 `PendingError`"…下面每小节末尾都标了现状。

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

### 4.3 接入清单（**逐项现状**）

1. ~~实现 `BatchUploader`（上传/轮询/下载）~~ ✅ **`agentmemhub/batch_client.py`**
   —— 实际做成 `MiMoBatchClient` **六动作**（`upload` / `create` / `retrieve` /
   `list_batches` / `cancel` / `download`）+ `find_batch()` 对账，而不是三个方法：
   厂商是 OpenAI Batch 兼容语义（"建任务"与"传文件"分开，且**建任务不幂等**、
   超时后只能靠列表接口对账）✓
2. ✅ **`OfflineRunner`**（`llm_batch.py`）：build 请求 → 建 `BatchJob` →
   `save_state()` → `upload` → `create` → `on_submitted(info)` → 抛 `PendingError`
3. ✅ **CLI**：动作名与当初设想**不同** —— 原想的
   `batch-status | batch-export | batch-import` 落地为
   **`batch-status` / `batch-run` / `batch-cancel` / `batch-resubmit`**：
   状态机已能自判"该提交还是该回读"，拆成 export/import 只会让调用方多记一套规则
   （见 §8 决策变更）
4. ✅ **配置键加了，但位置不同**：不是 `wiki.l2.plan_mode`，而是
   `wiki.batch.*`（离线触发器）+ `wiki.full.mode`（全量重跑通道）——
   因为后来发现"离线 vs 实时"该按**触发场景**分，而不是按 L2 阶段分
5. ⚠️ **假 uploader 打通闭环还没固化成 e2e 测试** —— 目前三段式由单测覆盖
   （`test_llm_batch.py` / `test_wiki_batch.py` / `test_wiki_scripts.py`），
   端到端假通道**仍是 M6 待办**

---

## 5. ⚠️ 为什么当初**不加**配置开关（以及后来加了什么）

> **后来怎样了（2026-10-04 补）**：这个判断**在方法上是对的、在粒度上被修正了** ——
> 现在是"**先有真实消费点，再加键**"地逐个加的，而不是一次性上一组开关：
> · `wiki.batch.enabled/schedule/dirty_memories/first_write_daily` —— **离线触发器**，
>   `_run_locked()` 真有消费点（判定顺序"离线优先 → 实时兜底"）；
> · `wiki.batch.check_interval_s` —— 收割节流，`task_queue.harvest()` 真读；
> · `wiki.full.mode` —— 全量重跑通道，`batch_ops.full_mode()` 真读。
> 每个都过了 `scripts/check_config_keys.py`（僵尸键 0）✓ 

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

> **后来怎样了（2026-10-04 补）**：落地形态比这里设想的更细 ——
> ① **切分点变了**：不再"按域切换"，而是"**按阶段**"——归域与细分走实时（条数少、
>    且细分结果要在本地算才能生成编译请求），**只有编译段走离线**（实测 258 条）；
> ② **两条通道不会同时开工**：唯一交互规则是"**队列非空 ⇒ 不提交实时**"
>    （否则离线正在编译的域会被实时重编一遍、白花一次实时的钱）；
> ③ **自动触发**（若开启）的判定顺序是"**离线优先 → 实时兜底**"，
>    两个开关都开时优先走离线，离线不可用才回落到实时 ✓

---

## 7. 顺带修掉的根因（本次一并做了）

`stage_plan` 原本**一次调用**处理整个域，失败则兜底「退化为整域一页」。
本次改了两处，缺一不可：

1. **分批**：成员按上限切批，prompt 不再随域无限增长 → 大域不再必然超时；
2. **兜底语义改为「成员各自成页」**：即使某批仍然失败，也只影响那一批覆盖的成员，
   **不再把整个域压成一页**（配合 `SyncRunner` 的单条失败隔离）。

再叠加写盘侧的三道闸（域级原子写 / 页数崩塌拒写 / 整轮降级中止），
「降级」的破坏力被限制在**质量**层面，不再触及**数据完整性**。

---

## 8. 实施记录（2026-09-29 ~ 2026-10-04）

### 8.1 最终形态：三段式 + 惰性收割

```
① 提交轮  归域（实时）→ 细分（实时，条数少）→ 编译请求**一次交出去**（离线）
              ↓ 落盘现场 batch_state.json（含每个域的成员快照，回读时还原全局编号）
② 等厂商   18 分钟 ~ 24 小时（实测：最快 18 分钟，最慢一轮排队 24 小时）
              ↓
③ 落盘轮  回读 → 组装页面 → 写盘 → linkfix → 刷基线（`partial=True`，不清目录）
```

**"什么时候跑第 ③ 步"—— 不做轮询、不引入常驻进程**：挂在**已有事件**上
（`agentmemhub/task_queue.py`）：开 DSH（MCP 启动）· 写入记忆 · 蒸馏收尾 ·
**跑任意 CLI 命令**（实测跑 `stats` 也会顺手检查）。三层短路保证零成本：
**队列空 ⇒ 立即返回**（连厂商都不查）→ 节流 300 秒 → 跨进程锁。

### 8.2 实测四轮（全部真凭据）

| 轮次 | 提交 | 结果 |
|---|---|---|
| ① 首次真实发起（09-29） | 细分 11 条（**纯排队 24 小时**，且遇到平台侧 `validation_failed`） | 见 §8.4 踩坑 |
| ② 细分 + 编译（09-30~10-01） | 11 条 → 交编译 113 条 | 落盘 **3 域 / 182 页** |
| ③ 只离线编译（10-01） | 250 条，50 分钟跑完 | 落盘 **5 域 / 398 页** |
| ④ 只离线编译（10-03~10-04） | 258 条，约 13.5 小时 | 落盘 **8 域 / 378 页**，**链接验收 0 未解析** |

**第 ④ 轮还验证了一个修复**：落盘后 `l2_inputs 1833 = l1_inputs 1833`（上一轮是
1826 vs 1827，L2 基线多算了一条）—— 见 §8.3 的"静默掩盖"。

### 8.3 落地时改掉的四个设计判断

| 当初的设计 | 落地后 | 为什么改 |
|---|---|---|
| 细分与编译**都**离线（两段式） | **只离线编译，细分走实时** | 细分只有"域数×批数"条（实测 11 条），实时跑分钟级；两段都离线意味着**付两轮排队延迟**（实测细分段单独排了 24 小时） |
| `PendingError` 标记"已落盘" | `OfflineRunner` 落盘现场后抛 `PendingError` | 同上，只是名字与职责更清楚 |
| 按 `wiki.l2.plan_mode` 切通道 | **按触发场景**：`wiki.batch.*`（离线触发器）+ `wiki.full.mode`（全量） | "离线还是实时"与"L2 的哪个阶段"不是一回事 —— 用户可能"日常实时、全量离线" |
| 离线终局失败 → 作废回池 | 作废回池**但保留现场**，可用 `batch-resubmit` **复用原输入文件**重提 | 平台侧 `validation_failed` 时（它自己的错误文案就是"请尝试重新创建任务"），重跑 L1/归域纯属白花钱 |

### 8.4 踩坑清单（都是实测撞出来的）

| 坑 | 症状 | 根因 / 修法 |
|---|---|---|
| `request_counts.total == 0` 被当成"数据有问题" | 怀疑上传的 JSONL 有问题 | 那是**执行进度**、不是解析结果。排除法：下载回输入文件数行数 + 建一个"官方示例逐字照抄"的对照任务（它也一样卡 → 平台侧问题） |
| 平台侧批量调度异常 | 连官方示例都 `validation_failed` | 平台问题，**与我们数据无关**；处置=复用原输入文件重提（`batch-resubmit`） |
| 终局作废**把现场一起删了** | 12 条请求的成员快照丢失，只能重新生成 | `import_results()` 改为**只标记 `voided`、保留在 state**（"不在飞"与"是否留着"是两件事） |
| 域名被写成目录名（**自激循环**） | `manifest_l2` 从 18 个域涨到 21 个，产物出现 `20-04-…` 双重前缀目录 | 老产物 `index.md` 首行是**目录名** → 被当域名写进 manifest → 下轮当真域。修法：域名只认 `_domains.json` + slug 反查（`_domain_dir_map()`） |
| 离线落盘**静默掩盖**批次期间新写入的记忆 | L2 基线多算了 1 条（1826 vs 1827） | 异步时间窗：基线照"当前库"建 → 把没编译的记忆记成已编译。修法：`_rebuild_l2_manifest(since=job.submitted_at)` 剔除"晚于提交时刻"的输入 |
| linkfix `'list' object has no attribute 'get'` | 链接修复与 Obsidian 验收**一起没跑**（产物却已写盘） | `collect_redirects` 遇到数组型 JSON 没做类型容错 |
| linkfix 扫描**隐藏目录** | 漏修 252 条死链 | `.bak_*` / `.tmp_*` 里的旧同名页面让它误判"链接能解析"，而 Obsidian 忽略这些目录 → 口径不一致。修法：跳过 `.` 开头的目录分量 |
| `os.kill(pid, 0)` 判进程存活 | **跑测试时 pytest 把自己杀了**（整条命令无输出、exit 1） | Windows 上那是 `TerminateProcess`（杀掉对方，不是探测）。修法：只按时间戳判锁陈旧 |

### 8.5 还没做的（M6 待办）

- **假 uploader 的端到端测试**（原计划要求"e2e 全用 fake uploader"）——
  目前三段式由单测覆盖，缺一条端到端假通道；
- **`repair_model` 兜底**：离线回读只做 `extract_json()`，而实时路有本地模型修复
  ——实时能救回的 ` ```json ` 围栏，离线会退化成"成员各自成页"；
- 其余见 [`batch-offline-plan.md`](batch-offline-plan.md) §17.5 与里程碑 M6。
