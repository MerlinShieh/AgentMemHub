# feed — 外部投喂转译层

> 设计文档：[`../feed-adapter-plan.md`](../feed-adapter-plan.md)（v2.2，**为什么这么设计**）
> 本文是**模块文档**：职责边界 / 数据形态 / 怎么用 / 怎么排查（**改代码前先读它**）
> 状态：**M0 地基 + M1（素材库 + T1 文本直通）已实施**；M2 入图并列、M3 网页公众号、M4 PDF/图片未做

---

## 1. 职责边界（别和 `adapters/` 混）

| | `agentmemhub/adapters/` | `agentmemhub/feed/`（本文） |
|---|---|---|
| 干什么 | **采集**内部 Agent 会话 | **转译**外部素材 |
| 输入 | DSH / Qwen / ZCode… 的会话存档 | 本地文件 / URL / PDF / 图片 |
| 输出 | `conversations` + `events`（源库） | 统一 md（**交付物**）+ 登记表 |
| 下游 | 摄取 → 蒸馏 → wiki 编译 | **知识库页面层**（与 wiki **并列**，不进编译链） |

两者形态同构（注册表 + 认领 + 统一产物），但数据流方向与服务对象完全不同，**不合并**。

**并列的含义**（用户 2026-10-10 拍板）：投喂内容**不进**记忆/蒸馏/wiki 编译 ——
它只以"知识库页面"的形态存在，召回时与自有 wiki 页面同台竞争（`origin` 维度可区分）。

---

## 2. 三段物料（用户明确要求的三份保留）

```
第 1 段  raw/      不可变原件字节，**内容寻址** raw/<sha256 前 2 位>/<sha256>.<ext>
第 2 段  mirror/   转译中间形态，按 doc_id 归档（HTML 源码 / 渲染快照 / OCR 中间态）
第 3 段  kb/       最终 md（**统一交付物 / 知识库本体**）—— 默认 exports/wiki_work/out_feed/
```

三个位置由 `feed` 配置段给出（`db` / `store_dir` / `kb_dir`），
经 `Config.feed_paths()` **统一解析成绝对路径**（三处必须同源，否则会写出
"原件进 A、登记表指向 B"这类错位）。

**为什么原件走内容寻址的文件系统而不是塞进库 BLOB**：溯源强度由 hash 锚保证，不必把字节放进库里
——而库一旦进快照，每份快照都要复制一遍素材（×`snapshot.keep` 放大）。内容寻址顺带带来两个好处：
**同内容重复投喂天然去重**，且文件可被任何工具直接打开。

**`materials.db` 是独立库且不进快照轮换**：它只是"元数据 + 指针"，丢了能从 `raw/` 重建
（原件才是不可再生的那一份）。

---

## 3. 数据结构

### 3.1 `FeedDocument`（转译层**对内唯一契约**）

所有转换器的输出都收敛成它（`feed/base.py`）：

| 字段 | 说明 |
|---|---|
| `doc_id` | 内容锚 = `sha256(原件字节)[:16]` —— **同字节 ⇒ 同 doc_id**（幂等的根） |
| `title` / `markdown` | 标题 / **最终 md**（统一交付物） |
| `kind` | `text` \| `url` \| `wechat` \| `pdf` \| `image` |
| `source_ref` | 原始路径或 URL（人可读的"从哪来"） |
| `doc_time` | **文档自身时间**（发布/修改），**不是投喂时刻** —— 时间衰减按它算 |
| `mirror` / `converter` | 镜像文件表 / `名字@版本`（**重转判据**） |
| `quality` / `warnings` | 质量自评：既是给人看的指标，也是"要不要升级到 LLM 兜底"的**触发器** |

**空正文直接 `FeedError(kind="empty")`** —— 空页面会变成"标题正当、内容为空"的召回单元。

### 3.2 `materials` 表（登记表 + 状态机）

```
received ──convert──▶ converted ──publish──▶ published
    └────────────── failed ◀──────────────┘（retracted：撤下交付物但原件保留）
```

`failed` **不删原件** —— 换转换器版本后可以 `feed retry`（这就是三段保留的价值）。

