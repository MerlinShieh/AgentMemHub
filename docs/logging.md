> 覆盖：日志体系与事件字典 · 最后核对：2026-09-27 · 纪律原文见 [AGENTS.md](../AGENTS.md)

# 日志与可观测性

日志纪律的**原文**在 [AGENTS.md](../AGENTS.md)「日志纪律」段（何时写、标准头五项、三流分工不许破）；
本文补的是那份纪律里**没有**的东西：每条日志到底写了哪些字段、每个 `event` 是什么意思、
出问题时该看哪个文件用哪条命令。加新事件必须登记到本文 §5。

## 1. 设计原则

**调用方只写业务字段，标准头由写入口统一补齐。**
`logs.standardize()` 是所有 JSONL 事实流的唯一字段补齐点：调用方传 `{event, ...业务字段}`，
`ts / level / module / line / msg` 五项由它注入（调用方显式给了就以调用方为准，幂等不覆盖）。
理由写在 `agentmemhub/logs.py` 的注释里：写入点散落在 60+ 处
（`mcp_server` / `web/app` / `distill` / `wiki_*` / `scripts/*`），靠"每个人都记得写全"必然漂移 ——
实测 `wiki.log` 早期条目连 `ts` 都没有、`memory.log` 出过一次全 `1970`。

三条事实流分开而不是合并，因为**读者和保留策略不同**，混在一起会让"查一条记忆的来龙去脉"变成考古：

| 流 | 视角 | 回答的问题 |
|---|---|---|
| `logs/mcp.log` | 协议层 | 谁调了什么工具、参数是什么、耗时多少、成功没有 |
| `logs/memory.log` | 数据层 | 哪条记忆被写/读/删、内容是什么、从哪条路径来 |
| `logs/wiki.log` | 流水线层 | 编译跑到哪了、花了多少、哪些失败了、上次断在哪 |

另外三条独立通道：`logs/web.log` / `logs/cli.log`（看板与控制台的操作记录，`logs.record()`）、
`logs/tasks/<id>.log`（看板后台任务的逐行完整输出）、以及应用内 `logging` 的文本日志
（`agentmemhub.log` / `asrag*.log`）。

**全程旁路**：所有日志写入都包在 `try/except` 里静默失败 —— 日志设施绝不能因为磁盘或权限问题把业务写挂
（`_append_line` / `_maybe_archive` / `setup_app_logging` 任一步失败都跳过，下次写入再试）。

## 2. 日志文件与事实流

| 文件 | 谁写（写入口） | 记录什么 | 典型用途 |
|---|---|---|---|
| `logs/mcp.log` | MCP 唯一分发点 `mcp_server._audited()` → `logs.audit_mcp()`（stdio 与 Streamable HTTP 共用） | 协议层：`tool` / `args` / `elapsed_ms` / `ok` / `error`，**每次调用两条**（`phase=call` 与 `phase=result`，用 `call_id` 关联） | 查"Agent 调了什么、失败没有"；`scripts/mcp_log.py` |
| `logs/memory.log` | `distill._audit_memory_write()` → `logs.audit_memory()`，挂在**落表处**（`save_memories` / `save_direct_memory` / 回填） | 数据层：哪条记忆被写/读、**内容全文**、`source`/`type`/`result`，来源路径由 `logs.memory_path()` contextvar 标记 | 查"某条记忆的来龙去脉"；`python -m agentmemhub memory-log` |
| `logs/wiki.log` | `logs.audit_wiki()`：两个 wiki 脚本的 `_wlog()`、`wiki._audit()`、`wiki_triggers._audit()`、`batch_client._audit()` | 流水线层：阶段 / 目标 / 每次 LLM 调用 / token 用量 / 成败 / 触发器判定 / **批量动作** | 长任务"跑到哪、花多少、断在哪" |
| `logs/notify.log` | `logs.audit_notify()`，由 `notify.notify()` 的写入点调用 | 通知事实流：`level` / `category` / `title`（通知**本身**落 SQLite `database/notify.db`，这里留的是"谁在何时发了什么"） | 查"发过哪些通知、落库成没成功"；⚠️ 通知的等级写 `warning`，进本流时映射成 `warn` |
| `logs/web.log` | `logs.record(dest="web")`（`web/app.py` 各处） | 看板操作：任务提交 / 开始 / 完成 / 失败、记忆打分、记忆加权、索引清理失败 | 面板留痕；`GET /api/logs` 与内存环形缓冲（`_MAX_RING = 500`） |
| `logs/cli.log` | `logs.record(dest="cli")`：`cli._cli_log()`、`console._action_log()`，以及 `mcp_server` 的两条 `memory_save` 失败提示 | 终端 / 控制台操作的结果摘要 | 查"命令行侧干了什么" |
| `logs/agentmemhub.log` | 命名空间根 logger `agentmemhub` 的兜底 handler（`logs.setup_app_logging()`，由 `_append_line` 顺带挂上） | 应用内 `logging.getLogger("agentmemhub.*")` 的文本日志：LLM 瞬态重试 / 格式修复、蒸馏失败与判重、`store` 提示 | 看应用内部发生了什么；标准库 `RotatingFileHandler`，**5 MB × 3 份** |
| `logs/asrag.log` | 命名空间根 logger `asrag` 的兜底 handler | 没有专属文件的 `asrag.*` logger（如 `asrag.bridge` 的检索记录） | 桥接 / 召回侧文本日志 |
| `logs/asrag-<component>.log` | `rag.logkit.get_logger(component, log_dir)`，并设 `propagate = False` | `ingest` / `eval` / `search` 等组件的文本日志 | 组件级隔离（这些记录不会再进 `asrag.log`） |
| `logs/engine.log` | `memos_daemon._log_file()` | MemOS **回退引擎** daemon 的输出 | 只在走 vendored 回退路径时才有内容 |
| `logs/tasks/<job_id>.log` | `logs.append_task_line(job_id, line)`（`web/app.py:_logged_task()` 逐行转写） | 看板后台任务的**完整输出**（纯文本，非 JSONL） | 页面关掉 / 进程中断后追溯 |
| `logs/wiki_trigger_state.json` | `wiki_triggers.STATE_FILE` | 触发状态（`last_update_at` / `update_started_at` / `last_error` / `fired`）；`wiki_triggers._LOCK_FILE` 是单飞行锁（两者都是**相对路径** `Path("logs")/…`，跟随进程 CWD，不走 `logs.log_dir()`） | 不是日志，但排查触发器必须一起看 |

