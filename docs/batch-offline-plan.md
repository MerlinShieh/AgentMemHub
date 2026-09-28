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
| `create(input_file_id)` | `POST /v1/batches` | **只对明确 4xx 重试**；超时→不自动重试→通知人工确认（无 list 接口，防重复建任务重复计费） |
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
| 网络超时（不确定建成没建成） | 无 list 接口可查 | 当轮该域**回退实时** + `notify(warning)` 附"**请到控制台确认任务是否已建成，若建成请手动取消**"（重复计费风险靠人工确认兜底） |
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

## 10. 触发器调整

- `wiki.triggers.enabled: false`：**只关自动判定**（定时槽 09:00/12:00/18:00、
  脏数据阈值、当日首写三条路径全部短路，返回明确"已关闭"状态，**且不写状态文件**，
  避免重开时误判）；**手动触发保留**（CLI `--action trigger` / MCP probe 照常）。
- 阈值 10 → 30：自动触发关闭期间不生效；将来配合离线模式重开后，攒批提交
  本来就该更高阈值（离线单价再低也架不住高频小批），30 合理，甚至可再高。
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
| 3 | **建任务重试导致重复建任务、重复计费** | 只对明确 4xx 重试；超时→通知+人工确认（§6 / §7） |
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
| **M0** 探测+客户端 | 连通性探测脚本（域名两种拼写 / key / 模型名小写 / completion_window 可选值 / 错误响应形状）+ `MiMoBatchClient` 五方法 + 沙箱熔断 | 新建 `docs/modules/batch-offline.md`（厂商契约 + 探测结论）；`docs/logging.md` 事件字典登记 `batch_*` 五事件骨架 | 探测结论回填 §15；mock 单测全绿；沙箱下直连抛 | ☐ 未开始 |
| **M1** 标记与决策扩展 | `BatchJob` 扩展（input/output/error_file_id、provider_status、expire_at、stage=l2compile、voided）+ `decide()` 终局判定 + 作废/取消回池 + `notify.py` 最小闭环 | 新建 `docs/logging.md`（三条事实流 + 标准头 + **事件字典**）；`batch-offline.md` 补状态机图；字典登记 `notify_write` | 单测覆盖状态机全转移；notify 落库 / 降级路径均有测试 | ☐ 未开始 |
| **M2** 离线运行器 | `OfflineRunner`（plan）+ **compile 阶段 runner 化** + 三段式 `run()` 状态机 + `update_wiki(mode=)` 入口抽离 + §11 三补丁 | `batch-offline.md` 补三段式流水线图与 runner 契约；字典补 `offline_submitted` / `batch_consumed` / `batch_voided` / `batch_expired` | fake uploader 下三段全链路跑通；PendingError 不触发降级（复现测试） | ☐ 未开始 |
| **M3** CLI 四动作 | `batch-status / batch-submit / batch-import / batch-cancel` | `docs/API.md` §五 CLI 表 + 根 README「命令行」表；`batch-offline.md` 收录操作剧本（§14） | 手动剧本（§14）全程可操作；状态展示 request_counts | ☐ 未开始 |
| **M4** 开关与配置接口 | `wiki.update_mode` 生效 + 模式切换作废逻辑 + override 层 + HTTP 配置/通知接口 + 审计通知 | `docs/API.md` §三 HTTP 分组行（含 Swagger `summary` + Query `description`）与 §六 现状表；新建 `docs/modules/config.md` 的 override 小节；字典补 `config_change` | 白名单外键拒绝（400）；主 yaml 注释零损失；切换后数据回池验证 | ☐ 未开始 |
| **M5** 触发器调整 | `triggers.enabled` 开关 + 阈值 30 + 27 个触发器测试适配 | 新建 `docs/modules/wiki.md`（两级编译 + 触发器 + 失败兜底）；配置项表更新 | 自动判定短路且不写状态文件；手动触发不受影响 | ☐ 未开始 |
| **M6** 收尾 | fake uploader e2e + 真凭据小额实测（1 个域）+ 全量回归 + `check_config_keys` + 文档一致性复查 | 根 README「模块地图」；AGENTS.md「相关文档」；`llm-batch-offline.md` 升级为实施记录；`API.md` §六 复查；`logging.md` 字典覆盖全部新事件 | 全量测试全绿；实测域产物与实时路 diff 可解释；§17.5 文档门槛全过 | ☐ 未开始 |

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

## 15. 待验证项（M0 探测清单）

- [ ] 域名：`batch-apicn.xiaomimimo.com` vs `batch-api-cn.xiaomimimo.com`（文档模板是后者，配置值是前者，以实测为准）
- [ ] ApiKey 有效性（鉴权失败的响应形状 → 错误分类依据）
- [ ] 模型名 `mimo-v2.6-flash`（小写、无前缀）在批量端点可用
- [ ] `completion_window` 能否传非 24h 值（48h / 72h / 168h）
- [ ] 上传 → 建任务 → 取消 → 下载 全链路（1~2 条请求，最小费用）
- [ ] 余额不足 / 文件过期 的错误响应形状
- [ ] 状态字段实际取值集合（文档表格 6 种 vs 字段说明 9 种，finalizing / expired 未列全）

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

- [ ] `docs/modules/batch-offline.md`、`docs/modules/wiki.md`、`docs/modules/config.md`(override 小节)
      —— A2 随附三份，6 小节齐全
- [ ] `docs/logging.md` 事件字典覆盖 A2 全部新事件
- [ ] `docs/API.md`：新端点均有表格行 + Swagger `summary` / Query `description`；§六 现状表按实情更新
- [ ] 根 `README.md`「模块地图」可一跳到达任一模块文档（含 Phase 2 占位条目）
- [ ] `AGENTS.md`「相关文档」补齐；`llm-batch-offline.md` 升级为实施记录
- [ ] 提交前跑 `scripts/sensitive_scan.py`（新文档含真实 base_url / 路径，需确认无密钥）

---

## 附录 A：厂商契约速查（MiMo Batch API，2026-09-27 通读官方文档）

- Base URL：`https://batch-api-${region}.xiaomimimo.com/v1`（region 取值文档未给，控制台批量推理页获取）
- 调用链：`POST /v1/files`（multipart purpose=batch）→ `POST /v1/batches`
  {input_file_id, endpoint:"/v1/chat/completions", completion_window, name} →
  `GET /v1/batches/{id}` → `GET /v1/files/{output_file_id|error_file_id}/content`；
  另有 `POST /v1/batches/{id}/cancel`（异步：cancelling → cancelled，完成后
  output/error file 非空——已完成部分会产出且已计费）。
- 结果行形状（与 `llm_batch._content_of()` / `load_batch_results()` 兼容）：
  `{"custom_id":..,"response":{"status_code":200,"body":{"choices":[{"message":{"content":".."}}]}},"error":null}`
- 状态：validating / in_progress / completed / failed / cancelling / cancelled
  （+ finalizing / expired，文档未列全；completed/failed 在表格中误写为大写，判定一律 lower()）。
- 限制：单文件 ≤128MB 且只能一个 endpoint（body 里 model 可逐条不同）；custom_id
  文件内唯一字符串；数据保留 30 天；闲时调度，提交后可能长时间不启动。
