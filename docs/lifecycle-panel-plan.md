# M3 面板生命周期视图（实施计划）

> **前置**：[`memory-lifecycle.md`](memory-lifecycle.md) 的 **M1（衰减）** 与 **M2（冲突检测）**
> 已落地并跑过真实数据。本文只规划 **M3**：把已有的报表数据变成**人能操作的视图**。
>
> **起草**：2026-11-19 ｜ **状态**：待评审 ｜ **依赖**：无新表（M2 的 `memory_conflicts` 已就位）

---

## 0. 一句话

把「哪些记忆该复核」从 CLI 报表变成面板上的**可操作清单**——
每个动作**都可逆**、都**要人工确认**，且**默认不预选任何一条**。

---

## 1. 现状盘点

### 1.1 已有能力（本阶段不重写）

| 能力 | 位置 | 说明 |
|---|---|---|
| stale 报表 | `rag/recall_stats.py::stale_memories()` | 返回 `id/src_id/source/role/time/value/priority/hit_count/last_hit_at/text/freshness/stale_score`，按 `stale_score` 升序 |
| stale CLI | `cli.py::cmd_stale` | `agentmemhub stale --days 90 [--json]`，含冷启动说明 |
| 冲突列表 | `conflicts.py::list_conflicts(status, limit)` | 带**两侧正文**（各 200 字）、`src_id`、`time`、`detail`（规则/相似度证据） |
| 冲突处置 | `conflicts.py::resolve(id, action, reason, by)` | `superseded` / `kept_both` / `dismissed`；`superseded` 走 `_archive_unit()` **两层退场 + 双投影一起锁** |
| 冲突扫描 | `conflicts.py::scan()` / `detect_for_unit()` | 纯 SQL KNN，2 万条约分钟级；新记忆写库时自动检测 |
| 归档底层 | `conflicts.py::_archive_unit()` | `manual_value=0`（召回退场）+ `wiki_ignore_at`（编译退场），按 `content_hash` 找双投影 |
| 加权接口 | `POST /api/engine/weight?traceId=&value=` | **只写 `unit_values`**，不碰 `wiki_ignore_at` |

### 1.2 缺口

| 缺口 | 影响 |
|---|---|
| **没有 HTTP 接口** | 面板完全够不着 stale / conflicts（`web/app.py` 里 `stale`、`conflict` 零匹配） |
| **没有归档/恢复接口** | `/api/engine/weight` 只退召回层，**退不掉 wiki 层** —— 正是 §11.5 踩过的"归档了但 wiki 还在教旧结论" |
| 没有面板视图 | 数据躺了两周没人看 |
| **日志事件未实现** | `conflict_detected` / `lifecycle_archive` 在全部源码里 **0 处**（设计文档 §6 要求登记）→ ✅ M3.1 已补 4 个 |
| `stale` 不标证据分层 | 候选 1070 条彼此无区别 → ✅ M3.1 已加 `tier`（文本模式打标记、`--json` 带字段）|

> `agentmemhub conflicts` CLI **已经存在**（`--scan` / `--list` / `--resolve` / `--from-wiki`），
> 不是本阶段的缺口 —— 只有面板侧够不着它。

---

## 2. 实测数据（2026-11-19，决定了后面的设计）

```
stale 清单（创建满 90 天 + 最近 90 天零命中）   1076 条 → 排除已归档后 1070 条
  freshness   min 0.849 / median 0.945 / max 0.998
  value       min 0.00  / median 0.30  / max 0.30   ← 全部 ≤0.3
  age(天)     min 90    / median 104   / max 132
  hit_count   全部 0
  source      zcode 578 / opencode 349 / qwen 73 / hermes 61 / dsh 15
  其中：已归档（manual_value=0）混入 6 条 ← 已修
        冲突 superseded 旧方 6 条 ← 但它们全都已归档，不在候选里了
```

### 2.1 由此**否掉**两条原设计

**① `freshness < 0.5` 判据（§5.2）上线即空转。**
它要求锚点年龄 > 90 + 180 = **270 天**，而当前最老的记忆只有 **132 天**，实测**满足 0 条**。
这不是参数调错，是"绝对阈值 + 数据还太年轻"的结构性问题 —— 半年内它永远筛不出东西。

