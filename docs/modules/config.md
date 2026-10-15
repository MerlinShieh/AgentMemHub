> 覆盖模块：agentmemhub/config.py 等 · 最后核对：2026-09-27 · 权威机制见 [data-architecture.md §13](../data-architecture.md)

# 配置体系

## 1. 职责边界

**做什么**

| 职责 | 落点 |
|---|---|
| 提供配置**单一真源**：模型 / 分桶 / 召回 / 写入策略 / 蒸馏 / LLM / 日志 / 面板端口全部经 `agentmemhub.yaml` | `agentmemhub/config.py`、`agentmemhub/rag/config.py` |
| 分段解析 + 内置默认兜底：`DEFAULT_DISTILL` / `DEFAULT_WIKI` / `DEFAULT_SNAPSHOT` / `DEFAULT_LOGS`，`rag` 段的 `DEFAULT_EMBED` / `DEFAULT_RETRIEVAL` / `DEFAULT_WRITE` / `DEFAULT_PAGE_POLICY` | 同上 |
| 环境变量临时覆盖（`AGENTMEMHUB_*` / `MEMOS_*` / `AGENTMEM_HUB_DATA_DIR`） | 各属性内逐键读取 |
| 给出「某调用方**最终生效**的 LLM 配置」（继承 / 覆盖标注） | `config.llm_effective()`、CLI `llm-config` |
| 兜住「定义了却没有任何读取点」的僵尸键 | `scripts/check_config_keys.py` + `tests/test_config_audit.py` |
| 从别的客户端同步 LLM 接入信息（不手抄密钥） | `scripts/sync_llm_from_zcode.py`、`scripts/sync_llm_from_dsh.py` |

**不做什么**

| 不做 | 当前语义 |
|---|---|
| **不做运行时热重载** | `Config.__init__` 只在构造时读一次 yaml（`self._file`），`config()` 是模块级单例（`_instance`）。yaml 改动对**已运行的进程无效**——MCP 常驻进程（`agentmemhub mcp`）、面板（`agentmemhub serve`）、`memos-daemon` 都要重启；CLI 每次是新进程，天然重读 |
| 不写回主 yaml | 除两个同步脚本（原地改 `llm` 段、保留注释）外，程序不修改 `agentmemhub.yaml` |
| 不集中托管 `rag` 段 | `rag` 段由 `agentmemhub/rag/config.py` 的 `load_settings()` **自行读同一份 yaml**，不经过 `Config` |
| 不做跨段语义校验 | `load_settings()` 只校验「`active` 已注册 + 模型文件齐全（`onnx` / `tokenizer.json` / `config.json`）」，不校验档位与阈值是否自相矛盾 |
| 没有**文件级** override 覆盖层（`agentmemhub.override.yaml`） | 见 §7。⚠️ 但**触发规则有一处运行时覆盖**（状态文件 `logs/wiki_trigger_state.json` 的 `overrides` 段）—— 那是**另一种机制**、已经实现，别混为一谈 |

## 2. 对外入口

| 入口 | 位置 | 说明 |
|---|---|---|
| `agentmemhub.config.config()` | `agentmemhub/config.py` | **模块级单例**（`_instance`）。首次调用时 `Config()` 默认构造；`Config(path=..., env=...)` 可显式注入；`reset()` 清空单例（测试用） |
| 属性读取 | 同上 | `data_dir` / `db_path` / `web_port` / `memory_backend` / `agent_path(source)` / `llm` / `distillation` / `wiki` / `wiki_l2` / `snapshot` / `logs` / `memos_*`。段属性返回的是**已合并默认值**的字典，调用方不必处理缺键 |
| `agentmemhub.rag.config.load_settings(root=None)` | `agentmemhub/rag/config.py` | 读 `rag` 段；`rag_bridge.settings()` 是它自己的模块级单例（`_SETTINGS`，`configure()` 可注入），`_derive_settings()` 再用 `config().data_dir` / `config().db_path` 覆写 `source_db` / `index_db` / `log_dir` |
| CLI：查 LLM 生效配置 | `python -m agentmemhub llm-config [--json]` | `cmd_llm_config`（注册于 `_register_llm_config_parser`）。打印四个使用点（记忆蒸馏 / 批量评分 / Wiki 第一级 / Wiki 第二级）的最终值，每项标 `[继承]` 或 `[覆盖]`；`api_key` 只显示「（已设置）/（未设置）」，**绝不回显** |
| CLI：`config` 子命令 | — | **不存在**。`main()` 的 `handlers` 表与 argparse 都没有 `config`：改配置只能编辑 yaml 或设环境变量（`config get/set` 是 A2/M4 的计划项，见 §7） |
| 引擎自身 CLI | `python -m agentmemhub.rag.cli` | 子命令 `ingest` / `stats` / `reembed` / `eval` / `search` / `bench` |
| 直读（不经 Config） | — | `agentmemhub/rag/config.py`、`agentmemhub/logs.py::rotate_policy`、`agentmemhub/wiki_triggers.py`、`agentmemhub/wiki.py` 等通过 `config()` / `load_settings()` 取值；脚本层（`scripts/wiki_compile.py`、`scripts/wiki_aggregate.py`）也读同一份 `config()` |

