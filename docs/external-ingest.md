# 外部投喂通道调研

> 分支：`feat/external-ingest`（自 `feat/llm-wiki` 拉出）
> 日期：2026-09-22　性质：**调研 + 方案，未实施**
> 上游材料：Karpathy《LLM Wiki》gist + `ChavesLiu/second-brain-skill`
> （原件快照见 `exports/wiki_work/refs/`，该目录不进版本库；重新获取方式见文末）

---

## 0. 一句话结论

AgentMemHub 的 wiki 层**已经就是 Karpathy 范式的一个实现**，缺的只有最底下那层
「**不可变原件**」—— 现在这一层是**会话**。所谓"外部投喂通道"，本质就是
**给素材层增加一类"非会话"来源**，而不是新造一条知识管线。

而且**下游已经全部预留好了**（`origin: external` 的列、判定、召回过滤、页面解析
都在代码里），缺的只是**上游没有生产者**。所以这件事的成本被高估了。

**但实测发现一个真缺口**：采集层写 `units` 时**不填 `origin`**，投喂文档的
**原始段落**会被当成"自有沉淀"（详见 §2.3）。

---

## 1. 上游两个项目拆解

### 1.1 Karpathy《LLM Wiki》gist —— 范式定义

不是代码，是**一份可粘贴给 Agent 的理念文件**（11,985 字节）。核心主张：

> 不要每次提问都从原始文档里重新检索（RAG），而是让 LLM **增量维护一座持久的
> wiki** —— 知识**编译一次、持续保鲜**，而不是每次重新推导。

**三层架构**：

| 层 | 内容 | 谁拥有 |
|---|---|---|
| **Raw sources** | 文章/论文/图片/数据文件，**不可变**，LLM 只读 | 人（策展） |
| **The wiki** | LLM 生成的 markdown：摘要、实体页、概念页、对比、综述 | **LLM 独占写**，人只读 |
| **The schema** | 告诉 LLM 目录结构、约定、工作流的文件（`CLAUDE.md`/`AGENTS.md`） | 人机共演进 |

**三个操作**：

- **Ingest** —— 把新素材丢进 raw，让 LLM 处理：读 → 讨论要点 → 写摘要页 →
  更新 index → 更新相关的实体/概念页 → 追加 log。**一份素材可能触及 10~15 个页面**。
- **Query** —— 对 wiki 提问，LLM 找相关页、读、带引用地综合。
  **关键洞察**：*好的回答应该回填成新页面* —— 你探索出来的对比、分析、联系，
  不该消失在聊天记录里。
- **Lint** —— 定期体检：页面间矛盾、被新素材推翻的旧论断、无入站链接的孤岛页、
  被提及却没有独立页面的重要概念、缺失的交叉引用、可用一次搜索补上的数据缺口。

**两个特殊文件**：`index.md`（内容目录，每次 ingest 更新；提问时先读它再下钻 ——
作者实测在 ~100 素材 / 数百页的规模上**够用，可以不需要向量检索基础设施**）、
`log.md`（时间线，append-only；每行用固定前缀就能被 unix 工具切：
`grep "^## \[" log.md | tail -5`）。

**工具建议**：Obsidian Web Clipper（网页→markdown）、图片本地化（LLM 一次读不了
含内联图片的 markdown，得**先读文本、再逐张看图**）、graph view、Marp（幻灯片）、
Dataview、以及"wiki 就是个 git 仓库，版本历史白送"。

**为什么成立**：维护知识库真正累人的不是读和想，是**记账**（更新交叉引用、保持摘要
新鲜、标注矛盾）。人放弃 wiki 是因为维护成本增长快过价值；**LLM 不会烦**。
理念源头是 Vannevar Bush 的 Memex（1945）—— 他没能解决的就是"谁来维护"。

### 1.2 `ChavesLiu/second-brain-skill` —— 范式的 Skill 实例化

把上面的理念做成了一个 **Claude Code Skill**（最后更新 2026-04-15）。
体量极小，且**几乎全是 markdown**：