**② 按 `value` 排序无区分度。**
1076 条里 median = max = **0.30**（蒸馏默认分），同源内完全同分。
「低价值」这个词在蒸馏记忆里没有信息量。

### 2.2 顺带发现一个真 bug

`stale_memories()` **没有排除 `manual_value = 0` 的已归档条目** —— 实测 6 条
（全库共 50 条已归档）。它们已经退场，却仍出现在"建议复核"清单里：
面板上会让人重复确认一件已经做过的事，归档按钮点了也没反应（本来就是 0）。

> 这一条必须在 M3.1 修掉，否则视图一上线就是错的。

### 2.3 结论：判据要从「绝对阈值」改成「证据分层」

现有数据里**没有任何一条**能算作"高置信度该归档"的证据 —— 唯一有证据的那类
（冲突旧方）在处置时就已经被归档了，见下。

所以本阶段**不承诺"建议 = 该删"**，只做两件事：
**把有未处置冲突的排前面**、**把没证据的交给人看**。

> ⚠️ **实施中纠正的一个假设（2026-11-19）**：原计划用「`superseded` 冲突的旧方」
> 当高优先级证据，但 `conflicts.resolve("superseded")` 会**立刻归档**旧方
> （`manual_value=0`），而归档条目已被候选清单排除 —— 那个中间态**不存在**。
> 真实库验证：`superseded` 31 条，其旧方出现在候选里的 **0 条**。
> 因此 tier 改成 `conflict_old`（`status='open'` 的旧方）：它提醒你去冲突队列
> 先做决定，而不是盲目归档。`dismissed` / `kept_both` 已表过态，不算证据。

---

## 3. 设计

### 3.1 证据分层（替代原 §5.2 判据）

| tier | 判据 | 面板表现 | 默认勾选 |
|---|---|---|---|
| `conflict_old` | 是某条 `status='open'` 冲突的 `unit_old` | 橙色徽章「有未处置冲突」，点击跳到冲突队列 | ❌ 不预选（但排最前） |
| `never_hit` | 零命中 + 创建满 `days` + 未被用户加权（`manual_value` 为空） | 灰色徽章「从未命中」 | ❌ 不预选 |
| `archived` | `manual_value = 0` | **不进候选清单**，进「已归档」抽屉 | — |
| `pinned` | `manual_value` 非空且 ≠ 0 | 不进清单（用户已表态） | — |

排序：`superseded_old` 优先，其余按 `stale_score` 升序（现有口径）。

**不做**：不标"建议删除"、不给"该删概率"、不自动勾选。真正能区分的那天
（数据满 270 天、或引入内容特征）再回来加阈值，而不是现在硬凑一个。

### 3.2 归档 / 恢复语义

**归档 = 两层同时退场 + 双投影一起**（复用 `conflicts._archive_unit()`，不另写一份）：

```
manual_value = 0        → 召回层 apply_value_boost 硬剔除
wiki_ignore_at = now    → wiki 编译输入排除；align 判成 removed，下次增量自动重编受影响页
dst_/mcp_ 两侧一起锁     → 只锁一行等于没归档（§11.5 实测）
```

**恢复 = 反向操作**：`manual_value = NULL`（清除）+ `wiki_ignore_at = NULL`。
恢复后 align 会判成 `added`，同样只重编受影响页面。

⚠️ **两者都会改变 wiki 基线**，UI 必须说清楚（见 §6）。

