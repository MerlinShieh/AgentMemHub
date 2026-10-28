# AgentMemHub 架构

## 定位

AgentMemHub 从所有 Agent Harness（ZCode / OpenCode / Hermes / WorkBuddy / Qwen / QoderCN / DSH / Trae）读取完整对话历史
→ 归一化为**统一事件流**（user / assistant / tool / reasoning / patch / shell，含工具链、思维链、Shell 执行）
→ 存入本地 SQLite（可搜索，可导出 JSONL / Markdown）
→ **记忆蒸馏**（LLM 离线提炼为结构化记忆）
→ **内置记忆引擎 `agentmemhub.rag`**（向量化 + 混合召回 + 价值评分，进程内直调）
→ 面板（四标签页：统一会话 / 记忆报表 / **wiki知识库** / **设置**）与 MCP 网关（供任何 Agent 实时读写记忆）。

```
各 Agent 原始数据                       AgentMemHub
┌──────────────────┐   ┌────────────────────────────┐   ┌──────────────────────┐
│ ZCode db.sqlite  │   │ adapters/ (统一接口)        │   │ 采集库 agentmemhub.db │
│ OpenCode state.db│──>│ zcode/opencode/hermes/     │──>│ conversations(含 uid) │
│ Hermes state.db  │   │ qwen/qodercn/workbuddy/    │   │ events + events_fts   │
│ Qwen chats/*.jsonl│  │ dsh/trae                   │   │ memory_exclusions     │
│ WorkBuddy db     │   └────────────────────────────┘   │ deleted_conversations │
│ DSH *.zstd       │           统一事件流 models.py       └──────────┬───────────┘
└──────────────────┘                                               │
                                    ┌──────────────────────────────┴─────────┐
                                    │  distill.py  记忆蒸馏（离线，四入口）    │
                                    │  S0 切片 → S1 段级 → S2 合并 → S3 去重  │
                                    │  → S4 投影为可召回 unit（LLM 经 llm.py） │
                                    └──────────────┬─────────────────────────┘
                                                   ▼
                                    ┌──────────────────────────────┐
                                    │ 索引库 session_rag.db         │
                                    │ units / unit_vectors / fts    │
                                    │ unit_values(+manual) / feedback│
                                    │ distilled_memories / hashes   │
                                    └──────────────┬───────────────┘
                                                   ▼
              rag_bridge.py（唯一接缝）→ MCP 工具（记忆 5 / 投喂 3）/ 面板网关 / CLI / scoring.py
```

## 目录结构

