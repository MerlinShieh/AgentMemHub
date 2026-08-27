# AgentMemHub Web 端到端测试（浏览器级）

用 Playwright 驱动本机浏览器（Edge / Chrome）对仪表盘做用户行为级验证：
点击行开详情抽屉、编辑标题（真实写库）、删除会话（真实写库）。

## 前置

- Node.js（含 npm）
- 本机已安装 **Microsoft Edge**（Windows 自带）或 Chrome

## 运行

```bash
# 1. 安装 playwright（首次）
cd scripts/e2e && npm install

# 2. 构建临时测试库（3 条样例会话，勿连真实库）
#    在项目根目录执行：
uv run python scripts/e2e/build_db.py /tmp/e2e.db

# 3. 启动测试服务（临时库 + 独立端口）
uv run python -m agentmemhub serve --port 8087 --db /tmp/e2e.db --no-open

# 4. 跑 E2E（脚本默认连 127.0.0.1:8087，默认用系统 Edge）
cd scripts/e2e && node e2e.js
```

全部通过输出 `9 通过, 0 失败`；失败时打印具体断言与页面控制台错误。

## 说明

- 默认使用系统 Edge（`channel: 'msedge'`）；如需 Chrome 改 `e2e.js` 中 `channel: 'chrome'`
- 测试会真实修改临时库（改标题 + 删一条会话），请勿指向生产数据库
- 服务端 API 层行为另有 `tests/test_web_server.py`（pytest）覆盖