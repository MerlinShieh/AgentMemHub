# 离线批处理（Batch API）模块

> 覆盖模块：`agentmemhub/batch_client.py`（客户端）· `agentmemhub/llm_batch.py`（编解码与运行器协议）
> 最后核对：2026-09-29 · 计划与决策见 [batch-offline-plan.md](../batch-offline-plan.md) · 接缝设计见 [llm-batch-offline.md](../llm-batch-offline.md)

---

## 1. 职责边界

| 做什么 | 落点 |
|---|---|
| 把一批待编译请求编成 OpenAI Batch 兼容的 JSONL | `llm_batch.batch_to_jsonl()` |
| 上传文件 / 建任务 / 查状态 / 列表对账 / 取消 / 下载结果 | `batch_client.MiMoBatchClient` |
| 解析结果 JSONL → `{custom_id: {ok, data\|error}}`（逐行容错） | `llm_batch.load_batch_results()` |
| 离线任务的**跨进程现场**（batch_id / custom_id↔域 / 成员快照 / 已回读结果）与落盘 | `llm_batch.BatchJob` + `save_state()` / `load_state()` |
| 处置决策（消费 / 继续等 / 部分消费 / 回退实时） | `llm_batch.decide()` |
| 单条失败隔离（不让一条失败拖垮整批） | `llm_batch.SyncRunner` |

**明确不做**：

| 不做什么 | 归属 |
|---|---|
| 不做任务调度与重试编排（谁在什么时机提交/回读） | `wiki_aggregate` 的 `runner=` 接缝 + CLI（见计划 M2/M3） |
| 不做通知推送 | 计划 M1 的 `notify.py`（**尚未实现**） |
| 不改 wiki 产物 | 写盘仍在 `scripts/wiki_aggregate.py` |
| 不做实时调用 | `agentmemhub/llm.py`（两套端点与凭据互相独立） |

---

## 2. 实测契约（2026-09-29 真凭据探测）

本次探测用 1 条 `max_tokens=32` 的极短请求跑通全链路，**顺带纠正了官方文档的三处不准**：

| 项 | 官方文档 | **实测** |
|---|---|---|
| 主机 | `batch-api-${region}.xiaomimimo.com`，region 取值"去控制台取" | **`https://batch-api-cn.xiaomimimo.com/v1`**；写成 `batch-apicn.…` **DNS 解析失败**（少一个连字符） |
| 建任务返回码 | 未写 | **HTTP 201**（不是 200） |
| 任务初始状态 | 列了 6 种（声称 9 种） | 实际存在文档未列的 **`created`** |
| `completion_window` | 字段说明"当前固定 24h"，正文说 1–14 天 | **可传 `168h` / `336h`（14 天）** —— 正文对、字段说明错 |
| 列表接口 | **完全没写** | **`GET /v1/batches` 与 `GET /v1/files` 都存在** —— 建任务超时后的对账全靠它 |
| 文件有效期 | 30 天 | ✓ 一致（`expire_at - created_at` = 43200 分钟） |
| 取消 | 异步，需再查 | ✓ 一致（`cancelling` → 4 秒后 `cancelled`） |

**六个动作的实测行为**：

| 动作 | 端点 | 实测 |
|---|---|---|
| 上传 | `POST /v1/files`（multipart：`purpose=batch` + `file`） | 200；返回 `file-…`，`status=active` |
| 建任务 | `POST /v1/batches`（JSON：`input_file_id` / `endpoint` / `completion_window` / `name`） | **201**；`status=created` |
| 查状态 | `GET /v1/batches/{id}` | 200；带 `request_counts{total,completed,failed}` |
| 列表 | `GET /v1/batches?limit=N` | 200；`{object:"list", data:[…]}` |
| 取消 | `POST /v1/batches/{id}/cancel` | 200 → `cancelling`；**取消后 output/error file 均为空**（除非已有完成请求） |
| 下载 | `GET /v1/files/{file_id}/content` | 200；JSONL 文本（成功与错误是**两份文件**） |

**错误体统一形状**（分类依据）：

```json
{"error": {"message": "Invalid API key", "type": "authentication_error", "code": "invalid_api_key"}}
{"error": {"message": "Batch not found: xxx", "type": "not_found", "code": "batch_not_found"}}
```

**冒烟任务**（留作真实执行验证，不取消）：`batch_04cc53ea36324c8598abbffa`
—— 1 条 `mimo-v2.6-flash` 请求，等闲时调度完成后用它验证"模型名可用 + 结果行形状 + 下载"。

---

## 3. 对外入口

`MiMoBatchClient(cfg, *, log=None, opener=None)` —— `cfg` 就是 `Config.llm_batch`：

