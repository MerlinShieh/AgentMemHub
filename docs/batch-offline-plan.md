# 离线批处理接入计划（A2 · MiMo Batch API）

> 状态：**计划已定稿，未开工**
> 分支：`feat/batch-offline`（自 `feat/external-ingest` @ `79c9581` 分出）
> 日期：2026-09-27 · 拍板人：Merlin（决策记录见 §1）
> 前置文档：[`docs/llm-batch-offline.md`](llm-batch-offline.md)（接缝设计与已就位的纯逻辑层）
> 厂商契约：小米 MiMo 批量推理官方文档（2026-09-27 已通读，要点摘录见附录 A）

---

## 1. 决策记录（已拍板，不再开放讨论）

| # | 议题 | 决策 | 备注 |
|---|---|---|---|
| D1 | **离线化范围** | L2 的**细分（plan）+ 页面编译（compile）**走离线批量；**归域（cluster）与 L1 逐会话编译保持实时** | 离线路径使用**独立新配置段**（`llm_batch:`，新 endpoint / key / model），不与 Command Code 复用；compile 离线化的理由：其调用量远大于 plan，只做 plan 省不了多少钱 |
| D2 | **取消语义** | **整批作废**：取消 / 模式切换 / 终局失败后，任务关联数据全部回到待更新池 | 放弃"取消前先消费已完成部分"——已完成部分已计费但按此决策直接丢弃（重跑会再花一次，成本已知且接受） |
| D3 | **MCP 配置热更** | **方案 B**：配置接口改动落盘（override 层）+ 通知提示"重启 MCP 后对其生效" | CLI 路径天然即时（每次新进程重读）；不做运行中 reload（避免 Config 单例引用方式的连锁重构） |
| D4 | **completion_window** | **可配置，先 24h 观察** | 预期夜间算力空闲可完成；M0 仍实测能否传更大值（文档自相矛盾：正文说 1–14 天、字段说明说固定 24h），将来资源紧张反复 `expired` 时再调大 |
| D5 | **提交失败回退** | **当轮回退实时** + 通过通知接口发 `warning` | 保产物质量优先；细分见 §7（明确 4xx 与网络超时两种情形的差别） |
| D6 | **通知分级** | `debug / info / warning / error` 四档 × `batch / wiki / trigger / config` 四类来源 | SQLite 微库，不用队列（单机应用，队列过度设计） |

---

## 2. 背景与目标

**背景**：wiki 二级编译是本项目 LLM 开销的大头，且大域细分曾是超时重灾区（已分批缓解）。
小米 MiMo 批量推理价格为实时的 **50%**，闲时调度、OpenAI Batch 协议兼容——适合
"不需要当场拿结果"的 wiki 更新。接缝（`stage_plan(runner=)`、`llm_batch.py`
编解码 / `BatchJob` / `decide()`）已在 `fix/wiki-product-defects` 线落地并测试。

**目标**：wiki 更新可通过配置在**实时 / 离线批量**两种模式间切换；离线路径的
上传、建任务、查询、取消、下载全部经项目自有客户端中转（异常兜底 + 日志落档）；
配套全局通知系统与配置修改接口。

**非目标（本期不做）**：
- L1 逐会话编译、L2 归域（cluster）的离线化 —— 保持实时；
- 面板前端 UI（配置与通知先到 HTTP + CLI，前端后续）；
- 通知的聚合 / 限流（先观察量）；
- MCP 运行中热 reload（D3 方案 B）。

---

## 3. 范围与流水线形态

一次 L2 更新的 LLM 调用全景与归属：

```
L1 逐会话编译   每会话 1 次分组 + N 次组编译           → 实时
L2 ① 归域       全量页面 → 域划分（全局视角、单次调用）  → 实时（拆批破坏归域质量）
L2 ② 域内细分   每域每批 1 次                          → 离线批量（接缝已就位）
L2 ③ 页面编译   每页 1 次（调用次数最多的大头）         → 离线批量（新增 runner 化）
```

### 3.1 三段式运行状态机（核心）

② 依赖 ① 的输出，③ 依赖 ② 的输出 —— 离线模式下一次 L2 更新被切成**三次运行**：

```
运行 1（提交轮）   归域（实时）→ 生成细分请求 → 上传+建任务 → 抛 PendingError
                   → run() 提前返回 offline_submitted（不刷 manifest、不跑 linkfix）
运行 2（回读轮 A） batch-import：下载细分结果 → 生成编译请求 → 上传+建任务 → 返回
运行 3（回读轮 B） batch-import：下载编译结果 → 写页 → linkfix → Obsidian 验收
                   → 刷 manifest_l2 → 清理 BatchJob
```

- **跨运行现场由 `BatchJob` 承载**（它本来就是为此设计的："已交给厂商"的标记 + 现场快照）：
  - `stage="l2plan"`：items（custom_id↔域/批号）+ members（域成员快照，已有）；
  - `stage="l2compile"`（新）：items（`l2page|域|页号`）+ members 扩展为**页组编译输入**
    （组成员正文、title_hint、all_titles）；
  - 一个 `BatchJob` 一次提交（一个 batch_id），一次更新可有多个 job（按提交分片）。
- **周期预期**：三段运行叠上闲时调度，一轮 L2 更新可能 **1~2 天**。wiki 本来就滞后
  一个更新周期，接受；`batch-status` 必须能回答"哪些域在等、等了多久、进度几比几"
  （直接展示厂商 `request_counts`）。
- `PendingError` 的传播边界：提交轮 `run()` 捕获后走 `offline_submitted` 分支，
  **绝不进入失败降级**（否则会把"还没算"误判成"算不出来"，触发成员各自成页）。

### 3.2 防重复提交

已在飞 `BatchJob` 覆盖的域不重复提交（`BatchJob.domains()` 判重，已有实现）。
手动 `trigger` 在离线模式下：有在飞任务 → 只处理新增脏域；无 → 正常提交。