实盘核对（2026-09-27）：`logs/` 下另有 `asrag-eval.log` / `asrag-ingest.log` / `asrag-search.log`；
**没有** `asrag-bridge.log`（`asrag.bridge` 没走 `logkit`，它的记录在 `asrag.log`）；**没有** `logs/archive/`（尚未滚动过）。

## 3. 标准头五项

`logs.standardize(entry, *, msg="", level="info", depth=3)` 补齐的五个字段：

| 键 | 类型 | 来源 | 含义 |
|---|---|---|---|
| `ts` | float（epoch 秒） | `out.setdefault("ts", round(time.time(), 3))`；调用方显式给了不覆盖 | 事件发生时刻。`audit_*` 的调用点多数显式传 `ts=round(time.time(), 3)`（毫秒 3 位小数）；`record()` 传的是 `time.time()` 原值（微秒级，实测 web.log 形如 `1789656947.075757`） |
| `level` | str，`debug` / `info` / `warn` / `error`（小写，`logs.LEVELS`） | 显式 `level` > `ok=False` / `error` > 错误事件名 > `info` | 等级；非法值回落 `info`（实测 `"VERBOSE"` → `info`） |
| `module` | str，`__name__` | `caller_site(depth)` 用 `sys._getframe` 取**业务调用点**（不是日志模块自己） | 哪个模块打的点 |
| `line` | int | 同上 | 业务调用点的行号 |
| `msg` | str | `audit_*` 各自渲染（`_mcp_msg` / `_memory_msg` / `_wiki_msg`）或调用方显式 `msg` / `message` | **单行可读摘要**，正文仍在业务字段里 |

`depth=3` 是同口径约定：`caller_site(0)` ← `standardize(1)` ← `audit_xxx`/`record(2)` ← 业务代码(3)。
取栈失败一律返回 `("", 0)` —— 日志设施绝不能因为取栈失败把业务写挂。

**level 推断规则**（`logs._level_of()`，按顺序，先命中先用）：

1. 显式 `level` 且在 `LEVELS` 里 → 用它；
2. `entry["ok"] is False`，或 `error` 非空 → `error`；
3. `event` 命中该流的错误事件表，或以 `_error` / `_fail` 结尾 → `error`；
4. 其余 → `info`。

wiki 流的错误事件表是 `_WIKI_ERROR_EVENTS = ("page_fail", "run_aborted", "domain_write_skipped")`。
`mcp.log` / `memory.log` 没有额外错误事件表，只有第 2 条（`ok=False` / `error`）生效。

**`msg` 的截断**（`_clip()`）：换行替换为空格、去首尾空白；超过 `MSG_MAX = 200` 时截断并追加 `…`。
但 `_clip` 只在**作为 `msg` 参数传入** `standardize` 时才生效 —— 也就是 `audit_mcp` / `audit_memory` / `audit_wiki`
（它们都算好摘要再传参）。`record()` 是直接把 `msg` 放进 entry 的，**既不截断也不去换行**：
实盘 `web.log` 里有带 `\n` 且超过 200 字（末尾接 600 字终态文本）的条目。

（待核实：`AGENTS.md` 的纪律把 `web.log` / `cli.log` 与三条 JSONL 流并列，并说 `msg` ≤ 200 字；
`record()` 的实现绕开了 `_clip`，两处口径不一致 —— 需要确认是有意为之还是遗漏。）

### 文本与 JSONL 两种形态的差异

| | JSONL 事实流（`mcp|memory|wiki|web|cli`.log） | 文本日志（`agentmemhub.log` / `asrag*.log`） | 任务日志（`logs/tasks/<id>.log`） |
|---|---|---|---|---|
| 格式 | 一行一个 JSON 对象 | `%(asctime)s %(levelname)-5s [%(name)s:%(lineno)d] %(message)s` | `YYYY-MM-DD HH:MM:SS INFO [module:line] 内容` |
| 字段来源 | `logs.standardize()` 注入标准头 | 标准库 `logging` 的 formatter | `logs.append_task_line()` 手工拼前缀（`caller_site(2)` 取调用点） |
| 等级 | `level` 字段 | `levelname`（`INFO` / `WARNING` / `ERROR`） | **固定 `INFO`**（逐行透传第三方/子进程文本，没有等级语义） |
| 行号 | `line` 字段 | `%(lineno)d`（Python 自己给的） | `[module:line]` 取的是**转写者**的调用点，不是子进程的 |
| 滚动 | `logs.rotate` 策略（见 §4） | 标准库 `RotatingFileHandler`：5 MB × 3 份（`agentmemhub` / `asrag` 根 handler；`logkit` 同参数） | 一次性文件，按 `keep_days` **直接删**（`prune_task_logs()`） |
| 读取 | `read_mcp_audit` / `read_memory_audit` / `read_wiki_audit`（坏行跳过，不让一行损坏拖垮整个查询） | 直接读文本 | `logs.task_log_tail(job_id, limit=40)` |

两处格式串**必须一致**（代码里各写了一次，注释也说明了这一点）：
`logs._TEXT_FORMAT`（`agentmemhub/logs.py`）与 `rag.logkit._FORMAT`（`agentmemhub/rag/logkit.py`）。

## 4. 滚动、归档与清理

配置读 `agentmemhub.yaml` 的 `logs` 段 → `config.DEFAULT_LOGS`（`agentmemhub/config.py`）。
生效策略由 `logs.rotate_policy(name)` 解析：`{**logs.rotate, **logs.files.<name>}`，单文件里没写的项回落全局。