**想知道某调用方实际用哪份 LLM 配置**：跑 `uv run python -m agentmemhub llm-config`（或 `--json` 给机器读）。原理是 `llm_effective()` 逐字段比对「使用段合并结果」与「顶层 `llm`」，相等记继承、不等记覆盖。

**密钥纪律**：`api_key` 只从 yaml / 环境变量读进内存，文档与日志都只记「已设置/未设置」；同步脚本终端输出一律脱敏（ZCode 脚本 `_mask()` 只留首尾各 4 位，DSH 脚本只报字符数）。本文档不记录任何密钥值，一律写作 `<本地配置>`。

## 3. 数据流与不变量

### 3.1 加载顺序

| 优先级 | 来源 | 细节 |
|---|---|---|
| 1（最高） | 环境变量 | 各属性内**每次访问**时读：`data_dir` ← `AGENTMEM_HUB_DATA_DIR`；`db_path` ← `AGENTMEMHUB_DB`；`web_port` ← `AGENTMEMHUB_PORT`；`memory_backend` ← `AGENTMEMHUB_BACKEND`；`llm` 三项 ← `AGENTMEMHUB_LLM_ENDPOINT` / `_API_KEY` / `_MODEL`；`memos.*` ← `MEMOS_BASE_URL` / `MEMOS_REPO_DIR` / `MEMOS_PLUGIN_DIR` / `MEMOS_HOME` / `MEMOS_PASSWORD` / `MEMOS_LIGHTWEIGHT` |
| 2 | `AGENTMEMHUB_CONFIG` 指定的文件 | 非空即用，`~` 展开 |
| 3 | `<项目根>/agentmemhub.yaml` | 两者都没命中时的第一候选 |
| 4 | `<项目根>/database/config.yaml` | 第二个候选 |
| 5 | `~/.agentmemhub/config.yaml` | 第三个候选 |
| 6（最低） | 内置默认 | `DEFAULT_*` 常量 + 属性里的字面默认（如 `8086`、`http://127.0.0.1:18800`） |

只取**第一个存在**的候选文件；相对路径统一按项目根解析，`~` 展开为用户目录，`$VAR` 经 `os.path.expandvars`。

### 3.2 配置继承的四种形态

| # | 形态 | 机制 | 本项目真实例子 |
|---|---|---|---|
| ① | **内置默认 < yaml** | `config.py` 的段用 `_deep_merge`（嵌套字典逐键合并）；`rag` 段在 `_load_rag_section()` 里用浅合并 `{**DEFAULT_X, **yaml}`（嵌套的 `retrieval.page` 另在 `Settings.page_policy` 处理） | `agentmemhub.yaml` **只写了** `wiki.out_l1` / `out_l2` / `obsidian_cli` / `wiki.llm.timeout` / `wiki.l2.*`——`wiki.enabled`、`single_shot_max`、`plan_batch_max`、`page_workers`、`workers`、`snapshot.keep`、`logs.rotate.*` 全部来自内置默认；`distillation` 段整段不写也有完整默认 |
| ② | **集中式细分覆盖** | 全局键 + 同一处的按实例细分；未配的实例回落全局 | `logs.rotate` < `logs.files.<日志名>`（本项目 `files: {}`，全部走全局 `max_mb: 5`）；`rag.retrieval.recall_level` < `rag.retrieval.callers.<调用方>`（本项目两者都未配 → 全局默认档 3；调用方名由 `retrieval_caller()` 标注：`mcp` / `panel` / `cli` / `eval`） |
| ③ | **就地覆盖** | 使用段自带一份 `.llm`，分散在各段 | `llm`（顶层）< `wiki.llm`：本项目只写了 `timeout: 900`，`endpoint` / `model` / `api_key` / `headers` **继承顶层**；`llm` < `distillation.llm`：本项目未配 → 蒸馏与批量评分**共用顶层同一份**；`wiki.l2.llm`：写了 `reasoning_effort: low` + `timeout: 900`，继承链是**顶层 llm → wiki.l2.llm，刻意跳过 `wiki.llm`**（级联会让第一级换 provider 牵连第二级） |
| ④ | **三层** | 内置默认 < 档位表 < yaml 显式项 | `DEFAULT_PAGE_POLICY` < `RECALL_LEVELS[档位]` < `rag.retrieval.page.*`（`Settings.page_policy`）。本项目未显式配 `retrieval.page` → 生效值 = 档位 3（均衡）展开值 |

另有一处**状态文件生效层**（同属三层思路，但第三层不在 yaml），而且**两套规则各一份**：

