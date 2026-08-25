# 当前任务

状态只使用：`planned`、`active`、`review`、`blocked`、`done`、`cancelled`。

| ID | 任务 | 状态 | Owner | 依赖 | Handoff |
|---|---|---|---|---|---|
| T-001 | 整理当前 v0.4.0 工作区，建立项目接力合同并推送开发分支 | done | primary Codex integrator | 无 | [handoffs/T-001.md](handoffs/T-001.md) |
| T-002 | 审查开发分支、强化项目合同生命周期并发布 v0.4.0 | done | primary Codex integrator | T-001 | [handoffs/T-002.md](handoffs/T-002.md) |
| T-003 | 修复打包漏项，交付双安装方案并发布 v0.4.1 | done | primary Codex integrator | T-002 | [handoffs/T-003.md](handoffs/T-003.md) |
| T-004 | 适配国内 Agent 一句话安装并更新双语 README | done | primary Codex integrator | T-003 | [handoffs/T-004.md](handoffs/T-004.md) |

## 执行纪律

- 同一时刻最多一个 `active` 写任务。
- `planned` 不等于已授权执行。
- 合并 `main`、Release 和对外宣发必须在独立 handoff 中明确授权。