| 配置键 | 默认值 | 行为 |
|---|---|---|
| `logs.rotate.max_mb` | `5` | `> 0` → 按**大小**滚动；**这是最高优先级**（配了它，`daily` 被忽略） |
| `logs.rotate.daily` | `false` | 无大小规则时为真 → 按**自然日首次写入**滚动（用文件 mtime 的日期判定，不需要额外状态文件） |
| `logs.rotate.keep_days` | `30` | 归档目录保留天数，超期删最旧；`0` = 不限。对 `tasks` 的含义是"按天直接删任务日志" |
| `logs.rotate.compress` | `false` | `false` → 归档保留**原始文件**（`path.rename(dest)` 直接搬走）；`true` → gzip 后删原文件（配置位已预留） |
| `logs.rotate.archive_dir` | `"archive"` | 归档根目录（相对日志目录） |
| `logs.files.<日志名>` | `{}` | 单文件覆盖，代码注释给的例子：`{wiki: {max_mb: 10}, tasks: {daily: true}}` |

**滚动规则二选一**（不会同时命中）：`max_mb > 0` → 按大小；否则 `daily` 为真 → 按自然日；两者都没配 → 不滚动。
时间维度的最低单位是**一个自然日（24 小时）**，没有小时级细粒度。

- 归档路径：`<日志目录>/<archive_dir>/<日志名>/<日志名>.<YYYYmmdd_HHMMSS_mmm>`（先按日志名分文件夹）。
  时间戳精确到**毫秒**：秒级精度下，清理腾出的旧名字会被下一次滚动重用，归档名会指向不同内容。
  同一毫秒内第二次滚动时追加 `_1` / `_2`…
- 清理：`_prune_archive(name, pol)` 按 `keep_days` 删归档目录里超期的文件；`keep_days <= 0` 直接返回。
- 触发时机：`_append_line()` 在**每次追加前**调 `_maybe_archive()`；文件不存在或大小为 0 时直接返回。
- 只有经 `_append_line(..., name=...)` 写的日志（`mcp` / `memory` / `wiki` / `web` / `cli`）参与 `logs.rotate`；
  任务日志走 `append_task_line()` → `_append_line()`（不传 `name`，所以不滚动，靠 `prune_task_logs()` 按天删）。
  `agentmemhub.log` / `asrag*.log` 走标准库 handler，**不受 `logs.rotate` 管辖**。

当前 `agentmemhub.yaml` **没有 `logs` 段**（实测：该文件里搜不到 `logs` / `rotate` / `max_mb` / `keep_days`），
因此全部走内置默认值。

## 5. 事件字典

只有 `wiki.log` 与 `memory.log` 的条目带 `event` 字段；`mcp.log` 用 `phase`，`web.log` / `cli.log` 用 `actor` + `msg`
（见 §5.3 / §5.4）。本节的事件名全部来自代码 grep，**不含仅测试文件里出现的值**
（`tests/test_logs_format.py` 用过 `event="note"`、`"x"`、`"a"`、`"b"`，生产代码没有写入点）。

### 5.1 `logs/wiki.log`（33 个事件：21 个流水线 + 12 个离线批处理）

| event | 所属流 | 关键字段 | 含义 | 排查用途 |
|---|---|---|---|---|
| `run_start` | wiki.log | `script`（`wiki_compile` / `wiki_aggregate`）、`out` / `src` / `db`、`sessions` / `skipped` / `todo`、`workers`、`thinking`；聚合侧还有 `stage` / `limit` / `domain` / `batch` | 一轮编译开始 | 确认"这轮到底起来了没有"、并发与处理范围 |
| `progress` | wiki.log | `script="wiki_compile"`、`target="<source>/<cid>"`、`message` | 会话内逐组进度（一个 101 条的会话要几十次 LLM 调用） | **唯一能区分"在正常推进"与"卡死了"的信号**（曾因此误判并杀掉两次正在正常工作的编译） |
| `session_done` | wiki.log | `script`、`source`、`cid`、`memories`、`pages`、`seconds` | 单会话编译成功（全量并发与单会话直调两处都写） | 逐会话进度与产出页数 |
| `session_fail` | wiki.log | `script`、`source`、`cid`、`memories`、`error` | 单会话编译失败（`level=error`） | 断在哪、为什么；失败项同时进 `failures.jsonl` 的 `stage='l1'` |
| `call` | wiki.log | `tag`（归域/细分/编译等）、`ok`、`attempt`、`seconds`、`error`、`usage`（`calls`/`prompt`/`completion`/`reasoning`/`cached_hit`/`cached_miss`/`seconds`） | 第二级**每一次** LLM 调用（成功与失败都记，`usage` 是相对本次调用的**增量**） | 把成本与重试归因到具体步骤；`attempt > 1` 说明脚本层重试兜住了"模型返回空内容"这类不触发内部重试的错误 |
| `domain_planned` | wiki.log | `name`、`pages` | 一个域细分完成、计划出 N 页 | 域级进度；并 `fl.resolve("l2-plan", name)` 销账 |
| `page_done` | wiki.log | `domain`、`title`、`merged`、`truncated` | 一个最终页编译完成 | 页面级进度；`merged` = 合并了几个源页 |
| `page_fail` | wiki.log | `domain`、`title`、`error` | 单个最终页编译失败（`level=error`，属错误事件表） | 哪些页没出来；同时进 `failures.jsonl` 的 `stage='l2-compile'` |
| `domain_write_skipped` | wiki.log | `domain`、`reason`（`page_collapse` / `swap_failed`）、`old_pages`+`new_pages`（崩塌时）、`error`（换入失败时） | 该域**跳过写盘、保留旧产物**（`level=error`，属错误事件表）：`page_collapse` = 页数崩塌保护；`swap_failed` = 整域换入失败（如该域文件被 Obsidian 占着，`rename` 报 WinError 5） | 两类都是**保护生效**而非产物损坏；该域不进新基线 → 下轮自动重试；同时进 `failures.jsonl` 的 `stage='l2-write'` |
| `run_aborted` | wiki.log | `script="wiki_aggregate"`、`stage="l2"`、`reason="degrade_ratio"`、`degraded`、`tasks` | 降级页占比超阈值 → **本轮完全不写盘**（`level=error`，属错误事件表） | 上游 LLM 大面积故障的判据；旧产物与旧基线都保留，下次写入自动重试 |
| `run_end` | wiki.log | `script`、`ok` / `failed`（L1）或 `domains`（L2）、`pages`、`skipped`、`seconds`、`call_seconds`、`usage` | 一轮结束汇总 | 花了多少 token / 钱；`seconds` 是**墙钟**（机器休眠会撑大）、`call_seconds` 是**累计调用耗时**（真实在等 LLM 的时间），两者并列防误判 |
| `manifest` | wiki.log | `script`、`stage`（`l1` / `l2`）、`n_inputs`、`domains`（L2） | 编译清单（`manifest_l1.json` / `manifest_l2.json`）已写 | 与 `align` 的 diff 锚点；没有它，"库改了什么"无从得知 |
| `link_check` | wiki.log | `ok`、`available`、`unresolved_targets`、`unresolved_links`、`error` | Obsidian 权威口径的产物链接验收 | `[[…]]` 是否点得开；`available=False` 表示**没法验**（不是失败），`ok=True` 才是唯一通过标准 |
| `page_index` | wiki.log | `ok`、`seconds`、`pages`、`inserted`、`updated`、`removed`、`skipped_empty`、`error` | 页面层投影进召回面（L2 知识页 → `units`） | 收尾那段长空窗期的判断依据（实测 466 页要投 4.5 分钟，此前误判成"卡住/标记没打上"） |
| `trigger_check` | wiki.log | `hook`（`write` / `manual`）、`should_run`、`reasons[]`、`due_slots[]`、`dirty`、`last_update_at` | 触发器判定结果，**不跑也记** | 回答"某次为什么触发 / 为什么没触发"，不必靠 `fired` 记录反推 |
| `trigger_skip` | wiki.log | `hook`、`message` | 单飞行锁没拿到（可能来自其它进程），本次跳过 | 区分"没触发"与"被跳过"（并发时"本次没跑"此前完全查不到） |
| `trigger_update_start` | wiki.log | `hook`、`reasons[]`、`dirty` | 判定通过、开始跑 `wiki.update()` | 补上从判定到第一次 `run_start` 之间的断档（update 内部还有快照、align、投影） |
| `trigger_update_end` | wiki.log | `hook`、`seconds`、`updated`、`dirty_sessions`、`l1_recompiled`、`l1_failed`、`l2_domains`、`l2_pages`、`l2_aborted`、`l2_skipped[]`、`page_index_seconds`、`hints[]` | 触发驱动的增量更新**成功**汇总 | 一次触发到底做了什么（此前只能去流水线日志里拼） |
| `trigger_update_error` | wiki.log | `hook`、`error`、`seconds`、`reasons[]` | `wiki.update()` 抛异常（`level=error`） | MCP 那条 daemon 路径会丢弃返回值，这条是它**唯一的**留痕 |
| `trigger_fired_skipped` | wiki.log | `hook`、`l1_failed`、`reasons[]` | L1 有会话编译失败 → 不算成功、不消耗当日触发额度 | "部分失败不算成功"的判据；下次写入自然重试 |
| `trigger_error` | wiki.log | `hook`、`error`；MCP 侧另有 `stage="mcp_probe"` | 触发器外层兜底异常 | 连 `on_memories_written()` 都没兜住的情况（正常结果由 `trigger_check` / `trigger_update_*` 覆盖） |