```
AgentMemHub/
├── agentmemhub/                # Python 包
│   ├── models.py               # 统一事件模型：Event 数据类 + 归一化/渲染
│   ├── schema.sql              # 采集库 schema：conversations / events / events_fts
│   ├── store.py                # 采集库存储层（增量 upsert / FTS / 会话 uid / 删除墓碑）
│   ├── adapters/               # 各 Agent 适配器（统一接口 AgentAdapter）
│   ├── export.py               # JSONL / Markdown 导出
│   ├── config.py               # 统一配置（agentmemhub.yaml：路径/后端/LLM/蒸馏）
│   ├── config_doc.py           #   yaml 带注释解析（面板配置页数据源；行扫描，不用 safe_load）
│   ├── cli.py / console.py     # CLI 子命令 + start.bat 交互式控制台
│   ├── logs.py / watermarks.py # 统一日志 / 增量水位
│   ├── distill.py              # ★ 记忆蒸馏全链（切片/蒸馏/合并/去重/投影）
│   ├── llm.py                  # OpenAI 兼容 LLM 客户端（重试/截断抢救/代理策略）
│   ├── sanitize.py             # 脱敏（凭据/PII 扫描与剥离；has_substance 门限）
│   ├── scoring.py              # LLM 三轴评分（入口已隐藏；与蒸馏共用 llm 段）
│   ├── mcp_server.py           # MCP 记忆网关（stdio / Streamable HTTP；含调用审计）
│   ├── engine_api.py         # 引擎门面（/api/v1/* 语义 → 进程内 rag_bridge）
│   ├── rag_bridge.py           # ★ 引擎接缝：MCP/面板/CLI 的统一调用入口
│   ├── health.py               # 记忆库一致性巡检（面板健康卡片/CLI/测试共用判据）
│   ├── failures.py             # ★ 长任务失败清单（分类/汇总/单独重跑；wiki 两级共用）
│   ├── snapshot.py             # 快照与回滚（索引库整库 + wiki 产物目录）
│   ├── notify.py               # ★ 通知：落库（SQLite）/ 推送策略（五个开关）/ **可插拔通道注册表**
│   ├── obsidian.py             # Obsidian 官方 CLI 封装 —— 产物链接的**权威验收**（验收前先重载索引）
│   ├── wiki.py                 # ★ Wiki 更新流程编排（增量/全量、两级衔接、基线刷新、投影）
│   ├── wiki_triggers.py        # 自动更新触发器（规则判定 + 事件驱动惰性收割 + 离线优先 dispatch）
│   ├── wiki_manifest.py        # 两级编译清单（manifest_l1/l2：记"实际编译成了什么"）
│   ├── wiki_pending.py         # 待更新记忆的查看/软删除/硬删除/恢复
│   ├── wiki_index.py           # 页面层投影：L2 知识页 → 召回面（units）
│   ├── batch_client.py         # MiMo Batch API 客户端（上传/建任务/查状态/对账/取消/下载）
│   ├── llm_batch.py            # 离线运行器接缝（OfflineRunner + 任务现场 + 终局判定）
│   ├── wiki_batch.py           # 离线编排（查状态 → 决策 → 取结果 → 作废回池 + 通知）
│   ├── batch_ops.py            # CLI / HTTP 共用的批处理操作层（precheck/status/run/cancel/…）
│   ├── task_queue.py           # 通用异步任务收割（节流 + 跨进程锁 + 检测流水，**无常驻进程**）
│   ├── rag/                    # ★ 内置记忆引擎（自有代码，可改可测）
│   │   ├── config.py           #   引擎配置（模型注册/分桶/召回/写入策略）
│   │   ├── ingest.py           #   摄取：unites 落库 + 向量 + FTS（含 schema/索引自愈）
│   │   ├── embedder.py         #   ONNX 推理（长度分桶批处理）
│   │   ├── runtime.py          #   embedder 进程级单例缓存
│   │   ├── search.py           #   六路通道召回（向量/FTS/标识符/短语 + 页面×2 + RRF）
│   │   ├── memstore.py         #   原子记忆写侧 + 价值分（含状态式反馈/手动加权）
│   │   ├── ext.py              #   召回增强（价值 boost、轮次扩展、安全截断）
│   │   ├── eval.py             #   评测（71 题集 + MRR/precision）
│   │   └── source.py / cli.py  #   素材读取与引擎 CLI
│   └── web/                    # 面板（FastAPI + 原生 JS 单页）
│       ├── app.py              #   路由：会话/记忆报表/知识库/加权/反馈/管理任务/日志
│       ├── aggregates.py       #   统计聚合（SQL 下推 + TTL 缓存）
│       ├── tasks.py            #   后台任务（单并发 + 进度 + 日志落盘）
│       ├── wiki_pages.py       #   知识库页面清单/读取（**以磁盘为准**；路径限根内）
│       └── static/index.html   #   四标签页面板（vendor 离线化）
├── models/                     # 嵌入模型（自带 bge-small；更大模型按需下载）
├── scripts/                    # fetch_model / sync_llm_from_zcode / sync_llm_from_dsh /
│                               #   export_memories / distill_sandbox / web_verify /
│                               #   health_check（记忆库一致性巡检）/ mcp_log（调用审计查询）/
│                               #   wiki_compile + wiki_aggregate + wiki_linkfix（LLM Wiki 两级编译）/
│                               #   e2e
├── docs/                       # 使用示例/蒸馏设计/召回融合/LLM Wiki/MCP 模板/截图
├── database/                   # 采集库 + 索引库（gitignore：真实数据）
└── README.md / ARCHITECTURE.md / AGENTS.md / SECURITY.md / pyproject.toml
```

