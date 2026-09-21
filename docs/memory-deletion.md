# 记忆软删除与 wiki 引用状态（设计基准）

> **状态**：设计已定，**待实施**　｜　拟实施日期：2026-09-21
>
> **本文的作用**：这次变更的**规划基准**。后续相关改动（`docs/API.md` 的接口契约、
> README 的功能描述、`docs/data-architecture.md` 的表矩阵与召回章节）**都以本文为准**，
> 实现完成后按第 10 节逐项同步，避免文档与实现漂移。
>
> 关联文档：[`data-architecture.md`](data-architecture.md)（§2 表矩阵 / §4 召回 / §10 快照）、
> [`API.md`](API.md)（对外契约）、[`branch-milestones.md`](branch-milestones.md)（Roadmap）

## 1. 要解决的问题

| # | 问题 | 现状 |
|---|---|---|
| **P1** | 用户删掉一条记忆后，wiki 页面正文里的 `[m<id>]` 引用指向了**不存在的记忆** | 现在没有任何"删除单条记忆"的入口，但一旦有了就会产生这种"死引用" |
| **P2** | 删除**不可恢复** | 误删就是永久丢失（蒸馏产物重编要花钱，且结果会漂移） |
| **P3** | 召回 wiki 页面时，调用方**不知道**这页里的引用已被删除 | 页面照常召回（正确），但 Agent/用户看不出"这段知识的来源已被删除" |

## 2. ⚠️ 概念澄清：两条完全不同的"引用失效"路径

**这一节是理解整个方案的前提**——两条路径的成因、语义、处理方式都不一样：

### 2.1 用户删除（← 本次方案解决）

用户在面板勾选一条或多条记忆 → 点删除。**这是"用户意图"，应当可恢复、可查询、
并且不妨碍 wiki 引用继续可解析**（显示为"原文已删除"占位）。

### 2.2 重蒸清理（← 已有的机制，本次只让它"可见"）

**触发**：提示词版本变更（`prompt_ver` 与记录不一致）或强制重蒸。

**代码位置**：`agentmemhub/distill.py` 的 `_purge_old_prompt_ver()`

```python
rows = SELECT id, content_hash FROM distilled_memories WHERE prompt_ver != ?   # 旧版本产物
for _mid, h in rows:
    drop_projection(idx, h)        # 删 units 投影 + 向量（FTS 触发器同步）
DELETE FROM distilled_memories WHERE prompt_ver != ?    # ← 物理删除，id 永久消失
log.info("清理旧提示词版本（!=%d）的蒸馏产物 %d 条", prompt_ver, len(rows))
```

**后果**：wiki 页面里所有指向这些旧 id 的 `[m<old_id>]` **永久失效**。

**为什么软删除救不了它**：重蒸产出的是**新内容、新 id**。把"旧的那条"标记为已删除
并不能让引用重新指向有效目标——引用需要的是**重定向**，不是"标记"。

**它的定位**：这是「**版本作废**」（旧版蒸馏结果被新版取代），不是「用户删除」。
所以它不属于本次软删除方案，而归第 9 节的**阶段 3**：引用重映射（同 `content_hash`
可映射到新 id）+ 局部重编（无法映射的改为占位文本）。

**顺带说明**：`wiki --action align` **已经能检测**这类失效引用（输出里的
`missing` = "页面引用了但库里已不存在"），所以"能查到"这一半已经具备，
缺的是"能修"。

## 3. 数据模型

三层落点，遵循项目既有的「源库=真相源、索引库=可重建派生」约定：

| 层 | 改动 | 说明 |
|---|---|---|
| **源库** `agentmemhub.db` | 新增墓碑表 `deleted_memories` | **用户删除意图必须持久**（索引库会被重建）。与既有 `deleted_conversations` 同风格，**防复活**语义一致 |
| **索引库** `distilled_memories` | 加列 `deleted_at INTEGER` | 召回过滤需要本地列（跨库查询不可行）；重建时从墓碑**重放**标记 |
| **索引库** `units` | 加列 `deleted_at INTEGER` | 检索侧统一过滤；投影时从蒸馏表带过来 |
| **索引库** 新表 `wiki_page_refs` | `(unit_id, ref_id, state)`，state ∈ `ok`/`deleted`/`missing` | 页面 → 引用 → 状态的**明细**；聚合计数由它 GROUP BY 得出（单一数据源，避免计数与明细不一致） |

