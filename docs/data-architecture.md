# AgentMemHub 数据与系统架构全景

> 本文是数据层与架构层的**权威参考**：三个存储的分工、每张表的生产/消费关系、
> ID 锚体系、召回链路、价值体系、wiki 编译与增量同步机制。所有机制均经过实测
> 验证（2026-09-19，feat/llm-wiki 分支）。
>
> 历史文档分工：`ARCHITECTURE.md`（系统模块）、`docs/recall-fusion.md`（召回实测）、
> `docs/memory-distillation.md`（蒸馏设计）、`docs/llm-wiki.md`（Wiki 编译实测）、
> `docs/API.md`（对外接口）。本文聚焦**数据怎么流、ID 怎么关联**。

---

## 0. 一图总览

```
┌───────────────────────────────────────────────────────────────────────────┐
│ ① 源库 database/agentmemhub.db（采集层，只读真相源）                        │
│    conversations（统一会话，session_uid 全局递增）                          │
│    events（原始对话事件）  memory_exclusions（排除 / Tombstone）            │
└──────────────┬────────────────────────────────────────────────────────────┘
               │ ② ingest 摄取（会话级增量，持续运转）
               ▼
┌───────────────────────────────────────────────────────────────────────────┐
│ ③ 索引库 database/session_rag.db（检索层 + 记忆实体层，自持）               │
│                                                                           │
│  units（召回投影面，~2.3 万行）——六路通道都只查它                          │
│    ├ 消息层  msg:/line:/p:* 投影          ←─ ② ingest                     │
│    ├ 记忆层  dst_<content_hash> 投影      ←─ ③ 蒸馏 S4                    │
│    ├ 手写层  mcp_<content_anchor> 投影    ←─ ④ memory_save（引擎写入）      │
│    └ 页面层  wiki_<内容指纹> 投影          ←─ ⑥ 页面投影（整页，不切片）      │
│  vec_* / FTS 影子表（向量 + 全文索引）   unit_values（⭐/👍👎）              │
│                                                                           │
│  distilled_memories（蒸馏表，记忆实体账本）                                 │
│    状态生命周期：new → similar → merged/duplicate                          │
│  distill_hashes（蒸馏幂等登记）                                            │
└──────────────┬────────────────────────────────────────────────────────────┘
               │ ⑤ wiki 编译（两级 LLM，输入 = status IN new/similar）
               ▼
┌───────────────────────────────────────────────────────────────────────────┐
│ ⑥ wiki 产物目录（配置 wiki.out_l1 / wiki.out_l2）                          │
│    L1 会话页 + L2 主题域页 + index.md + manifest_l1/l2.json                │
│    + failures.jsonl + _domains.json                                       │
└───────────────────────────────────────────────────────────────────────────┘
```

**唯一引擎事实**：`memory_backend` 默认 `rag`（自研内核，`agentmemhub/rag/`）。
MCP 五工具虽有 `/api/v1/*` 的 MemOS 端点语义（兼容层），但 `engine_request` 在
rag 后端下**进程内直调 `rag_bridge`，不发 HTTP**；`memOS/` 目录只是可一行回滚的
vendored 存货，数据路径上无任何 MemOS 残留。`memory_stats` 里的
`traces/episodes` 是 rag_bridge 对索引库数据的兼容命名视图。

---

## 1. 三条记忆生产线（汇入蒸馏表）

| 生产线 | 触发 | 加工方式 | 落表 |
|---|---|---|---|
| **A. 离线记忆蒸馏**（主） | `agentmemhub distill`（源库有新会话即增量） | **LLM 提炼**：会话事件 → 分片 → 提炼 → S3 跨会话去重 → S4 投影 | `distilled_memories`（new）+ units dst_ 投影 |
| **B. Agent 在线直写** | MCP `memory_save`（每写一条） | **不经 LLM**——内容已是记忆形态；脱敏兜底 + 指纹幂等 + 落表即补分/投影 | `distilled_memories`（new）+ 手写投影 + dst_ 投影 |
| **C. 历史补账** | `wiki --action backfill-manual`（幂等） | 同 B，把 B 通道上线前的存量手写记忆落账 | 同 B |

两条活跃来源（A 离线批量、B 在线零散）在蒸馏表汇合——**蒸馏表是记忆实体的
统一账本**，wiki 编译与记忆报表都以它为输入。统一会话管道是**持续增量**的
（新会话不断被各 harness 产生并摄取），"离线"指加工方式，不是一次性。

---

## 2. 核心表清单（作用 / 来源 / 消费）

| 表（库） | 作用 | 数据来源 | 消费方 |
|---|---|---|---|
| **conversations**（源库） | 统一会话登记，`session_uid` 全局递增 | 各 harness adapter | 面板会话页；蒸馏原料定位；记忆报表"来源会话"列 |
| **events**（源库） | 原始对话事件（真相源） | 同上 | 蒸馏的直接原料；units 消息投影的原料 |
| **memory_exclusions**（源库） | 会话/轮次排除与 Tombstone | 面板删除、排除操作 | 摄取与蒸馏的防回流过滤 |
| **units**（索引库） | **唯一召回投影面** | 三层：ingest 消息投影 / 蒸馏 dst_ 投影 / memory_save 手写投影 / **wiki 页面投影** | **六路通道召回**（§4）；面板 src_id 锚定位 |
| **unit_values**（索引库） | ⭐ 值与 👍👎 反馈 | 来源初始分（0.3/0.6）+ importance 精调 + 反馈演化 | 召回排序加权；面板 ⭐ |
| **distilled_memories**（索引库） | **记忆实体账本**（类型/主题/置信度/生命周期） | 生产线 A/B/C | **wiki 编译唯一输入**；面板蒸馏路；S3 去重池；dst_ 投影源 |
| **distill_hashes**（索引库） | 蒸馏幂等登记 | save_memories | 蒸馏断点续跑 |
| **manifest_l1/l2.json**（wiki 目录） | 编译基线：输入 id → 内容指纹 + 会话归属 | 两级编译收尾自动写 | align / 增量 update / 触发器 |
| **failures.jsonl**（wiki 目录） | 编译失败清单（跨轮累积） | 编译失败记录 + 销账 | retry 补跑、needs_manual 提示 |
| **_domains.json**（wiki 目录） | L2 域结构（域名 → 成员页序号） | 全量归域生成；增量收尾按最新序号重写 | L2 编译的成员依据 |

---

## 3. ID 与锚体系（⚠️ 设计上最需要小心的地方）

**一张图记住所有 ID**（以一条 Agent 直写记忆为例）：

```
MCP memory_save(content="…", importance="high", type="lesson")
  │
  ├─ tid = _trace_id(content, ts)          ← AgentMemHub 计算：'mcp_<hash>'
  │    ├─ 返回给调用方（trace_id）           ← memory_score 靠它定位
  │    ├─ bundle.traces[0].id = tid ──→ 引擎 import_bundle：
  │    │     units.legacy_id = tid        ★ 引擎存储的 trace id（去重/feedback 锚）
  │    │     units.src_id = content_anchor(text)   ← 内容锚 'mcp_<sha256(原始内容)[:16]>'
  │    │       （与 tid 是两套 hash！src_id 用于引擎幂等，不用作跨表关联；
  │    │         也**不等于** content_hash —— 后者用规范化后的内容算）
  │    └─ 蒸馏表 distilled_memories（save_direct_memory）：
  │          slice_key = 'mcp:<tid 去 mcp_ 前缀>'   ← wiki/面板关联锚
  │          content_hash = fingerprint(content)    ← 内容指纹 sha256[:16]（规范化后）
  │          status = 'new'，type/importance 随参数
  │
  └─ 投影（S4/直写补投影）：
       units dst_ 行：src_id = 'dst_' || content_hash   ← 记忆层的检索分身
       unit_values：value = 0.8（importance 精调）/ 0.6（Agent 直写）/ 0.3（离线蒸馏）
```

### 各 ID 的定义速查

| ID / 锚 | 生成者 | 存储位置 | 用途 | ⚠️ 易错点 |
|---|---|---|---|---|
| `session_uid` | 源库 ingest | conversations.session_uid | 全局会话唯一 id，面板双向跳转 | 与 (source, conversation_id) 1:1 |
| `conversation_id` | 各 harness | 源库 + 蒸馏表 + units | 会话标识 | 直写记忆固定为 `'direct'`（source='mcp'） |
| `tid`（trace id） | `_trace_id(content, ts)` | 返回值；引擎 **legacy_id**；蒸馏表 slice_key 前缀 | memory_score 定位；面板去重关联 | **引擎的 units.src_id 不是 tid**，是内容锚 |
| `units.src_id`（手写层） | `content_anchor(text)` | units.src_id（`'mcp_<sha256(原始内容)[:16]>'`） | 引擎幂等（同内容不重投影）；**召回侧的双投影去重也用它** | **不能假定它与 `content_hash` 相同**——它用**原始**内容算哈希，而 `content_hash`（`fingerprint`）用**规范化后**内容（去空行 + 每行 strip）。两者只在"入库未改动内容"时一致（实测 114 条直写单元里 **11 条因 `sanitize.redact` 改动内容而分叉**）|
| `units.legacy_id` | import_bundle（存传入 tid） | units.legacy_id | trace id 的引擎侧真身 | **面板去重 / 直写关联的唯一正确锚** |
| `dst_<content_hash>` | 蒸馏表 content_hash | units.src_id（记忆层投影） | 记忆的检索分身；面板 unit_id/value | 锚不含会话——同 hash 跨会话共享一个投影行 |
| `content_hash` | `fingerprint(content)`（**sha256[:16]**，规范化后） | distilled_memories.content_hash | 内容指纹；幂等；dst_ 锚；manifest 指纹 | 直写经 sanitize 后的文本才计入——与原始 trace 文本可能不一致（**这正是 `mcp_` 与 `dst_` 锚偶尔分叉的原因**）|
| `slice_key` | 蒸馏：切片键；直写：`mcp:<tid>` | distilled_memories.slice_key | 溯源；面板去重键 | **必须与去重 SQL 的口径逐字一致**（见 §7.4） |
| `dedup_of` | S3 / 直写投影 | distilled_memories.dedup_of | similar/merged 指向池中目标 | similar 行与目标行在面板可能"语义相似" |
| 会话内序号 `[n]` | wiki_compile 的 source_map | L1 页正文 | 会话内溯源 | 提升为全局 `[m<id>]` 后才跨会话有效 |
| `[m<id>]` | wiki_aggregate `_promote_refs` | L2 页正文 | 反查 distilled_memories.id → 原始轮次 | wiki 溯源的唯一锚 |
| manifest inputs 键 | `wiki_manifest.snapshot_inputs` | manifest_l1/l2.json | 编译基线（id → 指纹） | diff 口径 = `status IN new/similar` |

**铁律**：跨模块关联一个记忆时，先确认两端用的锚是**同一个 hash 体系**。
`src_id`（内容锚）、`legacy_id`（trace id）、`content_hash`（内容指纹）是
三套独立 hash，混用即 bug（面板重复显示的根因）。

---

## 4. 召回链路（六路通道融合）

```
查询 → ┌ 向量路     vec_bge_*  kNN（语义相似、同义改写）
       ├ 全文路     FTS5 trigram + bm25（精确术语、专有名词）
       ├ 标识符路   高熵串（≥12 字符）LIKE 精确匹配（代码符号、id）
       ├ 短语路     多词英文词组 LIKE 整串连续出现（command code / api key）
       ├ 页面向量路  source='wiki' 子集内 KNN（L2 知识页专用）
       └ 页面全文路  页面子集内 FTS（L2 知识页专用）
       → 候选级 RRF 融合 → value 加权 → 阈值截断（**页面豁免**）
       → 页面准入（高分优先 + 低分字面兜底 + 席位）→ 多样性限席
       → 页面保位 → units 行 → 按 src_id 锚反查内容与元数据
```

**六路通道的分工**——前四路是"**信号类型**"的异质化（向量=稠密语义、全文=词法、
标识符/短语=字面精确），后两路是"**候选源**"的异质化（L2 知识页整页太长，
混在大池里排不进去，必须单独成路）：