### 3.3 HTTP 接口（✅ M3.2 已实现，契约详见 [`API.md`](API.md) §3.9）

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/api/lifecycle/summary` | 概览数字：候选数 / 冲突数（按状态）/ 已归档数 |
| `GET` | `/api/lifecycle/stale?days=90&limit=200` | 候选清单（含 `tier` 字段与两侧对照所需的 `unit_id`） |
| `POST` | `/api/lifecycle/archive` | body `{"ids": [unit_id...], "reason": ""}` → 逐条 `_archive_unit()`，返回成功/失败明细 |
| `POST` | `/api/lifecycle/restore` | body `{"ids": [...]}` → 清 `manual_value` + `wiki_ignore_at` |
| `GET` | `/api/lifecycle/conflicts?status=open&limit=50` | 冲突队列（沿用 `list_conflicts` 的字段） |
| `POST` | `/api/lifecycle/conflicts/{id}/resolve` | body `{"action": "superseded\|kept_both\|dismissed", "reason": ""}` |

纪律：
- 全部 `POST` 走既有 `_same_site_guard`（CSRF 闸已覆盖非只读方法，无需新增）
- 归档/恢复**不做批量超限**：单次 `ids` ≤ `MAX_BATCH = 100`，超出 400
- 响应必须带 `changed / skipped / errors` 三段，不能只回 `ok: true`（部分失败要能看见）

### 3.4 面板（记忆报表页内，不加新标签页）

放在「记忆库健康」卡片下方，折叠式分区 **「生命周期」**，三个子块：

```
┌─ 生命周期 ─────────────────────────────────────────────┐
│ 候选 1076 │ 已归档 50 │ 冲突待处置 0 │ [立即扫描冲突]    │
├────────────────────────────────────────────────────────┤
│ ○ 全选   [ 归档所选 ]（二次确认，显示条数）              │
│ ☐ #30652 已被取代  132天前  从未命中  ← 新结论: …        │
│ ☐ #30653 从未命中  132天前  从未命中                     │
│ …                                                       │
├─ 冲突队列（空时折叠显示「暂无待处置冲突」）───────────────┤
│ #12 [新] xxx …  vs  [旧] yyy …   规则: 模型替换 0.94      │
│                          [ 归档旧方 ] [ 都保留 ] [ 误报 ] │
├─ 已归档（可展开）──────────────────────────────────────┤
│ ☐ #29487 归档于 11-07  [ 恢复 ]                          │
└────────────────────────────────────────────────────────┘
```

交互纪律（项目既有）：
- 归档 / 恢复 / 冲突处置 **一律 `confirm()` 二次确认**，文案写清后果（"将退出召回转 wiki 重编"）
- 批量归档确认框显示**实际条数**，不显示"全选"
- 操作后局部刷新，不整页 reload
- 空态：候选为 0 时显示"没有满足条件的记忆"，而不是空白表格

### 3.5 CLI

```
agentmemhub stale --days 90 [--limit 200] [--json]      # 文本模式已给 conflict_old 打标记
agentmemhub conflicts [--status open] [--limit 50] [--json]
agentmemhub conflicts --scan [--k 10] [--min-similarity 0.90]
agentmemhub conflicts --resolve <id> --action superseded|kept_both|dismissed [--reason "..."]
```

`conflicts` 全部参数**早已存在**（`--scan` / `--list` / `--resolve` / `--from-wiki` / `--reset`）。
`stale` 的 `tier` 在 M3.1 已随输出可见（文本模式打 `[有未处置冲突]`，`--json` 带完整字段），
**不再需要额外的 `--suggest` 开关** —— 参数越多越容易配错，而这里没有"要不要看"的取舍。

CLI 与 HTTP **共用同一份 service 函数**（`agentmemhub/lifecycle.py`），
避免"面板一种口径、CLI 另一种口径"——对齐审计已踩过这类分裂。

### 3.6 日志事件（必须登记 `docs/logging.md`）

| event | 何时 | 关键字段 |
|---|---|---|
| `conflict_detected` | `detect_for_unit` / `scan` 新增冲突 | `unit_new`, `unit_old`, `kind`, `rule_hit`, `similarity`, `layer` |
| `lifecycle_archive` | 归档成功 | `unit_ids`, `tier`, `distilled_tagged`, `by`（`panel`/`cli`）, `reason` |
| `lifecycle_restore` | 恢复成功 | `unit_ids`, `by` |

`conflicts.py` 目前**一条日志都没有**（实测 grep 0 处），这一项属于补债。

---

## 4. 里程碑

| # | 内容 | 产出 | 验收 |
|---|---|---|---|
| **M3.1** 数据层 ✅ | ① `stale_memories()` 排除 `manual_value=0`（修 bug，1076→1070）；② 新增 `tier` 计算（`conflict_old` / `never_hit`，含"为什么不 superseeded 旧方"的实测纠正）；③ 新建 `agentmemhub/lifecycle.py` 收敛 service；④ 补 4 个日志事件：`conflict_detected` / `lifecycle_archive` / `lifecycle_restore` / `lifecycle_conflict_resolve` | 12 个测试 | ✅ `tests/test_lifecycle.py` 全绿 |
| **M3.2** HTTP ✅ | 6 个路由 + `MAX_BATCH` 上限 + 三段式响应；顺带修掉「双投影在候选里出现两次」（接口测试撞出来的）| 11 个测试 | ✅ `tests/test_lifecycle_api.py` |
| **M3.3** 面板 ✅ | 记忆报表页「生命周期」卡片：概览 chips + 候选（tier 徽章 / 单条与批量归档）+ 冲突队列（两侧对照 + 三处置）+ 已归档抽屉（恢复）+ 空态；全部二次确认。顺带补了 `GET /api/lifecycle/archived`（原计划漏了这个数据源）| `index.html` | ✅ `test_web_layout.py` 元素断言；`audit_frontend_api.py` 73 路由 / 已用 62 |
| **M3.4** 收尾 | 文档同步（`memory-lifecycle.md` / `API.md` / `logging.md` / `README` 模块地图）—— **M3.1 已随改动一并完成**；M3.2/M3.3 落地后再补接口与面板两节 | 文档 | 全量 `uv run pytest` 绿 |

顺序上 M3.2 依赖 M3.1，M3.3 依赖 M3.2，M3.4 最后。**M3.1 可单独先上**（它修的是 bug）。

---

## 5. 验收标准

1. 面板能完成一次完整闭环：**看到候选 → 归档一条 → 它在「已归档」里 → 恢复 → 回到候选**
2. 归档后 `manual_value=0` **且** `wiki_ignore_at` 非空 **且** 双投影两行都被锁（测试断言三件事）
3. `agentmemhub stale` 的 `tier` 与面板一致（同一 `lifecycle.stale_candidates()`）
4. `scripts/audit_frontend_api.py` 无未接入的新路由
5. 全量测试绿（当前 1642 passed / 1 skipped）

---

## 6. 风险与权衡

### 6.1 归档会触发 wiki 重编（**必须在 UI 里说清**）

归档 = 编译输入减少 → 下次 `align` 判成 `removed` → 增量更新重编**受影响页面**（不是全库）。
代价取决于归档条目的分布：一次归档 100 条跨 20 个域，就会触发 20 个域的重编。

对策：
- UI 提示写明"会在下次增量更新时重编受影响页面"
- **不自动触发**重编；给一个「立即更新 wiki」的跳转按钮，让用户决定何时花这笔钱
- 批量归档上限 100 条（`MAX_BATCH`）

### 6.2 恢复同样会触发（`added`）

同上，只是方向相反。恢复按钮旁标注"恢复后该条会重新进 wiki"。

### 6.3 stale 报表的冷启动仍在

命中统计自 2026-11-07 才开始积累，此前老记忆一律"从未命中"。
所以 1076 条候选里绝大多数是**真的没人用过**，但也可能是"统计上线前用过"。
UI 需要一个常驻说明（一句话），不能只写在 CLI 输出里 —— 否则用户会误以为系统在推荐清理有价值的东西。

### 6.4 不做的

- 不自动归档（远期配置键 + 干跑日志，等两三个周期）
- 不加 MCP 工具（处置是人工决策，与"投喂不给删除"同一纪律）
- 不做 NLI / LLM 兜底判冲突（等规则层数据）
- 不改 `priority` 语义（挂起中）

---

## 7. 待拍板

| # | 问题 | 我的建议 |
|---|---|---|
| 1 | 生命周期放**记忆报表页内**还是独立第五个标签页？ | **页内折叠分区**——它和「记忆库健康」是同一件事的两个层次，拆页会让用户来回切 |
| 2 | 批量归档要不要一次最多 100 条？ | **要**，超过就 400；防一手误点全选 |
| 3 | 归档后要不要自动跑一次 wiki 增量？ | **不要**，只提示 + 给跳转按钮。重编要花钱，让用户选时机 |
| 4 | 原 §5.2 的 `freshness<0.5` 判据保留吗？ | **删掉**，并在文档里记一句"等数据满 270 天再议"，避免以后又照着它实现一遍 |
| 5 | 冲突队列首期是否只处理 `open`？ | **是**。`superseded/kept_both/dismissed` 只做筛选查看，不做撤销（撤销 = 恢复记忆，走已归档抽屉） |