**批量任务事件**（6 个，2026-09-29 随 M0 落地，写点在 `agentmemhub/batch_client.py::_audit`）：

| event | 所属流 | 关键字段 | 含义 | 排查用途 |
|---|---|---|---|---|
| `batch_upload` | wiki.log | `file_id`、`bytes`、`seconds` | 批次输入 JSONL 已上传（文件 30 天后过期） | 上传是否成功、文件多大；`file_id` 是后续建任务的凭据 |
| `batch_create` | wiki.log | `batch_id`、`status`、`completion_window`、`n_requests`、`name`、`seconds` | 批量任务已建（成功码 **HTTP 201**，初始 `status=created`） | 花钱的起点；`n_requests` 应与 `batch_retrieve` 的 `counts.total` 一致 |
| `batch_retrieve` | wiki.log | `batch_id`、`status`、`counts{total,completed,failed}`、`seconds` | 查一次任务状态 | 回答"离线任务等了多久、进度几比几"（`counts` 直接来自厂商） |
| `batch_list` | wiki.log | `n`、`seconds` | 列最近任务（**建任务超时后的对账**） | 与 `client.find_batch()` 配合确认"到底建成没"，避免重复提交、重复计费 |
| `batch_cancel` | wiki.log | `batch_id`、`status`（`cancelling` / `cancelled` / `not_found`） | 取消任务（**异步**：先 `cancelling`，再查才 `cancelled`） | 作废回池的依据；`not_found` 表示任务已消失 |
| `batch_download` | wiki.log | `file_id`、`bytes`、`lines`、`seconds` | 下载成功 / 错误结果文件 | 回读了多少条；与 `batch_create` 的 `n_requests` 比对可发现缺失 |
| `offline_submitted` | wiki.log | `batch_id`、`stage`、`n_requests`、`n_domains`、`seconds`；**失败时**带 `ok=False` + `error` | 离线**提交轮**结束：活已交出去、结果还没回来（**不是失败、不阻塞**；本轮不刷 manifest、不跑 linkfix） | 这是"离线模式跑了一轮"的唯一锚点；`ok=False` 表示运行器没抛 `PendingError`（实现有问题，本轮其实什么都没提交） |
| `batch_import` | wiki.log | `n_jobs`、`n_consumed`、`n_voided`、`n_waited` | 一次**回读**的汇总（`agentmemhub/wiki_batch.import_results`） | 回答"离线任务等了多久、这轮取回来多少、作废了几个" |
| `batch_consumed` | wiki.log | `batch_id`、`file_id`、`kind`（`output`/`error`）、`n` | 从某个结果文件取到并解析了 N 条结果 | 成功文件与错误文件**是两份**，`kind` 区分；两边都不为空才算真的消费完 |
| `batch_voided` | wiki.log | `batch_id`、`stage`、`status`、`n_domains` | 任务被**作废**（终局：失败/过期/取消）→ 它覆盖的域回到待更新池 | 这是"这批要重跑"的依据；配合通知一起看（人会收到 `warning`） |
| `batch_expired` | wiki.log | `batch_id`、`stage`、`expire_at` | 任务**过期**（厂商侧 `expired`，等于没在窗口内跑完） | 反复出现说明 `completion_window` 太小或厂商资源紧张；调大窗口后重新提交 |
| `offline_written` | wiki.log | `domains`、`pages`、`degraded` | 离线**落盘轮**完成：编译结果已组装成页面并写盘 | 三段式的终点；`degraded > 0` 说明有页面走了 fail-open 拼接（结果缺失/失败）—— 不丢内容，但质量不如正常编译 |
| `batch_expire_warn` | wiki.log | `batch_id`、`stage`、`hours_left`、`expire_at`、`n_requests` | 任务**即将过期**（距 `completion_window` 不足阈值，默认 6 小时）→ 已发一条 `warning` 通知 | 提前几小时知道，才能决定"调大窗口重提"还是"先忙别的"。**每批只记一次**（靠 `BatchJob.warned_expire` 持久化去重，因为收割是高频事件） |
| `batch_repair` | wiki.log | `ok`、`custom_id`、`error` | 一条坏 JSON 走了 **`repair_model` 兜底**（离线回读时才做） | `ok=False` 说明修复也失败（原文仍保留，下轮可再修或重试）。为什么需要它：实时路一直有这一步，离线路此前缺，同一条输出会在两条路上质量不同 |
| `batch_repair_done` | wiki.log | `tried`、`repaired`、`max_repair` | 一次回读的**修复汇总**（只在本轮真有坏 JSON 时记） | `tried < repaired` 说明部分没救回来；`tried == max_repair` 说明**撞了单轮上限**（`wiki.batch.max_repair`），其余留给下轮 |

