# Hub plugins（未接入）

这套代码留在仓库里方便以后接回，**当前产品没有插件中心、也不连腾讯记忆网关**。

接回时：

1. 在 `server.py` 恢复 `/api/plugins*` 和 `/agent/memory`
2. 在 `hub_agent.py` 恢复 `memory` / `hub_memory`
3. 在设置页加回插件入口

约束仍是：默认关闭、只连 localhost、不写各家原始对话库、不替用户启动第三方服务。