| 通道 | 判据 | 最擅长 | 弱点（实测） |
|---|---|---|---|
| `vec` | 双塔余弦相似度 | 同义改写、语义泛化 | 长文本区分度弱；中文模型对英文短语弱 |
| `fts` | FTS5 trigram + bm25 | 精确术语、专有名词 | bm25 对长文本不利（记忆条目排名靠后） |
| `ident` | 高熵串（≥12 字符含 `_-:.`，或纯字母数字 ≥16）LIKE | 代码符号、id（`wait_for_element`） | 覆盖不到多词短语与短专名 |
| `phrase` | 多词英文词组 LIKE **整串连续出现** | `command code`、`api key` 这类中英混合专名 | 只处理 ASCII 词组（中文由 trigram 覆盖） |
| `page` | `source='wiki'` 子集内 KNN | L2 知识页（长文本聚合产物） | 池小 → 对任何查询都能凑出 top-k，须配准入策略 |
| `page_fts` | 页面子集内 FTS | 页面里的精确术语 | 同上 |

- 检索动作只发生在 **units 投影面**；源库、蒸馏表都不直接参与检索
- 蒸馏表的新记忆要**投影进 units**（拿向量、进 FTS）才可召回：
  蒸馏 S4 统一补（`_pending_projection` 自动捞 `status='new'` 且无投影的条目）；
  直写链路落表后立即补（`project_memories`，含跨会话去重判定）
- MCP `memory_search` 与面板召回**同源**（rag_bridge 进程内直调同一索引库）
- 向量表注意：存在 bge_small / bge_base 两套（多模型写入架构），**active_spec
  决定现用表**——诊断/脚本别拿错表
- 评测基线（五路时期的 5 题集）：混合 recall 0.930，见 `docs/recall-fusion.md`

### 4.0 通道融合与截断的**顺序**（改动召回前必读）

顺序本身就是设计，动任何一步都会影响另两步：

| 步骤 | 做什么 | 为什么在这个位置 |
|---|---|---|
| ① 通道并行检索 | 六路各自取 `candidate_k` 条 | 异质信号独立取候选，互不干扰 |
| ② RRF 融合 | `relevance = best_channel_score + 0.4·Σ 1/(60+rank)` | 多通道一致命中 = 更强证据 |
| ③ value 加权 | ≤0.3 有界 boost + 30 天半衰期（`rag/ext.py`） | 用户反馈演化值参与排序 |
| ④ 相对阈值 | `threshold_filter`（非页面，`0.2×top`） | **页面必须豁免**——页面 rel 天然低一个量级，否则被系统性误杀 |
| ⑤ 页面准入 | `apply_page_policy`：高分优先 + 低分字面兜底 + 席位 | 分数与证据都只在各自区间有效（§4.2/§4.4） |
| ⑥ 多样性限席 | 会话轨迹限席；页面/记忆各自独立成席 | 防同一会话刷屏 |
| ⑦ 页面保位 | `reserve_pages`：**替换**末尾非页面（不追加） | 相关页 rel 低会被挤出 k；追加又会被下游截断切掉 |
| ⑧ 终审截断 | `safe_cutoff_hits`（桥接层，`floor_ratio` 与两个兜底席来自档位） | 页面占名额、不参与窗口截断；**非页面也有字面兜底席**（§4.2 末） |

⚠️ **下游有两处"取前 N 条"**（`safe_cutoff_hits` 的 `hits[:max_keep]`、面板
`/api/memos/search` 的 `top`），所以第 ⑦ 步必须保持"总数不变"——实测踩过：
追加让 `len(hits)=k+1`，`k=8` 时页面在窗口内正常、`k=20` 时页面掉出窗口消失。

### 4.1 页面层（L2 知识页）召回

wiki 页面此前是**只读产物**（Agent 搜不到）。现在 L2 页面**整页投影**成 units
的一层，参与召回：

| 项 | 取值 |
|---|---|
| 锚 | `src_id='wiki_<md 内容指纹>'`、`source='wiki'`、`role='wiki'` |
| conversation_id | 主题域（可按域筛选） |
| text | 标题 + 摘要 + 正文（全文进向量与 FTS） |
| `wiki_path` | 页面相对路径——**两阶段召回的第二阶段入口** |
| 来源分 | 0.6（与 Agent 主动写入同档，参与价值加权） |
| 投影 | **全量对齐**（扫目录 → upsert → 删已不存在的页面），幂等收敛 |

召回形态（`kind` 字段区分三层）：

| kind | 是什么 | 返回 | 下钻 |
|---|---|---|---|
| `page` | L2 知识页（聚合答案） | 标题 + **摘要** + 路径；正文截断到 600 字符 | 按 `wiki_path` 读整页 |
| `memory` | 蒸馏/直写记忆（具体结论） | 内容本身（短文） | `[m<id>]` → 记忆 → 对话轮次 |
| `message` | 原始对话细节 | 原文片段 + 轮次上下文 | — |

四个关键设计：

1. **页面走独立通道**（`subset_vector_search`）：页面是长文本，混在 2.3 万条
   大池子里做 top-30 几乎排不进去（实测：200 名内仅 2 个页面）。给异质候选源
   各自成路是本引擎既有设计（向量/全文/标识符本就是这个思路）。
2. **页面词面回路**（`subset_fts_search`）：页面池内再做一次 FTS——精确术语
   （"容错语义""traceId"）命中就是命中，与向量回路一起投票。
3. **页面准入 = 高分优先 + 低分字面兜底**（`apply_page_policy`，§4.2 有实测依据）：
   分数与证据各自只适用一段区间，谁都不能"一刀切"当门槛——
   - **高分看分数**：候选按分数降序，≥ `literal_required_below` 的页面直接入选。
     实测 `github网络失败`：真正对症的「网络环境确认与连接故障排查」0.848、
     「opencode 卡在网络故障排除」0.841 **都没有字面证据**，若按证据排序会输给
     0.334/0.197 两条"只字面沾边"的页面；
   - **低分看字面证据**：低于该线的页面必须命中 `page_fts`/`fts`/`ident` 才算数
     ——拼错场景里真相关页只有 0.014~0.026 分，靠的正是残缺的词面匹配；
   - **字面兜底席位**（`literal_seats`）：专门给这类"低分模糊匹配"留席，否则中分
     的池内页（拼错查询的「Clink」0.627）会按分数把它们全部挤出配额；
   - **豁免通用阈值**：页面 rel 天然低一个量级（0.014~0.03），
     `threshold_filter(exempt_ids=page_ids)` 让页面不参与 `0.2×top` 的通用截断
     ——实测两条有字面证据的真相关页（rel≈0.19）正好卡在这条线外被剔掉；
   - **保底占位**（`reserve_pages`）：选中的页面不被记忆挤出 k 条。
     ⚠️ 占位必须是**替换末尾非页面**而非追加：追加会让 `len(hits) = k+1`，
     而下游有两处按前 N 条截断（`rag_bridge.safe_cutoff_hits` 的
     `hits[:max_keep]` 窗口、面板 `/api/memos/search` 的 `top` 参数），排在
     末尾的页面**正好被切掉**——实测 `k=8` 正常、`k=20` 时页面凭空消失。
     同理页面在 `safe_cutoff_hits` 里**占名额、不参与窗口截断**，总数守恒。
4. **向量用整页正文而非摘要**：实测摘要向量更差（查询"容错语义"时核心页从
   0.826 掉出榜单）——摘要虽聚焦却丢了正文关键词。

### 4.2 页面噪声治理（已实施）与残余局限

**治理前基线**（12 个真实查询走完整召回链路）：

| 指标 | 治理前 |
|---|---|
| 出现页面的查询 | **12 / 12（100%）** |
| 结果里页面条目 | 50 条 / 12 查询 → 平均 **4.2 条**（`k=8` 时最差整条全是页面） |
| 无关查询"Windows 下怎么设置 git 代理" | 结果 8 条**全是页面** |

**根因不是"排序不准"，而是"页面该不该进结果从未被判定"**：页面池只有 231 条，
子集 KNN 对**任何**查询都能凑出 top-k；桥接层又给页面 `0.5×top` 的宽松例外
且"至少保 1 条"。

**关键实测结论——分数不可用**（这是整次调优的核心发现）：

| 页面 | 对查询"LLM Wiki 增量更新" | 融合分 | 真相 |
|---|---|---|---|
| LLM Wiki工程化：架构、增量更新与实战教训 | page_fts 命中**第 1 名** | **0.21×top** | 相关 |
| Mobile_App_AutoTest v1.5.0 发布记录 | 只有 `page`（池内 KNN） | **0.63×top** | 完全无关 |

页面作为长文本聚合产物，融合分被"短条目天然高分 + 价值加权"系统性压制，与
记忆/消息**不可比**。所以把门限从 0.5 提到 0.9 只会走向另一个极端——实测那样
页面对**所有**查询都不再返回（12/12 → 1/12），连本该命中的页面一起杀掉。

**关键实测结论二——证据也不能当门槛**（第二版踩的坑）：把"无强信号即剔除"
上线后，用拼错查询 `windowsctrol` 一验就露馅——它**同时做错了两件事**：

| 页面候选 | 分数 | 证据 | 真相 |
|---|---|---|---|
| Clink：增强 Windows Cmd 的命令行工具 | 0.627 | `page`+**`vec`** | ✗ 噪声（却被放行） |
| **Windows Control Core 窗口控制内核** | 0.584 | 仅 `page` | ✓ 语义最相关（却**被判死**） |
| WindowsControl 图标检测模型选择优先级 | 0.026 | `page_fts` | ✓ 相关 |
| MCP 服务器配置与 WindowsControl 集成 | 0.019 | `page_fts` | ✓ 相关 |
| Windows Control Core 项目结构 | 0.014 | `page_fts` | ✓ 相关 |

拼错、换词、近义表达时，"真相关"恰恰**最缺字面证据**。所以证据只能**排序**，
配额才做**取舍**——用户验收标准也正是"相关的不遗漏 > 排序靠前 > 噪声可容忍"。

**最终形态与效果**（**高分优先 + 低分字面兜底** + 阈值豁免 + 保底占位）。
用户点名的四组查询（`rag_bridge.search`，k=8，**默认档 3**）：

| 查询 | 结果 |
|---|---|
| 电脑窗口控制 | 4 条：记忆 1.246 + 页面「Windows Control Core 窗口控制内核」**0.885**、桌宠菜单 0.808、项目结构 0.246 |
| windowsctrol（拼错） | 4 条：对话 1.007 +「Clink」0.805 +「桌面AI助手产品定义」0.768 + **「WindowsControl 图标检测」0.204**（低分真相关页，靠字面兜底席救回） |
| AI助手 | 4 条：对话 1.012 + **3 条页面全相关**（0.825 / 0.741 / 0.728） |
| deskflow（库内仅 1 处提及） | 4 条：相关那条 **#1**（1.007）+ 3 条填充噪声 —— 符合预期（库内无料时的语义邻近填充） |

**代价（诚实记录）**：默认档 3 下 12 查询基线约 **60 条 / 页面 35 条**（治理前是
50 条**全页面**、且无关查询整条刷屏）。页面绝对值没有降到最低，但**结构完全变了**：
排序有依据（高分语义在前、字面兜底在后），无关查询被"低分必须有字面证据"挡住
（"Windows 下怎么设置 git 代理"从 8 条全页面降到 0~2 条）。
要更干净把 `recall_level` 调到 1~2，要更全调到 4~5（§4.3）。

**残余局限**：噪声仍会出现，且**排序不完美**——拼错查询 `windowsctrol` 下
噪声「Clink」(0.805) 仍排在真相关页「WindowsControl 图标检测」(0.204) 前面，
因为它的向量分确实高。**分数与证据都无法可靠区分"高分噪声"与"低分相关"**。

### 4.2.1 非页面层的字面兜底席（2026-09-22 补）

页面层早在 §4.2 里就有了"低分字面兜底席"，**非页面层（记忆/消息）却一直没有** ——
于是同一次召回里出现荒谬的不对称：