---

## 4. 怎么用（CLI）

```powershell
agentmemhub feed add <文件路径> [更多路径…]     # 投喂（默认动作，可省略）
agentmemhub feed add --dry-run <路径>           # 只查重与认领，不写盘
agentmemhub feed add --force <路径>             # 同内容已投喂过也重跑（修标题/换转换器后用）
agentmemhub feed list [--status failed] [--low-quality] [--json]
agentmemhub feed show <doc_id>
agentmemhub feed retry <doc_id>                 # 重转：优先原始来源；来源不可达时**自动回落 raw/ 原件**
agentmemhub feed retry --from-raw <doc_id>      # 强制用 raw/ 原件重转（不碰原始来源）
agentmemhub feed retract <doc_id>               # **撤下**（可恢复）：删交付物+收敛投影，**原件保留**
agentmemhub feed forget <doc_id> [--purge]      # **清除登记**；--purge 才连原件一起删
agentmemhub feed lint [--json]                  # **确定性体检**；P0>0 时退出码 1
```

**撤下与清除的分界**：`retract` 撤下投喂内容但**原件保留** ⇒ `reconvert` 能把同一份
重新发布出来（不是单向门）；`forget` 连登记一起清除，**原件默认保留**，只有显式
`--purge` 才删。理由是同一条：`raw/` 是不可再生的那一份，其他两层都能从它重建。

**为什么不需要回收站**：交付物（kb）是**派生物**，删了随时能从原件重建 —— 这正是
三段保留的回报。而回收站恰恰会引出"扫描器漏排回收站 ⇒ 真断链被掩盖"的**双向失效**
坑（参考实现 `lint.py` 踩过，见计划 §11.4）。所以这里**直接删，不设 `.trash`**。

**投喂后要让交付物进召回面**（M2 起）：

```powershell
agentmemhub wiki --action index-pages    # 投影**所有页面根**（L2 自有 + 投喂交付物）→ units
```

`wiki --action update` / 面板增量更新也会顺带投影一次（都走
`wiki_index.project_pages`，根集合由 `page_roots()` 单点给出）。

**为什么 `retry` 要有"回落 raw"这条**：三段保留的全部意义就是"原件不可再生、其余都可重建"。
若重转只能靠 `source_ref`（用户的下载目录），用户一清理文件就再也转不了 ——
**保留下来的原件等于白存**（2026-10-12 核对投喂数据时发现该缺口）。
回落后 `source_ref` **不被改写**（仍指向原始路径），溯源不受影响。

动作既支持自然写法 `feed add X`，也支持项目惯例 `feed --action add X`；
**flag 可以夹在中间**（`feed add --force X`）—— 这一条是对 feed 特意放宽的
（argparse 默认会把 X 判成"unrecognized arguments"，见 `cli.main()` 里的说明）。

`MCP` 工具（`feed_add` / `feed_list`）与面板入口**归 M5**。

---

## 5. 并列关系与多根投影（M2）

投喂交付物与自有 wiki **并列**（用户 2026-10-10 拍板），落在**两个独立的页面根**里：

```
页面根（由 wiki_index.page_roots() 单点定义）
├── exports/wiki_work/out_l2_mimo/   ← 自有记忆长出来的（**主根**）
└── exports/wiki_work/out_feed/      ← 外部投喂的交付物（**非主根**）
```

两者都投影进 `units(source='wiki')`，靠 `origin`（`native`/`external`）区分，
一起参与召回融合（默认混排）。

**多根投影的三个安全设计**（改动 `project_pages` 前必读）：

| # | 设计 | 不这么做会怎样 |
|---|---|---|
| ① | **主根不加根标签**，其余根用目录名 | 给所有根加标签 ⇒ 主根既有 `src_id` 全变 ⇒ **库里几百个页面 unit 整体重键**，挂在它们上的价值分与 👍👎 反馈变孤儿 |
| ② | **标签按配置位置定**，不按"已存在的根" | 某个根临时缺席时另一根标签漂移 ⇒ 身份变 ⇒ 旧行被判"磁盘上已不存在"而清掉 |
| ③ | **清扫只清本轮真扫过的根** | 某个根暂时不可用（目录被移走/还没建）⇒ 它的投影**被全部删光**，等它回来再重建 |

