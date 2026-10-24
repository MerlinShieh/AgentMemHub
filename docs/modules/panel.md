> 覆盖模块：`agentmemhub/web/**`、`agentmemhub/mdmini.py` · 最后核对：2026-10-23 ·
> 数据模型见 [data-architecture.md](../data-architecture.md)，接口契约见 [API.md](../API.md)

# 面板后端网关（Web）

## 1. 职责边界

面板是**一个人看的界面**，不是给 Agent 的接口。它的价值在于把已有能力摆到人眼前，
所以"有没有入口"和"能不能跑"是两件事——后端 64 个路由，2026-10-18 之前面板只调了 18 个。

**做什么**

| 职责 | 落点 |
|---|---|
| 只读浏览：会话列表/详情/全文搜索/统计聚合 | `agentmemhub/web/app.py`、`aggregates.py` |
| 受控写入：改会话标题、删会话、记忆加权与 👍/👎 | 同上（真实写库，不是乐观更新） |
| wiki 页面浏览：域 → 页列表 → 渲染正文 | `agentmemhub/web/wiki_pages.py` + `agentmemhub/mdmini.py` |
| wiki 运维：健康概览 / 待更新记忆 / 编译失败补跑 / 触发器热改 / 离线批次 / 快照回滚 | `app.py`（多数复用 `scripts/` 与 `wiki_triggers.py`） |
| 外部投喂的素材管理：路径/链接/上传/正文四种投喂、扫描批量、重转/撤下/删除 | `app.py` + `agentmemhub/feed/**` |
| 配置**只读**视图：带原注释、按模块分组、与 `.example` 做键集体检 | `agentmemhub/config_doc.py` |
| 长任务统一调度与进度上报 | `agentmemhub/web/tasks.py` + `_logged_task`（`app.py:446`） |
| 前端（单文件，4337 行）：四标签页 + 三子视图 + 浮层 | `agentmemhub/web/static/index.html` |

**不做什么**

| 不做 | 为什么 |
|---|---|
| **不做鉴权** | 只绑定 `127.0.0.1`，且不上传任何数据。要暴露到局域网得自己加反代与认证——代码里没有这层 |
| **不写 `agentmemhub.yaml`** | `PUT /api/config` **不存在**，也没有任何路径会把配置写回磁盘。这是刻意的：面板那点收益不值得冒"程序冲掉中文注释"的风险（对比见 [config.md](config.md) §8） |
| **不自己实现编译逻辑** | 运维动作都是"调 `wiki.py` / `wiki_batch.py` / `snapshot.py` 的既有入口"，面板只负责包成长任务 + 写日志 + 报进度。业务逻辑留在原模块，面板薄 |
| **不做页面正文缓存** | 每次请求按需读盘。面板看的是**知识库本身**，"能不能被召回"是投影的事（投影会滞后：实测磁盘 424 页 / 投影 399 行） |
| **不恢复记忆排除 UI** | 后端能力（`memory_exclusions` 表 + 三条路由 + CLI/MCP）保留，但面板自 P3 起下线，会话列表只做查看/删除/改标题 |
| **不上传数据 / 不用 CDN** | Tailwind、lucide、Chart.js 全部本地化在 `static/vendor/`，断网可用 |

## 2. 对外入口

### 2.1 启动

```bash
uv pip install -e ".[web]"                     # fastapi + uvicorn 是可选依赖
uv run python -m agentmemhub serve             # 默认 http://127.0.0.1:8086，自动开浏览器
uv run python -m agentmemhub serve --port 9000 --no-open --db D:/path/to/agentmemhub.db
```

交互式接口文档在 `/api/docs`（Swagger UI），每个端点都带 `summary`，关键 Query 参数带
`description`——这部分是给排障用的，别删。

### 2.2 路由分组（64 个）