- `wiki_triggers.get_effective_config()` = `wiki.update.*`（含 `DEFAULT_WIKI["update"]`）
  < `logs/wiki_trigger_state.json` 的 `overrides` 段（**顶层四字段**：`enabled` /
  `schedule` / `dirty_memories` / `first_write_daily`）
- `wiki_triggers.get_effective_batch_config()` = `wiki.batch.*` < 同一 overrides 的
  **`batch` 子段**（字段相同）—— 两个通道**各一套、互不干扰**

两套都由 `set_overrides()` 写入，**热生效**（不用重启）：
`wiki --action triggers --set-batch on|off`，或 `PUT /api/wiki/triggers` 的
`{"batch": {...}}`。⚠️ `wiki.batch` 里的 `check_interval_s` / `expire_warn_s` /
`max_repair` **不是触发规则**，不参与这套覆盖（它们各有自己的消费点）。

四种形态的完整对照表与改键检查清单见 [data-architecture.md §13](../data-architecture.md)。

### 3.3 不变量与生效时机

| 不变量 | 后果 |
|---|---|
| yaml 只在 `Config.__init__` 读一次 | 常驻进程改 yaml 必须**重启**；`config()` 单例跨模块共享，一次构造全局同值 |
| `rag_bridge.settings()` 缓存在 `_SETTINGS` | 同上；测试/启动期用 `configure(settings)` 注入 |
| 环境变量在属性访问时读（`self._env` 默认直接引用 `os.environ`） | 单键属性（`data_dir` / `db_path` / `web_port` / `llm` 等）每次访问都重读 env；**段字典**（`wiki` / `distillation` / `snapshot` / `logs`）只由 yaml + 默认值构成，env 不参与 |
| `rag.models` 的模型各自独立向量表（`ModelSpec.vec_table` = `vec_<id>`） | 换 `rag.active` / 扩 `rag.retrieval.models` 前，目标模型必须有向量：靠 `rag.write.order` 多模型写入，或跑 `python -m agentmemhub.rag.cli reembed --model <id>` 重建。`agentmemhub rebuild` 只补缺向量（repair），不重算历史 |

| 改动的键 | 生效要求 |
|---|---|
| 任何 yaml 键 | 重启常驻进程（MCP / 面板 / daemon）；CLI 无需额外动作 |
| `rag.active` / `rag.models` / `rag.write.order` / `rag.retrieval.models` | 重启 **+ 确认目标模型有向量**（必要时 `reembed --model`） |
| `distillation.prompt_ver` | 显式给整数即**强制重蒸**并作废旧版本产物；留空 = 跟随代码常量，提示词一改自动触发 |
| `wiki.out_l1` / `wiki.out_l2` | 重启；换目录等于换产物仓库，需重新编译才有内容 |
| `wiki.obsidian_cli` | 重启（配置优先于 `OBSIDIAN_CLI` 环境变量，见 `agentmemhub/obsidian.py::find_cli`） |
| `logs.rotate` / `logs.files` | 重启（`rotate_policy(name)` 每次调用读 `config()`，但 `Config` 已缓存文件） |
| `snapshot.keep` | 下次创建/清理快照时生效（`snapshot.keep_count()`，非法值回退 `DEFAULT_SNAPSHOT["keep"]`） |
| `wiki.update.*` | 触发器每次判定时读；若被状态文件 `overrides` 覆盖，yaml 改动**看不到效果** |
| `wiki.batch.*`（触发规则那四个） | 同上，但吃的是 `overrides.batch` 子段 —— 与实时**各一套**。⚠️ `check_interval_s`（收割节流）/ `expire_warn_s`（过期预警）/ `max_repair`（单轮修复上限）不属于触发规则，不受 overrides 影响 |
| `notify.pushplus.*` / `notify.push_when` / `notify.dedup_window_s` | **例外：每次直接读盘**（不走 `config()` 单例）—— 填个 token 不该要求重启 DSH，见 `notify._notify_cfg()` |

## 4. 配置项

**全量清单不在此穷举**：权威键表是 `agentmemhub.yaml.example`（每个键带中文注释与默认值说明）；本地实际配置是 `agentmemhub.yaml`（已 gitignore，含真实密钥，勿外传）。本节只列每段的关键键与一个真实示例。

### 4.1 基础目录与后端

| 键 | 作用 | 示例 |
|---|---|---|
| `data_dir` | 可写数据根（db / 水位 / 评分状态 / 备份 / 托管 pid）；默认 `<项目根>/database` | `data_dir: database` |
| `db_path` | 统一会话库 SQLite；留空 = `<data_dir>/agentmemhub.db` | `db_path: ""` |
| `backend.backend` | 记忆后端：`rag`（内置引擎，默认）\| `memos`（vendored 回退，需其 daemon）；非法值一律回落 `rag` | `backend: {backend: rag}` |
| `agents.<source>` | 各 Harness 会话数据位置（`zcode` / `opencode` / `hermes` / `qwen` / `qodercn` / `workbuddy` / `dsh`）；留空 = adapter 走官方默认探测 | `dsh: ""` |

