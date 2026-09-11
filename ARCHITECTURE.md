# AgentMemHub 架构

## 定位

AgentMemHub 从所有 Agent Harness（ZCode / OpenCode / Hermes / WorkBuddy / Qwen / QoderCN / DSH / Trae）读取完整对话历史
→ 归一化为**统一事件流**（user / assistant / tool / reasoning / patch / shell，含工具链、思维链、Shell 执行）
→ 存入本地 SQLite（可搜索，可导出 JSONL / Markdown）
→ **记忆蒸馏**（LLM 离线提炼为结构化记忆）
→ **内置记忆引擎 `agentmemhub.rag`**（向量化 + 混合召回 + 价值评分，进程内直调）
→ 面板（双标签页：统一会话 / 记忆报表）与 MCP 网关（供任何 Agent 实时读写记忆）。

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
              rag_bridge.py（唯一接缝）→ MCP 五工具 / 面板网关 / CLI / scoring.py
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
│   ├── cli.py / console.py     # CLI 子命令 + start.bat 交互式控制台
│   ├── logs.py / watermarks.py # 统一日志 / 增量水位
│   ├── distill.py              # ★ 记忆蒸馏全链（切片/蒸馏/合并/去重/投影）
│   ├── llm.py                  # OpenAI 兼容 LLM 客户端（重试/截断抢救/代理策略）
│   ├── sanitize.py             # 脱敏（凭据/PII 扫描与剥离；has_substance 门限）
│   ├── scoring.py              # LLM 三轴评分（入口已隐藏；与蒸馏共用 llm 段）
│   ├── mcp_server.py           # MCP 记忆网关（stdio / Streamable HTTP）
│   ├── memos_daemon.py         # 引擎派发层（backend=rag 走 rag_bridge）
│   ├── memos.py                # [回退] MemOS bundle 生成/推送
│   ├── rag_bridge.py           # ★ 引擎接缝：MCP/面板/CLI 的统一调用入口
│   ├── rag/                    # ★ 内置记忆引擎（自有代码，可改可测）
│   │   ├── config.py           #   引擎配置（模型注册/分桶/召回/写入策略）
│   │   ├── ingest.py           #   摄取：unites 落库 + 向量 + FTS（含 schema/索引自愈）
│   │   ├── embedder.py         #   ONNX 推理（长度分桶批处理）
│   │   ├── runtime.py          #   embedder 进程级单例缓存
│   │   ├── search.py           #   三路混合召回（向量/FTS/精确串 + RRF 融合）
│   │   ├── memstore.py         #   原子记忆写侧 + 价值分（含状态式反馈/手动加权）
│   │   ├── ext.py              #   召回增强（价值 boost、轮次扩展、安全截断）
│   │   ├── eval.py             #   评测（71 题集 + MRR/precision）
│   │   └── source.py / cli.py  #   素材读取与引擎 CLI
│   └── web/                    # 面板（FastAPI + 原生 JS 单页）
│       ├── app.py              #   路由：会话/记忆报表/加权/反馈/管理任务/日志
│       ├── aggregates.py       #   统计聚合（SQL 下推 + TTL 缓存）
│       ├── tasks.py            #   后台任务（单并发 + 进度 + 日志落盘）
│       └── static/index.html   #   双标签页面板（vendor 离线化）
├── models/                     # 嵌入模型（自带 bge-small；更大模型按需下载）
├── scripts/                    # fetch_model / sync_llm_from_zcode / sensitive_scan /
│                               #   export_memories / distill_sandbox / web_verify / e2e
├── docs/                       # 使用示例/蒸馏设计/召回融合/MCP 模板/截图
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
  `units`（原子记忆 + 蒸馏投影 `role='distilled'`、`src_id='dst_<hash>'`）、
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
- **召回**：三路信号（向量 KNN / FTS5 / 精确串）→ RRF 融合 → 价值 boost（有界 ≤0.3）
  → 同会话限席 → 轮次扩展（命中轮自动带上相邻上下文）
- **价值**：`unit_values.value`（反馈演化）+ `manual_value`（用户 ⭐ 锁定，不衰减，优先）
- **接缝**：`rag_bridge.py` 以原 MemOS 端点语义实现进程内调用，
  MCP 五工具 / 面板网关 / CLI 三方契约零改动（`backend.backend: memos` 可整体回退）

### 5. 记忆蒸馏（distill.py）

离线把会话提炼为结构化记忆，四入口（Python / CLI / 控制台 `[3]` / 面板按钮）：

| 阶段 | 做什么 |
|---|---|
| S0 切片 | 固定轮数窗口（默认 16 轮）+ 话题边界细化；追加对话不改历史切片 |
| S1 段级蒸馏 | LLM 逐片提炼结构化 JSON（类型/主题/内容/置信度；脱敏；长内容限长） |
| S2 合并沉淀 | 同会话多片再合并去重（层级合并，批次化防超上下文） |
| S3 跨会话去重 | 与既有蒸馏记忆向量近邻三档（new / similar / duplicate） |
| S4 投影 | 新/相似条目写入 units → 立即可被召回 |

幂等键 = 「内容哈希 + 提示词版本」（切片级与合并级各一套 `distill_hashes`）；
fail-open（失败不登记哈希，重跑自动补）；`PROMPT_VER` 升版自动清旧版产物。

### 6. 权重体系

`来源初始分（蒸馏 0.3 / Agent 主动写入 0.6）` → `反馈演化（unit_feedback）`
→ `手动加权（manual_value，优先且不衰减）`。

- 面板 👍/👎 走**状态式反馈**（一 unit 一条当前表态，再点同极性=取消，
  无反馈回初始分）——与 MCP `memory_score` 的**历史累加**语义分开（两者并存）
- 召回时 `boost = min(0.3, |value|) × 极性`，`manual_value` 条目不参与时间衰减

### 7. Web 面板设计要点

- **双标签页**：统一会话（浏览/筛选/抽屉/改标题/删除）+ 记忆报表（筛选/展开溯源/
  ⭐加权/👍👎/表头排序/语义检索）；两页共用全局任务进度条
- **会话↔记忆绑定**：`session_uid` 为绑定键；会话行「🧠」→ 记忆页按会话筛选；
  记忆行「来源会话」→ 回会话抽屉并定位轮次（`turn_key`）
- **性能模型**：引导数据只含会话元数据；事件按需分页；记忆报表服务端分页 +
  `units(src_id)` 索引（避免 JOIN 退化为全表扫描）；统计聚合 SQL 下推 + TTL 缓存
- **用户修改保护**：改标题/删除落 `title_custom` 与墓碑表，后续 ingest 不覆盖
- **安全**：只绑 127.0.0.1；删除/加权为显式接口；无外发通道

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