### 5.2 `logs/memory.log`（2 个事件）

| event | 所属流 | 关键字段 | 含义 | 排查用途 |
|---|---|---|---|---|
| `write` | memory.log | `memory_id`、`content_hash`、`source`、`conversation_id`、`type`、`confidence`、`topic`、`result`（`inserted` / `revived` / `duplicate`）、`slice_key`、`content`（**全文**）、`chars`、`path`；Agent 直写经 `audit_extra` 追加 `trace_id` / `importance` / `value` / `tags` / `actor` | 一条记忆**落表**（`distilled_memories`）的事实；`result` 区分新增、归档条目复活、同内容重复写入尝试 | 回答"当时到底写进去了什么"；`duplicate` 解释"为什么新增数为 0"；`slice_key`（直写为 `mcp:<hash>`）可溯源到 MCP 的 `trace_id` |
| `read` | memory.log | `path`（`mcp` / `http`）、`actor`（Agent 名 / `panel`）、`query`、`top`、`hits`、`origin_filter` / `kinds{}`、`elapsed_ms` | 一次记忆**检索**；读也留痕 | 区分"没写过"与"写了但没召回"——对比 `write` 的 `content` 与 `read` 的 `query` / `hits` |

`path` 由 `logs.memory_path()`（contextvar）标记，未标记时为 `unknown`。内容记**全文**是有意的：
蒸馏记忆受 `distill.CONTENT_MAX = 120` 的代码级硬截断（实测没有任何模型稳定遵守提示词，故由代码兜底），
而直写记忆没有这层限制（实测一条 698 字），
所以日志膨胀靠**单文件上限 + 轮转**兜底，而不是靠"记忆一定很短"的假设。

### 5.3 `logs/mcp.log`（无 `event` 字段，靠 `phase` 配对）

每次工具调用写**两条**，用 `call_id` 关联；只记结果的话，"发起了却没有返回"（超时、进程被杀）就完全无迹可寻。

| phase | 所属流 | 关键字段 | 含义 | 排查用途 |
|---|---|---|---|---|
| `call` | mcp.log | `kind="mcp"`、`call_id`、`tool`、`agent`、`ts`、`args` | 工具调用**发起** | 谁·何时·调了什么·参数是什么；Agent 的意图在 `args.note` |
| `result` | mcp.log | `call_id`、`tool`、`agent`、`ok`、`elapsed_ms`、`error`（失败）或 `digest{trace_id, hits}` + `result_head`（成功） | 工具调用**返回** | 成败与耗时；`digest` 从结果文本里轻量提取关键标识 |

`args` 经 `_audit_args()` 处理：`content` 换成 `content_chars` + `content_head`（正文全文已在记忆库，
凭 `trace_id` 可回查，日志里再存一份既冗余也让敏感内容二次落盘）；其余长度 > 300 的字符串截到 300 加 `…`。
`result_head` 上限 `_AUDIT_TEXT_CAP = 400`。

### 5.4 `logs/web.log` 与 `logs/cli.log`（无 `event` 字段）

这两份是 `logs.record(msg, level, actor, dest)` 写的"操作记录"，靠 `actor`（`web` / `cli` / `mcp`）与 `msg` 前缀区分。
按 `msg` 前缀归类（全部来自代码 grep）：