| 方法 | 返回 | 重试策略 |
|---|---|---|
| `upload(jsonl_text, *, filename="input.jsonl")` | file 对象 | 可重试（重复上传只是多一个临时文件） |
| `create(input_file_id, *, name, endpoint, completion_window, n_requests)` | batch 对象 | ⚠️ **绝不重试**（见下） |
| `retrieve(batch_id)` | batch 对象 | 可积极重试（只读） |
| `list_batches(*, limit=20)` | `list[batch]` | 可重试（只读） |
| `cancel(batch_id)` | batch 对象（已消失则 `{"status": "not_found"}`） | 幂等，可重试 |
| `download(file_id)` | JSONL 文本 | 可重试（只读） |
| `find_batch(*, name, input_file_id, limit=30)` | batch 或 `None` | **对账专用**（内部走 list） |

### 3.1 为什么 `create()` 绝不自动重试

其余动作都幂等，唯独**建任务不是**：同一个 `input_file_id` 提交两次 = 两个任务、
两份计费。所以 `create()` 用 `retries=1`，网络类失败抛
**`BatchUnknownStateError`**（"结果未知"，不是"失败"），由上层调 `find_batch()`
按 `name` / `input_file_id` 确认那笔提交到底建成没有。

> 这条策略成立的前提正是第 2 节里"文档没写、实测有"的列表接口 —— 没有它，
> 建任务超时后只能人工去控制台翻。

### 3.2 离线运行器（`llm_batch.OfflineRunner`）

`stage_plan(..., runner=OfflineRunner(uploader=…, model=…, stage=…))` 就把一个域的
细分从"当场调用"换成"交出去"：

```
请求（一个域的全部批次）
  └─ batch_to_jsonl()        正文过 scrub_text，extra 透传 thinking / reasoning_effort
       └─ uploader.upload()  → input_file_id
            └─ uploader.create() → batch_id
                 └─ on_submitted(info)    ← 上层在这里把 BatchJob 落盘
                      └─ raise PendingError  ← "还没算" ≠ "算不出来"
```

- **顺序不能反**：`on_submitted` 必须在 `raise` **之前**调用 —— 反过来的话，
  `PendingError` 一旦被上层吞掉或进程中断，"已交给厂商"就丢了，下轮会重复提交、
  重复付费。
- **一次提交的粒度是"一个域"**：`stage_plan` 把该域的全部批次请求一次性交给 runner
  （5 个成员按 `plan_batch_max=2` 切成 3 条请求 → **1 次上传、1 个任务、3 行 JSONL**），
  不是每批一个任务。
- `BatchUploader` 协议只要求 `upload` / `create` 两个动作（生产实现是
  `MiMoBatchClient`），所以测试注入假实现即可**全程不发真请求**。

---

## 4. 数据流与不变量

```
待编译请求 ──batch_to_jsonl()──▶ JSONL ──upload()──▶ file_id
                                                        │
                              create(input_file_id) ────┘──▶ batch_id（status=created）
                                                                    │ 闲时调度
   {custom_id: {ok,data}} ◀──load_batch_results()── 结果 JSONL ◀──download()◀── poll
```

| 不变量 | 后果 |
|---|---|
| **`custom_id` 自带定位信息**（`l2plan\|<域名-crc32>\|<批号>`） | 上传与回读可能隔几小时、甚至换机器；裸序号定位不了 |
| **建任务不重试**（`retries=1`） | 重试 = 重复建任务 = 重复计费；不确定时用 `find_batch()` 对账 |
| **沙箱熔断先于发请求** | `AGENTMEMHUB_TEST_SANDBOX=1` 时任何动作直接抛（这批调用真花钱） |
| **日志是旁路** | `_audit()` 内部吞掉一切异常，审计炸了不影响返回值 |
| **错误分类决定处置**（鉴权/余额 → 回退实时并通知人；瞬态 → 重试；不存在 → 数据回池） | 反了就是"白等"或"白花钱" |
| 文件与结果**只保留 30 天** | `batch_id` 有保质期；过期后 retrieve 报 `batch_not_found` |
| **不支持任务内重试 / 续跑**（厂商限制） | 失败只能**新建任务**；`BatchJob` 必须允许一个域被多个 job 覆盖 |

### 4.1 任务现场的字段与两个判定（M1，2026-09-29）

| 字段 | 用途 |
|---|---|
| `input_file_id` / `output_file_id` / `error_file_id` | 重新提交、回读结果文件（后两者 `completed` 之后才有值） |
| `provider_status` | 厂商原始状态（一律 `lower()`）；**回读轮据此判断"继续等"还是"改走实时"** |
| `expire_at` | 任务过期时刻。文件与结果只留 30 天，过期后回读报 `batch_not_found` |
| `voided` / `voided_reason` | 已作废 → 它覆盖的域**回到待更新池**（模式切换 / 手动取消 / 终局失败） |

三个方法回答三个**容易混**的问题：

- **`results_state`**（`complete` / `partial` / `pending`）—— **我们**收齐结果没有。
  刻意**不叫 `status`**：厂商也有一个 status（见 `provider_status`），同名不同义
  最容易误用（M1 改名）。
- **`is_terminal()`**（`failed` / `expired` / `cancelled`）—— **厂商**是否已经放弃。
  终局后 `decide()` **不等 deadline** 直接给结论；否则终局失败会被当成"还在等"
  而干等到天荒地老。