```
290 行  scripts/lint.py          ← 确定性检查（唯一的实质代码）
136 行  scripts/router.py        ← 子命令路由 + 知识库注册表
178 行  workflows/ingest.md      ← 投喂工作流（本文最关心的一份）
165 行  workflows/wipe.md  /  165 行 workflows/init.md
 89 行  workflows/query.md  /   68 行 lint.md  /  72 行 test.md
 68 行  SKILL.md  /  64 行 SCHEMA.md  /  43 行 IDEA.md
```

> **`IDEA.md` 就是那份 gist 的全文副本**（仅多一行 Original 链接）。
> 真正原创的是 **目录契约 + 工作流纪律 + 两个脚本**。

**目录契约**：

```
<KB_ROOT>/
├── raw/            原始素材（只读不改）+ raw/assets/ 图片
└── wiki/
    ├── index.md        内容目录（按 Sources/Entities/Concepts/Analyses 分类）
    ├── log.md          时间线（倒序）
    ├── overview.md     统计与近期活动
    ├── conventions.md  用户对该库的操作偏好（每次操作先读）
    ├── sources/        素材摘要页（与原始文件同名）
    ├── entities/       实体页（人物/组织/工具/项目/产品/数据集）
    ├── concepts/       概念页（思想/方法/技术/模式/理论）
    └── analyses/       回填的分析
```

**ingest 工作流的 8 步**（`workflows/ingest.md`）：

1. **发现新素材** —— 读 `index.md` 比对，扫 `raw/`（排除 `assets/`）找出未收录的
2. **阅读素材** —— md 直接读；PDF/图片用 Read 工具；**含图素材分两步**
   （先读文本 → 再扫图片引用逐张看 → 图表信息**整合进正文**，不能只靠图传达；
   重要的图用普通 markdown 图片语法引用）
3. **分析与决策** —— 提炼 3-5 个要点、判定要建/更新哪些页、找关联、**查矛盾**
4. **建 source 摘要页**（`wiki/sources/`，模板固定）
5. **建/更新实体页与概念页**
6. **维护图谱完整性**（**最关键的步骤**）
7. **更新索引与日志**
8. **跑确定性检查**（`lint.py`，P0 问题必须先修完）

**"什么时候问人"的判据**（这条设计值得单独记）：

- **自动执行**（不打断）—— 需**同时**满足：无矛盾 / 分类无歧义 / 无"可能是同一个
  但不确定"的合并歧义 / 计划新建页面 **≤ 5 个**。执行前用一段**不阻塞**的文字告知计划。
- **必须询问** —— 三场景：① **矛盾**（默认选项"标注矛盾并保留双方"）
  ② **合并歧义**（可能是同一概念） ③ **大规模收录**（新建 > 5 页，问收录范围）。
  多场景可在一次提问里合并（上限 4 问）。

**页面类型判定**：能用专有名词指代的是 **entity**，需要解释"是什么"的是 **concept**；
边界模糊时**偏向 concept**。仅被提及 1-2 次且非核心的不建页，在相关页里提一句即可。

**双向链接规则**：A 的 Related 链到 B，则 B **必须**链回 A；每个 entity/concept 页的
Related **必须**包含其 source 摘要页的反向链接；新建页后要**回头扫已有页面**补缺失链接。

**矛盾处理 5 条**：不删旧信息 / 用 `> ⚠️ 矛盾` callout 标注 / 分别注明出处 /
标注各自日期便于判断时效 / 涉及核心论点时提醒用户。

**`lint.py` 的检查项**（分档，实测 290 行）：
- **P0**：断链（`[[link]]` 指向不存在的页）、`[[raw/...]]` 误用（应走普通 markdown 链接，
  否则 Obsidian 图谱出现 raw 虚影节点）、frontmatter 完整性、`index.md` 与实际文件不一致
- **P1**：双向链接缺失、孤岛页（无入站链接）、entity/concept 页缺 `sources` 字段

**其它约定**：`KB_LANG` 决定 wiki 撰写语言（**素材本身什么语言不影响**，专有名词保留
原文），已有页面不自动翻译（靠 lint 检测后批量翻）；文件名一律小写英文+连字符
（≤50 字符）；frontmatter 必带 `aliases`（Obsidian Front Matter Title 插件据此显示标题）；
`tags` 始终中文。

### 1.3 二者关系与共同前提