| 前缀 / 形态 | 流 | 关键字段 | 含义 |
|---|---|---|---|
| `提交任务：<名称>（id=<job_id>）` | web.log | `ts` / `level` / `msg` | 面板提交一个后台任务（采集、写入记忆、评分、清洗、排除、蒸馏、补向量、wiki 补跑、wiki 增量更新、回滚快照） |
| `开始：<名称>（id=<job_id>）` | web.log | 同上 | 任务真正进入执行 |
| `完成：<名称>（id=<job_id>）\n<终态文本末尾 600 字>` | web.log | 同上（**带换行、可能超 200 字**） | 任务成功结束，附结果摘要 |
| `失败：<名称>（id=<job_id>）→ <异常>` | web.log | `level="error"` | 任务抛异常（`_logged_task` 包装器写） |
| `记忆打分：trace=<id> <极性\|取消反馈>（状态式=…，幅度 …）` | web.log | `msg` | 面板点赞 / 点踩成功落库（`polarity` 与 `revoke` 都写在 `msg` 里） |
| `记忆加权：trace=<id> → 锁定 <value>\|清除手动设定` | web.log | `msg` | 面板手动加权 |
| `索引清理失败 <source>/<cid> turn=<turn_key>` | web.log | `level="error"` | 删除会话时清理索引失败 |
| `ingest sources=… full=… → N 会话, M 事件`、`memos push → imported=…, skipped=…`、`sync → …`、`clean(…)`、`rebuild(…) → …`、`score(<mode>) → …`、`distill(<mode>) → …`、`weight <ref> → value=… clear=…`、`vectorize … completed=…` | cli.log | `ts` / `level` / `actor="cli"` / `msg` | CLI 与控制台的 `_cli_log()` 结果摘要（失败时 `level="error"`，`distill` 未执行时 `level="warn"`） |
| `memory_save 写入未生效 id=… imported=0 skipped=…`、`memory_save 蒸馏表同步失败 id=…: …` | cli.log | `actor="mcp"`、`level="error"` | MCP 直写记忆的两条失败提示（写进 cli.log 而不是 web.log：不面板化） |
| `看板启动（控制台）→ PID … 端口 …`、`看板停止（控制台）→ …`、`clean（控制台）→ …` 等 | cli.log | `actor="cli"` | 交互式控制台（`console.py`）的动作 |

### 5.5 `logs/tasks/<job_id>.log`

无 `event` 字段，纯文本行：`YYYY-MM-DD HH:MM:SS INFO [module:line] 内容`。
`module` / `line` 取的是 `append_task_line()` 的**调用点**（`web/app.py` 的 `emit_full`），
所以它是"哪个组件在转写这些行"，不是子进程自己的位置。等级固定 `INFO`。

（待核实：现存 `logs/tasks/*.log` 实盘是旧格式 `[HH:MM:SS] 内容`，没有日期/等级/模块/行号 ——
应属格式变更前的历史遗留文件，只是按 `keep_days` 还没被清掉；未逐个核对每个文件的写入时刻。）

### 5.6 `logs/notify.log`（1 个事件）

| event | 所属流 | 关键字段 | 含义 | 排查用途 |
|---|---|---|---|---|
| `notify_write` | notify.log | `ok`（落库成功否）、`level`、`category`、`title` | 发了一条通知。**通知本身**落 SQLite `database/notify.db`（有已读态、可筛选）；这条是"谁在何时发了什么、落库成没成功"的**事实流水** | `ok=False` 说明走的是**降级路径**（落库失败，正文在 `logs/notify_fallback.jsonl`）——此时通知查不到，只能从降级文件里捞 |
| `notify_write`（`deduped: true`） | notify.log | 同上 + `deduped` | **去重命中**：同一 `(category, title)` 在 `notify.dedup_window_s`（默认 300 秒）内已发过 → 不再落库、不再推送 | 排查"为什么少了一条通知"时看它；返回 `True` 表示"这条已被代表"，**不是失败** |
| `notify_push` | notify.log | `ok`（推成功否）、`channel`（目前恒为 `pushplus`）、`level`、`category`、`title`、`error` | 把通知**推到微信**（Pushplus）的结果 | `ok=False` 时看 `error`：`disabled`/`no_token` 是没配，其余是网络或 token 问题。⚠️ **推送失败不影响落库** —— 通知仍在 `database/notify.db` 里查得到 |

⚠️ **等级写法两条流不同**：通知 API 用 `warning`（更标准），写进本流时由
`logs.audit_notify()` 映射成日志流的 `warn`。**映射必须在进 `standardize` 之前做** ——
它的规则是"`entry["level"]` 优先于传参的 `level`"，而 `warning` 不在日志流的
`LEVELS`（`debug/info/warn/error`）里，不先改写就会被静默回落成 `info`（实测踩过）。

## 6. 排查剧本

### ① 这轮 wiki 编译跑到哪了、断在哪

| 步骤 | 命令 / 位置 | 关注字段 |
|---|---|---|
| 这轮起来了没、范围多大 | `rg '"event": "run_start"' logs/wiki.log`（取最后一条） | `script`（区分 L1 `wiki_compile` 与 L2 `wiki_aggregate`）、`todo` / `sessions`、`workers` |
| 是否在推进 | `rg '"event": "progress"' logs/wiki.log` | `target`（会话）、`message`（组级进度）；**没有新 `progress` 才是真卡住** |
| 断在哪个会话 / 哪一页 | `rg '"event": "(session_fail\|page_fail)"' logs/wiki.log` | `source` / `cid` / `domain` / `title`、`error` |
| 是"保护生效"还是真坏了 | `rg '"event": "(run_aborted\|domain_write_skipped)"' logs/wiki.log` | `reason`（`degrade_ratio` / `page_collapse`）、`old_pages` → `new_pages`；这两类**不是产物损坏**，旧产物已保留 |
| 上一轮断在哪（跨进程） | `python -m agentmemhub wiki --action failures --out <产出目录> [--stage l1\|l2]`（读 `<out>/failures.jsonl`，不是 wiki.log） | `by_kind`、`fatal`、`needs_manual`；`quota` / `auth` / `model` 类**停止补跑**，先解决再重试 |
| 触发器这条路 | `logs/wiki.log` 里的链：`trigger_check` → `trigger_update_start` → `run_start` → `run_end` → `trigger_update_end`；再看 `python -m agentmemhub wiki --action triggers` | `reasons[]`、`due_slots[]`、`dirty`、`update_started_at`（"更新进行中"）、`page_index_seconds` |

注意 `run_end` 之后还有一段**页面层投影**（`page_index`，实测 4.5 分钟），期间 wiki.log 会短暂安静 —— 别当成卡死。

### ② 花了多少 token / 多少钱

