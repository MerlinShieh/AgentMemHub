# 分支差异记录：`main` ↔ `feat/llm-wiki`

> 记录时间：2026-09-18　｜　分叉点：`253257f`（Merge feat/memory-distillation）
>
> **为什么记这个**：`feat/llm-wiki` 是一次**大版本升级**（在 RAG 之上增加 LLM Wiki
> 编译链路），而 `main` 仍是上一版的 RAG 记忆引擎。两条线的定位不同，**暂不合并**。
> 但分叉期间容易出现"**本该属于主干的通用修复被夹带在功能分支里**"——
> 这份记录就是把这些差异挑出来，避免它们被埋没或将来冲突时才发现。

## 一、差异总览

```
24 files changed, 4143 insertions(+), 33 deletions(-)
├── 11 个新增文件（wiki 分支独有）
└── 13 个修改文件（需逐个人工分类）
```

**新增文件（全部属于 wiki 功能，无争议）**

| 文件 | 说明 |
|---|---|
| `agentmemhub/wiki.py` | Wiki 服务层（失败清单查询 + 定向补跑） |
| `agentmemhub/failures.py` | 长任务失败清单 |
| `scripts/wiki_compile.py` / `wiki_aggregate.py` / `wiki_linkfix.py` | 两级编译 + 链接修复 |
| `scripts/sync_llm_from_dsh.py` | 从 DSH 配置同步 LLM 接入参数 |
| `docs/llm-wiki.md` | Wiki 设计与实测记录 |
| `tests/test_failures.py` / `test_llm_repair.py` / `test_wiki_scripts.py` / `test_wiki_service.py` | 共 60 项测试 |

## 二、⚠️ 重点：**与 wiki 无关的通用差异**

以下改动**不属于 wiki 功能**，而是通用的**修复 / 能力 / 重构**。
它们目前只在 `feat/llm-wiki` 上，**`main` 缺失** —— 这是本次排查的主要产出。

### 2.1 记忆内容硬截断（`agentmemhub/distill.py`）

**性质**：通用修复　｜　**影响面**：**记忆库质量**

```python
def clamp_content(text, limit=CONTENT_MAX) -> str:
    """超长时在句读处断开；句读太靠前则硬切加省略号。"""
```

`normalize_memories()` 里对 `content` 做代码级兜底。**原先 `CONTENT_MAX=120`
只是提示词里的软约束，实测没有任何模型稳定遵守**（同一提示词、同一批 12 个切片：
MiMo 24% 超标、LongCat 44%、deepseek-v4.1-flash 52%、ling 53%）。

> **对 main 的意义**：RAG 记忆库里目前**存在一批超长记忆**（实测 704/1543 = 45.6%，
> 最长 2581 字），它们"一条讲好几个结论"，直接损害自包含性与召回质量。
> **这个修复与 wiki 毫无关系**，蒸馏链路自己也该有。

### 2.2 LLM 格式修复（`agentmemhub/llm.py`）

**性质**：通用能力　｜　**影响面**：所有 LLM 调用

主模型**返回了内容但 JSON 解析失败**时，把原始输出交给 `repair_model`
（JSON 遵从性更好的模型）转成规范 JSON —— 而不是丢弃或退化成拼接。

> **对 main 的意义**：蒸馏/合并/评分都会遇到"有内容但格式不对"，
> 现在只能整条丢弃（fail-open 不登记 hash，靠重跑碰运气）。

### 2.3 LLM 用量与成本统计（`agentmemhub/llm.py`）

**性质**：通用能力　｜　**影响面**：成本可见性

`usage_snapshot()` / `usage_reset()` / `_record_usage()` / `estimate_cost()`。
模块级累计（因为 `LLMClient` 被设计成每线程一个实例，统计必须跨实例汇总）。

> **对 main 的意义**：现在跑蒸馏**看不到花了多少钱**。

### 2.4 思考模式开关（`agentmemhub/llm.py` + 配置示例）

**性质**：通用能力　｜　**影响面**：**成本**

`thinking: enabled|disabled` 与 `reasoning_effort: low|medium|high|xhigh|max`。
请求体顶层下发 `{"thinking": {"type": ...}}`。

> **关键事实**：推理模型的思维链**计入输出计费**（实测占输出的 63–94%）。
> `reasoning_effort: low` **挡不住**推理量；**只有 `thinking: disabled` 能真正归零**。
> **对 main 的意义**：蒸馏成本可以显著下降。

### 2.5 配置白名单导致的**静默失效**（`agentmemhub/config.py`）

**性质**：**通用 bug 修复**　｜　**影响面**：排查成本