**`wiki_path` 是绝对路径**（不是根内相对路径）：它是"两阶段召回第二阶段"的入口，
消费者（MCP 输出的 `wikiPath`）要能**直接打开**。老数据是相对路径，下一轮投影自动重写。

**为什么 `out_feed` 不放进 `out_l2_mimo`**：`wiki_aggregate` 的写盘是**整域替换**
（会 `rmtree` 掉"不属于本轮"的域目录），放进去迟早被删。两个根**互不嵌套**是结构性保证
（有测试锁）。

## 6. 日志与排查

事实流：`logs/feed.log`（JSONL，标准头五项在写入口统一补齐）。

| event | 何时 | 关键字段 |
|---|---|---|
| `feed_received` | 原件已落 raw | `doc_id` `raw_path` `bytes` `sha256` |
| `feed_converted` | 转译完成 | `converter` `title` `chars` `quality` `warnings` |
| `feed_published` | 交付物已写 kb | `kb_path` `quality` `low_quality` |
| `feed_failed` | 任一步失败（`level=error`） | `error` `error_kind` `retryable` |
| `feed_skipped` | 幂等命中，**不是失败** | `reason="duplicate"` |

**排查剧本**：

```powershell
# 这次投喂跑到哪一步就断了？
rg '"doc_id": "<doc_id>"' logs/feed.log
# 哪些失败是可以重试的？
rg '"event": "feed_failed"' logs/feed.log | rg '"retryable": true'
# 素材库里现在什么状态（落库侧，与事实流互补）
agentmemhub feed list --status failed
```

⚠️ **`error_kind` 决定处置方式**，不要只看错误文本：
`unsupported`（要人换格式，重试无用）/ `not_found` / `registry`（代码 bug）/
`convert`（转换器内部失败，可重试）/ `needs_llm`（开关没开）/
`empty`（转出空正文，不可重试）/ `crash`。

---

### 确定性体检（`feed lint`）

灵感来自参考实现的 `lint.py`（它唯一的实质代码，352 行、零 LLM）：
**LLM 负责产出，脚本负责校验**。本项目此前只有 `obsidian.verify()`（依赖外部程序、
索引会过期），正因缺这一层，"顶层 index 链接指向 `.json`"那个 bug 才能活很久。

投喂链路尤其需要它：**四份东西必须严丝合缝** ——
登记表（`materials.db`）↔ 原件（`raw/`）↔ 交付物（`kb/`）↔ 投影（`units`）。
任何一处错位都是**静默的**：不报错、不让测试变红，只是"搜不到"或"点开是空的"。

| 级别 | 检查 |
|---|---|
| **P0** | 原件丢失 · 交付物丢失 · 交付物未登记 · frontmatter 缺字段 · `doc` 与登记不符 · `origin` 不是 external · **投影残留**（撤下没清干净 ⇒"删了还能搜到"）· 投影指向不存在的文件 · 投影 origin 错 · **页面根互相嵌套** |
| **P1** | 低质量（**打标但不拦发布**，§7.5）· 索引库/配置不可用（跳过而非失败） |

`P0 > 0` 时**退出码 1**，可直接当提交门槛或巡检脚本。

> **测试安全**：`run_lint` 的 `index_db` 可注入 —— 不注入就会读**真实索引库**，
> 结果依赖真实状态、测试不确定。撤下用的 `_reproject()` 则带**沙箱熔断**
> （它写的是真实索引库）：测试里会返回 `{"skipped": "test_sandbox"}`。

## 7. 测试在哪

`tests/test_feed_text.py`（**全部注入 tmp 配置，绝不碰真实库与产物目录**）：
- 契约：空正文拒绝、质量夹取与低质量判定
- 三段存储：内容寻址幂等、不留 `.part` 半截文件、kb 路径只由 doc_id 派生
- 登记表：增查、`sha256` 查重、状态白名单、低质量筛选
- T1 转换器：认领规则、md 原样直通、txt 包最小结构、**GBK 解码**、
  标题取值顺序、**代码块注释不外泄成标题**、`doc_time` 用发布日