- 后者是前者的**实例化**，`IDEA.md` 是前者原文的副本，两者**不冲突、可叠加阅读**。
- **共同前提（也是与 AgentMemHub 最大的差异）**：两者都是 **Agent 驱动、零程序化解析**
  —— PDF 和图片**靠 LLM 自己的 Read 工具读**，没有 pypdf/bs4 之类的东西。
  这是"Skill 形态"的红利：**把解析问题外包给了模型的文档理解能力**。
- 另一共同点：**人是策展者与提问者，LLM 是唯一的写者**。

---

## 2. AgentMemHub 现状盘点

### 2.1 三层对照：我们缺的正是最底下那层

| Karpathy 三层 | second-brain-skill | AgentMemHub 现状 |
|---|---|---|
| Raw sources | `raw/`（人放文件） | ❌ **缺失** —— 当前"素材"是**会话** |
| The wiki | `wiki/`（单级） | ✅ **已有** —— L1/L2 **两级**编译（`out_l1_mimo`/`out_l2_mimo`） |
| The schema | `SKILL.md`+`SCHEMA.md`+`workflows/` | ✅ **已有** —— `AGENTS.md` + 编译 prompt |

| 操作 | second-brain-skill | AgentMemHub 现状 |
|---|---|---|
| Ingest | `/wiki ingest`（人放文件后触发） | ⚠️ **只有会话入口**（采集→蒸馏→编译，全自动） |
| Query | `/wiki query` | ✅ 六路召回 + 终审兜底（比它强得多） |
| Lint | `lint.py`（确定性） | ⚠️ 部分 —— `align`（输入覆盖度）、`check_eval_grounding.py` |

### 2.2 已经就位的（下游全通）

`origin`（来源维度）**在代码里早就埋好了**，与 `role`（层级维度：页面/记忆/消息）正交：

| 环节 | 位置 | 状态 |
|---|---|---|
| 索引库列 | `units.origin`、`distilled_memories.origin` | ✅ `ensure_bridge_schema` 幂等补列 |
| **判定点** | `distill._project_one`：`origin = "external" if m["source"] == "feed" else "native"` | ✅ |
| 页面层判定 | `wiki_index.py` 解析 frontmatter `origin: external\|feed` | ✅（**但无生产者**） |
| 召回过滤 | `search` / `mcp_server` / `web/app` 的 `origin` 参数（`native`\|`external`） | ✅ |
| Agent 可见 | MCP `memory_search` 的 `origin` 入参 + 每条结果的 `origin` 字段 | ✅ |
| 面板展示 | 记忆报表的**来源筛选**与 `[自有]/[投喂]` 标签 | ✅ |

### 2.3 缺口（上游没有入口）+ ⚠️ 实测发现的真缺口

**结构性缺口**（本来就知道）：

1. **没有任何代码产生 `source == "feed"` 的数据**（全仓库 grep 只有"读"没有"写"）
2. **没有素材层** —— 没有原件快照的存放位置与登记表
3. **没有投喂入口**（CLI / MCP / HTTP 都没有）

**⚠️ 实测发现的缺口（本次新查出）**：
**采集层写 `units` 时根本不填 `origin`** —— `rag/ingest.py` 的 INSERT 列清单里
没有它，所以投喂文档的**原始段落**落库后 `origin = NULL`，而召回侧是
`or "native"` 兜底 → **被当成"自有沉淀"**。

实测（临时探针，已撤）：

```
造 source='feed' 的会话 → replace_source → run_ingest → ensure_bridge_schema
  source='feed' conv='doc-001' role='user' origin=None      ← ❌ 原始段落
蒸馏投影（project_memories，source='feed'）
  source='feed' origin='external'                            ← ✅ 蒸馏记忆
```

**结论**：同一条投喂数据，在**原始事件层**和**蒸馏记忆层**来源标记**不一致**。
只有等它被蒸馏成记忆之后才标对。**这是任何投喂方案都必须先修的一处**
（修法：`run_ingest` 写 units 时按 `source` 映射 origin，与 `_project_one` 同一口径）。

### 2.4 文档与代码口径不一致

`docs/data-architecture.md` §9 写的是「文档 = 会话（**source=pdf/web**，原始层 +
记忆层两层存储）」，而代码判的是 `source == "feed"`。
**两处必须统一** —— 建议以代码的 `feed` 为准（单一来源标识，不必按格式分裂；
格式信息放 `meta` 更合适，见 §4.2 草案）。