### 4.2 `rag`：检索与嵌入（`backend=rag` 时生效）

| 键 | 作用 | 示例 |
|---|---|---|
| `rag.active` | 激活模型（决定检索用哪套向量）；必须是 `rag.models` 里的键，否则报错 | `active: bge-small-zh-v1.5` |
| `rag.write.order` | 向量化写入顺序；先完成的立即可检索 | `order: [bge-small-zh-v1.5, bge-base-zh-v1.5]` |
| `rag.write.fast_first` | 前一个模型完成即返回，其余后台继续 | `fast_first: true` |
| `rag.write.background_hint` | 后台补跑时面板/CLI 的提示语 | `background_hint: "高精度模型正在后台向量化，稍后自动生效"` |
| `rag.embed.batch_size` / `bucketing` / `bucket_caps` / `intra_op_threads` | 批量嵌入：批大小、按字符长度分桶、分桶边界、onnxruntime 线程数（0 = 自动） | `batch_size: 32`、`bucket_caps: [64, 128, 256, 384]` |
| `rag.retrieval.recall_level` | **召回严格度档位** 1 最严格 … 5 最宽松（默认 3）；一条刻度同时决定候选宽度 / 非页面相对阈值 / 终审阈值 / 页面席位 / 字面兜底席 | 本项目未配 → 默认 `3` |
| `rag.retrieval.callers.<调用方>` | 按调用方细分档位（`mcp` / `panel` / `cli` / `eval`）；细分配错回落**全局**档位 | 本项目未配 | 
| `rag.retrieval.models` | 参与召回融合的模型（多路 RRF）；空 = 仅 `active` | `models: [bge-small-zh-v1.5]` |
| `rag.retrieval.page.*` | 页面层（L2 知识页）准入：`max_in_results` / `literal_seats` / `literal_required_below` / `floor_ratio` / `min_evidence` / `channel_k` / `reserve_seats` | 本项目未配 → 用档位展开值 |
| `rag.models.<id>` | 模型注册表：`path` / `dim` / `pooling` / `normalize` / `quantized` / `maxTokens` / `language` / `queryPrefix` / `family` / `source` | `bge-base-zh-v1.5`：`dim: 768`、`family: bge` |

**`rag.retrieval` 下有若干键当前没有任何读取点**（`candidate_k` / `threshold_floor` / `max_per_conversation` / `search_max_hits` / `rrf_k`），改了不生效且审计脚本查不出——见 §5.2。

### 4.3 `llm`：接入与超参

| 键 | 作用 | 示例 |
|---|---|---|
| `llm.endpoint` | OpenAI 兼容的 `chat/completions` 端点 | `https://api.deepseek.com/chat/completions` |
| `llm.api_key` | 密钥；可用 `AGENTMEMHUB_LLM_API_KEY` 覆盖；只读不打印 | 值一律记作 `<本地配置>` |
| `llm.model` | 模型 id | `deepseek-flash` |
| `llm.headers` | provider 特定请求头（过 WAF / 会话标识等） | `{User-Agent: "AgentMemHub/1.0"}` |
| `llm.thinking` | 思考开关：`disabled` = 彻底关闭思维链；`enabled` = 显式开启；留空 = 不下发（用 provider 默认）。思考模式下 `temperature` 不生效 | `thinking: "disabled"` |
| `llm.reasoning_effort` | 思考强度 `low` / `medium` / `high` / `xhigh` / `max`（仅思考模式生效；provider 接受档位不同） | `reasoning_effort: "low"` |
| `llm.repair_model` | 格式修复模型：主模型返回内容但 JSON 解析失败时，把原始输出交给它转成规范 JSON；留空 = 关闭（直接抛错） | `repair_model: "deepseek/deepseek-v4.1-flash"` |
| `llm.timeout` / `max_tokens` / `temperature` | 透传超参 | `timeout: 900` |

**使用方**：记忆蒸馏（`distillation.llm` 继承本段）与批量评分（`scoring.read_engine_llm()` 直读本段）。Wiki 两级各有自己的 `.llm`，见 §3.2 形态③。

**改这段要注意白名单**：`Config.llm` 与 `Config._merge_llm` 只透传 `_LLM_SCALAR_KEYS`（`timeout` / `max_tokens` / `temperature` / `thinking` / `reasoning_effort` / `repair_model`）加 `endpoint` / `api_key` / `model` / `headers`。**新增 provider 开关时必须同步加进白名单**，否则 yaml 写了也读不到（实测踩过 `thinking` / `reasoning_effort`）。

### 4.4 `wiki`：两级编译

