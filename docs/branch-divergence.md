# 分支差异记录：`main` ↔ `feat/llm-wiki`

> 记录时间：2026-09-18　｜　**2026-09-22 更新**：通用修复已回移到 `feat/llm-robustness`
> ｜　分叉点：`253257f`（Merge feat/memory-distillation）
>
> **为什么记这个**：`feat/llm-wiki` 是一次**大版本升级**（在 RAG 之上增加 LLM Wiki
> 编译链路），而 `main` 仍是上一版的 RAG 记忆引擎。两条线的定位不同，**暂不合并**。
> 但分叉期间容易出现"**本该属于主干的通用修复被夹带在功能分支里**"——
> 这份记录就是把这些差异挑出来，避免它们被埋没或将来冲突时才发现。

## 一、差异总览（2026-09-22 实测）

```
57 files changed, 12927 insertions(+), 175 deletions(-)
├── 大量新增文件（wiki 链路 / 召回 / 快照 / 测试）
└── 十余个修改文件（需逐个人工分类）
```

**规模与接口对照**：

| 层 | `main` | `feat/llm-wiki` |
|---|---|---|
| HTTP 路由 | 28 | **42** |
| MCP 工具 | 5 | 5（一致）|
| CLI 子命令 | 17 | **20** |
| 测试 | 488 passed | **723 passed / 1 skipped** |
| 提交数 | — | 领先 **25 个** |

## 二、通用差异（与 wiki 无关）

### 2.A ✅ 已回移 —— 分支 `feat/llm-robustness` @ `d992efd`

| # | 项 | 内容 | 为什么属于主干 |
|---|---|---|---|
| 1 | **记忆内容硬截断**（`distill.py`） | `clamp_content()` 在 `normalize_memories()` 里做代码级兜底：超长时断在句读处，句读太靠前则硬切加省略号 | `CONTENT_MAX=120` 原先只是**提示词软约束**，实测**没有任何模型稳定遵守**（MiMo 24% / LongCat 44% / deepseek 52% / ling 53% 超标）；`main` 的库里**存量问题已实测存在**（704/1543 = **45.6% 超标**，最长 2581 字），直接损害自包含性与召回质量 |
| 2 | **LLM 开关白名单修复**（`config.py`） | 抽出 `_LLM_SCALAR_KEYS`，`llm` 属性与 `_merge_llm` 共用它 | 原先是硬编码三元组白名单，**新增 provider 开关会被静默吃掉**（写进 yaml 却读不到，表现为"开关不生效"）；同时 `_merge_llm` 提升为 `classmethod` 并纳入 `repair_model` |
| 3 | **格式修复模型**（`llm.py`） | 主模型返回了内容但 JSON 解析失败时，把**原始输出**交给 `repair_model` 转规范 JSON | 蒸馏/合并/评分都会遇到"有内容但格式不对"，原先只能整条丢弃（fail-open 不登记 hash，靠重跑碰运气） |
| 4 | **用量与成本统计**（`llm.py`） | `usage_snapshot()` / `usage_reset()` / `_record_usage()` / `estimate_cost()`，模块级累计（`LLMClient` 每线程一个实例，统计必须跨实例汇总） | `main` 上跑蒸馏**看不到花了多少钱** |
| 5 | **思考模式开关**（`llm.py` + `config.py`） | `thinking: enabled\|disabled` 与 `reasoning_effort`，请求体顶层下发 `{"thinking": {"type": ...}}` | 思维链**计入输出计费**（实测占输出的 63–94%）；`reasoning_effort: low` **挡不住**推理量，**只有 `thinking: disabled` 能真正归零**——直接省钱 |
| 6 | **429 进程级限流闸门 + 尊重 `Retry-After`**（`llm.py`） | 撞限就推后"下次允许发请求的时间"，所有线程一起等；`Retry-After` 上限 300s | 并发调用下各自独立退避会"共振"；属于**所有链路共用**的鲁棒性 |
| 7 | **`HTTPException` 纳入瞬态重试**（`llm.py`） | `IncompleteRead` / `RemoteDisconnected` 继承自 `http.client.HTTPException`（**不是 `OSError`**），旧捕获会漏掉 | 表现为"网络抖一下直接丢一组、一次都不重试" |
| 8 | **计时口径**（`llm.py`） | 计时改用 `time.perf_counter()`（Windows `monotonic()` 精度仅约 15.6ms，短请求测出 0） | 度量正确性，与 wiki 无关 |
| 9 | **`_read_jsonl` 公共实现**（`logs.py`） | 把 `read_mcp_audit` 的行解析抽成 `_read_jsonl(path, default_name, limit)` | 通用重构（消除重复解析） |
| 10 | **测试断言改进**（`tests/test_config.py`） | 继承断言从"整字典相等"改为逐字段 | 否则每加一个可选 `llm` 字段就假失败 |
| 11 | **配置示例说明**（`agentmemhub.yaml.example`） | 补 `thinking` / `reasoning_effort` / `repair_model` 的完整注释 | 通用文档 |