---

## 4. 配置设计

新增**独立顶层段，零继承**（不继承顶层 `llm:`，也不被 `wiki.llm` / `wiki.l2.llm`
继承 —— 四形态里最简单的"内置默认 < yaml"）：

```yaml
llm_batch:                          # ← 全局批量推理配置（新接口，不与 Command Code 复用）
  endpoint: https://batch-apicn.xiaomimimo.com/v1   # ⚠️ 域名拼写 M0 实测（batch-api-cn?）
  api_key: <只写本地 agentmemhub.yaml，example 占位，不入库>
  model: mimo-v2.6-flash            # jsonl "model" 字段取值来源（小写、不带厂商前缀）
  completion_window: 24h            # D4：可配置，先 24h
  timeout: 60                       # 单次 HTTP 超时（秒）
  max_retries: 3

wiki:
  update_mode: realtime             # D1 开关：realtime | offline
  triggers:
    enabled: false                  # 需求：自动触发总开关（本期默认关）
  # dirty_memories: 10 → 30         # 阈值调高（在触发器配置段就地改）
  l2:
    plan_batch_max: 40              # 已有
    plan_wait_deadline_s: 0         # 0=不限时（batch-import 的 decide 用）
```

- **键名** `wiki.update_mode`（不是设计稿的 `wiki.l2.plan_mode`）：按"wiki 更新走实时
  还是离线"的用户语义命名，将来扩到 L1 不用改键。Phase 1 作用域 = L2 的 plan+compile。
- ⚠️ **假开关纪律**：每个新键必须有真实消费点，**键与消费代码同一里程碑落地**，
  不提前埋键；`check_config_keys.py` + `test_config_audit.py` 守护。
- 模型名映射显式化：现有 `xiaomi/mimo-v2.6-flash`（Command Code 带前缀）**不能**
  直接塞进 jsonl；`llm_batch.model` 独立配 `mimo-v2.6-flash`。
- 通知与配置修改的相关配置见 §8 / §9。

---

## 5. 数据标记与模式切换（状态机核心）

两种模式下"待更新"用**两套互补表示**，切换靠"作废显式标记、隐式差集自动接管"：

```
实时模式：待更新 = manifest 差集（隐式推导）
          L1: manifest_l1 没有的输入；L2: manifest_l2 对出的脏域
离线模式：待更新 = BatchJob（显式标记：batch_id + items + members 快照）
          提交轮不刷 manifest → 差集保持不变 → 双轨并存不冲突
```

| 场景 | 动作 |
|---|---|
| `offline → realtime` | 对每个在飞 BatchJob 调 `cancel`（**异步**：发起后再 retrieve 确认状态翻转，不盲等）→ 本地标记 `voided` → manifest 未刷过 → **数据自动回到隐式待更新池**，下次触发走实时。不搬数据 |
| `realtime → offline` | 无需处理在飞（实时任务完成即刷 manifest）；下次触发走离线提交 |
| 手动取消（CLI `batch-cancel`） | 同上整批作废（D2） |
| 终局失败（failed / expired / cancelled） | 同上作废回池；`expired`（30 天文件保留期 / completion_window 超时）单独识别 |
| 模式切换 vs 进行中的更新 | **不打断**。切换只影响下一次触发——更新动辄几十分钟，中途换模式会产出混合状态的页面 |
| 回读消费轮 | 下载 output/error 两份文件 → `decide()`（CONSUME / PARTIAL / WAIT / FALLBACK）→ 消费已到结果 → 编译写页 → **此刻才刷 manifest_l2** → 清 BatchJob |

`decide()` 扩展：新增**厂商终局状态**入参 —— failed / expired / cancelled 时
**不等 deadline 直接 FALLBACK/PARTIAL**（否则终局失败会被当成"还在等"干等）。

---

## 6. 批量客户端（`agentmemhub/batch_client.py` · MiMoBatchClient）

五个动作全部走它，**项目里不允许出现裸的批量 HTTP 调用**：

| 方法 | 端点 | 重试 / 兜底 |
|---|---|---|
| `upload(jsonl)` | `POST /v1/files`（multipart，purpose=batch） | 可安全重传（旧 file 等自然过期） |
| `create(input_file_id)` | `POST /v1/batches` | **绝不自动重试**（不幂等：重试 = 建出第二个任务 = 重复计费）；网络类失败抛 `BatchUnknownStateError`，由 `client.find_batch()` 按 `name` / `input_file_id` **对账**（2026-09-29 实测：厂商有列表接口，文档没写） |
| `retrieve(batch_id)` | `GET /v1/batches/{id}` | 只读，可积极重试 |
| `cancel(batch_id)` | `POST /v1/batches/{id}/cancel` | 幂等（已 cancelled 再调按成功处理） |
| `download(file_id)` | `GET /v1/files/{id}/content` | 只读，可重试 |

- **错误分类**：网络瞬态 / 鉴权失败 / 余额不足 / 文件过期 / 未知。全部落
  `logs/wiki.log`（新事件 `batch_upload / create / retrieve / cancel / download`，
  标准日志头五项）。
- **状态大小写**：厂商文档把 completed/failed 误写为大写，判定一律 `.lower()`。
- **测试熔断**：`AGENTMEMHUB_TEST_SANDBOX=1` 下 `MiMoBatchClient` 任何直连**硬抛**
  （复用 `wiki_triggers._targets()` 的熔断模式）—— 绝不允许测试真发请求、真计费。
- **HTTP 基建重构（需求 4.1 的实际范围）**：从 `llm.py` 抽共享层
  （opener / 超时 / 重试 / 错误分类），`llm.py` 的 `_post` 内部改调基建但
  **对外签名与错误类型零变化**（蒸馏/评分是生产关键路径）；wiki 更新入口抽离
  统一函数 `update_wiki(mode=...)`，distill 钩子 / MCP probe / CLI 三处调用点收拢。
  其余全部是新增，不动存量。