| 层级 | 条目 | 分数 | 结果 |
|---|---|---|---|
| 页面 | 真相关页 | **0.195** | ✅ 进了（字面兜底席救回） |
| 记忆 | 「WindowsControl 项目架构与技术栈」 | **0.701** | ❌ 被切掉 |

**根因**：非页面只套一条**乘性相对门限** `score ≥ curate_floor × top`，而 **top
本身可能是噪声**。拼错查询 `windowsctrol` 的 top1 是完全无关的字面命中(1.237)，
门限因此被抬到 0.990，24 条候选只剩 4 条 —— 而真相关那条 0.701 白白被切。

**修法（与页面层同构）**：`rag_bridge.safe_cutoff_hits` 改为
① 高分（≥ 门限）优先，但为字面兜底**留出席位**；② 低分但命中**字面通道**
（`fts`/`ident`/`phrase`，与页面用同一套 `_PAGE_LITERAL_SIGNALS`）的用兜底席救回；
③ **只救有字面证据的** —— 低分且只有向量分的仍然切掉，否则等于取消门限。
档位表新增 `nonpage_literal_seats`（1/2/**2**/3/4，默认档 3 = 2 席）。

**实测效果**（干净数据、6 个真实查询，`curate=False/True` 对比）：

| 查询 | 改前保留 | 改后 | 效果 |
|---|---|---|---|
| `记忆怎么软删除` | 7 | **12** | **救回了答案本身**（0.947） |
| `windowsctrol` | 4 | **6** | **救回了被误杀的「WindowsControl 项目架构与技术栈」** |
| `rerank 精排方案` | 4 | **11** | 多条相关记忆回来 |
| `command code` | 11 | **13** | — |
| `面板端口是多少` | 4 | **6** | — |

**仍然救不回的那一类**：查询「Windows 下怎么设置 git 代理」时，答案本身
「Git 代理与 SSL 配置处理」(0.789) **只命中 `vec`、没有任何字面证据** ——
兜底席给不了它，门限又够不着。**这一类只能靠 cross-encoder**（它直接比较
query 与候选的语义，不依赖通道命中）。

**所以两条腿不是替代关系**：规则兜底席解决"**有字面证据**的低分真相关"（拼错、
近义）；cross-encoder 解决"**只有语义证据**的低分真相关"（换词、纯语义）。

### 4.2.2 召回精排（rerank）的三条路线

| 路线 | 延迟 | 成本 | 确定性 | 决策 |
|---|---|---|---|---|
| **规则兜底席** | **0** | 0 | 确定 | ✅ **已实施**（§4.2.1） |
| **本地 cross-encoder** | **实测 12~18s/查询**（纯 CPU）× | 0 | 确定 | ⏸️ **暂缓** |
| **LLM 精判** | **+1~3s** | 每次召回一次 LLM 调用 | **不确定** | ❌ **否决** |

**本地 cross-encoder 的实测结论（2026-09-22，`gte-multilingual-reranker-base`）**：
模型 0.3B / 12 层 / hidden=768，FP32 ONNX 1.2GB。**纯 CPU 下单对 265ms、
30 候选 5.8s，真实查询（20~27 候选、候选文本 1200 字）12~18 秒** —— 比被否决的
LLM 方案还差一个量级。效果上 6 个查询里 3 个有改善（`windowsctrol` 的 top1 从
无关条目修正为「WindowsControl 项目架构与技术栈」，证明 cross-encoder 对
拼错/纯语义场景确实有效），但**恰恰没解决我们最想要的案例**（`git 代理` 的答案
仍没进 top5）。

**所以暂缓**：在纯 CPU 下性价比不成立。两次估算都偏乐观（先是按参数量线性外推
× 短文本样本，实际架构差异 + 长候选使延迟高出 4~6 倍）。**若将来上 GPU，值得
重测**（本机有 RTX 4060，但缺 CUDA Toolkit，需装 `onnxruntime-gpu` 或 DirectML；
模型与验证脚本已留在 `models/gte-multilingual-reranker-base/` 与
`exports/wiki_work/verify_reranker.py`）。

**否决 LLM 精判的理由**（两条，第二条是结构性的）：

1. 成本不可控 + 延迟明显：Agent 侧或许无感（它本来就在等 LLM），但**接口与面板
   查询能明确感知**。
2. **更硬的一条：缓存救不了它**。rerank 结果缓存的收益取决于**查询重复率**，而
   面板/接口的查询是**人输入的**、重复率天然很低 → 缓存几乎不命中，"每次都发一次
   LLM 请求"无从缓解。**这是结构性缺陷，不是调优能解决的。** 外加 LLM 输出
   **不确定**，会让"召回结果可复现"这条原则失效。

**本地 cross-encoder 的实测延迟**（本机纯 CPU，onnxruntime + `CPUExecutionProvider`）：

```
单条 query 编码     1.6 ms
单条 passage 编码   70 字 2.1ms / 150 字 4.9ms / 300 字 6.9ms / 600 字 13.1ms
```

cross-encoder 对**每个 (query, doc) 对**都要独立前向（不像 bi-encoder 能复用 doc
编码），成本 ≈ 逐条编码 × 参数量倍数：**30 候选 × 同规模 ≈ 106ms、× base 级
（约 3.3× 参数）≈ 282ms**。真实候选更长、再加首次加载，实际落在 **200~500ms**，
且可用"**只精排 top-N**"进一步压缩。

**第 3 条路（页面内分块向量）**：实测收益中等且有反例（块级 max 会让噪声页上位、
分数整体上移 0.03~0.10 需重标阈值），且**不解决准入问题**，优先级最低。

### 4.3 召回严格度档位（统一配置）

`rag.retrieval.recall_level`（**1 最严格 … 5 最宽松，默认 3「均衡」**）把调优
过程中攒下的全部旋钮收成**一条刻度**——它一次性设定候选宽度、非页面相对阈值、
终审阈值、页面席位/字面兜底席/证据门槛线，避免散着配出互相矛盾的组合。
调用方只需回答一个问题：要少而准，还是全而杂？

| 档 | 名称 | 候选池 | 非页面阈值 | 终审阈值 | 页面席位 | 页面兜底席 | **非页面兜底席** | 证据门槛线 | 实测（四组查询条数 · 12查询基线） |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 最严格 | 20 | 0.35 | 0.90 | 1 | 1 | **1** | 0.95 | 2/1/1/1 · 18 条 / 页面 7 |
| 2 | 严格 | 25 | 0.30 | 0.85 | 2 | 1 | **2** | 0.85 | 2/1/1/1 · 27 条 / 页面 13 |
| **3** | **均衡（默认）** | 30 | 0.20 | 0.80 | 3 | 1 | **2** | 0.70 | **4/4/4/4 · 55 条 / 页面 35** |
| 4 | 宽松 | 40 | 0.15 | 0.75 | 4 | 2 | **3** | 0.50 | 5/5/5/5 · 76 条 / 页面 46 |
| 5 | 最宽松 | 60 | 0.10 | 0.70 | 5 | 3 | **4** | 0.30 | 6/7/8/8 · 88 条 / 页面 57 |

「页面兜底席」= `page.literal_seats`、「非页面兜底席」= `nonpage_literal_seats`
（2026-09-22 新增，见 §4.2.1）；两者同方向：档位越宽松给得越多。

「证据门槛线」= `literal_required_below`：**这条线以上的页面只看分数**（高分语义
优先），线以下必须有字面证据（低分模糊匹配兜底）；「字面兜底席」= `literal_seats`，
专门留给后者。**档位表刻意不设 `min_evidence` 硬门槛**——它是"对所有页面生效"的
门槛，会连高分页面一起挡掉（实测档 3 曾用它，结果把候选里分最高的「网络环境确认
与连接故障排查」0.668 挡在门外，而 0.334/0.197 两条低分字面页入选，正好做反）。

⚠️ **1~2 档仍会明显漏**（所以默认不取它们）：门槛线 0.95/0.85 意味着几乎所有
页面都要字面证据，加上终审阈值 0.85~0.90——实测 `windowsctrol` / `AI助手` /
`deskflow` 在档 1 下**各只返回 1 条**，接近"查了没结果"。**3 档是"严格但可用"
的下沿**：四组查询都是 4 条（含 3 条页面）。要极限精度下调到 1~2，要宽召回到 4~5。

单独覆盖：`rag.retrieval.page.*` 的显式配置**优先于档位**（只想调某一项时不必
换档）。

配置项（`agentmemhub.yaml` → `rag.retrieval.page`）：

| 键 | 默认 | 语义 |
|---|---|---|
| `channel_k` | 8 | 页面独立通道候选数 |
| `max_in_results` | 3 | 页面总席位（默认档=3；档位会覆盖） |
| `literal_seats` | 1 | 保底留给"低分但有字面证据"页面的席位数 |
| `literal_required_below` | 0.7 | **这条线以上的页面只看分数**，以下必须有字面证据 |
| `floor_ratio` | 0 | 分数门限（**默认不设**——分数对页面不可用，见上表） |
| `min_evidence` | `any` | 可选证据硬门槛（`any`/`pool`/`vector`/`literal`）——**档位刻意不用它** |
| `reserve_seats` | true | 是否给选中页面保底占位（防被记忆挤出 k 条） |

### 4.4 短语精确匹配通道（`phrase_search`）

**补的是什么盲区**：`ident` 通道覆盖"单个高熵串"（`wait_for_element`），但它要求
≥12 字符且不含空格——**多词短语**（`command code`、`api key`）从来没人管；而
trigram FTS 又把查询拆成 3-gram 片段用 **OR** 连接（`"com" OR "omm" OR …`），
"词组整体出现"这个强信号被丢掉了。

**实测（这就是它存在的理由）**：库里 12 个含 "Command Code" 的单元，在查询
`command` 下 **11 个连候选池都没进**，页面「Command Code 网关选型」排 **63/63**
（rel=0.015）。三个原因叠加：

| 原因 | 说明 |
|---|---|
| `fts` 用"名次倒数"计分（`1/(rank+1)`） | 记忆条目长 → bm25 排名靠后（如第 50 名）→ 只得 0.02，被短对话消息的 1.0 压死 |
| `vec` 中文模型对英文短语弱 | `command code` 这类英文短语在 bge-small-zh 里语义权重低 |
| 多词短语不做整体匹配 | 内容里明明写着 `Command Code（api.commandcode.ai…）`，却没有通道把"这两个词连着出现"当证据 |

**做法**：

- `extract_phrases`：提取查询中的 ASCII 词组。**连续英文串拆成 2 词滑动窗口**
  （`deep seek command code` → `deep seek` / `seek command` / `command code`；
  整串吃下等于废掉——库里不会连着写这五个词），2 词优先、再补 3 词；过滤 <5 字符
  的短组合（`is a`）；去重、上限 4 条
- `phrase_search`：用 **LIKE** 做"整串连续出现"判据（正文或标题），命中即**恒强分
  1.0**——与 `ident` 同级，直接进"字面证据"档；多短语命中数决定次序
- 页面层：`phrase` 计入 `_PAGE_LITERAL_SIGNALS`（属字面证据，可用于低分兜底）

**为什么用 LIKE 而不是 FTS**：LIKE 是"整串连续出现"的判据，**不受 bm25 长文本
劣势影响**，也**不受 FTS 候选 `LIMIT` 截断影响**——而这两个正是 11/12 个单元消失
的原因。代价是全表扫描，但只做字符串匹配、不排序，2.3 万行量级完全可接受。

**效果**（同一份数据）：

| 查询 | 修复前 | 修复后 |
|---|---|---|
| `command code` 档 1 | 1 条（无关对话） | **7 条**：记忆 1.235 / 1.225×3 … + 页面「Command Code 网关选型」1.191 |
| `command code` 档 3 | 4 条，Command Code 页面 0.193（第 63/63 名） | **8 条**，页面 1.191 进前排 |
| 基线 12 查询（档 3） | 55 条 / 页面 35 | 60 条 / 页面 35（不劣化，页面数不变） |