---

## 3. 关键约束（直接决定实现形态）

这几条是查代码得到的**硬约束**，任何方案都绕不过：

| 约束 | 位置 | 影响 |
|---|---|---|
| **单事件截断 4096 字符** | `rag/source.py` `MAX_CHARS = 4096` | 长文档**必须分段**成多个 event，否则只留前 4096 字符 |
| **role 白名单 `("user","assistant")`** | `rag/source.py` `DEFAULT_ROLES` | 投喂段落若用新 role（如 `document`）**直接被过滤、不入库**；用 `user` 则语义污染 |
| **adapter 注册表硬编码** | `adapters/__init__.py` 的 `ADAPTERS` dict | 新 source 必须注册，否则 `get_adapter()` 返回 None |
| **未知 source 被静默跳过** | `cli.run_ingest`：`if a is None: out("[x] 未知 source"); continue` | 忘记注册不会报错，只会"什么都没发生" |
| **源库只读铁律** | `rag/source.py` `open_source_ro`（`mode=ro` URI） | 素材层**不能**直接写源库，必须经 `Store` |
| **会话唯一键** | `units` UNIQUE `(source, conversation_id, seq)` | 分段 seq 必须唯一且稳定 |
| **时间语义** | `distill._origin_time`：`units.time` 是**知识产生时间**而非写入时间 | 投喂应用**文档自身的时间**，不是投喂时刻，否则时间衰减全错 |

---

## 4. 路线

### 4.1 三条路线

**A. 程序化 adapter**（`FeedAdapter`）
`raw/` 目录 → 程序解析（md/txt/pdf/html）→ 造成"会话"→ **走现有摄取管线**
（`replace_source` → `rag ingest` → 蒸馏 → wiki 编译）。
- ✅ 白拿整条链路的既有能力：幂等锚（`src_id`）、会话级增量（`list_sessions`/
  `source_freshness`）、面板、召回、wiki 编译、快照回滚
- ✅ 可批量、可重放、可复现（程序化，不依赖 Agent 当次状态）
- ❌ 需要解析器依赖；PDF 表格/图表的解析质量不可控，而**这部分能力 LLM 本来就有**

**B. Agent 驱动投喂**（与 second-brain-skill 同构）
Agent（有 Read 工具，能读 PDF/图片）读原件 → 提炼 → 经一个**写入工具**入库。
- ✅ **零解析依赖**，直接吃模型的多模态理解力（表格、图表、版式）
- ✅ 与上游 skill 的工作流纪律天然对齐（可整段借鉴 ingest 的判据）
- ❌ 目前 **`memory_save` 没有 `origin`/`source` 参数**，写进去只会是 `native`
- ❌ 一次性、不可重放；质量取决于 Agent 当次的发挥；批量投喂成本高

**C. 分层混合**（raw 原件层 + A 管文本 + B 管复杂格式）

### 4.2 推荐 C，且按此顺序落地

**第 0 步（无论选哪条都要做）**：修 §2.3 的采集层 `origin` 缺口 + 统一 §2.4 的口径。
这一步**独立于路线选择**，且现在没有 feed 数据，改动零风险。

**第 1 步：确立素材层契约**（Karpathy 的第一层）
- 原件放 `raw/`（**不可变**，只读），加一张**登记表**（文档锚 / 路径 / hash / 来源
  URL / 投喂时间 / 状态）
- 幂等锚用**内容 hash**：同一文件内容没变就不重复投喂

**投喂文档 → 会话容器映射草案**（复用架构文档已定的"文档 = 会话"）：

| 字段 | 取值 | 理由 |
|---|---|---|
| `conversations.source` | `"feed"` | 与 `_project_one` 现有判定对齐 |
| `conversations.id` | 文档锚（内容 hash 前若干位） | 幂等 + 可溯源 |
| `conversations.title` | 文档标题 | 面板可读 |
| `events[].role` | **待定**（`user` 或扩白名单） | 见下方待决策 |
| `events[].seq` | 递增段号 | 满足 UNIQUE 约束 |
| `events[].content` | 每段 **≤ 4096 字符** | `MAX_CHARS` 硬约束 |
| `events[].time` | **文档自身时间**（发布/修改） | 与 `_origin_time` 的时间语义一致 |
| `session_meta` | 原件路径 / hash / URL / 格式 | 溯源；格式信息放这里而非 `source` |

