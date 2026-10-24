> 覆盖模块：`agentmemhub/rag/**` · 最后核对：2026-10-23 ·
> 召回融合的算法与评测数据见 [recall-fusion.md](../recall-fusion.md)，
> 表结构与 ID 锚见 [data-architecture.md](../data-architecture.md) §三层数据模型

# 内置记忆引擎（rag）

## 1. 职责边界

v2.0 起这个项目自带记忆引擎（`agentmemhub.rag`），进程内直调、**没有 daemon 可启停**。
它管的是"记忆怎么存、怎么取"，不管"记忆怎么产生"（那是蒸馏的事）。

**做什么**

| 职责 | 落点 |
|---|---|
| 采集库 → 索引库的搬运与向量化（`units` + `vec_*`） | `rag/ingest.py`（670 行：`run_ingest` / `run_reembed` / `run_ingest_multi`） |
| 多路召回 + 候选融合 + 相对阈值准入 + 多样性 | `rag/search.py`（909 行：`hybrid_search` 等） |
| 记忆写入与价值体系（base / manual / feedback 三路合分、时间衰减） | `rag/memstore.py`（373 行）、`rag/ext.py` |
| 页面（L2 知识页）的独立准入策略：配额 + 证据排序 | `rag/search.py::apply_page_policy` / `reserve_pages` / `page_evidence` |
| ONNX 嵌入推理（模型注册、批大小、线程数） | `rag/embedder.py`、`rag/runtime.py` |
| 召回效果评测（hit@k / MRR） | `rag/eval.py`（`run_eval` / `format_report`） |
| 引擎自身的读配置 | `rag/config.py`（`load_settings`，**自己读同一份 yaml 的 `rag` 段**，不经 `Config`） |

**不做什么**

| 不做 | 当前语义 |
|---|---|
| **不写采集库** | `source.py::open_source_ro()` 以只读方式打开；跨库查询用 `ATTACH ... AS src` 只读挂载。采集是适配器的事，引擎只读 |
| 不改写记忆内容 | 向量化是**派生**：`units.content` 原样保留，embedding 只是旁路索引 |
| 不做 LLM 判断 | 终审（`LLMFinalJudge`）通过 `llm.py` 调用，引擎本身不含提示词逻辑 |
| 不跨模型共用向量表 | 向量表名由模型 spec 决定（见 §3.6）；换模型是**并列新增**，不是覆盖 |
| 不保证"每条记忆都被召回" | 页面池与低价值记忆有独立闸门，召回面是**筛选后的**，不是全量 |

## 2. 对外入口

| 入口 | 位置 | 说明 |
|---|---|---|
| 引擎 CLI | `python -m agentmemhub.rag.cli` | 子命令 `ingest` / `stats` / `reembed` / `eval` / `search` / `bench` |
| 上层 CLI | `python -m agentmemhub memos` / `search` / `weight` / `rebuild` | 经 `cli.py` 转调，多一层参数整理与日志 |
| MCP 工具 | `memory_stats` / `memory_search` / `memory_save` / `memory_score` / `memory_recent` | `mcp_server.py` 里 5 个，**给 Agent 的语义接口** |
| HTTP | `/api/memos/{status,search,weight,feedback,traces}`、`/api/memories` | 面板用；契约见 [API.md](../API.md) |
| Python | `agentmemhub.rag.ingest.run_ingest(...)`、`search.hybrid_search(...)`、`memstore.save_memory(...)` | 测试与脚本直接调，是**最稳定的入口** |

## 3. 数据流与不变量

### 3.1 三库分工

| 库 | 角色 | 打开方式 |
|---|---|---|
| **采集库**（`agentmemhub.db`） | 原始会话与事件，只读真源 | `open_source_ro()`，只读 |
| **索引库**（`index.db`） | `units`（召回单元）+ `vec_*`（向量）+ 蒸馏/投影/价值表 | `open_index()`，可写 |
| **记忆存储**（同库 `memstore` 段） | 价值分、手动加权、用户反馈 | `memstore.py` 的各函数 |