| 组 | 代表路由 | 说明 |
|---|---|---|
| 会话 | `/api/conversations`、`/api/conversation/{sid}`、`/api/search`、`/api/stats`、`/api/facets`、`/api/title`、`/api/delete` | 列表分页 + 全文搜索 + 聚合统计；标题修改与删除**不会被后续 ingest 覆盖** |
| 记忆 | `/api/memories`、`/api/memories/{mid}`、`/api/memos/traces`、`/api/memos/search`、`/api/memos/weight`、`/api/memos/feedback`、`/api/memos/status` | 报表（蒸馏终稿 ∪ MCP 直写）+ 单条详情 + 时间线（原始输入、`sort=time`）+ 语义检索 + 加权/反馈 |
| 长任务 | `/api/admin/{ingest,push,clean,distill,rebuild,score,exclude,job}` | 全部走 `tasks`，进度条全局可见 |
| wiki 浏览 | `/api/wiki/pages`、`/api/wiki/page` | **磁盘为准**；`origin` 区分蒸馏与投喂 |
| wiki 运维 | `/api/wiki/align`、`/api/wiki/pending`、`/api/wiki/failures`、`/api/wiki/retry`、`/api/wiki/update`、`/api/wiki/triggers`、`/api/wiki/batch/*` | 对齐审计 / 待更新 / 补跑 / 增量 / 触发器热改 / 批次 |
| 快照 | `/api/snapshots`、`/api/snapshots/create`、`/api/snapshots/restore` | 列表与创建是同步（秒级），回滚是长任务 |
| 投喂 | `/api/feed/*`（11 个） | 清单/详情/投喂/上传/扫描/批量/重转/撤下/删除/体检/白名单 |
| 配置 | `/api/config` | 只读；带 `entries[]`（注释 + 行号 + `value_raw`）、`env_overrides[]`、`drift` |

对账工具：`python scripts/audit_frontend_api.py`（`--used` 连已用的列、`--json` 给机器读）。
它同时扫 `app.py` 的路由装饰器和 `index.html` 里的 `fetch('/api/...')` 字面量与**字符串拼接**，
2026-10-23 实测 **64 路由 / 已用 53 / 未用 11**。

### 2.3 长任务契约（最容易踩的地方）

```python
tasks.submit(name: str, fn) -> dict          # fn 必须是 fn(emit, meta) -> str
```

- `fn` 的**签名是两条参数**，返回最终文案（字符串）。传零参 lambda 会在**任务线程里**炸成
  `lambda() takes 0 positional arguments but 2 were given`——HTTP 侧已经返回 200 了，
  错误只出现在任务结果里，所以不看日志就以为"任务在跑"。历史上 16 个 CLI-only 路由
  （`/api/wiki/update`、`/api/wiki/batch/run` 等）全靠前端喂参数，正因为没人调用，
  这个签名错误潜伏了很久。现在有 `tests/test_wiki_long_tasks.py` 守签名（含反向守卫：
  防止有人把好的改坏）。
- **同一时刻只允许一个任务**：`tasks.submit` 在 `_CURRENT["status"] == "running"` 时直接抛错，
  前端拿到 409 类响应。这是全局约束（进度条只有一个），不是每个路由自己的。
- **进度必须自己报**：`tasks.set_progress(job_id, {"done": n, "total": m, "pct": x, "label": "..."})`。
  不调就永远是 0%。`pct` 不给时按 `done/total` 推算。
- **任务日志**：`_logged_task(name, fn)` 包一层 audit 日志；前端轮询 `GET /api/admin/job` 取状态。
  刷新页面后靠初始化时调一次 `adminPoll()` 重新挂上，否则进度条"消失"。

## 3. 数据流与不变量

### 3.1 一次页面请求的路径

```
浏览器 → FastAPI 路由 → （读采集库 / 索引库 / 磁盘文件）→ JSONResponse
                              ↑
                  读操作一律直读；写操作走 store/rag/feed 的既有函数，不自造 SQL
```

### 3.2 三条硬不变量

1. **磁盘为准，投影为辅**。wiki 浏览读 `out_l1_*` / `out_l2_*` / `out_feed/` 的实际文件，
   不查 `index-pages` 投影表。`wiki_pages.page_roots()` 用 `resolve()` + `relative_to()`
   做**目录包含校验**，防止 `../` 穿越读到库外文件。
2. **先转义再套结构**。`mdmini.render()` 第一步就是 HTML 转义，之后才生成标签；
   链接只放行 `http/https`。所以页面正文里写 `<script>` 只会显示成文本。
   这也是"外部投喂的 PDF/网页"能安全展示的前提。
3. **四视图容器必须挂在 `#app` 之内**。历史事故：设置页整片空白——`#viewSettings`
   被放在 `#app` 闭合标签之后，既不报错也不显示。`tests/test_web_layout.py` 现在用
   **祖先链**断言（不是数 `div` 开闭是否平衡——那件事一直是平衡的）。

### 3.3 四标签页的信息分层

| 标签页 | 看的是 | 与相邻页的区别 |
|---|---|---|
| 统一会话 | **采集原始**：事件流、思维链、工具调用 | 唯一能看到"原始对话"的地方 |
| 记忆报表 | **蒸馏终稿**：结构化记忆（∪ MCP 直写） | 时间线看的是**原始输入**，两者口径不同，别混 |
| wiki 知识库 | **编译产物**：L2 蒸馏页 + 投喂页 | 与会话零耦合；浏览/运维/投喂三子视图 |
| 设置 | **配置视图**：yaml 只读 + 键集体检 | 只读，没有保存按钮 |