- **`void(reason)`** —— 作废并返回它覆盖的**域名列表**（供上层放回池）。
  **不需要搬数据**：提交轮没刷 manifest，那批输入本来就还是"脏"的，作废只是把
  显式标记清掉，隐式差集自动接管。

---

## 5. 配置项

全部在顶层 `llm_batch` 段（**独立段、不继承顶层 `llm`**，理由见 `config.py` 的 `DEFAULT_LLM_BATCH` 注释）：

| 键 | 默认 | 作用 |
|---|---|---|
| `llm_batch.endpoint` | `https://batch-api-cn.xiaomimimo.com/v1` | 批量主机（与实时路由不是一套） |
| `llm_batch.api_key` | 空 | 小米开放平台 `sk-…`；**需实名 + 现金余额**（Token Plan 不抵扣）。空 = 拒绝发请求 |
| `llm_batch.model` | `mimo-v2.6-flash` | 批量仅支持 `mimo-v2.6-pro` / `mimo-v2.6-flash`，**小写、不带厂商前缀** |
| `llm_batch.completion_window` | `24h` | 实测可到 `336h`；资源紧张时调大 |
| `llm_batch.timeout` | `60` | 单次 HTTP 超时（秒） |
| `llm_batch.max_retries` | `3` | 只作用于幂等动作（`create()` 永远只发一次） |

---

## 6. 调试与排障

| 现象 | 看哪里 | 判断 |
|---|---|---|
| 域名解析失败（`getaddrinfo`） | `llm_batch.endpoint` | region 段是 **`-cn`**，不是 `cn`（实测踩过：`batch-apicn` 不通） |
| `鉴权失败（invalid_api_key）` | `llm_batch.api_key` | key 无效/过期；或误用了实时路由的 key（两套不通用） |
| `余额/配额不足` | 小米控制台 | 批量走**现金余额**，Token Pack/套餐额度不抵扣 |
| `对象不存在（batch_not_found）` | 文件是否超 30 天 | 过期即不可恢复，该批数据回待更新池重新提交 |
| 任务一直 `validating` / `created` | 正常 | 闲时调度，提交后可能长时间不启动（不是故障） |
| 任务 `expired` | `completion_window` 太小 | 调大窗口后重新提交（**不支持续跑**） |
| 想确认某笔提交建成没 | `client.find_batch(name=…)` | 不要重试 `create()` |
| 日志在哪 | `logs/wiki.log` | 事件：`batch_upload` / `batch_create` / `batch_retrieve` / `batch_list` / `batch_cancel` / `batch_download` |

---

## 7. 测试在哪

`tests/test_batch_client.py`（22 条）—— **全程注入假 opener，不发真请求**：

| 组 | 覆盖 |
|---|---|
| 熔断与配置 | 沙箱熔断在发请求**之前**生效；未配 key / endpoint 直接拒绝 |
| 错误分类 | 401/403 → Auth、余额 → Balance、404 → NotFound、500/429 → Transient、400 → 基类（`type() is` 精确断言） |
| 各动作 | multipart 编码（boundary、`purpose=batch`、文件名、JSONL 正文原样）、建任务请求体、下载、列表 |
| **重试策略** | 建任务失败**只发一次**且抛 `BatchUnknownStateError`；只读动作会重试 |
| 对账 | 按 `name` / `input_file_id` 命中；查不到返回 `None` 而不抛 |
| 旁路 | `audit_wiki` 抛异常不影响返回值 |

改动本模块时的回归清单：

```bash
uv run pytest tests/test_batch_client.py tests/test_wiki_scripts.py -q
uv run python scripts/check_config_keys.py     # llm_batch.* 六个键都要有消费点
```

---

## 8. 待办（随计划里程碑）

- ~~**M1**~~：✅ **已完成（2026-09-29）** —— `BatchJob` 补齐厂商侧字段 + `status` → `results_state`
  改名 + `is_terminal()` + `void()` 回池 + `decide()` 终局优先 + `notify.py`
  （SQLite 微库 `database/notify.db` + 落库失败降级 `logs/notify_fallback.jsonl` +
  事实流 `logs/notify.log`）。⚠️ **遗留**：`stage="l2compile"` 的**页组现场**（编译输入
  快照）留到 M2，与三段式状态机一起做 —— 单有 stage 字符串没有现场是没用的
- **M2**（进行中，2026-09-29）：✅ `OfflineRunner`（plan 侧，含"先落现场再抛"）与两个
  一致性补丁（`scrub_text` 清洗、`extra` 透传 `thinking` / `reasoning_effort`）已完成；
  **待做**：compile 阶段 runner 化、三段式 `run()` 状态机、回读时的 `repair_model` 兜底、
  `wiki.py` 对 `PendingError` / `offline_submitted` 的处理（**绝不能进降级分支**）
- **M3**：CLI `batch-status` / `batch-submit` / `batch-import` / `batch-cancel`
- **M6**：回读冒烟任务 `batch_04cc53ea36324c8598abbffa`，验证真实执行与结果形状
