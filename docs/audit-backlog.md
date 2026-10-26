# 审计待办清单（2026-10-26）

**这份清单是什么**：2026-10-26 对 `feat/kb-ui` 分支做了两轮分域审计（面板 / 记忆引擎 / wiki / feed+配置+适配器，后补 `scripts` / 命令行层 / 数据层），
按「真实可利用」「会不会丢数据」「静默失效」三个维度筛过。这里只留**还没修的**与**明确决定不修的**，
已修条目按 commit 归档在文末，避免以后重复排查。

行号以 2026-10-26 的工作树为准，会随改动漂移，**改之前先按关键词搜一遍**。

---

## 一、建议尽快（真实可达，或会静默丢数据）

### 1. 控制台 `[7] 停止看板` 会强杀占用端口的任意进程
- `agentmemhub/console.py:243-244`：`if os.name != "nt": return []` —— 非 Windows 永远拿不到进程清单，
  Windows 上 powershell 被策略禁或超时（`:246-250` 返回 `[]`）也一样。
- 于是 `:283-300` 退到「按端口取 PID」然后 `taskkill /PID x /F`、`os.kill(pid, SIGTERM)`。
- 确认框只写「将停止看板进程 PID x（端口 y）」，**没有归属校验**。端口被别家服务占着就会误杀。
- 修法：兜底路径再查一次进程名/命令行；对不上就拒绝并提示"端口被非看板进程占用"。

### 2. `merge_distill_into_db.py --dry-run` 会落 schema
- `scripts/merge_distill_into_db.py:79-81` 的 `ensure_distill_schema / ensure_memstore_schema / ensure_vec_table`
  在 `:132` 的 `if args.dry_run` **之前**执行；sqlite3 默认 `isolation_level=""` 下 DDL 不在隐式事务里，立即提交。
- 与 `:12`「全部写入在单个事务内」、`:133`「（--dry-run：未写入）」直接矛盾。
- 修法：把 schema 准备挪进 `if not args.dry_run`，或整个包进一个显式事务后再回滚。