## 核心设计

### 1. 统一事件模型（models.py）

每个会话 = 元数据 + 有序事件流：

| role | 含义 | 关键字段 |
|---|---|---|
| `user` | 用户输入 | content |
| `assistant` | Agent 文本回复 | content, model |
| `tool` | 工具调用/结果（含 Shell） | tool_name, tool_input, tool_output, tool_status |
| `reasoning` | 思维链 | content / reasoning |
| `patch` | 代码变更 | patch_file, patch_diff |
| `shell` | Shell 执行 | shell_cmd, shell_output |
| `meta` | 事件边界/元信息 | content, model |

每个事件保存 `raw_json`（各 Agent 原始事件 JSON）实现**无损保底**；
`src_id` / `turn_key` 是跨源稳定锚（记忆轮边界与幂等摄取）。

### 2. 双 SQLite 库

- **采集库**（`database/agentmemhub.db`，只增不删原文）：
  `conversations`（含 `session_uid` 全局递增 ID、`title_custom` 用户改标题标记）、
  `events`（PK `(source, conversation_id, seq)`，含 raw_json）、`events_fts`（FTS5 + LIKE 兜底）、
  `memory_exclusions`（不写入记忆的会话/轮次）、`deleted_conversations`（删除墓碑防回灌）
- **索引库**（`database/session_rag.db`，可由采集库重建）：
  `units`（原子记忆 + 蒸馏投影 `role='distilled'`、`src_id='dst_<hash>'`、`tags` 标签、
  `updated_at` 写入/改写时刻——注意 `time` 是**事件时间**，两者语义不同）、
  `unit_vectors`（sqlite-vec）、`units_fts`、`unit_values`（`manual_value` ⭐ 锁定）、
  `unit_feedback`、`distilled_memories`（蒸馏终稿）、`distill_hashes`（幂等锚）

### 3. Adapter 架构（adapters/）

```python
def candidate_paths(self) -> list[Path]   # 数据路径发现
def locate(self) -> Optional[Path]        # 找第一个存在路径
def load(self, path) -> list[dict]        # 返回统一会话列表
```
约定输出：`{source, id, title, cwd, created_at, updated_at, model, meta, events: [Event]}`。
ZCode / OpenCode 共享 `SqliteConversationAdapter`；新增 Agent = 一个 adapter 类 + 注册。

### 4. 内置记忆引擎（rag/）

- **摄取**：units 落库 + 向量（长度分桶批量推理）+ FTS 三处同步；`src_id` 归一化去重
- **召回**：六路通道（向量 KNN / FTS5 / 标识符 / 多词短语 + 页面专属两路）→ RRF 融合
  → 价值 boost（有界 ≤0.3）→ 阈值截断（**页面豁免**）→ 页面准入 → 同会话限席
  → 页面保位 → 轮次扩展（命中轮自动带上相邻上下文）。
  权威描述见 `docs/data-architecture.md` §4
- **价值**：`unit_values.value`（反馈演化）+ `manual_value`（用户 ⭐ 锁定，不衰减，优先）
- **接缝**：`rag_bridge.py` 以原 MemOS 端点语义实现进程内调用，
  MCP 五工具 / 面板网关 / CLI 三方契约零改动；MemOS 上游已于 v2.8 移除，无回退开关

### 5. 记忆蒸馏（distill.py）

离线把会话提炼为结构化记忆，四入口（Python / CLI / 控制台 `[3]` / 面板按钮）：

| 阶段 | 做什么 |
|---|---|
| S0 切片 | 固定轮数窗口（默认 16 轮）+ 话题边界细化；追加对话不改历史切片 |
| S1 段级蒸馏 | LLM 逐片提炼结构化 JSON（类型/主题/内容/置信度；脱敏；长内容限长） |
| S2 合并沉淀 | 同会话多片再合并去重（层级合并，批次化防超上下文） |
| S3 跨会话去重 | 与既有蒸馏记忆向量近邻三档（new / similar / duplicate） |
| S4 投影 | 新/相似条目写入 units → 立即可被召回 |

