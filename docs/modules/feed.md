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
agentmemhub feed retry <doc_id>
```

动作既支持自然写法 `feed add X`，也支持项目惯例 `feed --action add X`；
**flag 可以夹在中间**（`feed add --force X`）—— 这一条是对 feed 特意放宽的
（argparse 默认会把 X 判成"unrecognized arguments"，见 `cli.main()` 里的说明）。

`MCP` 工具（`feed_add` / `feed_list`）与面板入口**归 M5**。

---

## 5. 日志与排查

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

## 6. 测试在哪

`tests/test_feed_text.py`（**全部注入 tmp 配置，绝不碰真实库与产物目录**）：

- 契约：空正文拒绝、质量夹取与低质量判定
- 三段存储：内容寻址幂等、不留 `.part` 半截文件、kb 路径只由 doc_id 派生
- 登记表：增查、`sha256` 查重、状态白名单、低质量筛选
- T1 转换器：认领规则、md 原样直通、txt 包最小结构、**GBK 解码**、
  标题取值顺序、**代码块注释不外泄成标题**、`doc_time` 用发布日
- 端到端：投喂落三段并 `published`、重复投喂报幂等、内容变即新素材、
  `--force` 可重跑、`--dry-run` 不写盘
- 失败路径：文件不存在 / URL 未实现 / 未知类型 / **失败也保留原件并登记 failed**
- 日志：**事件行号不恒等**（包装层 +1 的守护）、失败事件为 `error` 级
- kb 渲染：frontmatter 可被 YAML 读回、低质量带 `warnings`

---

## 7. 不变量

1. **未知类型必须显式报错**，绝不静默跳过（本项目踩过"未知 source 静默跳过"的坑）。
2. **原始件永不丢**：认领失败、转译失败都要先落 `raw/` —— 否则将来补上转换器也无法重转。
3. **`kb` 文件路径只由 `doc_id` 派生**（`<doc_id>.md`）—— 投影的 `src_id`/`seq` 由路径派生，
   路径必须稳定；可变的标题放 frontmatter。
4. **`doc_time` 是文档自身时间**，不是投喂时刻（与 `distill._origin_time` 同语义）。
5. **`FeedDocument` 是唯一契约**：下游只认它，不为某种格式开后门。
6. **转译层的对外副作用必须可审计**：每个事件进 `logs/feed.log`，`depth` 要算对
   （隔了包装层必须 +1，否则行号恒等、字段作废）。
7. **质量自评低于阈值仍然发布，但必须打标**（`feed list --low-quality` 可筛）——
   静默失真比"拒收"更危险（`docs/feed-adapter-plan.md` §7.5）。
