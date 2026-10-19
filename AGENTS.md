# AgentMemHub 项目约定

## 输出与思考语言（硬性规则）

**一律使用中文**，包括思维链/内部推理、面向用户的正文、新增代码注释与 docstring、
提交信息、日志文案、面向用户的报错提示与界面文案。
保持英文的仅限：标识符（变量/函数/类/文件名）、第三方 API 字段名与取值、
库的专有名词，以及已有英文注释的历史代码（改到哪处再顺手中文化，不专门重构）。

## 记忆保存纪律（硬性规则）

任务收尾时自查：**本次是否产出了可复用结论（问题解决步骤 / 踩坑解法 / 架构决策 / 关键配置变更）？**
命中任意一条，必须走 `save-memory` Skill 流程完成持久化，不允许只写 Agent 自己的本地会话记忆：

1. `memory_stats` 探活——不可用时告知用户**建立索引**（本项目内 `uv run python -m agentmemhub sync`；
   v2.0 起引擎内置、进程内直调，**没有"启动引擎"这个动作、也没有 daemon 可启停**），不硬写；
2. `memory_save` 写入自包含结论（背景一句话 + 结论/做法）；
   可用可选参数 `importance` 顺手给档位（`high` 0.8 / `normal` 0.6 / `low` 0.4，
   **不传即 normal**）——用自己的推理直接判断即可，**无需外挂评分模型**：
   技术沉淀、踩坑解法、架构决策、用户明确要求记住的用 `high`；临时性、局部细节用 `low`。
   **别滥用 high**（什么都标 high 就失去区分度，与写后即评被废除同理）。
   `tags` 可选（字符串数组），供面板筛选与溯源；
3. **写入后不需要立即评分**——价值分起点由 `importance` 决定（默认 0.6），
   之后由真实使用演化（👍/👎 反馈）；仅当用户明确说「这条很重要」
   时才用 `memory_score` 打 positive。（2026-09-11 起：写后即评已废除——
   同一意图计两次会顶格 1.0、丧失区分度，批量评分入口亦已隐藏）

需要历史经验时（新任务开始 / 疑似曾解决过）主动调 `memory_search`，与保存流程无关、各自独立触发。

## 其它既有约束（速查）

- **引擎归属**：`agentmemhub/rag/`（内置记忆引擎）是**本项目自有代码**，可改、需写测试；
  `memOS/` 是**回退用的上游 vendored 引擎**，源码不得改动（所有修复落在本项目代码）。
  `memos*.py` 是回退路径适配层，非必要不动。
- **架构不变量**：MCP 的**五个记忆工具**与面板网关的**响应契约不得破坏**（Skill 侧强依赖）；
  `feed_add` / `feed_list` / `feed_status` 三个投喂工具是**新增独立**的（2026-10-16）：
  它们**不依赖记忆引擎**（本地文件处理，跳过 `auth_state()` 检查），且**只给写入与查看、
  不给删除**（`retract` / `forget --purge` 留在 CLI）。
  ⚠️ 加新入口时记住：**stdio 的 stdout 只许放协议** —— service 层的 `log` 默认值是
  `print`（CLI 场景），MCP 必须把过程日志收进返回文本（否则日志混进 JSON-RPC，客户端解析失败）；
  源库（`database/agentmemhub.db`）只读、索引库（`session_rag.db`）自持；
  摄取幂等（`src_id` 锚）、排除机制防回流。
