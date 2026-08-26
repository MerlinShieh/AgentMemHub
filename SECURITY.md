# 安全与脱敏策略

AgentMemHub 是一个**本地运行**的工具，会被推送到公开仓库的部分（代码、文档、示例配置）**严格脱敏**。

## 数据流边界

- 所有 Agent 对话历史**只在本机读取**，写入本地 SQLite（默认 `~/.agentmemhub/agentmemhub.db`）。
- 工具不会将任何会话数据上传云端；也无遥测。
- 只有你显式执行的 `export` / `memos --push` 才会把**脱敏后的结构化数据**写到本地文件或你本机运行的 MemOS 服务。

## 公开仓库脱敏规则

推送到公开 GitHub 仓库时，以下内容**一律不提交**：

| 内容 | 处理方式 |
|---|---|
| 真实用户名 / 主机名 | 不写入；代码用 `Path.home()` / 环境变量动态获取 |
| 真实私有路径（如 `<USER_HOME>\a\b`、`<DATA_DIR>\c\d`） | 不写入；用占位符 `~` / `<USER_HOME>` |
| API Key / token（含示例运行中抓到的） | 不写入；配置文件用 `<YOUR_API_KEY>` 占位 |
| 用户真实对话 / 会话内容 | 不写入；导出到 `exports/`（已 gitignore） |
| 真实设备配置 | 用 `*.example` 或 README 说明替代 |

## 配置文件与示例

- 运行所需的敏感配置一律用**示例文件**给出，真实值从本地环境变量或 `Path.home()` 探索得到，不落盘到仓库：
  - `.env.example` —— 环境变量占位说明
  - `.env.example` —— 数据源配置示例（路径留空）
- 例如 `~/.zcode/cli/db/db.sqlite` 这类路径由 adapter 的 `candidate_paths()` 在**运行时**通过 `Path.home()` 计算，代码里不硬编码真实用户名或绝对路径。

## 安全注意事项（使用者）

- `exports/` 目录已加入 `.gitignore`，请勿强制把含真实对话的导出文件 push 到公开仓库。
- 若你 fork 后要公开，先在本地跑一次敏感扫描（`python scripts/sensitive_scan.py`）确认干净。
- 数据库文件（`agentmemhub.db`）含真实会话明文，请妥善保管，不要提交。

## 反馈

发现泄露风险或安全问题，请通过 GitHub Issues 反馈。