**已知边界（未做）**：**高频单词**仍召不回特定专名——查 `command`（单个词、
7 字符）时 Command Code 记忆依旧进不了候选。因为 `command` 在库里是高频词
（几百条 CLI 相关对话都含它）、单词不构成短语证据、`ident` 又要求 ≥12 字符。
可能的方向：FTS 计分口径改归一化 bm25、对"库内命中数很少的词"也走字面通道、
层级偏置（同分时记忆优先于原始对话）。

L1 页面**不进召回面**（只作下钻）：实测 L2 覆盖了 98.7% 的 L1 内容（221/224），
且"按会话找内容"的需求由消息层投影覆盖——L1 进召回面只会与 L2 竞争排序。

---

## 5. 价值体系

| 分值 | 语义 | 写入点 |
|---|---|---|
| 0.8 | Agent 直写 + `importance=high`（技术沉淀/架构决策/用户明确要求） | memory_save 落表后精调 |
| 0.6 | Agent 直写默认 / importance=normal | 投影按来源（source='mcp'）+ 精调 |
| 0.4 | Agent 直写 + importance=low | 同上 |
| 0.3 | 离线蒸馏来源初始分（默认中等，靠使用升降） | _project_one（source≠'mcp'） |

- 初始分之后由**真实使用**演化：召回命中、👍👎（`unit_values`），**无 LLM 评分**
- ⚠️ 投影初始分曾写死 0.3，把 Agent 主动记忆系统性低估——已改为按来源区分
  （`_project_one`：source='mcp' → `BASE_VALUE_AGENT_WRITE`）
- `backfill_base_values`：老数据自愈（幂等，已有记录不触碰）
- `memory_save` 已支持可选 `type` 参数（decision/fact/preference/lesson，默认 fact）
  与 `importance` 档位，Agent 写入时自行声明

---

## 6. 记忆状态生命周期

```
new ──(S3 投影时相似 0.80~0.92)──→ similar（保留，打标互链 dedup_of）
 │ └─(相似 ≥0.92)────────────────→ duplicate（不投影，仅归档）
 └─(跨会话合并)──────────────────→ merged（合并终稿，dedup_of 指向各来源）
```

- **wiki 编译输入 = `status IN ('new','similar')`**；merged/duplicate 是被蒸馏
  过程淘汰/吸收的归档，不进 wiki 也不在面板默认视图显示
- 归档条目内容复现时会**复活**为 new（save_memories / save_direct_memory 均有）

---

## 7. Wiki 编译与增量同步

### 7.1 两级编译

```
蒸馏表（new/similar，按会话分组）─L1：会话内编译─→ L1 页面（每会话一文件）
        └ L2：跨会话归域（domains）→ 域内细分 → 主题页（[n] 提升为 [m<id>]）
```

- **域**：LLM 对全部 L1 页标题的主题聚类（约束 `dmin–dmax`，默认 20–60，
  当前 20 个）。**全量跑时生成、暂时固定**；增量只往现有域放（放不下的进
  "待整理"，积累过多触发全量归域重建）
- 编译收尾自动写 **manifest**（输入快照：id → 内容指纹 + 会话归属；L2 另附
  域 → L1 文件映射）。指纹**现算 md5(content)**，不用库里的 content_hash
  字段（它会被绕过蒸馏的直改弄脏，漏报变更）
- 一个 L1 文件（会话）的多个页面**可以分属不同域**——域归属是页级、多对多

### 7.2 触发器（`agentmemhub/wiki_triggers.py`）

**没有定时任务**。检测挂在写入时：MCP `memory_save` 每条（异步线程）+
蒸馏收尾（同步）各过一次 align（毫秒级只读），命中规则且有脏数据才同步跑
增量更新；成功才打标记（`logs/wiki_trigger_state.json`），失败不记下次重试。

| 规则 | 默认 | 语义 |
|---|---|---|
| `schedule` | 09:00 / 12:00 / 18:00 | **时点档**：当天第一次发生在某档位之后的写入补触发该档 |
| `dirty_memories` | 10 | 脏记忆（新增+变更）达到阈值触发 |
| `first_write_daily` | true | 每自然日第一次蒸馏写入后触发（手动不消耗额度） |

规则编辑（overrides 存状态文件，优先级最高，不改 yaml）：
`wiki --action triggers` / `trigger [--force]`、`GET/PUT /api/wiki/triggers`、
`POST /api/wiki/trigger/run`。单飞行锁防触发点撞车；update 刷 manifest 后
align 归零，天然防重复。

### 7.3 增量更新（`wiki --action update` / `POST /api/wiki/update`）

```
align 定位脏会话 → L1 整页重编（会话隔离，无副作用）
  → manifest_l2 域映射定位脏域 → 只重编脏域（partial：绝不清其它域目录、
    顶层索引磁盘重建、_domains.json 成员按最新序号重写）
  → 新 L1 文件 assign_new_pages 归入现有域（带每域代表页视野 + 待整理宽容出口）
  → linkfix（L2 标题可能变）→ 索引刷新 → 两级 manifest 刷新成新基线
```

实测（首次实战，80 条直写记忆触发）：L1 重编 1 会话（12 页）→ 归域 →
L2 重编 1 个域 → 全程 12 分钟。

### 7.4 静默失败：manifest 必须只记「实际编译成了什么」

2026-09-21 实测（一次 L1 分组调用超时）暴露**三个叠加缺陷**，合起来的效果是
**wiki 静默落后于记忆，而系统一直显示健康**：

| # | 缺陷 | 后果 | 修复 |
|---|---|---|---|
| ① | `update()` 正常返回但 `l1.failed > 0`，而触发器**只 catch 异常、不看返回值** | 当日档位与 `last_update_at` 被一起消耗，失败会话要等**明天**首次写入才有机会重试 | `failed > 0` 即不打标记，落 `trigger_fired_skipped` 审计 |
| ② | `update()` 失败后仍用「库当前状态」重建 manifest | 失败会话被**谎报为已编译** → 此后 align 一律报"库未变化"，脏数据**永久掩盖** | `build_manifest(drop_sessions=…)`：失败会话剔除出新基线，保留重试线索 |
| ③ | L1 全失败仍跑 L2 | L2 拿磁盘上的**旧 L1** 重跑同样的域，实测白烧 1290 秒 / 42 万 prompt token | 全失败直接跳过 L2 |
| ④ | **组级失败被 fail-open 吞掉**：逐组编译单组失败不影响整会话，会话整体算"成功"，但失败那几组的记忆**没进任何页面** —— 却照样被写进新基线 | 与 ② **同源、粒度更细**（会话 → 组）。2026-09-21 切网实测：23 组里 3 组因 `IncompleteRead` 失败，**20 条记忆被静默丢弃** | `compile_session` 报出失败组的成员 → `compile_one` 按 `source_map` 转成记忆 id → `build_manifest(drop_ids=…)` 一并剔除；`hints` 明示 |

**同时修掉一个网络层的漏网**：`IncompleteRead`（响应读了一半连接就断）继承自
`http.client.HTTPException`，而 `_post` 只把 `(URLError, TimeoutError, OSError)`
当瞬态 —— **它一次都不重试**，网络抖一下直接丢一组。现已把 `HTTPException` 一并
纳入瞬态重试。

**另一个盲区**：`align` 只比 manifest diff，**从不看"页面实际覆盖了多少输入"**。
实测库里 1781 条输入有 **61 条无任何页面引用**（最早可追到 09-10），而它一直报
"✓ 库未变化"。现在 `coverage.uncovered` 与 message 都会显式提示 —— 但**只告警、
不触发重编**（否则会陷入"每次都触发、却因脏会话为空而什么都不做"）。

**根因是配置漏配**：`wiki.llm` 此前没有 timeout 覆盖 → 继承默认 **60s**，而 L1 是
长调用（一个会话几十条记忆，分组与逐组编译的输入可达几万字符）。101 条记忆的
会话在**分组调用**上连续 3 次读超时（60s × 3 + 退避 ≈ 187 秒），整会话编译失败。
第二级早已把 timeout 放宽到 900s（yaml 注释写着"默认 60s 对输入几万字符的归域
调用必然读超时"），**第一级漏配** —— 现在**默认值本身**就是 900s，不再依赖使用者
先知道这个内部坑。

**遗留**：`compile_session` 里 `compile_page` 有 fail-open（单组失败不影响其它组），
但 `plan_groups` **没有** —— 分组一失败整会话就废。暂不加"兜底分组"降级：那样的
产出质量不可控，宁可如实失败并重试（① ② 已保证它能被重试且可见）。

### 7.5 长任务必须能从日志回答"跑到哪了"（同日第二次定位）

修好 timeout 后重跑，**40 多分钟仍无任何输出** —— 起初误判成"又卡住了"（并因此
两次杀掉了**正在正常工作**的编译）。用短超时（90s、不重试）做规模探测才看清：

```
前  3 条（  826 字）：✅ 60.3s
前 10 条（ 6792 字）：✅ 75.9s
前 40 条（27900 字）：✅ 68.2s，分 14 组      ← 分组根本没卡
```

真正的耗时在**逐组编译**：101 条 → 30+ 组，每组一次 LLM 调用（30~90 秒），
合计 **30~60 分钟**；而 `page_done` / `call` 事件**只在整会话完成后才写**，
全程在日志里静默 —— 于是"正常推进"与"卡死"无法区分。

两处修正：

- **分组分批**（`wiki.plan_batch_max`，默认 40）：分组是"**输出规模随输入线性
  增长**"的调用，101 条一次性分组会真的卡到超时；分批后每批约 1 分钟。批内编号
  必须映射回全局编号（`compile_page` 按全局清单取记忆，不映射会越靠后错得越离谱）
- **进度落盘**：`compile_one` 此前把 `log` 整个吞掉；现在每步写 `wiki.log` 的
  `progress` 事件（"两段式：先分组（101 条，单批上限 40）"→"[分批 1/3] 40 条"→
  逐组编译…）

顺带修掉一处"配置形同虚设"：`wiki.single_shot_max` 虽在 `DEFAULT_WIKI` 与 example
里存在，**却从未被读取**（脚本用硬编码常量，默认值恰好一致才一直没暴露）——
现在由 `wiki_compile._limits()` 统一取值，非法值回落常量。

**墙钟 ≠ 真实工作量**。`run_end` 的 `seconds` 是**墙钟**，机器一休眠就被撑大：
实测一次 L2 汇总显示 **33052 秒（9.2 小时）**，据此误判成"上游重试了 9 小时"，
实际约 1 小时（71 次调用 / 45 万 token）。现在汇总与 `run_end` **并列两个口径**：

| 字段 | 含义 |
|---|---|
| `seconds` | 墙钟时长（含休眠、含退避等待） |
| `call_seconds` | **累计调用耗时** —— 只算真正在等 LLM 的时间，不含退避等待；休眠时没有请求在跑，所以不受影响 |

计时细节：用 `time.perf_counter()` 而**不是** `monotonic()` —— 后者在 Windows 上
基于 `GetTickCount64`，精度只有约 **15.6ms**，短请求会测出 0（实测踩到，单元测试
直接暴露）。

**教训**：可观测性的缺口会**反过来制造错误判断**。日志静默时，"慢"看起来和
"死"一模一样，排查者（包括 Agent）会做出破坏性动作（杀掉正常任务）；而一个
**失真的时间数字**同样会误导 —— 它会让人去治根本不存在的病。

### 7.6 面板记忆报表的双路 UNION 与去重

```
api_memories = 蒸馏路（distilled_memories JOIN dst_ units）
             ∪ manual 路（units WHERE source='memory'
                          AND NOT EXISTS 蒸馏表同 slice_key 行）  ← 排除双份
```

关联锚必须是 **legacy_id**（`dm.slice_key='mcp:'||substr(u.legacy_id,5)`）——
`units.src_id` 是内容锚（另一套 hash），用它永远匹配不上（实测踩坑：
同一条记忆双份显示）。

---

## 8. 面板与看板

- **统一会话页**：源库 conversations/events（查看/删除/改标题）
- **记忆报表页**：双路 UNION（§7.4）；类型/状态/来源筛选、⭐加权（manual_value）、
  👍👎（unit_values）、蒸馏入口
