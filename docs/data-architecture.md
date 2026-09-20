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
  │    │     units.src_id = content_anchor(text)   ← 内容锚 'mcp_<内容hash>'
  │    │       （与 tid 是两套 hash！src_id 用于引擎幂等，不用作跨表关联）
  │    └─ 蒸馏表 distilled_memories（save_direct_memory）：
  │          slice_key = 'mcp:<tid 去 mcp_ 前缀>'   ← wiki/面板关联锚
  │          content_hash = fingerprint(content)    ← 内容指纹 md5
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
| `units.src_id`（手写层） | `content_anchor(text)` | units.src_id（'mcp_\<内容hash\>'） | 引擎幂等（同内容不重投影） | **不能用它与蒸馏表关联**——两套 hash |
| `units.legacy_id` | import_bundle（存传入 tid） | units.legacy_id | trace id 的引擎侧真身 | **面板去重 / 直写关联的唯一正确锚** |
| `dst_<content_hash>` | 蒸馏表 content_hash | units.src_id（记忆层投影） | 记忆的检索分身；面板 unit_id/value | 锚不含会话——同 hash 跨会话共享一个投影行 |
| `content_hash` | `fingerprint(content)`（md5） | distilled_memories.content_hash | 内容指纹；幂等；dst_ 锚；manifest 指纹 | 直写经 sanitize 后的文本才计入——与原始 trace 文本可能不一致 |
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
| ⑧ 终审截断 | `safe_cutoff_hits`（桥接层，`floor_ratio` 来自档位） | 页面占名额、不参与窗口截断 |

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
因为它的向量分确实高。**分数与证据都无法可靠区分"高分噪声"与"低分相关"**，
这只能靠 rerank 解决。进一步方向（按性价比）：

1. **本地 cross-encoder rerank**（如 bge-reranker ONNX）：双塔余弦分重叠的
   标准解法，无 API 费用、延迟 ~100-300ms、确定性好；需新增第三个模型
2. **LLM 条件精判**（引擎已有 `judge` 参数与 `LLMFinalJudge`，fail-closed）：
   判别力强，代价是每次召回 +1~3s 与 token 费
3. **页面内分块向量**：实测收益中等且有反例（块级 max 会让噪声页上位、分数
   整体上移 0.03~0.10 需重标阈值），且**不解决准入问题**，优先级最低

### 4.3 召回严格度档位（统一配置）

`rag.retrieval.recall_level`（**1 最严格 … 5 最宽松，默认 3「均衡」**）把调优
过程中攒下的全部旋钮收成**一条刻度**——它一次性设定候选宽度、非页面相对阈值、
终审阈值、页面席位/字面兜底席/证据门槛线，避免散着配出互相矛盾的组合。
调用方只需回答一个问题：要少而准，还是全而杂？

| 档 | 名称 | 候选池 | 非页面阈值 | 终审阈值 | 页面席位 | 字面兜底席 | 证据门槛线 | 实测（四组查询条数 · 12查询基线） |
|---|---|---|---|---|---|---|---|---|
| 1 | 最严格 | 20 | 0.35 | 0.90 | 1 | 1 | 0.95 | 2/1/1/1 · 18 条 / 页面 7 |
| 2 | 严格 | 25 | 0.30 | 0.85 | 2 | 1 | 0.85 | 2/1/1/1 · 27 条 / 页面 13 |
| **3** | **均衡（默认）** | 30 | 0.20 | 0.80 | 3 | 1 | 0.70 | **4/4/4/4 · 55 条 / 页面 35** |
| 4 | 宽松 | 40 | 0.15 | 0.75 | 4 | 2 | 0.50 | 5/5/5/5 · 76 条 / 页面 46 |
| 5 | 最宽松 | 60 | 0.10 | 0.70 | 5 | 3 | 0.30 | 6/7/8/8 · 88 条 / 页面 57 |

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

### 7.4 面板记忆报表的双路 UNION 与去重

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
| 外部投喂 | 未做。架构已定：文档 = 会话（source=pdf/web，原始层 + 记忆层两层存储） |

---

## 10. 快照与回滚（`agentmemhub/snapshot.py`）

**一份快照 = 索引库整库 + wiki 产物全目录**——蒸馏表（LLM 产物，重蒸贵且结果
漂移）、units 反馈演化值、wiki 页面与域结构，全部可一次回滚。

- 存放：`database/backups/<时间戳id>/`（data_dir 沙箱覆盖，测试自动隔离）
- **增量更新前自动快照**（`wiki.update` 开头，对齐判定通过后才做；失败旁路
  不阻塞更新），手动随时可做
- **保留份数可配**：`snapshot.keep`，默认 **5** 份，超出自动删最旧。一份约
  190 MB（索引库 + 两级 wiki 产物），默认总占用约 1 GB——磁盘紧就调小，
  想留更长历史就调大。**非法值（非整数 / <1）回退默认 5**：该值的唯一用途是
  决定删哪些目录，配置写错时宁可多留，不能因一个笔误清空历史快照
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

# HTTP（面板）
GET  /api/wiki/align?out=       POST /api/wiki/update?l1=&l2=
GET  /api/wiki/triggers         PUT  /api/wiki/triggers
POST /api/wiki/trigger/run      GET/POST /api/wiki/failures|retry
```

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