| 键 | 作用 | 示例 |
|---|---|---|
| `wiki.enabled` | 总开关。**当前唯一消费点是 `wiki_triggers._configured()`**（被 `status()` 调用）→ false 时状态报告"目标未就绪"。自动触发链（蒸馏钩子 `on_memories_written()`、MCP 的 `_wiki_trigger_probe()`）与 `run_manual(force=True)` **都不经过 `_configured()`**：它们走 `_targets()`（要求 `out_l1` / `out_l2` 都配）+ `evaluate()`（读的是 `wiki.update.enabled`）。故按当前代码，置 false **不会**拦住自动触发器，与 `agentmemhub.yaml.example` 的说明不一致——见 §5.3 | `enabled: true` |
| `wiki.out_l1` / `out_l2` | 两级产物目录的正式配置位（触发器 / 对齐审计 / 增量更新 / Obsidian vault 名的默认来源）；不配则这些功能需显式传目录 | `out_l1: exports/wiki_work/out_l1_mimo` |
| `wiki.llm` | 第一级 LLM 就地覆盖（留空字段继承顶层 `llm`） | `{timeout: 900}` |
| `wiki.single_shot_max` | 记忆数 ≤ 此值时单次编译（省一次分组调用） | `12` |
| `wiki.plan_batch_max` | 分组调用单批记忆数上限，超出分批 | `40` |
| `wiki.page_workers` | 组级（页级）并发度 | `3` |
| `wiki.workers` | 会话级并发；**真实并发 = `workers` × `page_workers`** | `4` |
| `wiki.obsidian_cli` | Obsidian CLI 可执行文件（产物链接验收）；留空按 `OBSIDIAN_CLI` → PATH 解析 | `D:\tools\Obsidian\Obsidian.com` |
| `wiki.l2.llm` | 第二级 LLM 覆盖；继承链**跳过 `wiki.llm`** | `{reasoning_effort: "low", timeout: 900}` |
| `wiki.l2.workers` / `min_pages` / `domain_max` / `plan_batch_max` | 聚合并发、成页最少页数、主题域上限、域内细分单批成员上限 | `workers: 6`、`min_pages: 2`、`domain_max: 60` |
| `wiki.update.enabled` / `schedule` / `dirty_memories` / `first_write_daily` | 增量更新触发器：总开关、每日时点档、脏记忆阈值、当日首写检测 | `schedule: ["09:00", "12:00", "18:00"]` |

`wiki.single_shot_max` / `plan_batch_max` / `page_workers` / `workers` 的读取点是 `scripts/wiki_compile.py::_limits()`（非法值回落脚本常量）；`wiki.l2.*` 的读取点是 `scripts/wiki_aggregate.py::_l2_cfg()`（命令行显式传值 > 配置 > 脚本常量）。

### 4.5 `distillation` 与 `snapshot`

| 段 | 关键键 | 示例 |
|---|---|---|
| `distillation` | `enabled`；`prompt_ver`（留空 = 跟随代码常量）；`llm`（留空继承顶层）；`slice.max_chars` / `max_turns` / `topic_boundary` / `boundary_window` / `per_message_cap`；`merge.enabled` / `max_chars` / `max_rounds`；`dedup.cosine_duplicate` / `cosine_similar`；`sanitize.enabled`；`runtime.max_concurrent` / `timeout` / `dry_run` | `prompt_ver` 留空；`slice.max_chars: 24000`；`dedup.cosine_duplicate: 0.92` |
| `snapshot` | `keep`（最多保留份数，超出删最旧；非法值回退默认） | `keep: 5` |

### 4.6 `logs`：滚动与归档

| 键 | 作用 | 示例 |
|---|---|---|
| `logs.rotate.max_mb` | 超过多少 MB 滚动（0 = 不启用大小规则）。**有大小规则时优先级最高** | `max_mb: 5` |
| `logs.rotate.daily` | 按自然日首次写入滚动（有大小规则时被忽略） | `daily: false` |
| `logs.rotate.keep_days` | 归档保留天数，超出删最旧（0 = 不限） | `keep_days: 30` |
| `logs.rotate.compress` | 归档是否 gzip（false = 保留原始文件） | `compress: false` |
| `logs.rotate.archive_dir` | 归档根目录（相对日志目录） | `archive` |
| `logs.files.<日志名>` | 单文件覆盖全局；空或未写 = 回落 `rotate` | `files: {wiki: {max_mb: 20}}` |

滚动规则**二选一**：`max_mb > 0` 按大小，否则 `daily` 按自然日，都没有则不滚动。归档落到 `<日志目录>/<archive_dir>/<日志名>/<日志名>.<时间戳>`。生效策略由 `logs.rotate_policy(name)` 解析。

### 4.7 `web` / `memos`

