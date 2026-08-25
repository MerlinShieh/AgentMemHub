# AI Conversation Hub 项目状态

## 目标

做一个本地优先、离线可用、对原始会话数据只读的 AI 对话开关板，让用户能在不同 coding agent 之间完成“搜索 → 选中 → 诚实续接”。

## 当前阶段

`v0.4.1` 宣发准备阶段。`main` 已补齐 WorkBuddy、千问办公、Qoder/QoderCN 与 QoderWork 的一句话安装路由；本次只更新主线源码和双语说明，未创建新 Release。

## 范围

- 聚合并搜索本机多个 AI agent 的用户/助手会话。
- 提供精确会话、命令、工作区、客户端和无续接能力的诚实分级。
- 提供确定性的回顾卡和跨 Agent 历史接续包。
- 通过仓库合同支持跨 Agent 项目接力，但不把 Hub 变成任务调度器。
- 保持 Windows 优先，并保留 macOS/Linux 的可验证兼容路径。

## 非目标

- 不写回厂商会话数据库。
- 不做云端账号、团队协作平台或新聊天客户端。
- 不默认调用 LLM，不自动从历史聊天生成授权或执行任务。
- 不自动编排多 Agent、自动发布内容或自动创建 Release。
- 不把可选记忆插件变成核心依赖。

## 架构边界

- Hub：本地检索、阅读、回顾和历史接续包。
- `find-agent-data`：Hub 未覆盖时的只读发现与证据恢复。
- 仓库合同：`PROJECT.md`、`DECISIONS.md`、`TASKS.md`、`handoffs/`，定义当前授权和交付责任。
- Git：回滚、审查和交付记录；推送必须有明确 owner 和 target。

## Definition of Done

- 原始会话源保持只读，网络监听保持 `127.0.0.1`。
- 所有续接入口如实标注能力级别。
- 单元测试、CI smoke、语法/格式检查和项目合同校验通过。
- 版本、README、路线图与真实代码能力一致。
- 活跃任务的 handoff 记录范围、验收、Git 策略、证据和结束状态。
- 对外分支只由指定 integration owner 推送；合并 `main` 和 Release 继续单独确认。