| 步骤 | 位置 | 关注字段 |
|---|---|---|
| 每轮总量 | `logs/wiki.log` 的 `run_end` | `usage`：`calls` / `prompt` / `completion` / `reasoning` / `cached_hit` / `cached_miss` / `seconds` |
| 真实耗时 vs 墙钟 | 同上 | `call_seconds`（累计调用耗时）对比 `seconds`（墙钟）；实测墙钟曾显示 9.2 小时而实际约 1 小时（机器休眠） |
| 钱 | 同字段 | 由 `agentmemhub/llm.py` 的 `estimate_cost(usage)` 换算：默认 ¥2/M 未命中输入、¥0.04/M 缓存命中、¥8/M 输出（DeepSeek **高峰价**；空闲时段为半价，把三个单价除以 2）。`usage` 里没有价格字段，价格是**换算参数**不是记下来的事实 |
| 单步归因 | `logs/wiki.log` 的 `call` | `tag`（归域 / 细分 / 编译）、`usage`（本次调用的增量）、`attempt`、`ok` |
| 人可读报告 | 脚本控制台（`scripts/wiki_compile.py:_usage_report()`，`wiki_aggregate.py` 复用） | "用量统计"块：调用次数、调用耗时、输入 tokens（缓存命中/未命中）、输出 tokens（含思维链占比）、估算花费 |

`usage_*` 的累计口径在 `agentmemhub/llm.py`：`usage_snapshot()` 读、`usage_reset()` 清零（批量任务开始前）。
累计是**进程级**的，脚本退出即丢 —— 想知道历史某轮花了多少，只能回查那轮的 `run_end.usage`。

### ③ 哪些记忆被写入 / 读取过

| 步骤 | 命令 | 关注字段 |
|---|---|---|
| 全量时间线 | `python -m agentmemhub memory-log --limit 50` | 每行已渲染成 `[写·<path>] id=… source/type 内容摘要 ← result (N 字)` 或 `[读·<path>] q='…' 命中 N 条 {层级分布}` |
| 只看写入 / 读取 | 加 `--event write` / `--event read` | — |
| 只看某条路径 | 加 `--path mcp\|http\|distill\|cli\|unknown` | `path` 是协议层用 contextvar 标记的来源 |
| 关键词 | 加 `--grep <关键词>`，或 `--json` 后自己筛 | `content`、`query`、`trace_id`、`slice_key` |
| 溯源到协议层 | 用 `write.trace_id`（直写）/ `slice_key` 去 `logs/mcp.log` 找同一个 `call_id` 的 `phase=call` | Agent 的 `args.note`（意图） |

`write` 的 `result` 三态：`inserted`（新增）、`revived`（归档条目复活）、`duplicate`（同内容已存在，也算一条事实）。
`read` 的 `hits` 是**引擎实际返回条数**，`top` 是回给 Agent 的条数，两者不等是正常的（curate 只做相关度截断）。

### ④ 某次 MCP 工具调用为什么失败

| 步骤 | 命令 | 关注字段 |
|---|---|---|
| 全部失败与无响应 | `python scripts/mcp_log.py --failed` | 只有 `phase=call` 没有 `phase=result` → 渲染为"无响应 ← 只见 call 不见 result（超时/中断？）" |
| 按工具筛 | `--tool memory_save` | — |
| 看某次全貌 | `--call <call_id>`（支持前缀） | `phase=call` 的 `args`（含 `note`）与 `phase=result` 的 `error` / `elapsed_ms` |
| 搜参数或结果 | `--grep <关键词>` | 匹配整条记录（参数 / 结果 / 意图） |
| 机器可读 | `--json` | 按 `call_id` 配对后的对象；未配对的保留 `pending=True` |
| 覆盖日志路径 | `--path <文件或目录>` | 默认 `logs/mcp.log` |

旁证：`logs/cli.log` 里的 `memory_save 写入未生效 …` / `memory_save 蒸馏表同步失败 …`（都是 `actor=mcp`、`level=error`）；
`logs/agentmemhub.log` 里的 LLM 瞬态重试与格式修复（`[agentmemhub.llm:行号]`）。

### ⑤ 面板操作留下了什么记录

| 步骤 | 命令 / 位置 | 关注字段 |
|---|---|---|
| 近期操作 | `GET /api/logs?limit=100`（上限 500）→ `logs.recent()` | `actor`（`web` / `mcp` / `cli`）、`level`、`msg` |
| 直接读文件 | `logs/web.log`（JSONL） | 同上；`recent()` 优先读文件，文件为空才回落内存环形缓冲（`_MAX_RING = 500`） |
| 任务的完整输出 | `logs/tasks/<job_id>.log`（`<job_id>` 从 `提交任务：…（id=…）` 里取） | 每行 `YYYY-MM-DD HH:MM:SS INFO [模块:行号] 内容`；面板 `GET /api/admin/job` 只给当前任务的内存态 |
| 任务链条 | `提交任务：<名>（id=…）` → `开始：…` → 逐行落盘 → `完成：…` / `失败：…` | `完成` 那条的 `msg` 带终态文本末尾 600 字；`失败` 是 `level=error` |
| 记忆相关的面板动作 | `logs/memory.log` 的 `read` 且 `path=http` / `actor=panel` | 面板检索同样进事实流 |

`web.log` 与 `cli.log` 都**没有** `event` 字段，只能按 `msg` 前缀筛（`rg '提交任务' logs/web.log`）。
（待核实：实盘 `logs/web.log`（末次写入 2026-09-17）与 `logs/cli.log`（末次写入 2026-09-28 19:33）的**全部**条目都不含
`module` / `line` 两项，与 `record()` 的代码契约和守护测试 `tests/test_logs_format.py::test_cli与web记录带标准头`
不一致 —— 怀疑是常驻旧进程或另一份安装副本写入的；需要重启相关进程后再复验一次。）

## 7. 常用查询命令

项目内两种调用形态都有既有用例，按当前环境选一种：`uv run python -m agentmemhub …`（[AGENTS.md](../AGENTS.md) 的写法）
或 `.venv\Scripts\python.exe …`（`scripts/mcp_log.py` docstring 的写法）。