| 键 | 作用 | 示例 |
|---|---|---|
| `web.port` | 面板端口（`AGENTMEMHUB_PORT` 可覆盖；非法值回落 8086） | `port: 8086` |
| `memos.base_url` / `repo_dir` / `plugin_dir` / `home` / `password` / `lightweight` | 回退后端 MemOS 的地址、仓库位置、插件目录、引擎 home、viewer 密码、轻量模式（`null` = 交给引擎自管）；`backend=rag` 时这些不参与 | `lightweight: null` |

## 5. 调试与排障

### 5.1 僵尸键：识别与审计流程

**僵尸键** = 在 `agentmemhub.yaml.example` 或 `DEFAULT_*` 里定义了，但**代码里没有任何读取点**。它不抛异常、不让测试变红，唯一表现是「改了配置没反应」；如果**硬编码默认值恰好等于配置值**，连"改了没反应"都发现不了。

审计流程：

1. 加/改任何配置键之后跑 `uv run python scripts/check_config_keys.py`（人读）；
2. 机器读用 `--json`；
3. 接 CI 用 `--strict`（有疑似僵尸键则退出码 1）；
4. 提交前还有守护测试：`tests/test_config_audit.py::test_真实仓库_所有配置键都有读取点` 会在 example 与本地 yaml 上跑一遍，必须 0 僵尸键。

审计对象是**两个来源**：`agentmemhub.yaml.example`（支持的键）与 `agentmemhub.yaml`（当前实际配置）。判定方式：把 yaml 摊平成叶子键路径，取**最后一段**键名，在 `agentmemhub/**/*.py` + `scripts/**/*.py` 里找字符串字面量出现。

### 5.2 `check_config_keys.py` 查不出的情况（启发式边界）

| 边界 | 说明 |
|---|---|
| **同名字段但 yaml 配了不生效** | 脚本只问"这个键名在代码里出现过吗"，不问"出现的那个字段被谁消费"。典型：`rag.retrieval.candidate_k` —— `agentmemhub/rag/config.py` 的 `DEFAULT_RETRIEVAL` 里有 `candidate_k`（等于"有读取点"），但 `hybrid_search` 实际取的是 `settings.recall_profile["candidate_k"]`（档位表 `RECALL_LEVELS`），`settings.retrieval["candidate_k"]` 从未被读。**同类已核实的还有 `rag.retrieval.threshold_floor`（被档位遮蔽）、`max_per_conversation`（只有函数参数默认值 2）、`search_max_hits` / `rrf_k`（无任何消费点）**——改这五个键都不生效，且审计脚本一律报 OK，必须人工复核 |
| `DEFAULT_*` 定义块的剔除**只覆盖 `agentmemhub/config.py`** | `collect_sources()` 仅对 `agentmemhub/config.py` 调 `strip_default_blocks()`；`agentmemhub/rag/config.py` 的 `DEFAULT_RETRIEVAL` / `DEFAULT_EMBED` / `DEFAULT_WRITE` / `DEFAULT_PAGE_POLICY` **未剔除** → 块内键名天然构成"读取点"，这就是上一行漏检的机制 |
| 短键名假阳性 | `enabled` / `workers` / `llm` 这类键名可能命中无关上下文（任何模块的 `"enabled"` 字面量都算命中），故输出一律称「疑似」，由人复核 |
| 键名匹配只看**最后一段** | `a.b.workers` 与 `x.y.workers` 只要代码里出现过 `"workers"` 都算命中，无法区分段 |
| 只有**未被注释的**叶子键参与审计 | 审计用 `yaml.safe_load` 读文件，注释一律丢弃——`agentmemhub.yaml.example` 里大段被注释掉的示例（如 `rag.retrieval.callers` 的四个调用方、`llm.thinking` / `reasoning_effort` / `headers`）不在检查范围内，注释里的错键名也永远不会被发现 |
| 不校验**取值合法性** | 只查"有没有读取点"，不查"值合不合理"。`recall_level: 9` / `logs.rotate.max_mb: "abc"` 这类越界值审计不报，由运行时的逐键兜底回退默认值 |
| **不在这两个文件里的键，审计压根看不见** | 审计对象只有 `agentmemhub.yaml.example` 与本地 `agentmemhub.yaml`。在 `DEFAULT_*` 里定义了、但**这两个文件都没写**的键，不进检查范围 —— 2026-10-15 实测：`feed` 段四个假开关（`feed.convert.llm` / `feed.convert.ocr` / `feed.fetch.renderer` / `feed.depth_default`）就是这样**长期躲过审计**的（当时 example 里没有 `feed` 段，审计一路报"0 僵尸键"）。**推论：加配置键必须与"写进 `agentmemhub.yaml.example`"同一步做，否则这条防线是空的** |

### 5.3 本项目踩过的假开关