```python
_LLM_SCALAR_KEYS = ("timeout", "max_tokens", "temperature",
                    "thinking", "reasoning_effort", "repair_model")
```

原先是硬编码的 `for k in ("timeout", "max_tokens", "temperature")` 白名单，
**新增 provider 开关会被静默吃掉**（写进 yaml 却读不到，表现为"开关不生效"，
实测排查成本很高）。同时 `reasoning_effort` 的校验原先只认 `low/high/max`，
而 Command Code 实际支持**五档**。

### 2.6 其他通用项

| 文件 | 改动 | 性质 |
|---|---|---|
| `agentmemhub/logs.py` | 把 `read_mcp_audit` 的行解析抽成公共 `_read_jsonl` | 通用重构（消除重复）|
| `tests/test_config.py` | 继承断言从"整字典相等"改为逐字段 | 通用改进（否则每加一个字段就假失败）|
| `agentmemhub.yaml.example` | 补 `thinking` / `reasoning_effort` / `repair_model` 说明 | 通用文档 |
| `agentmemhub/config.py` | 文件开头 BOM 移除 | 清理 |
| `scripts/sensitive_scan.py` | 打印 ✅ 时 GBK 编码崩溃 | **已同步到 main** ✅ |

## 三、wiki 分支专有（预期内，不需处理）

| 文件 | 内容 |
|---|---|
| `agentmemhub/web/app.py` | `/api/wiki/failures`、`/api/wiki/retry` 两个端点（main 上不存在）|
| `docs/API.md` | 「3.6 LLM Wiki」一节 + CLI wiki 子命令（main 版本已剔除）|
| `agentmemhub/cli.py` | `wiki` 子命令（`--action failures\|retry`）|
| `agentmemhub/config.py` | `DEFAULT_WIKI` 段、`Config.wiki` / `wiki_l2` 属性 |
| `agentmemhub/logs.py` | `wiki_log_file` / `audit_wiki` / `read_wiki_audit` |
| `README` / `AGENTS` / `ARCHITECTURE` | wiki 章节、目录树条目、v2.1 更新记录 |
| 11 个新增文件 | 见第一节 |

## 四、接口差异对照

| 层 | `main` | `feat/llm-wiki` |
|---|---|---|
| HTTP 路由 | **28 个** | **30 个**（+2 wiki 端点）|
| MCP 工具 | 5 个（`note` 已齐全）| 5 个（同）|
| CLI 子命令 | 17 个 | 18 个（+`wiki`）|
| 文档 | `docs/API.md`（不含 wiki 章节）| `docs/API.md`（含 wiki 章节）|

> `docs/API.md` 在两边的**主体内容一致** —— 拆分时已确保 main 版本只描述
> main 上真实存在的接口（计数同步改为 28 / 9 个面板未调用接口）。

## 五、建议

**第三节（wiki 专有）不动** —— 随 wiki 分支一起交付。

**第二节（通用差异）建议逐个评估是否回移主干**，优先级：

| 优先级 | 项 | 理由 |
|---|---|---|
| **高** | 2.1 记忆内容硬截断 | 直接影响记忆库质量，且 main 上存量问题已实测存在（45.6% 超标）|
| **高** | 2.5 配置白名单修复 | 是 bug，且会拖慢后续任何 LLM 配置排查 |
| **中** | 2.4 思考模式开关 | 直接省钱（思维链占输出 63–94%）|
| **中** | 2.3 用量统计 | 成本可见性，蒸馏已经能跑到几元钱 |
| **中** | 2.2 格式修复 | 提升 LLM 调用的鲁棒性 |
| **低** | 2.6 各项 | 重构与文档，回移时顺手带上即可 |

**回移方式**：从 `feat/llm-wiki` 上 `git cherry-pick` 相应提交（`19ce71e` 含 2.1/2.2/2.3/2.4/2.5），
或在 main 上新建 `feat/llm-robustness` 分支单独做。**注意**：`19ce71e` 是混合提交
（既含通用修复也含 wiki 内容），cherry-pick 时需要剥离 wiki 部分——与这次拆分
`docs/API.md` 的处理方式相同。

## 六、一个操作提醒

两个分支**已分叉**，且接口相关的 5 个改动**存在两份**（SHA 不同）：

```
2fd1802 (main)      ← 166e8f3 … c939944   （拆分后的版本）
253257f             ← 分叉点
61183d5 (llm-wiki)  ← 37ad246 … 61183d5   （原来的版本，仍在）
```

将来合并 `feat/llm-wiki` 回 main 时会在
`docs/API.md`、`agentmemhub/web/app.py`、`AGENTS.md`、`README.md`
上冲突——**内容已一致，只是 SHA 不同**，解决时以任一侧为准即可。