- 端到端：投喂落三段并 `published`、重复投喂报幂等、内容变即新素材、
  `--force` 可重跑、`--dry-run` 不写盘
- **重转**：来源被删也能从 `raw/` 原件重跑（且 `source_ref` 不被改写）、
  原件也丢了则如实报错、不存在的 doc_id 报 `not_found`
- 失败路径：文件不存在 / URL 未实现 / 未知类型 / **失败也保留原件并登记 failed**
- 日志：**事件行号不恒等**（包装层 +1 的守护）、失败事件为 `error` 级
- kb 渲染：frontmatter 可被 YAML 读回、低质量带 `warnings`

`tests/test_feed_projection.py`（M2 多根投影与并列隔离）：
同名相对路径不撞 · **主根身份逐字节不变** · 标签不随根缺席漂移 · 根缺席时投影保留 ·
根在时页面删除仍收敛 · 根全缺则显式报错 · 根集合单点定义/互不嵌套 ·
配置读不到只投 L2 · **R1 锁**（全量 wiki 聚合的域清理不碰投喂根）

`tests/test_feed_lifecycle.py`（M2 撤下/清除/体检）：
撤下删交付物保原件且状态 `retracted` · 撤下可 `reconvert` 恢复 · 清除默认保原件、
`--purge` 才删 · 体检健康无 P0 · 交付物丢失/未登记/原件丢失/溯源字段写坏 **报 P0** ·
低质量是 **P1 而非 P0** · 投影残留（`projection_orphan`/`projection_stale`）报 P0 ·
投影 origin 错报 P0 · 页面根嵌套报 P0 · 报告可读且 P0 时提示退出码

---

## 8. 不变量

1. **未知类型必须显式报错**，绝不静默跳过（本项目踩过"未知 source 静默跳过"的坑）。
2. **原始件永不丢**：认领失败、转译失败都要先落 `raw/` —— 否则将来补上转换器也无法重转。
   **推论：重转不得依赖原始来源可达** —— `retry` 在来源消失时必须回落到 `raw/` 原件
   （`reconvert()`），否则"原件保留"只是摆设。
3. **`kb` 文件路径只由 `doc_id` 派生**（`<doc_id>.md`）—— 投影的 `src_id`/`seq` 由路径派生，
   路径必须稳定；可变的标题放 frontmatter。
4. **`doc_time` 是文档自身时间**，不是投喂时刻（与 `distill._origin_time` 同语义）。
5. **`FeedDocument` 是唯一契约**：下游只认它，不为某种格式开后门。
6. **转译层的对外副作用必须可审计**：每个事件进 `logs/feed.log`，`depth` 要算对
   （隔了包装层必须 +1，否则行号恒等、字段作废）。
7. **质量自评低于阈值仍然发布，但必须打标**（`feed list --low-quality` 可筛）——
   静默失真比"拒收"更危险（`docs/feed-adapter-plan.md` §7.5）。
8. **页面根只由 `page_roots()` 定义**（`wiki_index.py`）—— CLI / wiki 管道 / 面板
   各写一份必然漂移；两个根**互不嵌套**（否则一方的整域替换会毁掉另一方）。
9. **多根投影的三条身份纪律**（见 §5）：主根不加标签、标签按配置位置定、
   清扫只清本轮真扫过的根。违反任一条的后果都是**数据损失**（重键 / 投影错乱 / 误删）。
10. **`wiki_path` 是绝对路径** —— 它是两阶段召回第二阶段的入口，必须是"能直接打开"的
    形态，否则消费者拿到也读不了。
11. **派生物不需要回收站**：交付物（kb）与投影都能从原件重建，所以撤下**直接删**；
    只有 `raw/` 是不可再生的那一份 —— `forget` 默认保留它，`--purge` 才删。
12. **碰真实路径的出口必须有沙箱熔断**：`_reproject()` 写的是真实索引库，
    测试沙箱下必须跳过（本项目 2026-09-26 的事故就是"测试经真实路径改了真实产物"）。