### 3.2 写入链路

```
采集库 events ──切片──> units（每 unit 一条，带 src_id 锚）
                          │
                          ├──> 向量化 ──> vec_<model>（一堆 float32 blob）
                          └──> 投影投影：distilled_memories / wiki 页 ──> units(src_id = dst_* / wiki_*)
```

**`src_id` 前缀是层级的唯一可靠判据**（`search.py::kind_of`）：

- `wiki_*` → `page`（L2 知识页）
- `dst_*` / `mcp_*` → `memory`（蒸馏记忆 / Agent 直写）
- 其余 → `message`（原始对话切片）

### 3.3 召回：多路 → 融合 → 准入 → 多样性

**通道**（名字即 `HybridHit.channels` 的键）：`vec`（active 模型）、`vec:<model>`（其余配置模型）、
`fts`（全局全文）、`page_fts`（页面词面）、`ident`（标识符：路径/函数名/错误码）、
`phrase`（短语整串）、`page`（页面池 KNN）。

**融合**（`fuse_channels` + `relevance_of`）不是简单相加：

```python
rel = max(该 unit 在所有通道的原始分) + rrf_weight * Σ 1/(rrf_k + rank)
```

即"**最高原始分**打底 + **RRF 排名贡献**加分"——单通道高分能进，多通道同时命中会被抬起来。
多通道命中的意义不是分数叠加，而是**证据交叉**。

**准入**（`threshold_filter`）是**相对阈值**，不是绝对分：

- `rel >= floor × top` 留下（top 是本次查询的最高分，所以尺度自适应）
- 例外：命中 **≥2 个通道**且最好通道分 `>= strong` 时可**旁路**——防"纯关键词 rank-0 命中
  被 cosine 尺度绞杀"
- **页面不参与这条阈值**（`exempt_ids`）：页面的 rel 天然低一个量级（实测 0.014~0.03），
  0.2×top 对它们是系统性误杀（实测拼错查询 `windowsctrol` 下，两条有字面证据的真相关页面
  rel≈0.19 正好卡在线外被剔掉）。页面有自己的准入（下一步）。

**页面准入**（`apply_page_policy`）按**证据等级**排序，配额分配：

| 等级 | 信号 | 含义 |
|---|---|---|
| `literal` | `phrase` / `ident` / `page_fts` / `fts` | 查询词确实出现在内容里，可信度最高 |
| `vector` | `vec` / `vec:<model>` | 语义邻近但含偶然 |
| `pool` | 只有 `page` | 页面池只有几百条，对任何查询都能凑出 top-k，**单独出现几乎不构成证据** |

**多样性**（`select_diverse`）限制同一会话最多 2 条，防"一个会话占满全部结果"。

### 3.4 价值体系：三个来源合分 + 时间衰减

| 来源 | 写入方 | 行为 |
|---|---|---|
| base | `ensure_base_value` / `backfill_base_values` | 来源决定起点（Agent 主动写入 0.6、蒸馏按置信度等） |
| manual | `set_manual_value` | ⭐ 手动加权 → **锁定，不随时间衰减**（`no_decay_ids` 查询器） |
| feedback | `put_feedback` / `set_user_feedback` | 👍/👎 **状态式**（一个 unit 一条，可撤销，回退到来源初始分） |

衰减用 `ext.decayed_value(value, age_seconds, ...)`，加成用 `apply_value_boost`。
**MCP 的 `memory_score` 是历史累加，面板的 👍/👎 是当前表态**——两者语义不同，
不要用同一个字段理解（累加均值会越点越钝且无法取消，不适合人工按钮）。

### 3.5 换模型：向量表并列新增，不覆盖

向量表名由 `ModelSpec` 决定。换 `rag.active` 会**新开一张表**，旧模型的表原样留着：

- `reembed` 补算缺失向量；`gc_vec_orphans` 清理孤儿子行；`drop_vec_table` 只在显式要求时删表
- 所以"换模型"是**可回退**的：改回 `active` 立刻回到旧向量
- 代价是磁盘增长（每个模型一份向量），这是刻意的取舍