- **配置单点**：模型/分桶/召回/写入策略/蒸馏/LLM 一律经 `agentmemhub.yaml`，
  **禁止硬编码模型 id/维度/路径/密钥**。LLM 接入可用
  `scripts/sync_llm_from_zcode.py` 从 ZCode 配置同步（密钥只写本地 yaml，不入库）。
  **配置的"继承"有四种形态**（内置默认<yaml / 集中式细分覆盖 / 就地覆盖 / 三层），
  改配置前先看 [`data-architecture.md` §13](docs/data-architecture.md) 的对照表与
  检查清单 —— 不清机制最容易写出"配了不生效"或"覆盖范围超出预期"。
  **想知道某个调用方到底在用哪份 LLM 配置**：`uv run python -m agentmemhub llm-config`。
  **加/改配置键后必跑** `uv run python scripts/check_config_keys.py` —— 它查
  "定义了却没有任何读取点"的**僵尸键**。本项目踩过**四次**同一个坑：
  前三次是 `wiki.single_shot_max`、`wiki.workers`、`wiki.l2.*`，以及没有任何消费点的
  假开关 `wiki.enabled` —— 它直到 **2026-10-07** 才真修：此前只把
  `_configured()` 的注释改成了"现在是真开关"，代码里却**没有任何触发链检查它**
  （`_configured()` 只被 `status()` 调用）；真正的拦截点现补在
  `wiki_triggers._run_locked()` 开头。**教训：注释不等于实现 ——
  判定"某个键到底有没有消费点"必须去查调用点，别读注释**；
  前三次的**共同特征是配置里的值恰好等于代码里硬编码的默认值**
  —— 所以既不报错、也不让测试变红，能潜伏数月。
  **第四次是 `feed` 段的四个假开关**（`feed.convert.llm` / `feed.convert.ocr` /
  `feed.fetch.renderer` / `feed.depth_default`，2026-10-15 删除）：能力还没做，
  开关先写好了。这一次**审计压根没看见它们** —— `agentmemhub.yaml.example` 当时
  **没有 `feed` 段**，而审计只扫 example 与本地 yaml 里**出现过**的键
  （不是"注释骗过了它"：注释在 `yaml.safe_load` 时就已被丢弃）。
  **教训：加配置键必须与"写进 example"同一步做，否则这条防线是空的**。
  还有一条：**报错文案不许引用不存在的开关** —— 当时 `url.py` 指引用户
  "置 `feed.fetch.renderer=playwright`"，指向一个假开关，比不说更坏
  （用户会去改它，然后发现毫无反应）。`tests/test_config_audit.py`
  里有一条守护测试会在提交前拦住它。**注意该脚本是启发式**（按"键名在代码里
  出现过"判定）——像 `rag.retrieval.candidate_k` 这种"代码里有同名字段但 yaml
  配了不生效"的，它查不出来，需人工复核。
- **面板结构**：**三标签页**「统一会话 / 记忆报表 / wiki知识库」；会话页只做查看/删除/改标题
  （记忆排除 UI 已下线，后端能力保留）；记忆页承载筛选/溯源/⭐加权/👍👎/蒸馏入口；
  **知识库页**展示**页面产物**（L2 蒸馏 + 外部投喂，用 `origin` 区分）—— 它与「统一会话」
  **零耦合**，数据**以磁盘为准**（不查投影表，因为投影是运维动作会滞后），正文由
  `agentmemhub/mdmini.py` 渲染（**先转义再套结构**，防注入）。
  该页还有**二级视图「运维」**（增量更新 / 待更新记忆的软·硬删除 / 编译失败补跑 /
  触发器热改 / 离线批次推进）：长任务复用全局进度条，**有副作用的动作一律二次确认**。
  会话与记忆通过 `session_uid`（全局递增）双向绑定跳转。