```sql
-- 源库（墓碑；主键即稳定锚）
CREATE TABLE IF NOT EXISTS deleted_memories(
    source          TEXT    NOT NULL,
    conversation_id TEXT    NOT NULL,
    content_hash    TEXT    NOT NULL,
    src_id          TEXT,              -- 直写记忆的锚（mcp_<hash>），便于排查
    deleted_at      INTEGER NOT NULL,
    reason          TEXT,
    PRIMARY KEY(source, conversation_id, content_hash)
);
```

**为什么墓碑的锚用 `content_hash` 而不是自增 `id`**：`id` 在重建/重蒸后会变，
`hash` 才稳定——这样**重蒸后同内容的记忆不会"复活"**（这正是墓碑该防的）。
代价是**内容变了就是新记忆**，用户需要重新删（符合直觉）。

`distilled_memories` 已有的 `UNIQUE(source, conversation_id, content_hash)`
正好与墓碑主键对齐 ✓；`units` 已有**幂等加列**先例（`wiki_path`/`origin` 经
`ensure_bridge_schema` 渐进迁移），照此办理。

## 4. 接口（后端 + MCP 都要）

### 4.1 HTTP（面板/用户入口，优先级 1）

| 接口 | 作用 |
|---|---|
| `POST /api/memories/soft-delete`（body: `{ids: [...]}`） | **软删除**（支持多条，对应面板勾选） |
| `POST /api/memories/restore`（body: `{ids: [...]}`） | **恢复**（同样是勾选） |
| `GET /api/memories/deleted` | 已删除记忆清单（支持按来源/时间筛选） |
| `GET /api/wiki/broken-refs` | **死链清单**：页面 → 失效引用 → 状态（`deleted`/`missing`）+ 对应记忆 |
| `GET /api/wiki/page?path=` | 读页面 + 逐条引用状态（阶段 2 前端渲染用） |

### 4.2 MCP 工具（Agent 入口）

现有 5 个工具（`memory_search` / `save` / `recent` / `stats` / `score`）是 Skill 侧
硬依赖，**新增不破坏契约**。两个方案：

| 方案 | 形态 | 取舍 |
|---|---|---|
| **A（推荐）** | 新增 `memory_manage(action, ids, reason?)`，action ∈ `delete`/`restore`/`list` | 一个工具覆盖增删查，工具数少；与既有"五工具"风格一致 |
| B | 新增 `memory_delete` + `memory_restore` 两个工具 | 语义更直白，但工具数增至 7 |

> **待定**：采用 A 还是 B（实现前确认）。无论哪种，均需写进 `docs/API.md` 的
> 「MCP 工具」表并更新 Skill 侧说明。

### 4.3 召回 DTO 扩展（不是新接口，是既有检索的字段扩展）

页面命中项增加引用状态：

```json
{"kind": "page", "title": "...", "wikiPath": "...",
 "refs": {"total": 12, "deleted": 3, "broken": 1}}
```

MCP 文本输出相应追加一行提示，例如：
`⚠️ 本页 12 处引用中：3 条记忆已被用户删除、1 条引用已失效`。

## 5. 召回行为

| 层 | 行为 |
|---|---|
| `message` / `memory` | **过滤掉 `deleted_at IS NOT NULL` 的单元**（用户和 LLM 都搜不到） |
| `page` | **不过滤**——页面照常召回 ✓（用户明确要求），但 DTO 带 §4.3 的引用状态 |

实现要点：过滤条件加在 `hybrid_search` 的候选阶段（与现有 `exclude_session` 同层），
页面通道不受影响。引用状态由 `wiki_page_refs` 聚合，**在召回时算**（一次
`GROUP BY unit_id, state`，页面量级只有数百，开销可忽略）。

## 6. 前端行为（阶段 2，面板按用户要求放最后）

- **记忆列表**：已删除的记忆**照常显示**，带「用户已删除」标记；支持勾选 → **恢复**
  （与勾选删除同一套交互）
- **wiki 页面查看器**（新增）：渲染时把 `[m<id>]` 渲染为三种节点——
  - `ok` → 可点击下钻
  - `deleted` → 灰色占位「（原文已删除）」
  - `missing` → 占位「（引用已失效）」