### 3.6 两条硬约束

1. **采集库只读**。引擎里任何路径都不应出现对源库的 INSERT/UPDATE；跨库 JOIN 一律 `ATTACH ... AS src` 只读。
2. **`src_id` 前缀即层级**。改动前缀语义（比如把 `mcp_` 改成别的）会同时打断召回层级判定、
   页面池识别、wiki 溯源——改之前先 grep `kind_of` 与两个前缀常量。

## 4. 配置项

`agentmemhub.yaml` 的 `rag` 段由 `rag/config.py::load_settings()` **自行读取**，不经过 `Config` 类：

| 键 | 作用 |
|---|---|
| `rag.active` | 当前模型 id（必须是已登记模型，且目录里 `onnx` / `tokenizer.json` / `config.json` 齐全） |
| `rag.embed.*` | 批大小、线程数、归一化方式 |
| `rag.retrieval.recall_level` | **召回严格度档位**：一次决定候选宽度 `candidate_k` 与相对阈值 `threshold_floor`（调用方显式传参可覆盖） |
| `rag.retrieval.models` | 参与多模型向量路的名字列表；active 模型通道名固定为 `vec`（兼容既有 `vec_rank` 语义） |
| `rag.write.*` | 写入策略（是否写页、是否写 memory 投影等） |
| `rag.page_policy.*` | 页面池的配额与证据权重 |

环境变量覆盖见 [config.md](config.md) §3。**改 yaml 后必须重启进程**（`config()` 与
`load_settings()` 都是单例）。

## 5. 调试与排障

| 现象 | 排查路径 |
|---|---|
| 搜不到明明存在的词 | 先看通道：`hybrid_search(mode="fts")` 单独跑全文，通了说明向量路或融合的问题；再看 `threshold_floor` 是否把相对分卡掉 |
| 页面完全不出现在结果里 | 页面不参与 `threshold_filter`，所以不是阈值问题——查 `apply_page_policy` 的配额与 `page_evidence` 等级（只有 `pool` 命中的页面基本进不来） |
| 换模型后召回变差 | `rag.active` 改回去即可（旧向量表还在）；确认新模型 `dim` 与建表时一致 |
| 向量数少于 units 数 | `run_reembed` 补算；`run_stats` 里对账 `units` vs `vec_*` 行数 |
| 价值分不生效 | 分清三个来源；手动加权锁定后不衰减，反馈是状态式可撤销；`no_decay_ids` 查的是锁定集合 |
| 记忆写入后搜不到 | 写入是**派生**：先落 `units`，再向量化。写入成功但向量失败时，`units` 有、`vec` 无——`/api/memos/status` 与健康检查会报一致性 |

**评测**：`python -m agentmemhub.rag.cli eval --cases <path>` 跑 hit@k / MRR，
报告格式见 `eval.format_report`。

## 6. 测试在哪

| 文件 | 守住什么 |
|---|---|
| `tests/test_rag_bridge.py`（514 行） | 三库桥接：只读打开的源库、`ATTACH` 只读、schema 校验、切片与 `src_id` 前缀 |
| `tests/test_memos.py`（243 行） | 引擎对外语义：写入、检索、状态 |
| `tests/test_scoring.py`（619 行） | 价值分数值演化：base / manual / feedback 合分、衰减、锁定不衰减 |
| `tests/test_incremental.py`（503 行） | 增量：水位线推进后 `units` 的增删一致 |
| `tests/test_health.py`（147 行） | 一致性断言（`units.updated_at` 覆盖、投影一致、FTS 与向量一致）与回收 |
| `tests/test_llm.py` / `test_llm_timeout.py` | 引擎用到的 LLM 客户端（终审、清洗）——超时与重试在 `llm.py` 那一侧 |

改引擎时的最小自检：

```powershell
.venv\Scripts\python.exe -m pytest tests/test_rag_bridge.py tests/test_memos.py tests/test_scoring.py -q
.venv\Scripts\python.exe -m agentmemhub.rag.cli stats      # 行数对账
```
