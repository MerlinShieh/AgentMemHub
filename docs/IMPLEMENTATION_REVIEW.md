# AgentMemHub 需求实现检查报告

> **日期**：2026-08-26
> **对应基线**：GitHub 推送后（commit `f0fa545`，脱敏加固完成）
> **范围**：对照最初总体需求逐项核查实现状态

---

## 一、需求对照表

### ✅ 已实现

| 最初需求 | 状态 | 落地情况 |
|---|---|---|
| 统一提取所有 Agent 对话/记忆 → 统一结构 | ✅ | 7 个 adapter（超出要求的 5 个，另含 QoderCN、DSH）；实测 231 会话 / 6.1 万事件入库 |
| 保留输入输出 + 工具链 + 思维链 + Shell 执行 | ✅ | 统一事件流：user / assistant / tool / reasoning / patch / shell；每事件 `raw_json` 无损保底 |
| 最大兼容性（允许空 key/value） | ✅ | Event 全 Optional 字段 + 原始 JSON 保底；自动适配 schema 差异（如 message 表有无 sequence 列） |
| 本地数据库存储（推荐 DB） | ✅ | SQLite：conversations / events / events_fts（FTS5 全文索引） |
| 借鉴 AI Conversation Hub、统一接口不同实现 | ✅ | `AgentAdapter` 基类（candidate_paths / locate / load）；已 fork 二次开发 |
| 输出统一标准格式，可搜索索引、导出 | ✅ | FTS5 检索（中文 LIKE 兜底）+ JSONL / Markdown 导出 |
| 做成独立本地程序 | ✅ | `agentmemhub.py` CLI（ingest / list / show / search / export / memos / stats / adapters） |
| 支持 ZCode / Hermes / WorkBuddy / Qwen / OpenCode | ✅ | 全部支持（+ QoderCN、DSH） |
| 后续用 MemOS-Local-Plugin 清洗为记忆 | ⚠️ 半实现 | 已生成 MemOS bundle（TraceDTO，OpenCode 实测 317 traces）；**MemOS 实际部署与导入尚未跑通验证** |

### ⚠️ 略有变动的功能

| 计划/需求 | 实际落地 | 差异说明 |
|---|---|---|
| 数据源支持手配路径（原项目 sources.json 机制） | 数据源由代码内 adapter 自动发现，不走 sources.json | 更简单，失去 GUI 手配能力（对自动发现足够） |
| MemOS 导入粒度 | 每 user 消息 = 1 trace（粗粒度） | 需求要求"压缩/折叠后加载"；当前是原文级单条折叠，未做 L2/L3 摘要级压缩 |

---

## 二、可能存在的问题 ⚠️

1. **WorkBuddy 无完整对话**：只提取到会话元数据 + Shell 命令审计（audit-log），没有 user/assistant 对话文本（消息散落在客户端本地存储，需逆向）。**最明显的数据缺口**。
2. **DSH 未清洗注入元数据**：user 消息混入 "Current runtime context..." 等系统注入文本，会污染记忆质量。
3. **Qwen 会话合并去重问题**：同一 sessionId 跨文件合并时出现 user 消息重复（同内容两条）。
4. **ingest 全量重建、无增量**：`--signature` 默认空串，每次清空重写全部事件；store 有 signature 指纹字段但未接入增量判断。
5. **本项目核心零测试**：`tests/` 目录全部来自原项目；event_model / store / adapters / export / memos_bridge 无自己的单元测试。
6. **中文搜索性能**：中文查询走 LIKE `%..%` 全表扫描（不经过 FTS5），数据量增大后耗时上升。
7. **Hermes 事件膨胀**：一条 assistant 消息被拆成 content / tool_calls / reasoning 多条事件（18,822 事件 / 9,823 消息），短期可接受。
8. **CLI 无结构化输出 / 无 MCP**：输出是给人读的文本，无 `--json`；fork 的 hub_agent.py 连的是旧项目的 server，AgentMemHub 未对其它 Agent 暴露调用接口。

---

## 三、未实现 / 待实现 ❌

| 需求点 | 说明 |
|---|---|
| **MemOS 实际导入跑通** | bundle 已生成、`--push` 接口已写，但 memos-local-plugin 未部署端到端验证 |
| **压缩或折叠后的会话加载** | 需求明确提出；当前仅原文级折叠 |
| **逆向为 Agent 原生会话** | 将统一数据重建为某 Agent 可继续的会话（如 resume）；需求允许"先做记忆生成"，当前走了记忆生成路线 |
| **Agent 插件形式** | 需求允许二选一，已选独立程序 ✅；MCP / 插件接入未做（可选） |
| **Web UI 面板** | fork 保留了 server.py / static 但未接入本项目 store |
| **WorkBuddy 完整对话** | 需逆向客户端 blob / local_storage |

---

## 四、改进建议（按优先级）

| 优先级 | 改进项 | 价值 |
|---|---|---|
| **P0** | 实际部署 MemOS Local Plugin 并导入验证 | 让"任何 Agent 通过记忆插件了解全部历史"闭环 |
| **P0** | WorkBuddy 完整对话提取 | 补最大数据缺口 |
| **P1** | 记忆清洗规则（DSH 注入元数据、Qwen 去重、Hermes 事件合并） | 直接提升记忆质量 |
| **P1** | 本项目单元测试（event_model / store / adapters） | 保障后续迭代 |
| **P1** | `--json` 结构化输出 + MCP server（复用原项目 stdio MCP 模式，连本项目 store） | 满足"Agent 插件形式"，让其它 Agent 可检索统一记忆 |
| **P2** | ingest 增量重建（接入 signature 指纹） | 大库反复全量重扫耗时 |
| **P2** | Web UI（复用 static/server.py） | 可视化浏览 |
| **P2** | 折叠/压缩会话（L2/L3 摘要） | 贴近需求原文 |
| **P3** | 逆向导出为 Agent 原生会话格式 | 高级能力，可选 |

---

## 五、结论

核心链路（提取 → 统一事件流 → SQLite 检索 → JSONL/Markdown 导出 → MemOS bundle）**已全部打通并在真实数据上验证**，Agent 覆盖超出需求（7 种 ≥ 5 种）。

主要差距集中在三类：
1. **闭环**：MemOS 实际导入验证、WorkBuddy 完整对话
2. **记忆质量**：注入元数据清洗、去重、事件合并
3. **工程保障**：单元测试、增量重建、结构化输出 / MCP

下一步建议从 **P0 两项**（MemOS 部署导入 + WorkBuddy 完整对话）开始推进。