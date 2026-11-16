# 项目分桶（Project Buckets）

> 2026-11-14 落地。**MCP 直写记忆的 `conversation_id` 从「按自然日」改为「按项目」。**
> 这篇记录"为什么这么分、怎么编码、怎么迁移、踩过什么坑"，
> 架构全景见 [`data-architecture.md` §7.3](data-architecture.md)。

---

## 1. 为什么不再按日分桶

2026-10-19 引入按日分桶（`direct-YYYY-MM-DD`），目的是**切小 wiki L1 的脏单位**
——L1 按会话整编，单个 `direct` 会话攒到 210 条时，任何一条新增都要重编 84 页。

它解决了"重编放大"，但留下两个结构性问题：

| 问题 | 实测数据 |
|---|---|
| **桶数按天线性增长** | 一年 365 个桶 = 365 个 L1 页，每页只装几条 |
| **同项目记忆被拆散** | 254 条 MCP 记忆里 **159 条（63%）被判 `similar`**，主题高度集中在同一个项目，却因分散在 N 个日桶，L1 编译时彼此看不见，无法合并 |

根因：`conversation_id` 对 MCP 记忆是个**人造维度**——它既不是真实会话
（那是 `source_session_id` 的事），也不是项目。当初按日只是技术妥协。

DSH 工作区的真实结构是**项目**（实测只有 2~3 个），不是日期。

---

## 2. 编码规则：直接用 DSH 的 `projectKey()`

不自己发明编码。规则抄自 `dsh-session-persistence-jsonl/lib/index.js:880-899`，
**逐字对齐**，好处是 `~/.dsh/sessions/` 的目录名可以直接当项目标识用（零解析、零维护）。

```
/  \  :        → 合并成一个 -
字母数字 . _ -  → 保留
其它字符        → ~ + 4 位大写十六进制码点
去开头所有 -    → 空则 "root" → 截断 251 → 前后补 --
```

| 工作目录 | 项目编码 | 长度 |
|---|---|---|
| `D:\data\vibeCoding\Agent_Memory\AgentMemHub` | `--D-data-vibeCoding-Agent_Memory-AgentMemHub--` | 46 |
| `D:\data\vibeCoding\virtual-projects\dsh` | `--D-data-vibeCoding-virtual-projects-dsh--` | 42 |
| `/home/user/projects/foo` | `--home-user-projects-foo--` | 30 |
| `/` | `--root--` | 8 |
| `D:\数据\项目` | `--D-~6570~636E-~9879~76EE--` | 27 |

> **Unix 不需要额外分隔符**：根目录的 `/` 被 `lstrip("-")` 吃掉了。
> 曾经设想过 `--|-home-...--` 的写法，但那会破坏"直接对着 DSH 目录名反推"的能力。

### ⚠️ 文件名 60 字符截断

`wiki_compile._out_stem()` 会把 cid 清洗后截到 60 字符：

```python
re.sub(r"[^\w\-]+", "_", cid)[:60]
```

本机已有一个 **56 字符**的工作区（`--C-Users-alice-AppData-Local-Temp-...--`），
更深的路径会超限 → **两个不同项目截断后同名、互相覆盖**。

处理方案（已实施，见 §6）：超限时保留前若干字符 + 回退到最近的 `-` +
`-` + `md5(cid)[:8]`，总长 ≤ 60。**DB 里的 cid 保持完整**，只截文件名。

---

## 3. 写入时的三级取值（C 混合模式）

`memory_save(source_project=...)`：

| 优先级 | 来源 | 说明 |
|---|---|---|
| 1 | **调用方显式传入** | Agent 知道自己在改哪个项目，传**工作目录路径**最准（服务端自动编码） |
| 2 | **活动推断**（纯代码） | 读会话日志数路径频次，找含项目标记的目录（见下） |
| 3 | **回退工作区** | `infer_active_project()` 返回会话工作区编码 |
| 4 | **回退按日分桶** | 拿不到项目时用 `direct-YYYY-MM-DD`，**写入绝不失败** |

三种入参形态都接受：

```
D:\data\vibeCoding\Agent_Memory\AgentMemHub   →  --D-data-vibeCoding-Agent_Memory-AgentMemHub--
--D-data-vibeCoding--                          →  原样
dsh-dev                                        →  原样（人工声明的逻辑项目）
```

### 活动推断怎么做（**代码，不是 LLM**）

```
① 找活跃会话（复用 _scan_active，读目录 mtime，毫秒级）
② 解压 session.v4.jsonl.zstd，正则抽所有绝对路径（只取最近 800 行）
③ 过滤：跳过 node_modules / .git / _asar / site-packages 等噪音
④ 每个目录向上找第一个含项目标记的祖先：
     .git / pyproject.toml / package.json / Cargo.toml / go.mod / pom.xml …
⑤ 按「项目根」累计频次，取最高频 → project_key() 编码
```

**为什么不用 LLM**：这是确定性统计任务，LLM 更慢（秒 vs 毫秒）、要钱、结果不可复现、
还可能编造不存在的路径。

**实测成本**（本机 6633 行 / 2.8 MB 日志）：解压 0.20s + 扫描 0.02s ≈ **0.23s**；
相对 `memory_save` 自身中位 5.9s 是 **+4%**，而"让 Agent 多调一次 shell"是 +3.3s（+56%）。
加 60 秒 / mtime 缓存后，同会话连续写入**接近 0**。