- **测试纪律**：每次改动带测试；bug 修复先写复现测试（红→绿）；
  `uv run pytest` 全绿是提交门槛（数量以实测为准，当前 1344 passed / 1 skipped）。
  ⚠️ **别并发跑两个 `uv run`**（例如后台跑全量测试、前台又跑脚本）—— uv 会争用环境锁，
  实测导致 10 个毫不相干的测试假失败（snapshot / wiki_triggers），干净重跑即全绿。
  ⚠️ **`monkeypatch` 挡不住「活过测试的线程/子进程」**：MCP 的写入钩子会起 daemon
  线程（`mcp_server._wiki_trigger_probe`），线程若在本测试结束后才执行，patch 已被
  撤销 → 它拿到**真实配置**。**2026-09-26 实测事故**：某个 `memory_save` 测试的逃逸
  线程用**测试沙箱的 tmp 库**跑了"全量更新"，把真实 `out_l1` 当产物目录 ——
  224 个 L1 页面删到 25 个、`manifest_l1.n_inputs` 从 1804 覆盖成 8。
  所以关涉真实路径的旁路必须有**进程级、不随测试撤销**的熔断：
  `conftest._sandbox_data_dir`（session 级 autouse）置位 **`AGENTMEMHUB_TEST_SANDBOX=1`**
  （环境变量会被子进程继承），`wiki_triggers._targets()` 见它即抛。
  **新加"会起线程/子进程的旁路"时，务必确认它也被这道熔断覆盖。**
  ⚠️ **范围要放宽一档：凡"真实副作用"的出口都必须有熔断**，不限于线程/子进程。
  已被同一道闸（`AGENTMEMHUB_TEST_SANDBOX`）覆盖的出口：
  `wiki_triggers._targets()`（抛）、`wiki_batch._repair_client()`（返回 `None`）、
  `wiki_aggregate.make_client()`（抛）、`batch_client`（抛 `BatchError`）、
  **`notify.pushplus_send()`（返回 `{"skipped": "test_sandbox"}`）**。
  **2026-10-12 实测事故（第三例）**：`notify()` 默认 `push_too=True`，而
  `_notify_cfg()` 刻意绕过 config 单例**直读真实 yaml**（为"填完 token 立刻生效"）——
  两者相加 ⇒ `tests/test_notify.py` 里 **7 处"只测落库"的 warning/error 调用
  会真的往用户微信发消息，跑一次测试刷一串**。修法是在**唯一网络出口**兜死；
  要测 `pushplus_send()` 本体必须**同时**置 `AGENTMEMHUB_ALLOW_REAL_PUSH=1`
  **且**把 `urlopen` 换成替身（两道条件缺一不可，名字故意写刺眼）。
  三例同源（付费 LLM / 真删产物 / 发微信）：**花钱、发消息、删数据这类真实副作用，
  靠"测试记得 mock"一定守不住 —— 必须在唯一出口兜死，并让"忘记 mock"表现为
  响亮失败而不是静默外发。**
- **日志纪律**：统一 `logs/` 按程序分文件；测试必须隔离日志与数据目录
  —— 隔离靠**两道**：`conftest` 的 autouse 夹具把 `logs.log_dir` 指向 tmp
  （只对当前测试生效），以及**进程级**环境变量 `AGENTMEMHUB_TEST_LOG_DIR`
  （`logs.log_dir()` 认它，逃逸线程/子进程也躲不开）。
  只有前者时实测漏过：生产 `logs/wiki.log` 里混进了 **140 条**测试错误。
  **标准头五项**：**时间 / 等级 / 模块 / 行号 / 内容** —— 所有日志都必须有，
  **调用方只写业务字段，标准头由写入口统一补齐**（`logs.standardize()`；
  不要要求 60+ 个调用点各自写全，实测必然漂移）。三种形态的落法：
  · **JSONL 事实流**（`mcp.log` / `memory.log` / `wiki.log` / `web.log` / `cli.log`）：
    自动注入 `ts`（epoch 秒）/ `level`（`info|warn|error`，由 `ok=False`、`error`、
    错误事件名自动推断）/ `module` + `line`（**由调用栈取业务调用点**，不是日志模块自己）
    / `msg`（单行可读摘要，≤200 字；正文仍在业务字段里）。测试：`tests/test_logs_format.py`
  · **文本日志**（`asrag-*.log`、`agentmemhub.log`、`asrag.log`，见
    `agentmemhub/rag/logkit.py` 与 `logs.setup_app_logging()`）：
    `%(asctime)s %(levelname)-5s [%(name)s:%(lineno)d] %(message)s`
    ⚠️ 应用内 `agentmemhub.*` / `asrag.*` 的 logger **必须能被收进文件**：
    `logs.setup_app_logging()` 把兜底 handler 挂在**命名空间根**上（不是逐个登记 ——
    新模块加进来就会再漏）。此前 `agentmemhub.distill` / `llm` / `store`、
    `asrag.cli-sync` / `bridge` 五个 logger 从来没有 handler，root 也没有 ⇒
    只走 `logging.lastResort`（仅 WARNING+ 到 stderr），**info 级全丢**（实测
    24 处调用点）。它由 `_append_line`（所有日志的唯一漏斗）顺带调用，所以
    任何写日志的进程都会自动挂上。
  · **任务日志**（`logs/tasks/<id>.log`）：`YYYY-MM-DD HH:MM:SS INFO [module:line] 内容`
    （早先只有 `[HH:MM:SS]`，跨天排查无法定位是哪天）
  **三条事实流分工**：`logs/mcp.log` 记**协议层**（谁调了什么工具、耗时、成败，
  由服务端在唯一分发点自动写，不依赖 Agent 手动记）、`logs/memory.log` 记
  **数据层**（哪条记忆被写/读、**内容全文**、来自 `mcp`/`http`/`distill`/`cli`
  哪条路径；写入点埋在 `distill` 的落表处，一处覆盖所有路径，查询用
  `agentmemhub memory-log`）、`logs/wiki.log` 记**流水线层**（编译跑到哪、花了
  多少、断在哪，含触发器判定 `trigger_check`）。
  **滚动/归档**由 `logs` 配置段统一控制：全局 `logs.rotate` 对**所有**日志生效、
  `logs.files.<日志名>` 覆盖（空则回落全局）；滚动规则**二选一**——`max_mb > 0`
  按大小（**最高优先级**），否则 `daily` 按**自然日首次写入**；归档进
  `logs/archive/<日志名>/`（`compress: false` 时保留原始文件），`keep_days` 默认
  30 清理超期归档；`logs/tasks/*.log` 按天直接清理。
  Agent 的意图用可选参数 `note` 补充。**长任务同样要落 `logs/wiki.log`**（wiki 编译的每次 LLM 调用、
  每个会话/页面的成败、运行汇总）—— 跑几十分钟的批量任务必须能从日志回答
  "跑到哪了、哪些失败了、花了多少、上次断在哪"，控制台输出关掉就没了。