| 键 | 当年的表现 | 根因 | 现状 |
|---|---|---|---|
| `wiki.single_shot_max` | 配了不生效 | `scripts/wiki_compile.py` 用硬编码常量，且值恰好等于配置值 | 2026-09-21 收口到 `_limits()` |
| `wiki.workers` | 配了不生效 | `compile_all` 的 `workers=4` 默认值 + `wiki.py` 的 `workers or 4` 两处硬编码 | 2026-09-22 配置审计补上，`_limits()` 第 4 位 |
| `wiki.l2.workers` / `min_pages` / `domain_max` | 配了不生效 | `scripts/wiki_aggregate.py` 只认 argparse，默认值恰好等于配置值 | 已收口：命令行显式传值 > 配置 > 脚本常量 |
| `wiki.enabled` | **曾是假开关**：没有任何消费点，置 false 照样触发 | 只定义未接线 | 2026-09-22 接入 `wiki_triggers._configured()`。**实测复核（2026-09-27）：消费点仍只有 `status()`**，自动触发链不经过它（`agentmemhub/**/*.py` 与 `scripts/**/*.py` 全量 grep `get("enabled"` 只有 7 处，wiki 层的唯一一处就在 `_configured()` 内）→ 「置 false 后触发器不再跑」这句目前只对状态报告成立。**待核实**：是设计上就该由 `wiki.update.enabled` 管自动触发、`wiki.enabled` 只管可用性，还是漏接线；在结论出来前，要停自动触发请关 `wiki.update.enabled` |
| `feed.convert.llm` / `feed.convert.ocr` / `feed.fetch.renderer` / `feed.depth_default` | **四个从未有过消费点的假开关**（能力还没做、开关先写好了）；更坏的是**报错文案也在指引它们**（`feed/converters/url.py` 曾让用户"置 `feed.fetch.renderer=playwright`"） | **审计根本没看见它们**：`agentmemhub.yaml.example` 当时**没有 `feed` 段**，而不在这两个文件里的键不在审计范围（见 §5.2 末行）—— 所以一路报"0 僵尸键" | 2026-10-15 **删除**，并把真正有消费点的 5 个键（`db` / `store_dir` / `kb_dir` / `fetch.timeout` / `accept.exts`）补进 example；报错文案同步改掉 |
| `llm.thinking` / `llm.reasoning_effort` | 写进 yaml 读不到 | `llm` 段是**白名单过滤**，漏加进 `_LLM_SCALAR_KEYS` | 已加入白名单；注释里写明"新增 provider 开关务必同步" |

共同特征：**配置里的值恰好等于代码里硬编码的默认值**，所以既不报错也不让测试变红，能潜伏数月。

### 5.4 「配了不生效」排查步骤

1. **确认读的是哪个文件**：`AGENTMEMHUB_CONFIG` 是否指向别处；候选顺序 `agentmemhub.yaml` → `database/config.yaml` → `~/.agentmemhub/config.yaml`。`repr(Config())` 会打 `Config(file=...)`。
2. **确认这个键有没有被上层覆盖**：`logs.files.<名>` 覆盖 `logs.rotate`；`rag.retrieval.callers.<名>` 覆盖 `recall_level`；`rag.retrieval.page.*` 覆盖档位；状态文件 `overrides` 覆盖 `wiki.update.*`；`llm` 的就地覆盖见 §3.2 形态③。
3. **确认键真的被读**：跑 `check_config_keys.py`；若报 OK 仍不生效，回到 §5.2 逐条怀疑（同名不同段、被档位遮蔽、白名单外）。
4. **确认进程重启了**：yaml 只读一次；常驻进程（MCP / 面板 / daemon）改完必须重启。
5. **确认前置条件**：`wiki.*` 产线功能要求 `out_l1` / `out_l2` 都配（否则触发器的 `_targets()` 抛「未配置产出目录」）；`llm.*` 要求 `endpoint` / `api_key` / `model` 三项齐全功能才可用。**想停自动触发要关 `wiki.update.enabled`，不是 `wiki.enabled`**（见 §5.3）。
6. **确认模型侧就绪**：换了 `rag.active` 但没有向量（`rag.write.order` 没写它、也没 `reembed`）→ 检索拿不到该模型的结果。

## 6. 测试在哪