### 3. `sensitive_scan.py` 漏报（这是推送前的最后一道闸，漏了等于没有）
- `scripts/sensitive_scan.py:47` 只认 `github_pat_`，**不认 `ghp_`（经典 PAT）、`glpat-`（GitLab）、`AIza`（Google API key）**。
- `:53` 路径正则只覆盖 `[A-Z]:\Users` / `D:\data` / `C:\Users` —— 本仓库所在的 `D:\vibeCoding` 自己就漏。
- `:35` 用户名长度 `>= 3` 且能 `fullmatch` 才生成规则：**中文用户名、带空格用户名一条规则都不生成，且不提示**。
- `:42` 只扫 `git ls-files`，未跟踪的新文件不扫。
- 修法：补齐 key 前缀；路径规则改成通用 `[A-Z]:\` + 用户目录两者；用户名走不进正则时就打一条警告而不是静默跳过。

### 4. 水位文件多进程写会落半截，且坏文件被静默重置
- `agentmemhub/watermarks.py:79-85`：固定 tmp 名 `name + ".tmp"`、**无文件锁**，面板与 CLI/MCP 同时 `save_state` 就撞。
- `:55-58` `except Exception: return st` —— 读到坏 JSON 直接回默认，`delta` / `consumed` / `pending_score` 一起丢。
- 修法：tmp 名带 pid + 时间戳；`load_state` 失败时先把坏文件改名留证并记日志。

### 5. 日志滚动的锁是**进程内**的，多进程下长期不生效
- `agentmemhub/logs.py:344` `with _FILE_LOCK:` 是 `threading.Lock`；而 `wiki.log` 同时被面板与 MCP 写。
- `:222 path.rename(dest)` 在 Windows 上若另一进程持有该文件会抛 `WinError 32`，被 `:224-225` 吞掉 ⇒ 5MB 滚动失效；
  `compress: true` 时 `:217-220` 先复制再 `unlink()`，这之间追加的行直接丢。
- 修法：用文件锁（或至少保留日期后缀不 rename）；回头看一眼真的滚过没有。

### 6. `trae` 三类数据共用 id 空间，同名即整会话覆盖
- `agentmemhub/adapters/trae.py`：snapshot 用 `sid_dir.name`、SOLO 用 `task.name`、项目记忆用 `proj.name`，
  但 `source` 恒为 `"trae"`，库主键是 `(source, id)` ⇒ 同名直接覆盖。
- 另外 `_load_snapshots:117-118` 的 `max(...)` 没有兜底；`_load_memory` 的 `sorted(...)[:20]` 硬编码 20，第 21 个静默丢。
- 修法：id 加类别前缀（`snap:` / `solo:` / `mem:`）。

### 7. `qwen` 清单 id 与入库 id 可能不相等 ⇒ 每轮都判"新增"
- `agentmemhub/adapters/qwen.py:59` 清单 id = `_peek_sid(fp) or fp.stem`，而 `_peek_sid:73-75` 只读首个非空行；
  `load:120` 又用 `o.get("sessionId") or fp.stem` ⇒ 首行没有 sessionId 时两边算出来的 id 不同，
  `cli.py:122-128` 于是每轮都认为它变了。
- 同文件 `:186`：`type == "user"` 且缺 `parts` 时把**文件名**当用户内容入库。
- 修法：两侧统一用 `_peek_sid(fp) or fp.stem`；缺 parts 时跳过而不是编内容。

### 8. `snapshot restore` 在面板/引擎运行时直接覆盖索引库
- `agentmemhub/cli.py:1070-1072` → `agentmemhub/snapshot.py:200-203`：删掉 live 的 `-wal` / `-shm` 再 `shutil.copy2`，
  **不检查有没有进程正持有该库**。另一进程仍拿旧连接会继续 checkpoint，索引库直接分叉。
- 面板侧的回滚任务已经是长任务，但 CLI 侧的这条路径没有任何互斥。
- 修法：回滚前检查 `logs/*.lock` 与 `-wal` 是否被占用；CLI 侧也走 `wiki_triggers` 那套文件锁。

---

## 二、建议修，但不急（影响面小或触发条件苛刻）

| 位置 | 问题 |
|---|---|
| `agentmemhub/export.py:69-71` | `_safe` 不管 Windows 保留名（CON/PRN/COM1）与尾部点/空格；两个长 id 截断后同名即后写覆盖；`:33` 非原子写 |
| `agentmemhub/config_doc.py:40,48` | `key:   # 仅注释` 会被解析成 `value_raw = "# 仅注释"`（`_split_inline_comment` 只认 `" #"`）。当前真实 yaml 没有这个形态 |
| `agentmemhub/models.py:229-245` | `_to_epoch` 把 `>1e13` 一律当纳秒，微秒时间戳（≈1.7e15）会算成 1970 ⇒ `store.py:317-319` 的"时间没变"判定永不成立，该会话每次全量重写 |
| `agentmemhub/obsidian.py:236-240` | `verify()` 默认 `settle_s=None → RELOAD_SETTLE_S=20`，由 `wiki.py:453-454` 每轮收尾调用 ⇒ 每轮固定睡 20 秒 |
| `agentmemhub/logs.py:323-325` | `prune_task_logs` 对 `logs/tasks/` 下**所有** `is_file()` 按 mtime 删，不筛 `*.log` |
| `agentmemhub/notify.py:334` | `out[name] = dict(res)` 在 `try` 之外，任何通道返回非 mapping 就抛穿 `push()`，而调用方都包了 try ⇒ 无审计的旁路失败 |
| `agentmemhub/notify.py:378-381` | title/body 截断没有省略标记 |
| `agentmemhub/console.py:184-191` | 启动看板后**立刻**宣称成功（stderr 全 DEVNULL、无就绪探测）；反复按 [6] 会堆失败进程 |
| `agentmemhub/console.py:135-136` | `_choose_source` 拼错即回落"全部源"（`cla` → 全源） |
| `agentmemhub/cli.py:1114` | `serve --port` 默认硬编码 8086、无视 `config.web_port`，而控制台又按配置端口拼 URL ⇒ 报错地址 |
| `agentmemhub/cli.py:78-159` | `run_ingest` 无 `try/finally`：任一 adapter 抛错则 `Store` 不关闭、本轮水位不落 |
| `agentmemhub/cli.py:1070-1073` | `snapshot restore --wiki-only --db-only` 同时给 ⇒ 两个分支都不进，却照样打印"回滚完成"，只多出一份保护快照 |
| `scripts/wiki_aggregate.py:267-269` | `wiki_linkfix` 原地覆盖写（无临时文件/备份），进程被杀 = 页面被截断；服务路径 `wiki.py:699/1059` 同样走它 |
| `scripts/sync_llm_from_zcode.py:235-243` | `TARGET_YAML.write_text` 非原子；`splitlines()` + `"\n".join` ⇒ **CRLF 转 LF**；`:126` 只认独占一行的 `llm:`，写成行内 flow 会追加第二个 `llm:` 键 |
| `scripts/fetch_model.py:55` | 注释承诺 Range 续传 + Content-Length 终校，实际只检查 `returncode` 与 `size == 0` ⇒ 截断的 `.onnx` 会写进 `models.json` |
| `scripts/audit_frontend_api.py:72-76` | `used(path)` 只比路径段、不比 method ⇒ 有 `GET /api/feed/{id}` 就算 `DELETE/POST/PUT` 全部"已接入"；`_PATH_RE` 还会命中注释里的路径 |
| `scripts/check_config_keys.py:124,142` | 只判断叶子名是否作为带引号字面量出现过 ⇒ `llm` / `enabled` / `workers` 这类短名只要在无关字符串里出现就判"活"——这正是假开关能潜伏的机制 |
| `scripts/web_verify.py:33` | `st == 200 and "agentmemhub" in html or b"Agent" in html` 解析成 `(A and B) or C` ⇒ 非 200 也可能通过 |
| `agentmemhub/mdmini.py` | 表格分隔行、强调跨标签等边界按 CommonMark 还差一点（不影响安全，`quote=True` 之后注入面已封） |
| `scripts/health_check.py:80-82` | `--json` 模式**永远返回 0**，与 `:11` docstring「0 = 全部正常，1 = 存在问题」相反（文本模式是对的） |
| `agentmemhub/notify.py` | `list_notifications` / `mark_read` / `prune` 除测试外**没有任何调用者**，而 `config.py:212` 注释写「落库始终启用（面板可查、带未读状态）」⇒ `notify.db` 只增不减、`notify_fallback.jsonl` 不滚动 |
| `scripts/export_memories.py:69,89` | `--out` 指向已有目录时 `jsonl.open("w")` 静默覆盖，无「目录非空」检查 |
| `scripts/mcp_log.py:117` | `records[-args.limit:]` ⇒ `--limit 0` 变成全量、`--limit -5` 从第 6 条起 |
| `scripts/distill_sandbox.py:106` | `--force` 的 `shutil.rmtree` 只判 `exists()`；junction / symlink 下无显式校验（Windows 上实测会抛而挡住，属侥幸） |
| `scripts/wiki_aggregate.py:880` | `"title: %s" % page["title"]` 裸写（`aliases` 那行走的是 `_yaml_flow_scalar`）⇒ 标题含 ASCII `: ` 时 frontmatter 解析失败 |
| `agentmemhub/adapters/qodercn.py:72,86,90` | 所有事件时间 = 文件 mtime、跨文件顺序 = 写盘顺序；`src_id = f"line:{sid}#{ln}"` 跨文件同号撞锚。**本机无该源，未实证** |
| `agentmemhub/cli.py:86-90` | 未知 `--source` 只打印一行「未知 source」然后照样走到「完成: 0 会话」；退出码修好之后至少能看出失败 |
| `agentmemhub/cli.py:172-181,218-225` | `cmd_list` 空结果、`cmd_export` 提前返回时跳过 `store.close()`（连接/锁不释放） |
| `agentmemhub/console.py:164-165,180` | 角色是自由文本、不校验（拼错即静默 0 命中）；非 Windows 下自己的看板会被报成「端口已被其他程序占用」 |

---

## 三、明确决定不修（附理由，避免以后重复讨论）

1. **不加鉴权 / token**（用户 2026-10-26 明确：本地个人项目）。只绑回环是默认，`--bind 0.0.0.0` 时裸奔是已知取舍；
   CSRF 闸（`Origin` / `Sec-Fetch-Site`）仍然保留，因为它挡的是**浏览器里的其他网页**，与鉴权无关。
2. **MCP 只给 `feed_add/list/status` 与 `memory_save/...`、不给删除**：这是给 Agent 的无人值守接口的刻意约束；
   删除能力只留在面板（人工勾选 + 二次确认）。
3. **MCP 分桶存量 211 条 `conversation_id='direct'` 不迁移**：分桶对新写入已生效，存量迁移要动 `units` / `vectors` / manifest，
   风险大于收益；下次全量重编时会自然摊平。
4. **`scripts/` 不进包内覆盖率统计**：它们是独立入口，`tests/test_wiki_scripts.py` 等已直接 `sys.path` 导入测了 60+ 个函数。

---

## 四、覆盖缺口（比单条 bug 更值得补）

- `agentmemhub/export.py` **0%**（51 行）、`agentmemhub/rag/cli.py` **0%**（122 行）、`agentmemhub/cli.py` **20%**（1052 行）。
- `scripts/` 下各脚本的 **`main()` / argparse 分支完全没有测试** —— 本轮修的「退出码恒 0」就长在这个缺口里。
- `sensitive_scan.py` / `check_config_keys.py` / `audit_frontend_api.py` 三个脚本自身没有测试，
  而它们正是"用来保证质量"的工具（`sensitive_scan` 的漏报就是本轮发现的高危）。
- `is_system_inject` 直到 2026-10-26 才有第一条测试（`tests/test_models.py`）。

---

## 已修（按时间倒序，便于以后不再重复排查）

**2026-10-26 第三轮（本轮）**
- `fix(cli)` 子命令退出码恒 0 → `main() -> int` 透传返回值、`__main__.py` 走 `SystemExit(main())`；
  `scripts/wiki_compile.py` / `wiki_aggregate.py` 的失败与中止也反映到退出码。守卫：`tests/test_cli_exit_code.py`。
- `fix(models)` 系统注入正则的四个裸词改行首锚定 —— 正文里提到 `runtime.context` 之类不再整条判成注入。
  实测 2613 条 user 事件：改动前后判系统的都是 240 条，**没有误伤既有数据**。守卫：`tests/test_models.py`。
- `fix(adapters)` 源库缺列不再让整个 sync 中断：`SqliteConversationAdapter` / `WorkBuddyAdapter` 按列名取值失败即回落默认；
  `load_all()` 把单源失败写进 `logs/cli.log` 而不是静默吞成"该源没有会话"。守卫：`tests/test_adapter_schema.py`。
- `fix(scripts)` `wiki_aggregate` 的 `--domain` / `--limit` / `--retry-failed` 隐含 `partial`，
  不再 rmtree 掉本轮没处理的域目录。

**2026-10-26 第一、二轮**（四域审计）
- `memory_save` 的 `importance` 档位从未落库（UPDATE 后缺 `commit()`，`finally: idx.close()` 回滚）。
- `mdmini` 的链接属性位转义不全（`quote=False` → `quote=True`）。
- 运维「立即检测」按钮永久 405（前端 GET 调 POST 路由）。
- 设置页明文展示 `api_key`（`cfgMask` 只在配置体检里用了）。
- 快照 id / 产物路径越界（`_safe_sid`、`_wiki_out_guard`）。
- 投喂三道闸：凭据文件、内网地址（含 `100.100.100.200` / `metadata.*` / 数字形主机名）、越界文件名。
- 写端点 CSRF 闸（`_same_site_guard`，非只读方法校验 `Origin` / `Sec-Fetch-Site`）。
- `forget --purge` 不删镜像；空格式白名单语义。
- `tasks` 无超时无取消 ⇒ 改为协作式取消 + 超时旗标 + 僵尸检测 + 面板「请求取消」（`8bfa293`）。