- **文件大小防御**：全量请求合成一个 JSONL 预计仅几 MB（远低于 128MB 上限），
  仍按 100MB 阈值分片提交（每片一个 BatchJob），防极端增长。

---

## 7. 提交失败回退（D5 细则）

| 情形 | 判定 | 动作 |
|---|---|---|
| 明确 4xx（鉴权失败 / 余额不足 / 参数错） | 任务**确定没建成** | 当轮该域**回退实时**（SyncRunner）+ `notify(warning, batch, ...)` |
| 网络超时（不确定建成没建成） | `client.find_batch()` **对账**（实测有 `GET /v1/batches`） | 先对账：**已建成** → 按在飞任务处理（不重复提交）；**确实没建成** → 当轮该域**回退实时** + `notify(warning)`；**对账本身也失败**（网络仍不通）→ 不改本地状态、下轮重试，并 `notify(warning)` 提示人工确认 |
| 回退后产物语义 | — | 与纯实时模式完全一致（批量故障期间离线模式自动降级为实时，**会产生实时价格费用**，通知里写明） |

---

## 8. 通知系统（`agentmemhub/notify.py`）

- **存储**：SQLite 微库 `database/notify.db` 单表
  （id / ts / level / category / title / body / meta / read）。
- **解耦**：调用方只 `import notify` 调 `notify(level, category, title, body, meta)`；
  落库失败降级写 `logs/notify_fallback.jsonl`，**通知写入永不阻塞主流程**。
- **分级**（D6）：`debug / info / warning / error` × `batch / wiki / trigger / config`。
- **查询**：`GET /api/notifications?level=&unread=` + 已读标记；CLI `agentmemhub notify list`。
- **Phase 1 写入点**：批量提交 / 回读 / 终局失败、wiki 更新 abort、触发器失败、
  **配置修改审计**（谁在何时把哪个键从什么改成什么 —— 配置接口上线后必须有）。
- **保留**：默认 30 天清理（复用 logs 的 keep_days 思路）。先不聚合不限流。
- **测试纪律**：conftest 沙箱夹具把 notify.db 指向 tmp。

---

## 9. 配置接口（override 覆盖层）

- **写回方案**：API / CLI 改的键写独立 `agentmemhub.override.yaml`，加载时
  **override > 主 yaml**。主文件的中文注释与手工编辑**永不受损**（PyYAML
  round-trip 会丢全部注释，不可接受）；override 文件可查可清空。
- **白名单**（Phase 1）：`wiki.update_mode`、`wiki.triggers.enabled`、
  `llm_batch.*`（含 completion_window）、`dirty_memories`。
  模型 / 维度 / 路径等高危键**不在白名单**（防误改导致索引错乱）。
  override 只允许改**已有键**，不新增键 → 天然不产生僵尸键。
- **生效语义**（D3 方案 B）：CLI 天然即时；MCP 常驻进程改动落盘后由通知提示
  "重启 MCP 后对其生效"。面板展示生效值时读合并结果。
- **接口**：`GET /api/config`（生效值）、`PUT /api/config`（白名单内）；
  CLI `agentmemhub config get/set <path> <value>`。鉴权复用面板 password。

---

## 10. 触发器调整（2026-09-28 修正：**不新增开关**）

**勘察结论（读代码核实，推翻了本计划初稿的设计）**：现有开关已有三层，再加
`wiki.triggers.enabled` 会变成第四个语义相近的开关 —— 必须禁止。

| 现有开关 | 真实消费点 | 实际作用 |
|---|---|---|
| `wiki.update.enabled`（可被状态文件 `overrides.enabled` 覆盖） | `wiki_triggers.evaluate()` 第一行 | **真正的触发器总闸**：置 false → 自动链（`on_memories_written`）与**不带 `--force`** 的手动触发一律短路 |
| `wiki.enabled` | 仅 `wiki_triggers._configured()`，而它**只被 `status()` 调用** | ⚠️ **假开关**：关掉它照样触发（`_targets()` 不检查它）。其 docstring 自称"真正的总开关"，与实际不符 |
| `wiki.update.schedule` / `dirty_memories` / `first_write_daily` | `evaluate()` 的三条规则 | 规则级细调 |

**所以需求"关闭自动触发、保留手动"用现有键即可实现**：
`wiki.update.enabled: false` 拦住自动链；手动走
`agentmemhub wiki --action trigger --force`（`_run_locked()` 在 force 下直接给出
`should_run=True`，绕过 `evaluate()`）。
**注意**：不带 `--force` 的手动触发同样会被拦住 —— 这是现状语义，本期不改
（改它要动 27 个触发器测试和既有语义，收益不足）。

- 阈值 10 → 30：改 `wiki.update.dirty_memories`（或状态文件 overrides 段）。
  自动触发关闭期间它不生效；将来配合离线模式重开时，攒批提交本来就该更高阈值，
  30 合理，甚至可再高。
- **顺带处置 `wiki.enabled`（小任务，纳入 M5，二选一）**：① 在 `_targets()` 里
  接通它（成为真总闸）；② 删掉该键并同步 `agentmemhub.yaml.example` 与文档。
  **倾向 ②**：不再维护语义重叠的开关。⚠️ **此项待 Merlin 拍板**（见 §18）。
- 测试适配：`test_wiki_triggers.py`（27 个）在"默认关闭"的新默认下显式开启再跑。

---

## 11. 两路一致性的三处补丁（离线路径必须补齐，否则产物与实时路不一致）