| 命令 | 参数 | 输出形态 |
|---|---|---|
| `python -m agentmemhub memory-log` | `--limit N`（默认 50，`0` = 全部）、`--event write\|read`、`--path mcp\|http\|distill\|cli\|unknown`、`--grep <关键词>`、`--json` | 逐行 `MM-DD HH:MM:SS [写·<path>] id=… …` / `[读·<path>] q='…' 命中 N 条 {…}`；末行 `共 N 条（<memory.log 路径>）`；无匹配时打印"无匹配的记忆操作记录（…）" |
| `python scripts/mcp_log.py` | `--limit N`（默认 20）、`--tool <名>`、`--failed`、`--grep <关键词>`、`--call <call_id 前缀>`、`--json`、`--path <文件或目录>` | 每条 5~6 行：`[MM-DD HH:MM:SS] <tool> <OK\|失败\|无响应> <耗时ms \| 错误>` + `意图:` + `参数:` + `标识:` + `结果:` + `call_id:`；空结果时给出"MCP server 仍是旧进程"之类的提示 |
| `python -m agentmemhub wiki --action failures` | `--out <产出目录>`（必需）、`--stage l1\|l2\|留空` | 失败清单路径、未解决条数、按原因分类（`quota` / `auth` / `model` 标"需人工处理"）、按阶段计数、致命类提示 |
| `python -m agentmemhub wiki --action align` | `--out <目录>`，或 `--l1 <L1> --l2 <L2>`（两级一起给才能看同步性）、`--db <索引库>` | 各级 manifest 的 `generated_at` / `n_inputs`、新增/变更/失去输入、脏会话清单、引用有效性与正向覆盖率、两级同步判词 |
| `python -m agentmemhub wiki --action triggers` | — | `wiki_triggers.status()` 的 JSON：规则配置、`fired` 记录、`update_started_at`（"更新进行中"）、目标目录 |
| `python -m agentmemhub wiki --action trigger` | `--force` | 手动跑一次判定（`--force` 绕过规则直接跑 update） |
| `python -m agentmemhub wiki --action retry` | `--out`、`--stage`、`--src`（L2 必需）、`--workers` | 定向补跑失败项（只跑失败清单里未解决的） |
| `python -m agentmemhub wiki --action pending\|ignored` | `--out`、`--limit N` | 待 wiki 更新（新增/变更）或已软删除的记忆明细，各条带 `id` / `source` / `type` / 字数 / 时间 / 摘要 |
| `GET /api/logs` | `limit`（默认 100，上限 500） | `{"logs": [...]}`，`logs.recent()` 的结果（新 → 旧） |

按事件筛 JSONL（两份日志都是行式 JSON，`rg` / `Select-String` 直接可用）：

```bash
rg '"event": "run_aborted"' logs/wiki.log          # 整轮中止
rg '"event": "domain_write_skipped"' logs/wiki.log # 页数崩塌跳过写盘
rg '"event": "trigger_check"' logs/wiki.log        # 触发器判定（含不跑）
rg '"event": "write"' logs/memory.log              # 全部记忆写入
rg '"phase": "call"' logs/mcp.log                  # 所有 MCP 调用发起
```

## 8. A2 待实现的事件

以下事件**尚未实现**，登记在此是为了让 A2（离线批处理 · MiMo Batch API）后续里程碑落地时一次补齐。
来源：[batch-offline-plan.md](batch-offline-plan.md) §17.4「日志落实」。

> 已转正的：**批量动作的 6 个事件**（M0）、**`notify_write`**（M1，见 §5.6）、
> **`offline_submitted`**（M2b-1）与 **`batch_import` / `batch_consumed` /
> `batch_voided` / `batch_expired`**（M2b-2 回读，见 §5.1 末尾「批量任务事件」表）。
> 其中 `batch_list` 是探测时才发现厂商有列表接口、后补的第 6 个。

| event | 计划所属流 | 计划记录的内容 |
|---|---|---|
| `config_change` | 配置审计 | 谁 / 何时 / 哪个键 / 旧值 → 新值（`PUT /api/config` 白名单） |

A2 的验收要求是**这五个问题必须能被日志回答**：① 这轮 wiki 跑到哪了、断在哪 ② 花了多少 token / 多少钱
③ 离线任务等了多久、进度几比几、卡在哪个域 ④ 哪些域失败了、为什么 ⑤ 配置被谁改成了什么。
其中 ①② 由既有流水线事件覆盖、③ 由 M0 的批量事件覆盖（`batch_create` 记 `n_requests`、
`batch_retrieve` 记厂商 `counts`）、④ 部分覆盖（`domain_write_skipped` / `page_fail` / `run_aborted`）、
⑤ 依赖上表最后一行。

另：A2 计划把 `docs/logging.md` 本身纳入验收门槛（事件字典覆盖 A2 全部新事件），
并在 `docs/modules/*.md` 之外单独评估一份 `observability.md`（覆盖 `logs.py` / `failures.py` / `notify.py` / `snapshot.py`，
Phase 2，事件字典先行）。

---

**核对范围说明**：本文所有事件名与字段名来自对 `agentmemhub/`、`scripts/` 的 grep 与逐点阅读，
未从测试文件推断生产行为。已知的缺口（怀疑存在但代码里找不到写入点，故未登记为事件）：

- **记忆删除**：`wiki_pending.drop()` / `restore()` 没有任何审计写入 —— 硬删会清蒸馏表与召回投影
  （返回值里的 `projections_removed`，CLI 只打印一行结果）；CLI 侧 `cmd_wiki` 与 `cmd_snapshot` 也没走 `_cli_log()`，
  因此"谁在命令行删了哪条记忆""谁回滚了快照"目前只能靠终端回滚缓冲区。
- **快照**：`snapshot.py` 全文没有日志调用；面板路径由 `_logged_task` 记 `提交任务/开始/完成`（在 web.log），CLI 路径无痕。
- **`wiki.update()` 自身的起止**：没有 `update_start` / `update_end` 事件；
  非触发路径（CLI `--action update`、面板增量更新）只有内部脚本的 `run_start` / `run_end`，
  没有 `trigger_update_end` 那样的汇总行。
- **蒸馏批次汇总**：蒸馏只逐条写 `memory.log` 的 `write`，批次级汇总（处理了多少会话、花多少 token）只在 CLI/控制台输出与 `agentmemhub.log` 文本里。
- **向量化 / embedder**：没有 JSONL 事件，只在 `asrag-*.log` 文本日志里。
- **`snapshot.py` / `health.py` / `obsidian.py` / `export.py`**：确认无 `logs.*` 调用（`obsidian.py` 的验收结果由 `wiki.py` 的 `link_check` 事件承载）。
