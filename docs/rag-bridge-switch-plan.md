# AgentMemHub 桥接切换方案（MemOS → AgentMemRAG）

> 状态：**已实施完成（2026-09-10，R0-R5 全部落地）**——分支 refactor/agentmemrag，
> 提交链 e0e51e3（后端重接线）→ 2d54887（存量迁移）→ R5 文档。
> 实施修正：真实存量为 2253 trac_ + 33 mcp_（非下文旧估的"~1,903 条原子记忆"）；
> 触发面（Skill/MCP 五工具）与面板网关契约零改动，226 项测试全绿，
> backend=memos 一行回退演练通过。

## 切换架构

```
Skill/MCP 五工具（不动）→ AgentMemHub 桥接层
    └─ MemoryBackend 协议（新增薄抽象）
         ├─ MemosBackend（现有 HTTP 路径，保留为回退开关）
         └─ AgentMemRAGBackend（新）→ 进程内 import asrag
```

## 关键事实（AgentMemHub 摸底，2026-09-10）

- 所有 MemOS HTTP 流量已收口单点：`memos_daemon.engine_request`（memos_daemon.py:193-217）
  → 抽象接缝极薄；
- 用到的 MemOS 端点仅 8 个：auth/status、auth/login、overview、import、
  embeddings/rebuild、memory/search、traces?groupByTurn、feedback；
- MCP 五工具链路：`agentmemhub/mcp_server.py`（save→import+写后验证+repair；
  search→memory/search；score→feedback+mark_scored+sync_episode_r_task；
  recent→traces?groupByTurn；stats→daemon_status 组装）；
- 直连 memos.db 的越界点 4 处（scoring.py:241-353：评分枚举读 traces、
  UPDATE episodes.r_task）——切换后统一收进值表接口；
- 数据现状：memos.db traces 2,286（trac_*≈383 条会话派生 + mcp_*≈1,903 条原子记忆）、
  episodes 249（r_task 已填）、feedback 3,836、scored_traces.json 2,289 id；
- 看板前端只调 AgentMemHub 自有网关 `/api/memos/*`，不直连 :18800 → 换内联实现即可零改前端；
- 测试全 mock 不依赖真实引擎，耦合名字只有三个：`engine_request`、`auth_state`、
  `memos.push_bundle/rebuild_embeddings`——URL 级断言需随换后端更新。

## 已定设计（获批待执行）

1. **原子记忆落库**：memory_save → 直接 INSERT units（source=`mcp`，幂等锚=内容 hash）
   + 同步嵌入 + FTS 触发器自动同步；
2. **评分数据归引擎、策略归 Hub**：AgentMemRAG 新增 `unit_values` 值表 + feedback 明细表
   （引擎存值不判值；打分规则留在 AgentMemHub scoring.py）；
3. **存量迁移（必须，否则丢 1,903 条原子记忆）**：mcp_* traces → units；
   feedback 聚合 → unit_values；trac_* 按 src_id 并回既有 units；scored 清单换 src_id 语义；
4. **回退保险**：`agentmemhub.yaml` 增 `backend: agentmemrag | memos` 一行开关，MemOS 保留；
5. **面板/CLI 语义重定义**：status=引擎就绪（无常驻 daemon），start/stop→健康检查/reingest，
   lightweight 开关映射为 Judge/ValueProvider 注入开关。

## 分期（每期独立 commit + 回归门槛）

- S1：AgentMemRAG 写接口（store.py：save/search/list/values）+ unit_values + 单测
- S2：存量迁移脚本（memos.db → units/values，数量双向核对 + 抽样校验）
- S3：AgentMemHub MemoryBackend 抽象 + AgentMemRAGBackend + backend 开关
- S4：MCP/看板/cli 重接线 + 测试保活 + 端到端验证（重启 MCP 后真实五工具冒烟）

## 风险备忘

- 本会话 stdio MCP 进程挂载在旧实现上，S4 切换后需重启 MCP server 生效（短暂不可用属预期）；
- MemOS 引擎当前离线（睡眠后未启动），切换期间它自然停摆，无数据冲突风险；
- mcp_* 记忆的 id 前缀 `mcp_` + sha256 幂等锚语义要在新值表里保持，
  防迁移后重复 save 产生双份。