- 面板 serve（`agentmemhub serve`）与 MCP server 是**两个独立进程**，
  代码更新后**各自都要重启**才生效
- ⚠️ 进程环境差异：serve/MCP 可能以不同解释器（.venv / 全局 python）运行，
  重启后若行为与预期不符，**先确认进程实际加载的代码版本**（对比进程启动时间
  与文件 mtime、用 psutil 查 cwd），不要假设"重启 = 最新代码"

---

## 9. 已知边界与遗留

| 事项 | 说明 |
|---|---|
| 直写记忆的语义重复 | 直写记忆与蒸馏池中已有记忆主题重叠时（similar 判定）两条都显示——双来源固有；S3 判重线 0.92 之上的才算重复 |
| 蒸馏历史遗留重复组 | 同会话同内容"merged+new"成对（历史去重漏网，内容微差 hash 不同）；默认视图隐藏 merged，物理清理待定 |
| 增量归域不重跑全量归域 | 归域缓存按批次序号键控，页序漂移即错位；新页走 `assign_new_pages`（带代表页视野）；"待整理"积累 → 全量归域重建域结构 |
| `_domains.json` 成员序号 | L1 页数变化会使旧 members 序号漂移——增量收尾自动按最新页序重写 |
| 单成员页沿用源页 | L2 聚合时单来源页不调 LLM（成本优化） |
| 测试隔离 | conftest 已钉 `wiki_triggers._targets`（测试禁止解析真实 wiki 目录）——蒸馏收尾钩子在测试里必然早退；蒸馏/触发器测试自行 mock 目标 |
| wiki 进召回 | **已做**（§4.1）：L2 页面整页投影为 `wiki_<crc32>` 单元，走独立的页面向量路 + 页面全文路，参与六路融合；准入另有策略（§4.2） |
| 外部投喂 | **已落地到 M4（2026-10-14）**：素材库 `materials.db` + 三段保留（`raw/` 不可再生原件 · `mirror/` 中间物 · `exports/wiki_work/out_feed/` 交付 md）+ 转换器注册表（md/txt · 网页 · 公众号 · **PDF 四路由** · **DOCX**）+ CLI（`feed add/list/show/retry/retract/forget/lint`）。设计见 [`feed-adapter-plan.md`](feed-adapter-plan.md)、用法见 [`modules/feed.md`](modules/feed.md)；上游调研见 [`external-ingest.md`](external-ingest.md)。**仍缺**：T4 图片 OCR 档、MCP/面板入口（M5）。**下游全在代码里**（`origin` 列 / 判定点 / 召回过滤 / 页面解析）；M0 补的三处地基（① 口径统一以代码的 `source=="feed"` 为准，格式信息放 `meta`；② 采集层 `origin` 由 `rag.ingest.origin_of()` 单点给出；③ 页面层持久化 `doc_id` 作为回指原件的锚）至今仍是这条链路的地基 |
| ~~编辑器在产物目录里自动新建的空文件会污染召回面~~ | **已修（2026-09-25）**。把产物目录当 Obsidian vault 打开时，点击 `[[链接]]` 若**未解析**（我们的页面只有 frontmatter `title` 而没有 `aliases`，而 Obsidian 按**文件名/aliases** 解析），编辑器会在 vault 根**自动新建 0 字节 md**，文件名就是链接文字。`wiki_index.project_pages` 会照收：`parse_page` 的 title 兜底成 `path.stem`（**看起来完全正当**）、`domain` 取父目录名（得到错误的 `out_l2_mimo`）、`origin` 标成 `native` → 变成"标题很正当、正文是空"的召回单元。实测当时已有 3 个待投影（`ADBController 核心设计与功能清单` 等），**所幸上次投影是 09-22、早于文件创建的 09-25 12:15，尚未污染**。已删除 3 个空文件，并在投影处加闸：**正文为空即跳过**（判据用正文、不用文件大小），`skipped_empty` 进返回值与日志。**根因（`[[标题]]` 解析不了）见 [`external-ingest-plan.md`](external-ingest-plan.md) §5.1** —— 补 `aliases` 才是治本 |
| ~~全量聚合会删掉用户放在产物目录里的东西~~ | **已修（2026-09-25）**。`wiki_aggregate.py` 全量模式的清理是"**凡是不在本轮 `keep` 里的目录一律 `rmtree`**"，而用户**把产物目录直接当 Obsidian vault** 用时会在里面生成 `.obsidian/`（编辑器配置）—— 不在 keep 里 → **被静默删掉，不可逆**。逐字复现旧逻辑验证：清理后目录从 `['.obsidian','01-旧域','02-本轮域','用户自建目录']` 变成只剩 `['02-本轮域']`（**连"用户自建目录"也一起删了**）。现抽成 `_prune_stale_domain_dirs()` 并加两道闸：**跳过 `.` 开头的隐藏目录** + **只清 `%02d-` 数字前缀的域目录**（增量模式仍然绝不清目录）。教训：**产物目录若允许用户放东西，清理逻辑就必须白名单化，不能黑名单化**。⚠️ 已把用户的 `.obsidian/` 备份到 `exports/wiki_work/_obsidian_backup_20260925/` |
| **两级中间态（L1 领先 L2）谁都查不出来** | **已修（2026-09-25）**。实测出现过 `L2 产物 17:11 / L1 产物 21:01`（落后近 4 小时），而**没有任何现成接口能报出来**：① `wiki --action pending` **只比 L1 的 manifest** → 报 0；② `align` 一次只吃**一个目录**，而两级 manifest 名固定是 `<目录>/manifest_<stage>.json`、**分居两个目录** → 只传 `--out` 必然只对上一级；③ 更糟的是不带 `--stage` 时它会报"**manifest_l2：缺失 —— 编译于该功能上线前**"——**凭空断因**（真相是文件在另一个目录），把人带偏。**修法**：`align()` 新增 `l1_dir`/`l2_dir`（CLI 用现成的 `--l1/--l2`，面板加 `l1`/`l2` 查询参数，三者都不给则按配置 `wiki.out_l1/out_l2` 解析），返回新增 **`consistency`**：`{comparable, l1_at, l2_at, skew_seconds, l1_inputs, l2_inputs, inputs_delta, verdict}`，`verdict` 取 `ok`（≤ `SKEW_TOL` 60 秒）/ **`l1_ahead`（L2 落后，要重编 L2）** / `l2_ahead`；缺 manifest 时 `stages[st]` 带上**期望路径** `path`，CLI 据此区分"真没有这个文件"与"它在另一个目录" |
| **日志没有标准头** | **已修（2026-09-25）**。用户指出：日志只有"内容"，没有标准格式。**标准头五项 = 时间 / 等级 / 模块 / 行号 / 内容**。三种形态各自补齐：① **JSONL 事实流**（`mcp.log`/`memory.log`/`wiki.log`/`web.log`/`cli.log`）在**写入口统一注入** `ts`/`level`/`module`/`line`/`msg`（`logs.standardize()`）——`module`+`line` 用 `sys._getframe` 取**业务调用点**，取值失败回落 `("", 0)` 绝不抛；`level` 由 `ok=False` / `error` / 错误事件名（`page_fail`/`run_aborted`/`domain_write_skipped`）自动推断；`msg` 是**单行可读摘要**（≤200 字，`memory` 的正文仍在 `content` 字段里）。**为什么在入口注入而不是要求调用方写全**：写入点散落在 60+ 处，要求"每个人都记得"必然漂移——实测 `wiki.log` 早期条目连 `ts` 都没有、`memory.log` 出过一次全 `1970`。② **文本日志**（`rag/logkit.py`）补 `%(lineno)d`（原先只有 `%(name)s`，一个模块里几十个打点分不开）。③ **任务日志**（`logs/tasks/*.log`）从 `[HH:MM:SS]` 改为 `YYYY-MM-DD HH:MM:SS INFO [module:line] 内容`（原先跨天无法定位是哪天）。守护测试：`tests/test_logs_format.py`（12 条） |
| **测试逃逸线程用 tmp 库覆盖了真实 L1 产物** | **已修（2026-09-26）**。**实测事故（2026-09-19 那次同型复发）**：`tests/test_mcp_server.py::test_memory_save_passes_tags` 调 MCP handler → `memory_save` 末尾起 **daemon 线程**（`mcp_server._wiki_trigger_probe`）→ 线程**调度慢**，等它真正执行到 `wiki_triggers._targets()` 时**该测试已结束、`monkeypatch` 已被撤销** → 它拿到**真实配置**（真实 `wiki.out_l1/out_l2` + 真实 `STATE_FILE`），却配着**测试沙箱的 tmp 库**（`AGENTMEM_HUB_DATA_DIR` 把 `_default_db()` 指到 tmp）→ 用只有 8 条记忆的空库跑了一次"全量更新"。**损失**：`out_l1_mimo` **224 个页面删到 25 个**、`manifest_l1.n_inputs` 从 **1804 覆盖成 8**、`pending` 从 0 涨到 **200 条**；L2 未受影响（该轮跑到细分阶段时随 pytest 进程一起死掉）。**恢复**：从快照 `20260926_082028/wiki_l1` 只回拷 **L1**（224 页 + manifest 1800），**不动 L2**；受损现场留档 `exports/wiki_work/_damaged_l1_<ts>/`；陈旧锁（持锁进程已死）由 `_lock_is_stale()` 自动回收，另手动清掉。**根因与修法**：`monkeypatch` 只对**单个测试**生效，挡不住活过测试的线程/子进程 → 必须有一道**进程级、不随测试撤销**的熔断：`conftest._sandbox_data_dir`（session 级 autouse）置位 **`AGENTMEMHUB_TEST_SANDBOX=1`**（环境变量会被子进程继承），`wiki_triggers._targets()` 见它即抛。**验证**：全量跑测试前后 `L1 224页/1800`、`L2 466页/1804`、L1 最新页 mtime **完全一致**，被拦下的尝试如实记进 `wiki.log`（`trigger_error`），不再静默破坏 |
| **触发链路的四个静默点** | **已修（2026-09-26）**。一次触发从判定到收尾，检测（`trigger_check`）与流水线内部（`run_start`/`progress`/`manifest`）都有日志，但中间这四段没有：① **`update()` 抛异常**只塞进返回值 —— 而 MCP 那条 daemon 路径会**丢弃返回值** → 一次失败的触发完全无痕（看着像"触发了但什么都没发生"）；② **并发被跳过**（拿不到单飞锁）无痕，事后无法区分"没触发"与"被跳过"；③ **更新成功后的汇总**（脏会话/L1 成败/L2 页数/提示）无痕，想知道"这次更新干了什么"只能去流水线日志里拼；④ **页面层投影本身**无痕，而它实测要跑 **4.5 分钟**（466 页嵌入），期间 `run_end` 已过、状态文件却还是上一轮的标记 —— 2026-09-26 因此**误判过一次**"标记没打上、会重复触发"。**修法**：`wiki_triggers` 新增 `trigger_skip` / `trigger_update_start` / `trigger_update_end`（带 `seconds`/`l1_*`/`l2_*`/`page_index_seconds`/`hints`）/ `trigger_update_error` 四条事件；`update()` 加 `page_index` 事件（页数 + 耗时，并把 `seconds` 放进返回值）；MCP 的 `_wiki_trigger_probe` 异常必须留痕；状态文件新增 **`update_started_at` / `last_hook` / `last_error`**，`wiki --action triggers` 多出 **`in_progress`**（`running` / `elapsed_seconds`）与 `last_error`，从此"正在跑"一眼可见 |
| ~~写盘中止/跳过之后仍刷新 L2 基线，脏数据被永久掩盖~~ | **已修（2026-09-25）**。`wiki.py` 的 update 流程原本**丢弃** `wiki_aggregate.run(args)` 的返回值（`:479`），随后**无条件**刷新两级 manifest（`:517`）→ ① 新加的"整轮降级中止"虽然挡住了写盘，**产物一个字节没动，L2 基线却被刷成"已编译"** → `align` 报"库未变化" → 这批脏数据**再也不会重编**（正是 `:487-491` 注释想防的静默丢失）；② **`_prune_stale_domain_dirs` 跑在中止闸之前** —— 声称中止、磁盘却已经删了目录；③ **linkfix 在中止后照跑**，会按新标题改写旧页面，绕过中止闸动了磁盘；④ `n_pages` 写的是 `sum(len(p) for … dom_out.items())`，数的是 **dict 的键数（4）** 而不是页数 —— 19 域 × 4 = 76，实测 manifest 里那个可疑的 76 就是这么来的。**实测后果**：`L2 产物 17:11 / L1 产物 21:01` 落后近 4 小时，而 `wiki --action pending` 报 **0 条**。**修法**：`run()` 返回**如实回报** `{aborted, written_domains, skipped_domains, pages, pages_by_domain}`；中止闸**前移到一切破坏性动作之前**；中止时 **不跑 linkfix、不刷 L2 基线**（保留旧基线 → 脏数据保持"脏" → 下轮自动重试）；新增 `_l2_skip_ids()` 把"**没真正写出来**的脏域"（页数崩塌跳过的 / 整域编译失败的 / 什么都没写的 dry-run）的记忆 id 一并 `drop_ids` 出基线。**通用原则：基线的语义是"这些输入确实编译过了"，任何"没写出来"的路径都必须能从基线里剔除 —— 宁可下轮重试，也不能漏剔除。** |
| ~~LLM 全失败时 L2 聚合仍降级写出，会丢页~~ | **已修（2026-09-25）**（事故当天修）。**2026-09-25 实测事故**。定时档位触发（`reasons: ["定期档位 09:00/12:00 已到且未触发","今日首次写入记忆"]`，`last_update_at` 已 3 天前）→ `wiki_aggregate` 跑 28 分钟，期间 **36 次 LLM 调用全部 HTTP 524**（上游不可用）→ 域内**细分步骤降级**，把 `03-API-调用-协议与网关`(33→1)、`04-AgentMemHub-记忆系统`(2→1)、`05-DSH-Hermes-助手架构与运行`(44→1) 各压成 1 页 → **362 页掉到 289、丢 73 页、334 处 `[[链接]]` 悬空**。**根因三条**：① 每域写盘是"**先 `unlink` 本域旧 `*.md` 再写新页**"，没有原子交换；② **没有"页数崩塌"保护**（33→1 照写不误）；③ **没有按 LLM 失败率中止**。**恢复**：`database/backups/` 的"增量更新前自动快照"有完整 362 页 → `snapshot --snapshot-action restore --wiki-only` 回滚产物、**不动索引库**（`units(source='wiki')` 始终是对的 362）→ 再补跑 aliases 迁移（快照早于它）。⚠️ **两个附带坑**：恢复是**整目录 `rmtree` + `copytree`**，会把用户放在产物目录里的 `.obsidian/` 一并抹掉（幸好事先备份）；编辑器的空文件会**按文件名抢占 alias**（Obsidian 文件名匹配优先于 alias），页被删后链接悬空、点击即生成空文件，该空文件又永久遮住真页面的 alias。**已落地的三道防护**：① **域级原子写** `_swap_domain_dir()` —— 先全量渲染到 `.tmp_<域>`，再与正式目录交换（旧目录先改名为 `.bak_`，交换失败尽力还原），不再原地"删旧写新"；② **页数崩塌拒写** `_is_page_collapse()` —— 旧页数 ≥ `COLLAPSE_MIN_OLD`(4) 且新页数 < 旧 × `COLLAPSE_RATIO`(0.5) → **跳过该域、保留旧产物**，并记 `domain_write_skipped` + 失败清单；③ **整轮降级中止** `_is_degraded_run()` —— fail-open 降级页占比 ≥ `ABORT_DEGRADE_RATIO`(0.5) → **整轮不写盘**（`run_aborted`），等 LLM 恢复后重跑。阈值都是模块常量，可调。**根因也已修（同一日，第二批）**：细分原本**一次调用**处理整个域，prompt 随域内页数线性增长 → 114 个成员必撞 524；现改为**分批**（`plan_requests()`，上限 `L2_PLAN_BATCH_MAX` / 配置 `wiki.l2.plan_batch_max`，默认 40，批内编号**映射回全局编号** —— 与第一级 `wiki_compile.plan_groups_batched` 同一个坑），并把兜底从"**退化为整域一页**"改成"**成员各自成页**"（`SyncRunner` 还做了单条失败隔离）→ 即使某批仍失败，也只影响那一批覆盖的成员，**不会再出现"43 页的域被压成 1 页"**。同时把 LLM 调用层抽成 `runner` 接缝（`stage_plan(..., runner=)`），为**离线批处理**（上传 JSONL 给云平台空闲时段执行）预留了接口 —— 见 [`llm-batch-offline.md`](llm-batch-offline.md) |
| **页面 frontmatter 补 `aliases`（Obsidian 图谱可解析）** | 2026-09-25 新增（`wiki_aggregate.render_page()`）：写出 `aliases: ["<title>"]`。**为什么必须**：Obsidian 的 `[[X]]` 按**文件名或 aliases** 解析、**不读 `title`**；而 L2 文件名带 `NNN-` 序号前缀、页面里 `**相关**：[[标题]]` 用的却是**标题** → 缺 aliases 时 **948 处链接全部悬空**（点链接还会让编辑器自动新建空文件）。补后 **948/948 可解析**，其中 65 处落在 19 组同名 alias 上（Obsidian 会提示 ambiguous，占比 6.9%，可接受）。title 来自 LLM，故用 `_yaml_flow_scalar()` 做双引号转义。已有 362 页用一次性**纯文本迁移**补齐：不动正文、**不改 `src_id`**（它由**路径** crc32 派生、与内容无关）→ 投影不失效 |
| ~~顶层 `index.md` 链接指向 `.json`~~ | **已修（2026-09-24）**。增量路径的 `render_index_from_disk()`（`scripts/wiki_aggregate.py`）原本 `glob("*.json")` 并把 **json 文件名**拼进索引链接 —— 实测 `out_l2_mimo/index.md` 的 **362 条链接里 286 条指向 `.json`**（点开是 JSON 原文）。**全量路径 `render_index()` 用 `.md` 是对的**，所以只有"增量更新跑过之后"才会坏；域级 index 走 `render_domain_index()` 另一条路，因此没被污染 —— 两个条件叠加让这个坏链潜伏了很久。现改为**以 `.md` 为准**枚举（跳过 `index.md`），json 仍作 title/summary 首选来源、缺失时回落解析 md 自身（`_page_meta_from_md()`）。修后：`.json` 286→0、`.md` 76→362。**顺带暴露**：项目**缺一个产物结构 lint**（断链 / index 与实际文件不一致，正是上游 `lint.py` 的 P0 项）—— 有 lint 的话它不会活这么久 |
| ~~update 缺跨进程锁~~ | **已修（2026-09-21）**。`_RUN_LOCK` 原本是 `threading.Lock`，**只在单进程内有效** —— 实测 MCP 的写入钩子与人工恢复脚本并发跑，日志里出现两份"两段式：先分组"（101 / 102 两个版本），两者写同一批产物与 manifest。现改为 `_single_flight()` = 进程内锁 + **跨进程锁文件**（`O_CREAT\|O_EXCL` 原子创建，内容写 PID/时间戳，3 小时或持锁进程已死即抢占）。**两个坑都踩过**：① 先试 `msvcrt.locking`（追加模式打开 + 先写一字节再锁），实测**跨进程不互斥** —— 测试直接抓到子进程持锁时父进程仍拿到；② `psutil` **不在依赖里**，用它判进程存活会退化成"锁永久有效"，改为 `OpenProcess`/`os.kill(pid,0)` |
| **长 update 会拖长蒸馏收尾** | 两条写入钩子的行为**不同**，别混：**MCP `_save` 是 daemon 线程**（`_wiki_trigger_probe`，立即返回）—— 实测 update 跑 30~60 分钟期间 MCP 工具**仍即时响应**；**蒸馏收尾是同步**调用（`distill.py` 直接等返回值再 `emit`）。所以"长 update 会阻塞 MCP"是**误判**，真实代价是批量蒸馏会多等很久。根因见下一条 |
| ~~逐组编译是串行的~~ | **已修（2026-09-21）**：`compile_session` 原本对分组结果**串行**逐组调 LLM（32 组 × 30~150 秒 ≈ 48 分钟）。各组之间**无依赖**，串行纯属浪费 —— 改为 `wiki.page_workers`（默认 3）并发，**结果按组序重排**（完成顺序不确定，直接 append 会让页序随机漂移、产物不可复现）。实测降到约 12 分钟 |
| **429 限流：全局限流闸门** | 并发之后多个 worker 会几乎同时撞上同一个 RPM/TPM 窗口。若各自独立退避，它们会在同一时刻一起恢复、一起重试，再次撞限（**退避共振**）→ `agentmemhub/llm.py` 用**进程级共享**的闸门统一推后，所有线程一起等。另：解析 `Retry-After`（秒数或 HTTP-date，上限 300s 防服务端给离谱值卡死）、限流把总尝试次数放宽到 `max_retries+1+2`（RPM 窗口是分钟级，默认 3 次常常不够） |
| **待更新记忆的删除** | 2026-09-22 新增（`agentmemhub/wiki_pending.py`）：`wiki --action pending\|drop\|ignored\|restore` 与 `/api/wiki/pending*`。**软删除** = 写 `distilled_memories.wiki_ignore_at`（记忆**仍存在、仍可召回**，只是不再参与 wiki 编译，可 `restore` 取消）；**硬删除** = 真删蒸馏表行 + **两份召回投影**（`dst_<hash>` 与 `mcp_<hash>`）+ 向量（不可恢复，需 `confirm`）。**只允许删尚未进 wiki 的记忆** —— 已进 wiki 的会被拒绝（那类删除会让页面里的 `[m<id>]` 变死引用，属于 [`memory-deletion.md`](memory-deletion.md) 的范畴）。三个要点：软删待更新的记忆**不触发任何重编**（它本就不在基线里）；硬删后**自动重建 manifest**，否则 align 会报"失去输入"而触发无意义重编；**两份投影都要清** —— 只清 `dst_` 会让那条记忆**仍能被检索命中**（2026-09-22 实测踩到"删了还能搜到"） |