| 测试文件 | 守住什么 |
|---|---|
| `tests/test_config.py` | `Config` 默认值 / 环境变量覆盖 / yaml 解析与派生路径；`distillation` 深合并；`llm` 三键与 `headers`；`distillation.llm` 继承与部分覆盖；`llm_effective()` 标出继承 / 覆盖、**绝不回显密钥**、未配置时不炸 |
| `tests/test_config_audit.py` | 叶子键摊平、`DEFAULT_*` 块剔除、读取点匹配；假仓库能发现僵尸键；**真实仓库自检 0 僵尸键**（守护测试） |
| `tests/rag/test_rag_config.py` | `rag` 段加载（键会进入 `Settings`，是否被消费另见 §5.2）、`active` 校验、档位解析与非法值回落、`callers` 细分与回落、`threading` 局部覆盖 |
| `tests/rag/test_rag_page_policy.py` | 页面层策略（`DEFAULT_PAGE_POLICY` 与档位展开一致） |
| `tests/test_logs_rotation.py` | `logs.rotate` 全局 + `logs.files.<名>` 覆盖与回落、大小/自然日两种滚动 |
| `tests/test_snapshot.py` / `tests/test_snapshot_api.py` | `snapshot.keep` 生效值与非法值回落 |
| `tests/test_wiki_triggers.py` | `wiki.update.*` 判定、`overrides` 编辑生效与持久化、非法输入拒绝、`wiki.enabled` → `_configured()`（status 用） |
| `tests/test_wiki_scripts.py` | `wiki_compile._limits()` 取配置、非法值回落常量、命令行显式值优先 |
| `tests/test_obsidian_cli.py` | `wiki.obsidian_cli` 优先于 `OBSIDIAN_CLI` / PATH 的解析顺序 |
| `tests/test_llm_repair.py` | `repair_model` 未配时行为完全不变；配置后走修复路径 |
| `tests/test_distill.py` | `distillation.prompt_ver` 显式配置覆盖默认行为 |
| `tests/rag/test_rag_multi_model.py` / `tests/test_rag_bridge.py` | `rag.write.order` / `retrieval.models` 多模型写入与召回、档位对召回结果的影响 |

**改动配置体系的回归清单**

- [ ] 新键同时写进 `agentmemhub.yaml.example`（带中文注释）与 `agentmemhub/config.py` 的 `DEFAULT_*`；
- [ ] 键有真实读取点（不是只在 `DEFAULT_*` 或 `_get()` 里出现）；
- [ ] 若属于 `llm` 段的白名单字段，已加进 `_LLM_SCALAR_KEYS`；
- [ ] 跑 `uv run python scripts/check_config_keys.py`，0 疑似僵尸键；
- [ ] 跑 `uv run pytest tests/test_config.py tests/test_config_audit.py tests/rag/test_rag_config.py`（相关段再加 `tests/test_logs_rotation.py` / `tests/test_wiki_triggers.py` / `tests/test_snapshot.py`）；
- [ ] 单例缓存相关的改动要确认 `Config.reset()` / `configure()` 在测试里可用；
- [ ] 别并发跑两个 `uv run`（环境锁争用会造成无关测试假失败，见 [AGENTS.md](../../AGENTS.md)）。

## 7. 两种"覆盖"：一处**已实现**（运行时）、一处仍是计划（文件级）

**已实现的：触发规则的运行时覆盖**（状态文件 `logs/wiki_trigger_state.json` 的
`overrides` 段）：

- 实时与离线**各一套**（顶层四字段 = 实时；`batch` 子段 = 离线），互不干扰；
- **热生效、不用重启**：`wiki --action triggers --set-batch on|off`，
  或 `PUT /api/wiki/triggers` 传 `{"batch": {...}}`；
- 覆盖范围**只有触发规则那几个键**（见 §4）—— 不是通用配置覆盖。

**仍是计划的：文件级覆盖层 `agentmemhub.override.yaml`** —— 允许 API/CLI 改更多键
（含 `llm_batch.*` 这类不在触发规则里的），且主 yaml 的中文注释零损失。
以下出自 [batch-offline-plan.md §9](../batch-offline-plan.md) 的 A2/M4 设计：
当前代码里**没有** `agentmemhub.override.yaml` 的加载逻辑，也**没有**
`agentmemhub config` 子命令；改这类配置只能直接编辑 `agentmemhub.yaml` 或设环境变量。

| 设计点 | 计划内容 |
|---|---|
| 写回方案 | API / CLI 改的键写独立 `agentmemhub.override.yaml`；加载时 **override > 主 yaml**。主文件的中文注释与手工编辑**永不受损**（PyYAML round-trip 会丢全部注释，不可接受）；override 文件可查、可清空 |
| 白名单（Phase 1） | `wiki.update_mode`、`wiki.triggers.enabled`、`llm_batch.*`（含 `completion_window`）、`dirty_memories`。模型 / 维度 / 路径等高危键**不在白名单**（防误改导致索引错乱） |
| 僵尸键约束 | override 只允许改**已有键**，不新增键 → 天然不产生僵尸键 |
| 生效语义（D3 方案 B） | **不做运行中 reload**（避免 `Config` 单例引用方式的连锁重构）：CLI 路径天然即时（每次新进程重读）；MCP 常驻进程改动落盘后由通知提示「重启 MCP 后对其生效」；面板展示生效值时读合并结果 |
| 接口 | `GET /api/config`（生效值）、`PUT /api/config`（白名单内）；CLI `agentmemhub config get/set <path> <value>`；鉴权复用面板 password |
| 审计 | 配置修改写入通知（谁在何时把哪个键从什么改成什么），类别 `config` |

落地时本文档需要补齐：override 的加载顺序图、白名单全表、`config get/set` 用法与错误语义。