**投影与状态必须一致**：`status` 只决定"要不要投影"，不决定"已有投影是否
还在"。条目被 S2 归档为 `merged` 时，units 投影由 `save_merged` 即时回收，
`reclaim_stale_projections` 再幂等兜底扫掉历史遗漏——否则被取代的旧稿会与
新终稿同台召回（内容相近但不相同）。

幂等键 = 「内容哈希 + 提示词版本」（切片级与合并级各一套 `distill_hashes`）；
fail-open（失败不登记哈希，重跑自动补）；`PROMPT_VER` 升版自动清旧版产物。

### 6. 权重体系

`来源初始分（蒸馏 0.3 / Agent 主动写入 0.6）` → `反馈演化（unit_feedback）`
→ `手动加权（manual_value，优先且不衰减）`。

- 面板 👍/👎 走**状态式反馈**（一 unit 一条当前表态，再点同极性=取消，
  无反馈回初始分）——与 MCP `memory_score` 的**历史累加**语义分开（两者并存）
- 召回时 `boost = min(0.3, |value|) × 极性`，`manual_value` 条目不参与时间衰减

### 7. Web 面板设计要点

- **四标签页**：统一会话（浏览/筛选/抽屉/改标题/删除）+ 记忆报表（筛选/展开溯源/
  ⭐加权/👍👎/表头排序/语义检索/**时间线明细**）+ **wiki知识库**（L2 蒸馏产物 + 外部投喂页面：
  左域栏 / 右列表 / 右栏内渲染正文；**数据以磁盘为准**，不查投影表；正文的 `[mNNN]`
  渲染成可点引用，点开速览浮层）+ **设置**（`agentmemhub.yaml` 只读视图：按模块分组、
  带原注释、标出环境变量覆盖，**没有写接口**）；四页共用全局任务进度条
- **会话↔记忆绑定**：`session_uid` 为绑定键；会话行「🧠」→ 记忆页按会话筛选；
  记忆行「来源会话」→ 回会话抽屉并定位轮次（`turn_key`）
- **性能模型**：引导数据只含会话元数据；事件按需分页；记忆报表服务端分页 +
  `units(src_id)` 索引（避免 JOIN 退化为全表扫描）；统计聚合 SQL 下推 + TTL 缓存
- **用户修改保护**：改标题/删除落 `title_custom` 与墓碑表，后续 ingest 不覆盖
- **安全**：只绑 127.0.0.1；删除/加权为显式接口；无外发通道

### 8. LLM Wiki 两级编译（派生层，不反向影响上面各层）

把已沉淀的**蒸馏记忆**编译成结构化、互相链接、人可读的 markdown 页面。
与 RAG 是**互补**关系：RAG 优化「能不能捞到」，wiki 优化「有没有结构」。

设计约束（决定了实现形态）：

- **单向**：记忆可以影响 wiki，**wiki 绝不反向改写记忆**；产物是纯 markdown，
  随时可全量重建，删掉无副作用。
- **只读运行**：脚本以 `mode=ro` 打开索引库，不写任何数据表。
- **必须两级**：输出长度有硬上限。实测 45 条记忆一次性编译时 prompt 15150 +
  思维链 21483 tokens，输出到 32K 仍被截断（`finish_reason=length`）。
  所以先**逐会话**编译（第一级），再**跨会话**聚合（第二级）。
- **编号必须提升**：页面正文的溯源从**会话内序号** `[n]` 提升为
  **全局记忆号** `[m<id>]` —— 聚合后不会撞号，且能直接追回 `distilled_memories`。
  重建映射**必须查库**（按 `created_at, id` 重算会话内序号），
  只靠产物 json 的 `sources` 会漏掉一批引用（该数组没列全，实测 250/5552）。

三个脚本的分工：

| 脚本 | 职责 |
|---|---|
| `scripts/wiki_compile.py` | 第一级：逐会话「先分组、再编译」，两段式 |
| `scripts/wiki_aggregate.py` | 第二级：归域 → 域内细分 → 重新编译（按最终页并发） |
| `scripts/wiki_linkfix.py` | 收尾：按「被合并页 → 合并后页」重定向失效链接（92.6% → 0%） |

**长任务可运维性**：两级都可能跑几十分钟、数百次 LLM 调用，因此
每次调用落 `logs/wiki.log`、每次失败落 `failures.jsonl`（可 `--retry-failed` 定向补跑）。
详见 [docs/llm-wiki.md](docs/llm-wiki.md)。

### 9. 离线批处理（Batch API，A2）

wiki 第二级的**编译段**可以走厂商离线批处理（MiMo Batch API，官方价是实时的 50%、
闲时调度）。形态是**三段式**：

```
① 提交轮（实时跑完 L1 + 归域 + 细分）→ 把"逐最终页编译"交出去
② 等待（实测 18 分钟 ~ 24 小时）
③ 落盘轮（回读 → 组装页面 → 写盘 → linkfix → 刷基线）
```

设计要点（**为什么这么切**）：

- **只有编译段离线**：细分段实测只有 3~11 条请求（实时 1 分钟级），为省那点钱多付
  一整轮排队延迟（实测排过 24 小时）不划算；编译段是"每个最终页一条"（实测
  113~258 条），正是离线划算的地方。
- **自动收割，无常驻进程**：`task_queue.harvest()` 挂在 MCP 启动 / 写入记忆 /
  蒸馏收尾 / **任意 CLI 命令**上，三层短路（队列空 ⇒ 零成本返回 → 节流 300s →
  跨进程锁），所以"顺手检查"没有可感成本。
- **发起也能自动**：`wiki.batch.enabled` + 规则命中 ⇒ 提交离线批次，判定顺序
  **离线优先 → 实时兜底**；两条通道共用 `fired` / `last_update_at`，所以无论走哪条，
  一天只更新一次。
- **真凭据操作**：建任务即可能计费，所以测试一律用 fake uploader
  （`tests/test_offline_e2e.py`，全程不发真请求）+ 沙箱熔断。

要**怎么用/怎么排查**看 [docs/modules/batch-offline.md](docs/modules/batch-offline.md)；
要**为什么这么设计**看 [docs/batch-offline-plan.md](docs/batch-offline-plan.md)（M0–M6 追踪）。

### 10. 通知系统（落库 / 策略 / 通道 三层解耦）

三个概念**必须分开**，否则"要不要提醒我"会散落进几十个调用点：

| 层 | 做什么 | 落点 |
|---|---|---|
| **落库** | 一条通知**总是**先写进 SQLite（有未读态、可查询） | `database/notify.db` |
| **策略** | `notify.push_when`：**要不要往外推**。两族开关取**或** —— 按类别（`memory_write` / `wiki_trigger`）或按等级（`info` / `warning` / `error`） | `agentmemhub.yaml` |
| **通道** | **推到哪**：可插拔注册表 `register_channel(name, fn)`，内置 `pushplus` | `agentmemhub/notify.py` |

- **落库永不受策略影响**（策略只管"外发"）—— 否则静默期会把事实一起丢掉。
- **加一个渠道只要三步**（写函数 → `register_channel` → 配置开开关），
  详见 [docs/modules/notify.md](docs/modules/notify.md)。
- `notify()` 先做**去重窗口**（`dedup_window_s`，默认 300）再落库、再按策略推送；
  某个通道抛异常**不影响其他通道**。
- 另有事实流 `logs/notify.log`（谁在什么时候发了什么、成功与否）—— 与 SQLite
  职责不同：那边记**状态**，这边记**流水**。

## 使用

```bash
python -m agentmemhub ingest            # 提取全部 adapter 并入库
python -m agentmemhub sync              # 增量同步 + 向量化写入记忆索引（日常入口）
python -m agentmemhub distill           # 记忆蒸馏（需配置 llm 段；幂等可重跑）
python -m agentmemhub search "登录"     # 搜索
python -m agentmemhub show zcode <id>   # 查看会话 (Markdown)
python -m agentmemhub export --format jsonl --out exports/   # 全量导出
python -m agentmemhub score             # LLM 批量评分（增量优先）
python -m agentmemhub serve             # 记忆面板 http://127.0.0.1:8086
```