**第 2 步**：文本类（md/txt）程序化 adapter —— **零依赖**，先跑通全链路。

**第 3 步**：二进制类（PDF/网页）。两条子路：装解析器（重），或**让 Agent 读完后
把正文交给 A 的通道**（B 补 A，复用 B 的多模态而不必自建解析）。

---

## 5. 可以从 second-brain-skill 借的 / 不该借的

**值得借（且我们缺）**：

| 机制 | 为什么值得 |
|---|---|
| **`log.md` 时间线 + 固定行前缀** | 我们的 `logs/wiki.log` 是**运行日志**（跑到哪/花了多少），不是**知识演进史**。两者不同，可并存 |
| **双向链接强制性检查** | 我们有页面互链，但**没有确定性校验**（A 链 B 而 B 不链回 A 会被漏掉） |
| **`lint.py` 的分档（P0/P1）** | 我们只有 `align`（覆盖度）与 grounding 检查，缺"结构健康度"这一整类 |
| **矛盾标注三件套** | 保留双方 + `⚠️` 标注 + 注明各自出处与日期 —— 我们编译时是覆盖式的，没有"矛盾留痕" |
| **"何时问人"的四条判据** | 我们目前是全自动无打断；投喂场景引入人工判断点（尤其"合并歧义"）是有价值的 |
| **图片两步读** | 若走 B 路线，这是必须遵守的操作纪律 |
| **`index.md` 作为检索入口** | 我们的 L2 域结构已在做类似的事，可对照 |

**不该照搬**：

- **`index.md` 当唯一检索入口** —— 我们是六路召回 + 融合 + 终审，比"读 index 再下钻"
  强得多，不改。
- **单级 `wiki/` 扁平目录** —— 我们是 L1/L2 两级 + 域结构，规模上更合适。
- **纯文件、无数据库** —— 我们的三层数据模型（源库/索引库/产物）是核心资产。
- **`entity`/`concept` 二分** —— 我们的页面类型由编译链路决定，不必套用。

---

## 6. 待决策问题（请定）

1. **路线**：A / B / C？（我推荐 **C**，且第 0 步先修 `origin` 缺口）
2. **投喂段落的 `role` 用什么？** 用 `user`（能被现有白名单收下，但语义是"用户说的话"，
   可能污染会话语境的判定），还是**扩白名单**加一个 `document` role（干净，但要动
   `DEFAULT_ROLES` 及所有依赖它的地方）？
3. **原件是否入库**：`raw/` 只放文件 + 登记表（轻，依赖文件系统），还是把原件快照
   也存进库（重，但可回滚、不怕文件被删）？
4. **是否要 `log.md`**：在现有 `logs/wiki.log` 之外，另建一份**知识演进史**
   （人可读、可 grep、可给 Obsidian 看）？
5. **投喂的内容走几层**：跟着会话一起走 **会话→记忆→wiki** 三层（一致性最好，
   但要等蒸馏），还是给投喂一条 **raw→页面** 的短路（快，但与主链路双轨）？
6. **要不要 Obsidian 视图**：Karpathy 说"Obsidian 是 IDE"。我们已有 `exports/` 产物
   目录，验证一下能否直接挂进 Obsidian（这本来就是待办里的"Karpathy 第三件"）。

---

## 附：上游材料如何重新获取

`exports/wiki_work/refs/` 不进版本库（`exports/` 整体 gitignore），需要时重取：

```powershell
$env:HTTP_PROXY="http://127.0.0.1:7897"; $env:HTTPS_PROXY="http://127.0.0.1:7897"
$dst = "exports/wiki_work/refs"
curl.exe -sL "https://gist.githubusercontent.com/karpathy/442a6bf555914893e9891c11519de94f/raw" `
  -o "$dst/karpathy-llm-wiki.md"
git clone --depth 1 https://github.com/ChavesLiu/second-brain-skill.git "$dst/second-brain-skill"
```