---

## 10. 快照与回滚（`agentmemhub/snapshot.py`）

**一份快照 = 索引库整库 + wiki 产物全目录**——蒸馏表（LLM 产物，重蒸贵且结果
漂移）、units 反馈演化值、wiki 页面与域结构，全部可一次回滚。

- 存放：`database/backups/<时间戳id>/`（data_dir 沙箱覆盖，测试自动隔离）
- **增量更新前自动快照**（`wiki.update` 开头，对齐判定通过后才做；失败旁路
  不阻塞更新），手动随时可做
- **保留份数可配**：`snapshot.keep`，默认 **10** 份，超出自动删最旧。一份约
  190 MB（索引库 176 MB + 两级 wiki 产物 5 MB），默认总占用约 **1.8 GB**——磁盘紧
  就调小，想留更长历史就调大。**非法值（非整数 / <1）回退默认**：该值的唯一用途是
  决定删哪些目录，配置写错时宁可多留，不能因一个笔误清空历史快照
- **默认不压缩**（实测数据）：整份 gzip 可压到 **67%**（176 MB → 119 MB，6.5 秒），
  10 份省约 0.5 GB。但快照的价值在"出事时能立刻恢复"，**可读、可直接打开**比省这点
  空间更重要；且库里 93% 是含向量 blob 的索引库，本来也压不动多少。真要压缩，
  加 `compress` 开关即可（尚未实现）
- ⚠️ **频繁调试会加速淘汰历史快照**：keep 是"最近 N 份"而非"关键节点永久保留"。
  2026-09-21 一晚触发 5 次 update，就把更早的基线（含软删除基线）挤出了队列。
  **重要节点应手工打一份带 `--reason` 的快照**并单独留存