- 阶段 1 只预留接口，不碰 UI

## 7. 本地 Markdown 编辑器（阶段 3，可选）

- **静态 `.md` 文件不改**（`[m<id>]` 原样保留）——Obsidian 等编辑器**无法实时识别**
  引用状态（静态文本不查库），这是能力边界，不是实现取舍
- 可选降级方案：`wiki --action export-state` 生成带状态标注的副本供 Obsidian 阅读
  （信息有滞后，非实时）

## 8. 环境安全与可恢复性（本次实施的硬要求）

吸取此前"测试污染真实 wiki 产物"的教训，本次实施必须满足：

| 措施 | 具体做法 |
|---|---|
| **基线备份** | 实施前做快照 ✓（`20260921_194233`，189.7 MB：索引库整库 + 两级 wiki 产物）。快照保留份数已配置化（`snapshot.keep`，默认 5） |
| **开发隔离** | 开发/验证期把 `AGENTMEM_HUB_DATA_DIR` 指向**副本目录**（真实库的拷贝），新表新列先在副本上验证；确认无误再切真实库 |
| **幂等迁移** | 新表用 `CREATE TABLE IF NOT EXISTS`、新列用幂等 `ALTER TABLE`（项目已有先例：`units.wiki_path`/`origin`），**不重写、不重建既有数据** |
| **测试隔离** | 继续使用 `conftest` 沙箱（`_sandbox_data_dir` + 钉死 `wiki_triggers._targets`），新增测试同样不得触碰真实库/真实 wiki 目录 |
| **可回滚** | 任何异常状态用 `snapshot --snapshot-action restore --snapshot-id 20260921_194233` 整体回滚（索引库 + wiki 产物原子恢复） |
| **代码侧** | 改动在 `feat/llm-wiki` 分支（已推送，20 个提交），随时可 `git` 回退 |

## 9. 分阶段交付

| 阶段 | 内容 | 可独立验证 |
|---|---|---|
| **阶段 1（后端）** | 墓碑表 + 幂等迁移 + 软删除/恢复/清单接口 + MCP 入口 + 召回过滤 + `wiki_page_refs` + **召回带引用状态** + 死链接口 | ✓ 用 MCP/HTTP 端到端验证（副本库） |
| **阶段 2（前端）** | 记忆列表「已删除」标记与勾选恢复；wiki 页面查看器 + 三种引用节点渲染 | 面板重做时一起 |
| **阶段 3（收尾）** | 本地 markdown 导出状态（可选）；**重蒸死链的引用重映射 / 局部重编**（§2.2） | 独立 |

## 10. 本文驱动的后续文档变更（实现完成后逐项同步）

| 文档 | 需要变更的内容 |
|---|---|
| `docs/API.md` | 新增 5 个 HTTP 接口；MCP 工具表增补；召回 DTO 的 `refs` 字段 |
| `README.md` | 「数据模型」与「记忆引擎管理」章节补删除/恢复语义；MCP 工具清单；v2.3 版本记录 |
| `docs/data-architecture.md` | §2 表矩阵（`deleted_memories` / `wiki_page_refs`）、§4 召回（过滤与引用状态）、§10 快照（提及删除不影响引用）、§12 时间线 |
| `docs/branch-milestones.md` | Roadmap 勾掉"软删除"；新增阶段 3 的"重蒸引用重映射" |
| `docs/llm-wiki.md` | 页面渲染/引用状态一节；重蒸死链的处理方式 |

## 11. 开放项（实现前确认）

1. **MCP 工具形态**：§4.2 的 A（合并 `memory_manage`）还是 B（两个独立工具）？
2. **墓碑锚范围**：`(source, conversation_id, content_hash)` 够用吗？是否还要支持
   "按 `content_hash` 全局删除"（跨会话同内容一起删）？
3. **删除的连带范围**：删一条记忆时，是否也删除它的 wiki 引用**所在段落**（会改变页面
   正文），还是**只标注不删内容**（推荐——保持页面稳定，符合"正常召回"的要求）？
4. **已有存量**：当前库里有没有需要"补登记"的历史删除？（从现状看没有删除入口，
   预计为空；实现时确认一次）