| # | 缺口 | 后果 | 补法 |
|---|---|---|---|
| ① | `BatchRequest.body()` 只发 model/messages/max_tokens/temperature，**漏顶层 `thinking` / `reasoning_effort`** | MiMo 忽略该字段，但 DeepSeek 不忽略 → 换模型静默变行为 + 思维链白花钱 | body() 按 `llm_batch` 配置补发（批量段显式配，不从顶层 llm 继承，D1） |
| ② | 离线回读只做 `extract_json()`，**没有实时路的 `repair_model` 兜底** | 实时能救回的 \`\`\`json 围栏，离线路上会白白退化成"成员各自成页" | 回读后对"有内容但 JSON 坏"的条目走 repair_model（回本地实时修复，费用低） |
| ③ | `BatchRequest` 存原始 system/user，`batch_to_jsonl()` **绕过 `scrub_text()`** | 零宽/控制字符外发，审核与解析异常概率升高 | `batch_to_jsonl()` 编码时统一过 `scrub_text()` |

---

## 12. 风险清单（按严重度）

| # | 风险 | 缓解 |
|---|---|---|
| 1 | **真实付费操作**：建任务即可能计费，测试逃逸=真金白银 | 双保险：沙箱熔断硬抛 + e2e 全用 fake uploader；真凭据只在 M0 / M6 各一次小额实测（1 个域、几条请求） |
| 2 | **PendingError 被误当失败** → 触发降级污染产物 | 提交轮 `run()` 状态机单独处理（`offline_submitted` 提前返回，不进降级分支）；专门写复现测试（红→绿） |
| 3 | **建任务重试导致重复建任务、重复计费** | `create()` 硬性 `retries=1`（绝不自动重试）；不确定时用 `find_batch()` 对账（§6 / §7）；对账也失败则**不猜测**、下轮重试 |
| 4 | **三段运行 + 闲时调度 → 更新周期 1~2 天** | §3.1 已摊开并接受；`batch-status` 展示 request_counts 进度 |
| 5 | `llm.py` HTTP 基建重构**动生产关键路径** | 对外签名 / 错误类型零变化；全量回归 + 一次真实 wiki 更新灰度对比 |
| 6 | **配置热更语义不清** → "改了没生效"排查地狱 | §9 明确语义并写入 docs/API.md；通知带生效范围提示 |
| 7 | 24h 窗口 × 闲时调度 → 资源紧张时反复 `expired` 白跑 | M0 实测可传更大值；D4 先 24h 观察，必要时调大 |
| 8 | **密钥治理** | key 只进本地 yaml（gitignore），example 占位，`sensitive_scan.py` 过审；⚠️ 该 key 已出现于对话记录，**M0 验证后到小米控制台轮换** |
| 9 | 30 天文件保留期 → `batch_state.json` 里的 batch_id 失效 | retrieve 404/过期 → BatchJob 标记失效 → 数据回待更新池（与作废同路径） |
| 10 | **数据出域**：批量上传=记忆内容发往小米批量服务（闲时执行、保留 30 天） | 与实时经 Command Code 路由同级，无新增暴露面；在文档明示 |
| 11 | **文档欠账**：实现完成后再补文档必然失真（本项目已有 26 个模块零模块文档的先例） | 文档与日志**随里程碑同步写**（§17.5）；每个里程碑的验收含文档项，不为收尾补账 |

---

## 13. 里程碑与追踪

> 「随附文档与日志」列是**验收的一部分**，不是可选项 —— 该里程碑合入时文档未同步即视为未完成。

| 里程碑 | 内容 | 随附文档与日志 | 验收标准 | 状态 |
|---|---|---|---|---|
| **M0** 探测+客户端 | ✅ **已完成（2026-09-29）**：真凭据全链路探测（域名 / key / 窗口上限 / 错误形状 / **列表接口**）+ `MiMoBatchClient` 六动作 + `find_batch()` 对账 + 沙箱熔断 | `docs/modules/batch-offline.md`（含实测契约表）；`docs/logging.md` 登记 6 个批量事件（从"待实现"转正） | 探测结论回填 §15 ✅；22 条 mock 测试全绿；全量 **859 passed / 1 skipped**；僵尸键审计 0 | ✅ 完成 |
| **M1** 标记与决策扩展 | ✅ **已完成（2026-09-29）**：`BatchJob` 补齐厂商侧字段（input/output/error_file_id、provider_status、expire_at、voided）+ `status` → `results_state` 改名（与厂商状态消歧）+ `is_terminal()` + `void()` 回池 + `decide()` **终局优先** + `notify.py`（SQLite 微库 + 降级兜底 + `logs/notify.log` 事实流） | `docs/logging.md`：新增 `notify.log` 流、`notify_write` 事件转正（§2 / §5.6 / §8）；`batch-offline.md` 补任务现场字段与三个判定 | 20 条新测试（通知 11 + 状态机 9）；全量 **879 passed / 1 skipped** | ✅ 完成（`stage=l2compile` 的**页组现场**随 M2） |
| **M2** 离线运行器 | ✅ **已完成（2026-10-01）**：✅ M2a `OfflineRunner` + ✅ M2c 两补丁 + ✅ 提交轮 + ✅ 回读 + ✅ 落盘轮（`assemble_pages()` 三条规则与实时路逐条对应、`finalize_outputs()` **两条路共用**、`write_offline_stage()`）+ ✅ **状态机编排**（`update(offline_client=, offline_runner=)` 按在飞任务推进一段：无在飞→提交轮、结果到位→交下一段/落盘；`_rebuild_l2_manifest()` 落盘后按磁盘产物刷基线）。**已实测两轮端到端**：① 细分 11 条 + 编译 113 条 → 落盘 3 域 182 页；② 默认只离线编译 250 条 → 落盘 5 域 398 页，**链接验收 0 未解析**。**待做**：`repair_model` 兜底（低优先） | `batch-offline.md` §3.2/§3.3/§3.4 + `logging.md` + `API.md` §6.6 + README | 新测试 +39；全量 **922 passed / 1 skipped** | ✅ 完成 |
| **M3** CLI | ✅ **已完成（2026-10-01）**：`batch-status`（只读：厂商状态 / 进度 / 已等多久 / `decide()` 建议）/ `batch-run`（**推进一段**，状态机自判）/ `batch-cancel`（取消作废、域回池）/ `batch-resubmit`（**平台侧 `validation_failed` 后用原输入文件重提**，不重跑 L1/归域 —— 平台自己的错误文案就是"请尝试重新创建任务"）。⚠️ **原计划的 `batch-submit` / `batch-import` 合并进 `batch-run`**：状态机已能判断该推进哪一段，拆成两个动作只会让调用方多记一套规则 | `docs/API.md` §6.6 + `batch-offline.md` §3.4 | 四个动作**均已真实执行过**（含一次 `batch-resubmit` 从失败中恢复） | ✅ 完成 |
| **M4** 开关与配置接口 | 🔄 **部分完成（2026-10-01）**：✅ **HTTP 接口**（`/api/wiki/batch/status` 只读 · `/run` 长任务 · `/cancel` · `/resubmit` · `/checks` 收割流水，挂在**面板服务**上、不新增常驻进程）✅ **自动收割**（事件驱动：MCP 启动 / 写入记忆 / 蒸馏收尾 / 任意 CLI 命令，见 `task_queue.harvest()`）✅ **安全规则**（队列非空 ⇒ 不提交实时更新）；**未做**：`wiki.update_mode` 偏好开关（显式选实时/离线）、模式切换作废逻辑、override 层、审计通知 `config_change` | `docs/API.md` §6.6；新建 `agentmemhub/batch_ops.py`（CLI/HTTP 共用的动作层）与 `agentmemhub/task_queue.py`（通道无关的收割基础设施） | 五个接口有 7 个测试（含"只读不写状态"）；收割有节流/锁/退避测试 | 🔄 部分 |
| **M5** 触发器调整 | `triggers.enabled` 开关 + 阈值 30 + 27 个触发器测试适配 | 新建 `docs/modules/wiki.md`（两级编译 + 触发器 + 失败兜底）；配置项表更新 | 自动判定短路且不写状态文件；手动触发不受影响 | ☐ 未开始 |
| **M6** 收尾 | ✅ **已完成（2026-10-04）**：✅ 真凭据实测（**四轮**：细分 11 + 编译 113 → 182 页；250 条 → 398 页；258 条 → 378 页）✅ 全量回归 **994 passed / 1 skipped** ✅ `check_config_keys`（僵尸键 0）✅ `sensitive_scan` ✅ **链接验收 0 未解析** ✅ **假 uploader e2e** 固化成 `tests/test_offline_e2e.py`（3 条，全程不发真请求）✅ **`repair_model` 兜底**（`repair_broken_results()` + `wiki.batch.max_repair`）✅ `llm-batch-offline.md` 升级为实施记录 ✅ 文档一致性（README 模块地图 / AGENTS.md / `config.md` §7 / `logging.md` 事件字典） | 根 README「模块地图」；AGENTS.md「相关文档」；`llm-batch-offline.md` 升级为实施记录；`API.md` §六 复查；`logging.md` 字典覆盖全部新事件 | 全量测试全绿 ✓；实测产物与实时路 diff 可解释 ✓；§17.5 文档门槛**已过**（文件级 override 层仍是计划，已在 `config.md` §7 标明） | ✅ 完成 |

工作量估算：合计 **5~6.5 天**（M0 是硬前置——域名 / key / 窗口三个疑点全靠它落地；
文档与日志随里程碑写，不额外占收尾工期）。

---

## 14. 手动操作剧本（离线模式的用户旅程）

```
1. 配置：agentmemhub config set wiki.update_mode offline
   （或直接改 agentmemhub.yaml / override 文件）
