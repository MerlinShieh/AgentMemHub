# AgentMemHub 项目约定

## 记忆保存纪律（硬性规则）

任务收尾时自查：**本次是否产出了可复用结论（问题解决步骤 / 踩坑解法 / 架构决策 / 关键配置变更）？**
命中任意一条，必须走 `save-memory` Skill 流程完成持久化，不允许只写 Agent 自己的本地会话记忆：

1. `memory_stats` 探活（引擎离线则告知用户，不硬写）；
2. `memory_save` 写入自包含结论（背景一句话 + 结论/做法）；
3. 对刚写入的 id 立即 `memory_score`（多数应给 positive）；
4. 一次任务存多条时可改用 `python -m agentmemhub score` 批量补评未评条目。

需要历史经验时（新任务开始 / 疑似曾解决过）主动调 `memory_search`，与保存流程无关、各自独立触发。

## 其它既有约束（速查）

- **MemOS 是纯上游引擎**：`memOS/` 内源码不得改动，所有修复只落在 AgentMemHub 自身代码；
- `.bat` 脚本：纯 ASCII + CRLF + `if (...)` 块内不得含 `)`；
- 推送远端前必须脱敏（配置走 example 占位）；
- 用户验证功能期间不要主动 git commit/push，等明确指令。