- **restore 前自动把当前状态存为保护快照**——任何误恢复都可再回滚

```bash
uv run python -m agentmemhub snapshot                      # 列出快照
uv run python -m agentmemhub snapshot --snapshot-action create --reason "…"
uv run python -m agentmemhub snapshot --snapshot-action restore \
  --snapshot-id <id> [--wiki-only | --db-only]
```

### 面板 / HTTP 接口回滚

回滚既可手动 CLI，也可走接口（面板"选择快照 → 一键回滚"）：

```bash
GET  /api/snapshots                     # 列出可回滚快照（最新在前）
POST /api/snapshots/create?reason=…     # 手动创建（同步，秒级）
POST /api/snapshots/restore?snapshot_id=<id>[&wiki_only=|&db_only=]
```

`restore` 是**后台长任务**（`tasks.submit` 单飞行，进度轮询 `/api/admin/job`）：
索引库整库覆盖 + wiki 产物整体替换，**先自动保存当前状态为保护快照**。
⚠️ 覆盖索引库文件时，请确保 MCP server 无并发写入（面板 serve 自身持有
短连接，后台任务内执行安全）——保险起见回滚时避免同时进行检索/写入操作。
回滚后各关联关系（dst_ 锚、[m<id>] 溯源、manifest 指纹、评分）随数据整体
回到同一时点，自洽无需修补。

`GET /api/snapshots` 除 `snapshots` 外还返回 `keep`（生效的保留上限），
便于调用方提示"还能留几份"。

实测：首份快照 188.8 MB（索引库 + L1/L2 全目录）。⚠️ 备份依赖 `data_dir`
沙箱与 wiki 目录配置——新增路径类配置时，同步检查测试沙箱（见 §11 第 8 条）。

---

## 11. 运维命令速查

**召回调参**（不是命令，是配置）：`agentmemhub.yaml` → `rag.retrieval.recall_level`
（1 最严格 … 5 最宽松，默认 3）+ 可选的 `rag.retrieval.page.*` 单项覆盖；
详见 §4.3。改完**需重启 MCP server 与面板 `agentmemhub serve`** 才生效
（两者都是常驻进程，配置在启动时读取）。

**配置自检**：加/改配置键之后跑

```bash
uv run python scripts/check_config_keys.py            # 列出每个键的读取点
uv run python scripts/check_config_keys.py --strict   # 有僵尸键则退出码 1（接 CI）
```

它找的是"**定义了却没有任何读取点**"的僵尸键。本项目踩过三次同一个坑
（`wiki.single_shot_max`、`wiki.workers`、`wiki.l2.*`，以及没有任何消费点的假开关
`wiki.enabled`），**共同特征是配置里的值恰好等于代码里硬编码的默认值** —— 所以既
不报错、也不让测试变红，能潜伏数月。`tests/test_config_audit.py` 里有守护测试。

```bash
uv run python -m agentmemhub ingest                 # 采集（会话级增量）
uv run python -m agentmemhub distill                # 记忆蒸馏
uv run python -m agentmemhub wiki --action align    # 对齐审计（只读）
  --out <产出目录> [--stage l1|l2] [--db <索引库>]
uv run python -m agentmemhub wiki --action update   # 增量更新
  --l1 <第一级目录> --l2 <第二级目录>
uv run python -m agentmemhub wiki --action triggers # 触发器配置与状态
uv run python -m agentmemhub wiki --action trigger [--force]
uv run python -m agentmemhub wiki --action backfill-manual   # 直写记忆补账（幂等）
uv run python -m agentmemhub wiki --action index-pages --out <L2 目录>
                                                 # L2 知识页投影进召回面（页面级召回）
uv run python -m agentmemhub wiki --action failures [--stage l1|l2]
uv run python -m agentmemhub wiki --action retry [--stage l1|l2] --src <第一级目录>

uv run python -m agentmemhub memory-log              # 记忆操作事实流（写了/读了什么）
  [--event write|read] [--path mcp|http|distill] [--grep 关键词] [--limit N] [--json]
uv run python -m agentmemhub snapshot                # 快照与回滚（见 §10）
uv run python -m agentmemhub llm-config [--json]     # 各 LLM 使用点最终生效的配置（见 §13）

# 待更新记忆的查看与删除（见 §7.4 末尾与 §9）
uv run python -m agentmemhub wiki --action pending            # 列出待 wiki 更新的记忆明细
uv run python -m agentmemhub wiki --action drop --ids 4388,4392 --mode soft
uv run python -m agentmemhub wiki --action drop --ids 4388 --mode hard --confirm
uv run python -m agentmemhub wiki --action ignored            # 已软删除（不进 wiki）的
uv run python -m agentmemhub wiki --action restore --ids 4388

# HTTP（面板）
GET  /api/wiki/align?out=       POST /api/wiki/update?l1=&l2=
GET  /api/wiki/triggers         PUT  /api/wiki/triggers
POST /api/wiki/trigger/run      GET/POST /api/wiki/failures|retry
GET  /api/wiki/pending          POST /api/wiki/pending/drop
GET  /api/wiki/pending/ignored  POST /api/wiki/pending/restore
```

**两种删除的区别（务必分清）**：`--mode soft` 只是**不打进 wiki**（记忆仍在、
**仍可召回**，可 `restore`）；`--mode hard` 是**真删**（蒸馏表 + 召回投影 + 向量，
不可恢复，必须 `--confirm`）。两者**都只允许作用于"尚未进 wiki"的记忆** ——
已进 wiki 的删除会让页面里的 `[m<id>]` 变成死引用，属于
[`memory-deletion.md`](memory-deletion.md) 的范畴。

### 日志体系：三个事实流（视角不同，不要混用）

| 文件 | 视角 | 回答什么 | 写入点 |
|---|---|---|---|
| `logs/mcp.log` | **协议层** | 谁调了什么工具、参数、耗时、成败 | MCP 服务端唯一分发点（自动，不依赖 Agent 记） |
| **`logs/memory.log`** | **数据层** | **哪条记忆被写/读、内容全文、来自哪条路径** | `distill` 的落表处（一处覆盖 MCP/面板/蒸馏/CLI 全部路径） |
| `logs/wiki.log` | **流水线层** | 编译跑到哪、花了多少、断在哪；**触发器为什么触发** | wiki 编译与 `wiki_triggers._check_and_run` |

- `memory.log` 记**内容全文**（回答"当时到底写进去了什么"）：蒸馏记忆受
  `CONTENT_MAX=120` 硬截断，**直写记忆（`memory_save`）没有这层限制**（实测一条
  698 字），所以膨胀靠**单文件 5 MB 上限触发滚动归档**兜底，不靠"记忆一定很短"
  的假设
- **抗误删**：全部日志写入点都是 `mkdir(parents=True, exist_ok=True)` + 追加打开
  ——手工删掉日志文件（甚至整个 `logs/` 目录）后，下一次写入**自动重建并续记**
  （已实测 + 回归测试覆盖 `mcp.log`/`memory.log`/`wiki.log`/`web.log`/`tasks/`）
- **滚动与归档**（`logs` 配置段）：**全局 `logs.rotate` 对所有日志生效**，
  `logs.files.<日志名>` 覆盖它（单文件段为空则回落全局）。滚动规则**二选一**：
  `max_mb > 0` → 按**大小**滚动（**最高优先级**）；否则 `daily` 为真 → 按**自然日
  首次写入**（靠文件 mtime 判定，无需状态文件）。归档到
  `logs/archive/<日志名>/<日志名>.<毫秒时间戳>`（先按日志名分文件夹），
  默认 `keep_days: 30` 清理超期归档；`compress: false` 时保留**原始文件**。
  `logs/tasks/*.log` 是"一次性任务输出"，按 `keep_days` **直接清理**（不归档）。
- 来源路径由协议层用 `logs.memory_path("mcp")` 标记（contextvar），数据层自动读取
  —— 写入点在数据层、而"从哪来"只有协议层知道，这样无需改一长串函数签名
- 查询：`agentmemhub memory-log`（CLI）或 `logs.read_memory_audit()`（Python）

---

## 12. 架构演进时间线（备查）

1. **两级编译跑通**（L1 223 会话/660 页，L2 20 域/227 页，¥7）
2. **对齐审计 + manifest**：发现 wiki 与库分叉（163 条输入未进 wiki），建立
   内容指纹基线
3. **增量编译**：脏会话 L1 重编 + 脏域 partial 重编 + linkfix + manifest 刷新
4. **触发器**：写入时检测（MCP 每条 + 蒸馏收尾），三条规则，成功才打标记
5. **直写链路打通**：MCP memory_save 落蒸馏表（此前 wiki 完全不可见）——
   过程修复 5 个 bug：open_index 引用错误、slice_key 双前缀、投影初始分
   写死 0.3、`BASE_VALUE_AGENT_WRITE` 未 import（NameError）、面板去重锚
   用错（src_id → legacy_id）
6. **81 条历史补账** + value 统一 0.6 + LLM 重分类
7. **全链路实战验证**：触发 → L1 → 归域 → L2 → linkfix → manifest 刷新 →
   align 归零，一次通过
8. **测试污染事故与加固**：全量 pytest 中 run_distill 测试触发蒸馏收尾钩子 →
   钩子用真实 yaml 的 wiki.out_l1/out_l2 + 测试 tmp 库跑了 update，覆盖真实
   manifest、重编并覆盖 mcp__direct.md、波及 79 个 L1 会话产物。修复：
   conftest 钉死 `wiki_triggers._targets`（测试禁止解析真实 wiki 目录）；
   manifest 重建、L1 重编恢复（224 文件补齐，¥1.61）。教训：**任何"配置驱动的
   外部目标"（如产出目录）必须在测试沙箱中显式置空或钉死**，data_dir 沙箱
   不覆盖 yaml 里新增的路径类配置
9. **快照机制上线**：§10——起因正是第 8 条事故暴露的"交付物无快照、无法
   回滚"；首份快照 188.8 MB，此后增量更新前自动创建
10. **页面层进召回面 + 来源维度**：L2 页面整页投影为 `wiki_<crc32>` 单元，
   向量 + FTS 双回路独立通道参与融合（§4.1）；召回结果带 `origin`
   （native 自有沉淀 / external 外部投喂），Agent 与用户都能分辨两类数据
11. **L2 全量重跑统一版本**：此前存在混版（213/295/231 页并存 + 隔离目录
   183 文件），统一重跑为 **19 域 / 231 页、零失败**；随后补快照固定该稳定态，
   并把**快照保留份数配置化**（`snapshot.keep`，默认 5，非法值回退默认）
12. **页面噪声治理**（三轮迭代，§4.2）：诊断发现页面在"刷屏"（12/12 查询出现
   页面、平均 4.2 条、无关查询整条全是页面）。三次尝试的教训依次是：① **分数
   不能当门槛**（页面分与相关性甚至反相关：相关页 0.21×top vs 噪声页 0.63×top）；
   ② **证据也不能当门槛**（拼错查询下把真相关判死、把噪声放行）；③ **"证据等级
   绝对优先"仍不行**（低分字面匹配挤掉高分语义相关——`github网络失败` 的
   0.848/0.841 两条落选）。最终规则：**高分看分数、低分看字面证据，字面另留
   保底席位**；同批修掉两处：页面豁免通用阈值、保位用替换而非追加（§4.0 第 ⑦ 步）
13. **召回严格度档位**：`rag.retrieval.recall_level`（1~5，**默认 3「均衡」**）
   把候选宽度 / 相对阈值 / 终审阈值 / 页面席位收成**一条单调刻度**（§4.3），
   带单调性测试守卫——散着配容易配出互相矛盾的组合
14. **短语精确匹配通道**：`extract_phrases` + `phrase_search`（§4.4）——补上
   `ident` 通道"多词短语"的盲区。实测 `command code`：修复前 12 个含
   "Command Code" 的单元有 **11 个连候选池都没进**（页面排 63/63），修复后
   档 1 即返回 7 条全相关、页面 1.191 进前排