- `.bat` 脚本：纯 ASCII + CRLF + `if (...)` 块内不得含 `)`。
- 推送远端前必须脱敏（配置走 example 占位）；`scripts/sensitive_scan.py` 是兜底检查
  （规则含动态取本机用户名与私有路径模式）。
- 用户验证功能期间不要主动 git commit/push，等明确指令。

## 相关文档

- **`docs/data-architecture.md` — 架构全景（权威）**：三层数据模型、表矩阵、ID 锚体系、
  六路召回与准入、价值体系、快照回滚、运维速查、演进时间线（**改动架构先读它**）
- `docs/branch-milestones.md` — 分支里程碑时间线（四个大里程碑 + 即将做的事）
- `docs/memory-deletion.md` — 记忆软删除与 wiki 引用状态（**设计基准，待实施**）
- `README.md` — 使用说明与快速开始（含「记忆蒸馏」「Agent 协作配置」章节）
- `ARCHITECTURE.md` — 架构说明
- `docs/memory-distillation.md` — 记忆蒸馏的设计与实施记录
- `docs/llm-wiki.md` — LLM Wiki 两级编译、失败兜底与格式修复的实测记录
- `docs/llm-batch-offline.md` — 离线批处理（Batch API）**接缝设计 → 实施记录**：
  §1–§7 是当初的接缝设计（每节末尾补了"后来怎样了"），**§8 是实施记录**
  （最终形态、四轮实测数据、落地时改掉的四个判断、踩坑清单）。要**怎么用/怎么排查**
  看 `docs/modules/batch-offline.md`；要**为什么这么设计**看 `batch-offline-plan.md`
- `docs/batch-offline-plan.md` — 离线批处理接入计划（A2 · MiMo Batch API）：
  决策记录 D1–D6、三段式流水线状态机、里程碑 M0–M6 追踪表与风险清单
  （**动工前先读它**；M0–M3 已落地，M4/M5 的核心也已做，剩 M6 收尾）
- `docs/modules/*.md` — **按职责域的模块文档**（改代码前先读对应那份）：
  `wiki.md`（两级编译/触发器/链接验收）、`config.md`（配置继承与僵尸键）、
  `notify.md`（通知：落库 / 推送策略 / **怎么加一个新推送渠道**）、
  `batch-offline.md`（随 A2 落地）。根 README「模块地图」是索引
- `docs/logging.md` — **日志事件字典 + 排查剧本**：三条事实流、标准头五项、
  每个 event 的字段含义与用途（**加新事件必须登记在此**）
- `docs/model-selection.md` — LLM 模型选型与定价（推荐 MiMo / 免费 LongCat）
- `docs/API.md` — 对外接口契约（HTTP / MCP / CLI；含长任务约定与错误语义）
- `docs/recall-fusion.md` — 召回融合的机制与实测数据（**最新权威描述见 `docs/data-architecture.md` §4**）
- `docs/branch-divergence.md` — 分支差异记录（`feat/llm-wiki` 相对 `main`）
- `docs/EXAMPLES.md` — SQL / CLI / Python 查询示例