**三条纪律**：

| 情况 | 行为 |
|---|---|
| 命中次数 < 3 | **不猜**，回退工作区（标错项目比标空更糟） |
| 项目根 == 工作区根 | 视为"没有更细的项目"，回退工作区 |
| 日志不存在 / 解压失败 / 无 zstd | 回退工作区，**永不抛** |


### 虚拟项目

没有独立代码仓库、但需要作为项目归集记忆的，放在
`D:\data\vibeCoding\virtual-projects\<name>\`，编码天然稳定：

```
virtual-projects/dsh                 → --D-data-vibeCoding-virtual-projects-dsh--
virtual-projects/second-brain-skill  → --D-data-vibeCoding-virtual-projects-second-brain-skill--
virtual-projects/unclassified        → --D-data-vibeCoding-virtual-projects-unclassified--
```

**不要借用别人的安装目录当项目根**（如 `D:\AI_Agent\DSH`）——那是被研究的对象，
不是项目归属。语义错了比没分还糟。

---

## 4. 历史数据怎么迁移的

改造前的 252 条 MCP 记忆**既无 session 也无 cwd**，项目归属在数据层面不存在，
**无法用规则迁移**。所以用 LLM 两阶段分组：

```bash
# ① 分组（只读生产库，产物落 exports/，可人工复核）
uv run python scripts/project_grouping_probe.py
#    阶段1：id + 内容前 120 字 → 归纳项目清单（命名统一）
#    阶段2：分批（40 条/批）归类 → id → 项目 + 理由
#    产物：exports/project_grouping_report/report.html（可搜索/筛选）

# ② 迁移（先建快照，可回滚）
uv run python scripts/project_bucket_migrate.py --plan    # 预览
uv run python scripts/project_bucket_migrate.py --apply --yes
```

实测结果（252 条）：AgentMemHub 215 / DSH开发 27 / 未分类项目 8 / second-brain-skill 4，
置信度 high 占 96%，成本约 ¥0.2。

### ⚠️ 迁移必须改两张表

```sql
-- 蒸馏表（wiki 编译的输入）
UPDATE distilled_memories SET conversation_id=? WHERE id=?;
-- 召回面投影（否则「面板改名了、检索还在旧桶」）
UPDATE units SET conversation_id=? WHERE source='memory' AND legacy_id=?;
```

两表的关联键**差一个字符**，这是最容易踩的坑：

```
units.legacy_id              = 'mcp_3405ee949e14a506'   ← 下划线
distilled_memories.slice_key = 'mcp:3405ee949e14a506'   ← 冒号
正确关联：legacy_id = 'mcp_' || substr(slice_key, 5)
```

漏掉 `'mcp_'` 前缀会匹配 **0 行**，且**不报错**——表现为面板改了、召回没改。

---

## 5. ⚠️ `full-run` 不会重编 L1

这是迁移时最反直觉的一点，**必须记住**：

- `wiki.update(full=True)` 的 `full` **只影响 L2**（跳过早退、重新归域、全部域重编）；
- L1 编哪些会话由 `_resolve_dirty()` 决定，它**只看 align 的脏会话**；
- 而 align 按**记忆 id** 比对，迁移只改 cid 不改 mid → **不显示任何脏会话**。

后果：跑完 `full-run` 后，旧的 `mcp__direct*.md` 12 个页面**原样躺在那里**，
新项目桶只编了恰好被 align 认出的 2 个。

正确顺序：

```
1. 编译缺失的项目桶（wiki_compile.compile_one）
2. 确认 4 个项目页都在
3. 删除旧的 mcp__direct*.md
4. build_manifest("l1", db) 重建基线
5. 再跑 full-run（此时 L2 基于正确的 L1 文件集重新归域）
```

---

## 6. 后续项

| 项 | 状态 |
|---|---|
| **L1 文件名超长截断** | ✅ 2026-11-16 完成：`_out_stem()` 超 60 字符时保留「回退到 `-` 边界的前缀 + 原始 cid 的 md5 前 8 位」，保证不同项目不撞车（见 `modules/wiki.md` 不变量 15） |
| **项目名映射** | ✅ 2026-11-16 完成：`distillation.project.names`（编码 → 显示名）+ `session_origin.project_display_name()` 消费点，已登记进 `agentmemhub.yaml.example` |
| **投喂纳入项目** | ⬜ 未做：投喂（`origin=external`）目前**不进 wiki 编译**，是并列的独立层。要按项目组织是另一条链路，单独评估 |
| **多路径 → 同一项目** | ⬜ 未做：合并项目（如"DSH开发"跨安装目录与插件仓库）需要配置级多对一映射；当前用虚拟项目目录绕过了这个问题 |

### 6.1 采集的定位（2026-11-16 决策）

**本地会话采集不再作为主线。** 它是最早为「统一会话管理 + 记忆检索」做的；
后来重心转到中枢管理与知识库，**统一会话这一块逐步降权**，后续可能直接废弃，
或缩减为"简单的会话蒸馏"（只要结论、不要会话视图）。

因此：项目桶的输入**以 MCP 写入为主**，`~/.dsh/sessions` 里累积而未被 ingest 的
会话（2026-11 实测 50 个）**属预期状态，不是需要修的缺口**。
`agentmemhub sync` / `distill` 仍可用，但不再定期跑。