2. 手动触发：agentmemhub wiki --action trigger
   → 归域（实时）→ 细分批量任务已提交，返回 batch_id 与域清单
3. 等待（闲时调度，可能数小时~一天）：agentmemhub wiki --action batch-status
   → 展示各 job 状态 / request_counts 进度 / 已等多久 / decide() 建议
4. 回读：agentmemhub wiki --action batch-import
   → 细分结果落场 → 编译批量任务已提交（第二次等待）
5. 再回读：agentmemhub wiki --action batch-import
   → 写页 → linkfix → Obsidian 验收（未解析链接 = 0）→ 刷 manifest → 清 BatchJob
随时可：batch-cancel（整批作废回待更新池）/ 切回 realtime（在飞任务全部取消作废）
```

---

## 15. 待验证项（M0 探测清单 —— **2026-09-29 已全部完成**）

| 项 | 实测结论 |
|---|---|
| 域名 | ✅ **`batch-api-cn.xiaomimimo.com`**。配置里给的 `batch-apicn.…` **DNS 解析失败**（少一个连字符，是笔误）；正确域名解析到 220.181.52.24 等 3 个 IP |
| ApiKey 有效性 | ✅ 有效。错误体统一 `{"error":{"message","type","code"}}`：无效 key → 401 `invalid_api_key`、对象不存在 → 404 `batch_not_found` —— **这就是错误分类的依据** |
| 模型名 `mimo-v2.6-flash` | ⏳ **待冒烟任务回读**：已留 `batch_04cc53ea36324c8598abbffa`（1 条 `max_tokens=32` 请求，不取消）等闲时执行，M6 回读验证真实结果形状 |
| `completion_window` | ✅ 支持大窗口：`168h` 与 **`336h`（14 天）**均被接受，且返回的 `expires_at - created_at` 与之精确一致 → 文档"当前固定 24h"**是错的** |
| 全链路 | ✅ 上传（200）→ 建任务（**201**）→ 取消（`cancelling`）→ 4 秒后查得 `cancelled`；取消后 output/error 均为空、`counts=0/0/0` |
| 余额不足 / 文件过期 的错误形状 | 部分：`not_found` 系列形状已知（`batch_not_found`）；**余额不足未实测**（要真把余额耗尽，不做），按 `code` / `message` 关键字识别 |
| 状态取值集合 | ✅ 文档未列的 **`created`** 确实存在（建任务后的初始状态）；`completed` / `failed` 在文档表格里被误写成首字母大写，判定一律 `lower()` |
| **文档没写、探测挖到的** | ✅ **`GET /v1/batches` 与 `GET /v1/files` 列表接口存在** → 建任务超时后的**对账**靠它（`client.find_batch()`）；原计划里"超时只能人工去控制台确认"的结论**就此作废**（§6 / §7 / §12 已同步） |

---

## 16. 运维前提

- 小米开放平台账号已**实名认证**、有**现金余额**（Token Plan 额度不抵扣批量）；
- 批量只支持 `mimo-v2.6-pro` / `mimo-v2.6-flash`，价格为实时 50%，只计成功请求；
- 不支持任务内重试 / 续跑（失败重建任务）、不支持流式；
- 输入与结果文件保留 30 天，过期自动清理。

---

## 17. 文档与日志的落实（本次一并纳入交付）

> 依据：Merlin 2026-09-27 的要求 ——「日志和文档的落实，包括接口文档和各个功能模块的 README 文档」。

### 17.1 现状盘点（2026-09-27 实测）

| 项 | 现状 | 结论 |
|---|---|---|
| 项目自有文档 | 根 `README.md`（全面，面向使用者）+ `docs/` 15 份（架构 / 设计 / 接口）+ `scripts/e2e/README.md` | 顶层文档**不欠账** |
| **模块文档** | `agentmemhub/` **26 个顶层模块 + 3 个子目录（`rag` / `web` / `adapters`）零模块文档**；`scripts/` 16 个脚本同样为零 | ⚠️ **主要缺口**（`memOS/` 下那份 40+ README 是 vendored 上游，不算我们的） |
| 接口文档 | `docs/API.md`（333 行）已有完整骨架：三层接口 / 通用约定 / 长任务约定 / HTTP 分组 / MCP / CLI / **§七 维护约定** | 新接口**按既有约定追加**，不另起体系 |
| 日志 | `logs.standardize()` 已落地标准头五项 + 三条事实流分工（AGENTS.md 有纪律） | ⚠️ **事件字典缺失**：每个 `event` 的字段含义散在代码里，排查时靠读代码 |

### 17.2 模块文档策略：按职责域，不逐文件

逐文件 README（26 + 3 + 16 ≈ 45 份）**必然腐化**，且多数模块是内部实现细节。
改为按**职责域**分 9 份，放 `docs/modules/`：

| 文档 | 覆盖 | 优先级 |
|---|---|---|
| `batch-offline.md` | `llm_batch.py` / `batch_client.py`（新）/ `notify.py`（新） | **A2 随附** |
| `wiki.md` | `wiki.py` / `wiki_index.py` / `wiki_manifest.py` / `wiki_pending.py` / `wiki_triggers.py` / `scripts/wiki_*.py` / `scripts/obsidian.py` | **A2 随附**（A2 主战场） |
| `config.md` | `config.py` / `scripts/check_config_keys.py` / `sync_llm_*.py`（A2 先落 override 小节） | **A2 部分** |
| `observability.md` | `logs.py` / `failures.py` / `notify.py` / `snapshot.py` | Phase 2（事件字典先行，见 §17.4） |
| `memory-engine.md` | `agentmemhub/rag/**`（search / ingest / memstore / embedder / ext / eval / runtime / source） | Phase 2 |
| `panel.md` | `agentmemhub/web/**`（app / aggregates / tasks） | Phase 2 |
| `ingest-adapters.md` | `agentmemhub/adapters/**` / `store.py` / `models.py` / `watermarks.py` | Phase 2 |
| `distill-llm.md` | `distill.py` / `scoring.py` / `llm.py` / `export.py` | Phase 2 |
| `interfaces.md` | `mcp_server.py` / `cli.py` / `console.py` / `health.py` | Phase 2 |

**每份固定 6 小节（缺一不可，防退化成"介绍性废话"）**：

1. **职责边界** —— 做什么、**明确不做什么**
2. **对外入口** —— 谁调用它（函数 / CLI / HTTP / MCP）
3. **数据流与不变量** —— 读写哪些表与文件、哪些不能破
4. **配置项** —— 该域消费的键 → 默认值 → 生效形态（继承还是覆盖）
5. **调试与排障** —— 看哪条日志、跑哪个命令、常见故障与对策
6. **测试在哪** —— 对应 `tests/` 文件 + 改动时的回归清单

**索引**：根 `README.md` 增加「模块地图」小节（模块 / 目录 → 一句话职责 → 文档链接），
让任一模块**一跳可达**。

### 17.3 接口文档落实（`docs/API.md`，按 §七 既有约定）

| 类型 | 要追加的内容 |
|---|---|
| **HTTP** | `GET /api/notifications`（`level` / `unread` / 分页）、`POST /api/notifications/read`；`GET /api/config`（生效值 + 来源标注）、`PUT /api/config`（白名单）。每个端点补 `summary` + 每个 Query 参数 `description`（Swagger 自解释）→ 本文分组表格行 → §六 现状表 |
| **CLI** | `wiki --action batch-status / batch-submit / batch-import / batch-cancel`、`notify list`、`config get\|set` → 本文 §五 + 根 README「命令行」表 |
| **长任务约定** | **新增形态**：离线模式把长任务从"后台跑"变成"**跨运行的异步提交 / 回读**"。提交轮返回 `offline_submitted`（**不是失败、不阻塞、不占单并发锁**）必须写进 §二「长任务约定」 |
| **错误语义** | 批量特有错误（鉴权失败 / 余额不足 / 文件过期 / 任务终局失败 / 白名单外键 400）的对外表达与用户可执行动作 |

### 17.4 日志落实

- **新建 `docs/logging.md`**：三条事实流分工 + 标准头五项 + **事件字典表**
  （`event` / 所属流 / 关键字段 / 含义 / 排查用途）+ **排查剧本**
  （"跑到哪了"、"花了多少"、"等了多久"、"哪些失败了"）。
- **A2 新增事件必须登记**：
  - `logs/wiki.log`：`batch_upload` / `batch_create` / `batch_retrieve` / `batch_cancel` / `batch_download`
  - 流水线：`offline_submitted` / `batch_consumed` / `batch_voided` / `batch_expired`
  - 通知与配置：`notify_write`（含降级路径）、`config_change`（审计：谁 / 何时 / 哪个键 / 旧值 → 新值）
- **必须能被日志回答的 5 个问题**（验收）：① 这轮 wiki 跑到哪了、断在哪
  ② 花了多少 token / 多少钱 ③ 离线任务等了多久、进度几比几、卡在哪个域
  ④ 哪些域失败了、为什么 ⑤ 配置被谁改成了什么。
- 长任务落 `logs/wiki.log`（AGENTS.md 既有纪律）；批量任务额外落 `batch_state.json` 现场，
  二者职责不重叠（日志=事实流水，state=可续跑的现场）。

### 17.5 文档门槛（纳入 M6 验收）

**已完成（2026-09-28，按"按职责域 + 现在就写"的决策落地）**：

- [x] `docs/modules/wiki.md` —— 365 行：两级编译全链、12 个 CLI action、12 条不变量、
      18 个配置键、15 类日志事件、10 个测试文件；含 7 处 `> A2 改动点：`
- [x] `docs/modules/config.md` —— 271 行：四种继承形态 + 真实例子、僵尸键审计流程、
      `rag.retrieval` 死键清单、override 层预留小节
- [x] `docs/logging.md` —— 350 行：**23 个真实事件**的字典（wiki.log 21 + memory.log 2）
      + mcp/web/cli 的非 `event` 形态 + 5 类排查剧本
- [x] 根 `README.md`「模块地图」（9 个职责域 → 文档 → 状态）
- [x] `AGENTS.md`「相关文档」补 `docs/modules/*.md` 与 `docs/logging.md`

**待做（随 A2 里程碑，不再单独排期）**：

- [x] `docs/modules/batch-offline.md` —— ✅ **M0 已写**（实测契约表 + 六动作 + 对账 + 排障 + 测试清单）
- [x] `docs/logging.md` **7 个事件已转正**：6 个批量事件（M0；`batch_list` 是探测发现列表接口后补的第 6 个）+ `notify_write`（M1）；待 M2 / M4 再补 5 个（`offline_submitted` / `batch_consumed` / `batch_voided` / `batch_expired` / `config_change`）
- [ ] `docs/API.md`：新端点表格行 + Swagger `summary` / Query `description`；§六 现状表更新（M3 / M4）
- [ ] `docs/modules/config.md` override 小节由"计划中"改为已实现（M4）
- [ ] `llm-batch-offline.md` 升级为实施记录（M6）
- [ ] 提交前跑 `scripts/sensitive_scan.py`（新文档含真实 base_url 与路径，需确认无密钥）

---

## 18. 勘察发现的待修缺陷（2026-09-28，全部经代码 / 实盘核实）

> 本轮为写模块文档而通读代码，**顺带查出 9 个既有缺陷**。它们不属于 A2 的功能范围，
> 但其中 D-1 会直接决定 A2 离线模式的可靠性，D-2 决定"关自动触发"怎么配，故一并登记。

| # | 缺陷 | 证据 | 影响 | 处置建议 |
|---|---|---|---|---|
| **D-1** | **L2 域目录换入失败会中止整轮** | 2026-09-28 21:00 `l2_aborted: PermissionError WinError 5`（`04-AgentMemHub-记忆系统` → `.bak_…`）；`.tmp_04-…` 残留 153 文件；`manifest_l2` 停在 09-26 | 一次文件句柄占用 = 整轮 L2 白跑（88 分钟）；**离线模式多轮提交会更频繁** | ✅ **已修（2026-09-29，Merlin 重选为"现在就修"）**：新增 `_write_domain()` 收口异常、`_write_domain_outputs()` 作为逐域写盘唯一出口；换入失败与页数崩塌走同一条跳过通路（`reason="swap_failed"`：保留旧产物 + 进失败清单 + 进 `skipped_domains` → `_l2_skip_ids()` 剔出新基线 → 下轮自动重试），失败路径也不再残留 `.tmp_<域>`。3 个新测试 + 红→绿验证，全量 **837 passed / 1 skipped** |
| **D-2** | `wiki.enabled` 是假开关 | `_configured()` 只被 `status()` 调用；`_targets()` 不检查它 | 关掉它照样触发，而 docstring 自称"真正的总开关" | 见 §10 二选一（接通 / 删除），**待拍板** |
| **D-3** | `logs/web.log` / `cli.log` **缺 `module`/`line`** | 实盘 `cli.log` 121 行、含 `module` 的行 **0**；同时 `wiki.log` 有 | 标准头纪律在这两条流从未生效；`tests/test_logs_format.py` 的断言**假绿** | 修 `logs.record()` 路径 + 让测试真正走该路径（红→绿） |
| **D-4** | `record()` 的 `msg` 绕开 `_clip` | 实盘含 `\n`、600 字终态文本的条目 | 与"msg ≤200 字"口径不符，破坏单行可读性 | 在 `record()` 内补 `_clip` |
| **D-5** | `rag.retrieval` **5 个键配了不生效** | `candidate_k` / `threshold_floor` 被档位表 `RECALL_LEVELS` 遮蔽（`search.py:721` 读 profile 而非 settings）；`search_max_hits` / `max_per_conversation` / `rrf_k` 同类 | "配了不生效"，且 `check_config_keys.py` 一律报 OK | 要么接线、要么从 yaml 与文档移除；审计脚本扩展到 `agentmemhub/rag/config.py` |
| **D-6** | 触发器锁/状态文件用**相对路径** | `wiki_triggers._LOCK_FILE` / `STATE_FILE` = `Path("logs")/…` | CWD 不同 → 锁与状态写到别处 → **并发更新可能没被锁住**（单飞行失效） | 改用 `logs.log_dir()` 派生 |
| **D-7** | 记忆删除与快照**零审计** | `wiki_pending.drop()/restore()`、`snapshot.py` 全文无日志调用 | "哪条记忆被谁删了"查不到 | 接 `audit_memory` / `audit_wiki` |
| **D-8** | `web/app.py:_logged_task` docstring 与实际不符 | 实现走 `logs.task_log_path()`，docstring 写 `<data_dir>/tasks/` | 误导排查方向 | 改 docstring |
| **D-9** | `logs/tasks/*.log` 实盘仍是旧格式 `[HH:MM:SS]` | 格式变更前的历史遗留文件 | 跨天排查定位不到日期 | 随 `keep_days` 自然淘汰，无需专项 |

**优先级（供将来排期参考）**：D-1（产物可靠性，且与 A2 耦合）> D-2（配置语义，直接决定需求 7 的配法）
> D-3 / D-4（日志纪律 + **假绿测试**）> D-6（并发安全）> 其余。

> **2026-09-29 更新（Merlin 重选，取代 09-28 的处置）**：
> · **D-1 改为"现在就修"，已完成**（见上表）；其余 8 项仍**仅登记**、不在 A2 范围内。
> · 库的处置改为「**只关自动触发、暂不追平**」：已用
>   `wiki_triggers.set_overrides({"enabled": False})` 写进 `logs/wiki_trigger_state.json`
>   的 `overrides`（**最高优先级、每次判定都重读 → 常驻的 MCP 进程无需重启即生效**；
>   改 yaml 则要重启，见 D3 方案 B）。恢复自动触发只需把它改回 true 或删掉该项。
> · ⚠️ **副作用（务必记住）**：`evaluate()` 被 `enabled=false` 短路后，
>   **不带 `--force` 的手动触发同样会被拦住** —— 手动补跑必须用
>   `agentmemhub wiki --action trigger --force`。
> · 当前库状态：`manifest_l2` 仍停在 2026-09-26、`dirty` 累积中（**未追平**，
>   这是明知的选择）；那两个域页的占用**已在 09-29 解除**（独占打开测试 60/60 通过），
>   下一轮更新可正常换入。

**关于 D-1 的现场**：被占用的两个文件是
`04-AgentMemHub-记忆系统/001-仓库治理与交付约定-v2-0-0-发布-分支冻结与中文化输出规范.md` 与
`…/020-测试与并发的假故障-测试污染真实环境与批量任务通用坑.md`（均不在新一轮 `.tmp` 产物内
—— 它们在新一轮被合并 / 淘汰，而 Obsidian 仍打开着它们）。同目录的 `03-API-调用-协议与网关`
60 个文件可全部独占打开、换入成功，对照组坐实了"句柄占用"这一根因。

---

---

## 附录 A：厂商契约速查（MiMo Batch API）

> 来源：2026-09-27 通读官方文档 + **2026-09-29 真凭据探测校正**（差异处标 ⚠️）。
> 现场记录（含探测命令与返回）见 [modules/batch-offline.md](modules/batch-offline.md) §2。

- Base URL：⚠️ **实测 `https://batch-api-cn.xiaomimimo.com/v1`** —— region 段是 `-cn`。
  文档只给模板 `${region}`，写成 `batch-apicn.…`（少一个连字符）会 **DNS 解析失败**
- 调用链：`POST /v1/files`（multipart purpose=batch）→ `POST /v1/batches`
  {input_file_id, endpoint:"/v1/chat/completions", completion_window, name} →
  `GET /v1/batches/{id}` → `GET /v1/files/{output_file_id|error_file_id}/content`；
  另有 `POST /v1/batches/{id}/cancel`（异步：cancelling → cancelled；**取消后
  output/error 文件为空** —— 只有已有完成请求时才产出，且那部分照常计费）
- ⚠️ **文档未写、实测存在**：`GET /v1/batches?limit=N` 与 `GET /v1/files?purpose=batch`
  两个**列表接口**（返回 `{object:"list", data:[…]}`，files 另带 `first_id`/`last_id`/`has_more`）
- ⚠️ 建任务成功码是 **HTTP 201**（上传是 200）；建任务后的初始状态是 **`created`**
- ⚠️ `completion_window` 实测可传 **`168h` / `336h`**；文档字段说明那句"当前固定 24h"是错的
- 结果行形状（与 `llm_batch._content_of()` / `load_batch_results()` 兼容）：
  `{"custom_id":..,"response":{"status_code":200,"body":{"choices":[{"message":{"content":".."}}]}},"error":null}`
- 状态：`created` / `validating` / `in_progress` / `finalizing` / `completed` / `failed` /
  `expired` / `cancelling` / `cancelled`（文档表格只列 6 种；`completed`/`failed` 被误写成
  首字母大写，判定一律 `lower()`）
- 限制：单文件 ≤128MB 且只能一个 endpoint（body 里 model 可逐条不同）；custom_id
  文件内唯一字符串；数据保留 30 天；闲时调度，提交后可能长时间不启动。