15. **静默失败治理**（§7.4）：新加的 `trigger_check` 日志让"某次为什么跑"第一次
   可查，顺藤查出 wiki 会**静默落后于记忆、而系统一直显示健康** —— 三个叠加缺陷
   （`l1.failed>0` 仍打触发标记 / **manifest 谎报基线**：失败会话被写进新基线，
   致 `align` 此后永久报"库未变化" / L1 全失败仍跑 L2 白烧 1290 秒）+ 一个观测
   盲区（`align` 只看 manifest diff，**61 条输入无任何页面引用却报"健康"**）+
   一个配置漏配（`wiki.llm` 继承默认 timeout 60s，而 L1 是长调用：101 条记忆的
   分组调用连续 3 次读超时 ≈ 187 秒后整会话失败）。**教训：不抛异常、不使测试
   变红的缺陷才最危险** —— 它们能否被发现，取决于"日志能不能回答当时发生了
   什么"
16. **编译可靠性与运维闭环**（2026-09-22）：在 15 的基础上把"跑得住"钉死 ——
   **组级并发**（`wiki.page_workers`=3）+ **结果按组序重排**（32 组 48 分钟 → 12 分钟；
   不重排则页序随机漂移、产物不可复现）、**分组分批**（`plan_batch_max`=40）、
   **429 进程级共享闸门** + 尊重 `Retry-After`（避免多 worker"退避共振"）、
   **`HTTPException` 纳入瞬态重试**（`IncompleteRead` 继承自它而非 `OSError`，
   切网会丢组且不重试）、**组级失败剔除新基线**（第 15 条缺陷的**同源细化版**，
   粒度从会话降到组：23 组失败 3 组、20 条记忆差点静默入库）、**跨进程单飞行锁**、
   **计时双口径**（墙钟 `seconds` + 累计调用 `call_seconds`，休眠不再伪造"9 小时"）。
   同日补齐运维缺口：**待更新记忆的查看与删除**（§7.5，软删 = 不进 wiki 且不触发
   重编 / 硬删 = 真删并重建 manifest，**边界限定"尚未进 wiki"**）。主模型切
   `xiaomi/mimo-v2.6-flash`（同价优先新模型，V2.5 留回退位；**质量指标待收尾重测**）
17. **召回终审的对称化 + 两个既有缺口的修复**（2026-09-22）：页面层早有"低分字面
   兜底席"，非页面层却只套一条乘性相对门限 `score ≥ 0.8×top` —— 而 **top 本身可能
   是噪声**（拼错查询 `windowsctrol` 的 top1 是完全无关的字面命中 1.237，门限被抬到
   0.990，24 条候选只剩 4 条，真相关的「WindowsControl 项目架构与技术栈」0.701 被
   白白切掉），而同一次结果里页面 **0.195** 反而进了 —— 这个不对称本身就是缺陷。
   现与页面层**同构**（§4.2.1，新增档位键 `nonpage_literal_seats`）。**实测 6 个
   查询里 5 个改善**，两处救回了被误杀的答案（`记忆怎么软删除` 7→12 条、
   `windowsctrol` 4→6 条）。同日顺带修掉两个既有缺口：
   ① **直写记忆双投影** —— `mcp_<hash>`（引擎落账）与 `dst_<hash>`（蒸馏投影）内容
   相同，导致结果里重复出现且**各占一个席位**（实测吃掉兜底席）→ 召回侧按
   hash + 正文双重去重；② **硬删除漏清直写落账单元** —— `drop_projection` 只清
   `dst_`，删完 `mcp_` 仍在 units 里，表现为"**删了还能搜到**"→ 两份都清。
   仍未解决的一类：「只有向量证据、没有字面命中」的低分真相关（如 git 代理查询的
   答案 0.789），规则兜底席给不了它 —— **那类只能靠 cross-encoder**（§4.2.2）。

18. **产物健壮性：把"不可逆"的坑逐个堵上**（2026-09-25）：**域级原子写**
    `_swap_domain_dir()`（先渲染到 `.tmp_<域>`、再整体换入，失败尽力还原，不再原地
    "删旧写新"）；**页数崩塌拒写** `_is_page_collapse()`；**整轮降级中止**
    `_is_degraded_run()`；页面补 `aliases`（Obsidian 的 `[[X]]` 按**文件名或 aliases**
    解析、**不读 title**，缺它时 948 处链接全悬空）；**空页不进召回面**
    （`skipped_empty`，判据用正文而非文件大小）；**清理逻辑白名单化**
    （`_prune_stale_domain_dirs()` 跳过 `.` 开头 + 只清 `%02d-` 域目录）。
19. **离线批处理 A2（MiMo Batch API）**（2026-09-29 ~ 10-01）：**编译段**走厂商离线
    （官方价是实时的 50%）。三段式：**提交轮** → 等待（实测 18 分钟 ~ 24 小时）→
    **落盘轮**；只有编译段离线（细分段只有几条请求，为省那点钱多付一整轮排队不划算）。
    自动收割走 `task_queue.harvest()`（挂在 MCP 启动 / 写记忆 / 蒸馏收尾 / **任意 CLI
    命令**上），三层短路（队列空 ⇒ 零成本 → 节流 → 跨进程锁），**没有常驻进程**。
    新增 `batch_client` / `llm_batch` / `wiki_batch` / `batch_ops` / `task_queue`。
20. **通知系统三层解耦**（2026-10-03）：**落库**（SQLite `notify.db`，**永远发生**）/
    **策略**（`notify.push_when` 五个开关，**按类别或按等级，两族取或**）/ **通道**
    （可插拔注册表 `register_channel(name, fn)`，内置 pushplus）。落库**不受策略影响**；
    某个通道抛异常不影响其他通道；另有事实流 `logs/notify.log`。
21. **离线可自动发起 + 全量重跑 + 两轮体检**（2026-10-04）：离线触发器落地
    （判定顺序 **离线优先 → 实时兜底**，两通道共用 `fired`/`last_update_at`，
    所以无论走哪条一天只更新一次）；`wiki --action full-run [--offline]` 全量重跑
    （通道由 `wiki.full.mode` 决定）。两轮全面体检共修 **12 处缺陷**，其中两处会
    **丢数据 / 静默失效**（`keep` 为空时全量清理会把知识库清空；收割链缺
    `_ensure_scripts_on_path()` 导致 MCP 自动收割**静默失败**）。全量重跑收敛结果：
    **21 域 / 396 页**、`l2_inputs 1839 = l1_inputs 1839`、记忆覆盖 **98.6%**、
    跨域重复页 **0**、Obsidian 链接验收 **0 未解析**.
22. **外部投喂地基 M0：口径与溯源修补**（2026-10-10）：为 feed 转译层（设计见
    [`feed-adapter-plan.md`](feed-adapter-plan.md)）打的三处地基 ——
    ① **采集层补 `origin`**：`rag/ingest.py` 的 INSERT 此前**不含此列**，feed 会话的
    **原始段落**落库为 `NULL`、召回侧 `or "native"` 兜底 ⇒ **投喂原文被当成自有沉淀**
    （同一条数据在原始事件层与蒸馏记忆层来源标记不一致）。现按 `origin_of(source)` 写
    （`feed`→external / 其余→native），并把它抽成**单点函数**与 `distill._project_one`
    **共用** —— 两处各写一份必然漂移；
    ② **页面层持久化 `doc_id`**：`wiki_index` 一直在解析 frontmatter 的 `doc:`，却**只用它
    判 `is_external`、把 id 丢了** ⇒ 召回命中一个投喂页后无法反查是哪份外部素材（只能靠
    `wiki_path` 读文件再抠 frontmatter，页面一重编就断）。现 `units.doc_id` 补列，投影的
    **INSERT 与 UPDATE 两条路径**都写入（非投喂页为 NULL，便于 `IS NOT NULL` 查询）；
    ③ **直写通路可声明来源**：`import_bundle` 逐条接受 `origin`（非法值回落 native），
    供 Agent 用 Read 工具读完 PDF/图片后写回外部知识（原先硬编码 `source='memory'` 且不写 origin）。
    顺带修一处**潜在崩溃**：`run_ingest` 写桥接列却**不保证列存在** —— 全新索引库上只跑
    ingest 会报 `table units has no column named origin`；现在它自己调 `ensure_bridge_schema()`
    （不指望别的调用点已经迁移过这张库）。
    口径统一：本文件原写 `source=pdf/web`，**以代码的 `feed` 为准**（格式信息放 `meta`）。
23. **外部投喂 M1–M4：转译层落地**（2026-10-10 ~ 10-14）：在 M0 的地基（口径统一 /
    `origin` / `doc_id`）之上，把"用户投喂的外部素材"做成一条**与自有记忆并列**的通路 ——
    **三段保留**（`raw/` 内容寻址原件、不可再生 · `mirror/` 中间物 · `out_feed/` 交付 md）
    ＋ **转换器注册表**（md/txt 一等公民 · 网页 · 公众号 · **PDF 四路由** · **DOCX**）
    ＋ **素材库** `materials.db`（`received/converted/published/failed/retracted`）。
    三条关键决策：① **转译不改写**（LLM 只在质量自评不达标时介入，且产出立即冻结进 `mirror/`）；
    ② 投喂内容**不进蒸馏、不进自有 wiki 编译**，只作为 `origin=external` 的页面单元参与召回
    （两个页面根互不嵌套）；③ **能力缺口与数据损坏分开报**（`needs_ocr` 是 P1，数据不一致才是 P0）。
    召回侧因此多两个锚：`units.doc_id`（页面回指原件）与 `wiki_path`（**绝对路径** ——
    两阶段召回第二阶段必须能直接打开）。

---

## 13. 配置继承体系（改配置前必读）

配置里存在**四种**"父 → 子"关系。它们机制不同、用途不同 —— 不弄清就改，容易写出
"配了不生效"或"覆盖范围超出预期"的配置（本项目已经在这上面踩过多次）。

| # | 机制 | 例子 | 覆盖项位置 | 为什么是这个形态 |
|---|---|---|---|---|
| ① | **内置默认 < yaml** | `DEFAULT_WIKI` → `wiki` 段 | — | 兜底：不配也能跑。`agentmemhub/config.py` 的段用**深合并**（`_deep_merge`，嵌套字典逐键合并）；`rag` 段（embed/retrieval/write）用**浅合并**（`{**默认, **yaml}`）——**两者不一致**，但 `rag` 段的嵌套项（如 `retrieval.page`）另有专门处理 |
| ② | **集中式细分覆盖** | `logs.rotate` < `logs.files.<name>`<br>`retrieval.recall_level` < `retrieval.callers.<name>` | **集中**在同名键下 | 同一类配置的**多个实例**（多个日志文件、多个调用方）—— 收在一处便于横向对比 |
| ③ | **就地覆盖** | `llm` < `wiki.llm` / `distillation.llm` / `wiki.l2.llm` | **分散**在各使用段 | 每个使用段自带一份 `.llm`。**注意继承链不统一**：`wiki.l2.llm` 刻意**跳过** `wiki.llm` 直连顶层（级联会让第一级换 provider 牵连第二级，实测踩过） |
| ④ | **三层**（唯一） | `DEFAULT_PAGE_POLICY` < **档位** < `retrieval.page.*` | — | 档位要能覆盖内置默认、yaml 显式又要能覆盖档位 —— 前两种机制都是两层，这里多一层 |

**"谁在用哪份 LLM 配置"看不出来怎么办**：跑 `agentmemhub llm-config`，它把
四个使用点（蒸馏 / 评分 / Wiki 一级 / Wiki 二级）**最终生效**的配置拼好，并标出
每一项是**继承**还是**覆盖**（2026-09-22 加，正是因为③的覆盖项太分散）。

**改配置时的检查清单**：

1. 这个键属于哪一层？属于哪种机制？
2. 它有没有被别的层覆盖？（如 `recall_level` 会被 `callers.<name>` 覆盖）
3. **加/改键之后跑 `uv run python scripts/check_config_keys.py`** —— 查"定义了却
   没有任何读取点"的僵尸键（§11；本项目踩过三次，共同特征是配置值恰好等于代码里
   硬编码的默认值，所以既不报错也不让测试变红）