**回移方式**：**逐文件挑选**，没有直接 `cherry-pick`。原因有两个——
① `19ce71e` 是**混合提交**（既含通用修复也含 wiki 内容）；
② 分叉之后 `llm.py` / `config.py` / `distill.py` / `logs.py` 又各自长了新东西
（429 闸门、软删除列、wiki 日志等），直接 cherry-pick 会把 wiki 段落一并带过来。
实际做法：`llm.py` 与 `tests/test_llm.py` **无任何 wiki 引用**（`grep -i wiki` 零命中）→ 整取；
`config.py` / `distill.py` / `logs.py` / `yaml.example` 手工剔除 wiki 部分。

**验证**：`uv run pytest` → **503 passed / 1 skipped**（`main` 原 488，回移带入 15 项测试）。

**回移时暴露的一个连带**：`clamp_content` 生效后，
`test_merge_hierarchical_truncates_when_not_converging` 里构造的"超长 content"被截到
`CONTENT_MAX` 以内，**每批能装的条数变了、制造不出"不收敛"** → 该测试随之调整
（`long_text` 改小、`max_chars` 从 1000 降到 350）。**这是"兜底生效"的正常连锁，不是回归**。

### 2.B ⬜ 未回移（仍留在 `feat/llm-wiki`）

| 项 | 为什么暂不回移 |
|---|---|
| **记忆操作事实流**（`logs/memory.log`）+ **日志滚动归档** | 与 `wiki.log` 同批设计，回移需要先拆分 `DEFAULT_LOGS` 里的 wiki 部分；且滚动归档与 `logs.files` 配置段耦合，值得单独一轮 |
| **快照与回滚** | 面向"索引库整库 + **wiki 产物目录**"，主体是 wiki 运维 |
| **Agent 直写记忆落蒸馏表**（`save_direct_memory`）+ 历史回填 | 牵涉 MCP server 调用点与 CLI 子命令，是**独立特性**而非"夹带的修复"；但它确实是 `main` 的真缺口（直写记忆在记忆报表里看不见），**建议单独排期** |
| 四路召回调优 / 页面准入 / 严格度档位 / 短语通道 | wiki 分支专有（依赖页面层） |

## 三、wiki 分支专有（预期内，不需处理）

| 文件 | 内容 |
|---|---|
| `agentmemhub/wiki.py` / `wiki_manifest.py` / `wiki_triggers.py` / `wiki_pending.py` / `wiki_index.py` | Wiki 服务层与运维 |
| `agentmemhub/failures.py` | 长任务失败清单 |
| `scripts/wiki_compile.py` / `wiki_aggregate.py` / `wiki_linkfix.py` | 两级编译 + 链接修复 |
| `agentmemhub/web/app.py` | `/api/wiki/*` 端点（pending / drop / restore / failures / retry 等）|
| `docs/API.md` | 「3.6 LLM Wiki」一节 + CLI wiki 子命令（main 版本已剔除）|
| `agentmemhub/cli.py` | `wiki` 子命令 |
| `agentmemhub/config.py` | `DEFAULT_WIKI` / `DEFAULT_SNAPSHOT` / `DEFAULT_LOGS` 段，`Config.wiki` / `wiki_l2` / `snapshot` / `logs` 属性 |
| `agentmemhub/logs.py` | `wiki_log_file` / `audit_wiki` / `read_wiki_audit` / `wiki.log` |
| `agentmemhub/distill.py` | `wiki_ignore_at` 列迁移、`drop_projection`、`wiki_triggers` 写入钩子 |
| `README` / `AGENTS` / `ARCHITECTURE` | wiki 章节、目录树条目、v2.1~v2.4 更新记录 |

## 四、一个操作提醒

两个分支**已分叉**，且接口相关的 5 个改动**存在两份**（SHA 不同）：

```
2fd1802 (main)      ← 166e8f3 … c939944   （拆分后的版本）
253257f             ← 分叉点
61183d5 (llm-wiki)  ← 37ad246 … 61183d5   （原来的版本，仍在）
```

将来合并 `feat/llm-wiki` 回 main 时会在
`docs/API.md`、`agentmemhub/web/app.py`、`AGENTS.md`、`README.md`
上冲突——**内容已一致，只是 SHA 不同**，解决时以任一侧为准即可。

**另注**：`feat/llm-robustness` 从 `main` 分出后只含 2.A 的改动，
与 `feat/llm-wiki` 在这些文件上**会冲突**（两边都改了 `llm.py` / `config.py` /
`distill.py` / `logs.py`）。因为 `feat/llm-wiki` 上已有这些修复，
**合并顺序建议：`feat/llm-robustness` → `main` 先合**，之后 `feat/llm-wiki` 侧的冲突
以特性分支为准（它是超集）。