## 4. 配置项

| 键 | 默认 | 说明 |
|---|---|---|
| `web.port` | `8086` | `AGENTMEMHUB_PORT` 可覆盖；`--port` 优先级最高 |
| `data_dir` / `db_path` | 项目内 `database/` | `--db` 可指定别的索引库 |
| `AGENTMEMHUB_CONFIG` | `./agentmemhub.yaml` | 指向别的配置文件；设置页会标出这类**环境变量覆盖** |
| `wiki.page_workers` | `3` | 影响运维页"增量更新"的并发（不是面板自己的线程数） |
| `snapshot.keep` | `5` | 运维页快照卡显示的保留上限，超出自动删最旧 |

面板**没有自己的配置文件**：它读的就是 `config()`，所以改 yaml 后要重启 `serve` 进程
（`config()` 是模块级单例，只在构造时读一次）。

## 5. 调试与排障

| 现象 | 根因 | 处置 |
|---|---|---|
| 任务一直在跑、输出 0 行 | `wiki.update(on_progress=...)` 这个回调**从来没被调用过**（签名收了但没实现） | 面板改为 **tail `logs/wiki.log`**：`_tail_wiki_log(emit, stop)` 把 `[N/M]`、`call`、`page_done` 等事件转成任务输出 |
| 进度条永远 0% | 没人调 `tasks.set_progress` | 从 wiki.log 的 `[N/M]` 正则解析出 done/total，再喂给 `set_progress` |
| `lambda() takes 0 positional arguments but 2 were given` | `tasks.submit` 要求 `fn(emit, meta)` | 写显式 `def _run(emit, _meta)`；已有测试守这条契约 |
| 刷新页面后进度条没了 | 前端只在内网轮询、刷新就丢 | `init()` 里调 `adminPoll()` 重新挂上 |
| 某标签页整片空白 | 视图容器落在 `#app` 之外 | `test_web_layout.py` 的祖先链断言；改 HTML 时先跑它 |
| 运维页数字是"更新前"的 | 长任务还没结束，页面拿的是旧快照 | `opsSyncJob()` 在任务结束时重载；期间按钮置灰并显示"数据是更新前快照" |
| 面板改了 yaml 不生效 | `config()` 单例、进程内缓存 | 重启 `serve`（MCP 常驻进程同理） |

**日志位置**：`logs/web_stdout.log` / `logs/web_stderr.log`（启动时重定向）、
`logs/wiki.log`（编译事件流，运维页的进度来源）、`logs/mcp.log`。

## 6. 测试在哪

| 文件 | 守住什么 |
|---|---|
| `tests/test_web_layout.py` | 四个视图容器必须在 `#app` 内（**祖先链**，不是 div 计数）；标签按钮与视图 id 一一对应 |
| `tests/test_wiki_pages.py` | 磁盘扫描（不查投影）、`origin` 判定、`page_roots` 目录包含校验（`../` 穿越必须被拒）、分页与搜索 |
| `tests/test_mdmini.py` | **先转义再套结构**：`<script>` 变文本、链接只放行 http/https、`[m3236]` 出 `data-mid`、中英文混排的斜体正则不误伤 |
| `tests/test_wiki_long_tasks.py` | 长任务签名 `fn(emit, meta)`（含反向守卫）、任务真的 emit 出行、`_tail_wiki_log` 能把进度转成 output |
| `tests/test_audit_frontend_api.py` | 对账脚本本身：字面量路由、**`'/api/feed/show/' + encodeURIComponent(id)` 这类拼接**、以及"别把已用的报成未用" |
| `tests/test_batch_api.py` | 离线批处理 HTTP 层：在飞状态、推进、取消、重提 |
| `tests/test_config_doc.py` | 设置页数据源：点分路径、注释归属、`value_raw` 原样、与 `yaml.safe_load` 叶子键一一对上 |

**改前端后的最小自检**（不依赖浏览器）：

```powershell
node --check agentmemhub\web\static\index.html 的 <script> 段   # 或把 JS 抽出来检查语法
python scripts/audit_frontend_api.py                            # 路由↔前端对账
python -m pytest tests/test_web_layout.py -q                    # 容器嵌套
```

浏览器端到端在本机不可靠（headless 的 `--dump-dom` / `--screenshot` 出过空结果），
所以真要看效果就手动开一次 `http://127.0.0.1:8086`。
