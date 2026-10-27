# 审计待办清单（2026-10-26）

**这份清单是什么**：2026-10-26 对 `feat/kb-ui` 分支做了两轮分域审计（面板 / 记忆引擎 / wiki / feed+配置+适配器，后补 `scripts` / 命令行层 / 数据层），
按「真实可利用」「会不会丢数据」「静默失效」三个维度筛过。这里只留**还没修的**与**明确决定不修的**，
已修条目按 commit 归档在文末，避免以后重复排查。

行号以 2026-10-26 的工作树为准，会随改动漂移，**改之前先按关键词搜一遍**。

---

## 一、建议尽快 —— **2026-10-26 已全部修完**

8 条的成因与当时的修法建议保留在下面备查（行号会漂移），**不要再当待办处理**；
实际改法与验证见文末「已修 · 2026-10-26 第四轮」。它们之所以留在这里而不是删掉，
是因为「为什么会写出这样的代码」比「改成了什么」更容易被后来的人重犯。

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

> **2026-10-26 更新**：本表 29 条已修 **24 条**；剩下 5 条**判定不修**，理由写在下面
> （写清楚是为了以后不再重复讨论，而不是"懒得改"）：
>
> - `agentmemhub/obsidian.py:240` 固定睡 20 秒 —— 这一步是**等 Obsidian 重建内存索引**，
>   而它没有对外可观测的"重建完成"信号。改成轮询只是拿不确定性换不确定性，反而更不可靠。
> - `scripts/fetch_model.py` 不校验 Content-Length —— 截断的 `.onnx` 最终会被
>   `OnnxEmbedder._check_dim` 的维数断言挡下（首次加载即报错，不会静默用错模型）。
> - `agentmemhub/mdmini.py` 的表格分隔行/强调跨标签 —— 追求 CommonMark 完整实现超出
>   本模块定位（它只服务 wiki 正文渲染），且注入面已由 `quote=True` 封住。
> - `scripts/distill_sandbox.py:106` 的 `rmtree` —— 目标目录是写死的
>   `PROJECT_ROOT/database_distill_test`，且必须 `--force` 才动手；junction 场景在
>   Windows 上会直接抛错挡住（属侥幸，但改它要引入额外的 realpath 校验，收益不成比例）。
> - `agentmemhub/adapters/qodercn.py` 时间戳与锚 —— 本机没有这个数据源，**无实证**，
>   按"不改没证据的东西"处理。
>
> **已修的条目没有删行**：坑的教训比修本身有用 —— 后来者按位置检索时还能看到
> "这里曾经怎么错"，而不是只剩一条干净的记录。

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

### 2.1 修第一章时新暴露出来的（原清单里没有）

| 位置 | 问题 |
|---|---|
| `scripts/merge_distill_into_db.py:80-82` | 目标库**没有 schema** 时直接崩在 `no such table: main.units`（`ensure_memstore_schema` 的前提是 `units` 已存在）。脚本本意是"并进既有库"，但报错信息完全看不出这个前提，用的人会以为脚本坏了 |
| `agentmemhub/snapshot.py:restore` | 面板侧传 `force=True` 绕过占用检查（面板自己长期持有连接，不绕必然误报）。但**恢复期间面板仍在读旧库**：恢复完那几秒，面板拿到的仍可能是旧连接上的数据。彻底修要在恢复后重建连接池/重开库，属于结构性改动 |
| `agentmemhub/snapshot.py:_assert_db_idle` | 判据（`-shm` / 非空 `-wal`）在**崩溃残留**时同样成立 ⇒ 会误报"被占用"。这是有意的取舍（宁可让人确认，也不静默分叉），但要知道 `--force` 有时是正确选择而不是冒险 |

### 2.2 2026-10-26 深入检查新发现

| 位置 | 问题 |
|---|---|
| `agentmemhub/distill.py`（`_project_one`）、`agentmemhub/rag/memstore.py`（`save_memory`） | **2026-10-27 已修**。这两处直写 `units` 时只写 `settings.active_spec` 的向量 ⇒ 多模型下非 active 模型永远缺这些行（实测 base 表缺 **1912 条** `role='distilled'`）；而 `ingest --model` 的候选来自源库 `events` 而非 `units`，所以它**也补不上**，只有整表 `reembed --model`（实测 22228 条 / 19 分钟）能补齐。现改为经 `rag.ingest` 的 `write_models` / `encode_for_write` / `put_vecs` 写**全部** `write.order` 模型，代价是每次写入多编码一遍非 active 模型。附带更正：`rag_bridge.save_memory_trace` 与 `wiki_index.project_pages` **本来就走 `write.order`**，最初判断的「四处」不成立（那两个之所以看起来缺向量，是历史存量而非写入路径造成的） |
| `docs` 与代码 | `agentmemhub/config.py` 原注释写「落库始终启用（**面板可查**、带未读状态）」，但面板**从未有过通知查询页**，`list_notifications` / `mark_read` / `prune` 除测试外无调用者 ⇒ `notify.db` 只增不减。已改注释为实情，并给 `prune()` 接上唯一的自动调用点 |

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

**2026-10-26 第四轮（第一章 8 条）**
- `fix(console)` `[7] 停止看板` 不再误杀：兜底路径按端口拿到 PID 后**先问"你是谁"**
  （`_process_cmdline` + `_looks_like_dashboard`，要求命令行同时含 `agentmemhub` 与 `serve`），
  对不上就拒绝；确认后再核一次（发现到确认之间 PID 可能已被复用）。
- `fix(scripts)` `merge_distill_into_db.py --dry-run` 不再落 schema。**「开事务再回滚」是无效方案**：
  `sqlite3.executescript()` 会先隐式 COMMIT 再执行脚本，DDL 逃出事务之外 —— 实测空库跑完 dry-run
  反而多出 12 个对象。改为 dry-run 时**根本不建** schema，下游查询全部走容错取值。
- `fix(scripts)` `sensitive_scan.py` 重写为两档规则（fatal 命中即退出码 1 / soft 只提示）：
  补 `ghp_` / `glpat-` / `AIza` / `sk-ant-` 等前缀；路径规则改通用盘符；用户名无法表达为规则时
  降级成字面量并告警（原先中文/带空格用户名**一条规则都不生成且不提示**）；未跟踪文件进 soft 档。
- `fix(watermarks)` 水位 tmp 名带 pid + 毫秒时间戳，写入后 `replace`、finally 清理 —— 固定 `.tmp`
  在面板与 CLI 同时写时互相踩，两个进程各写一半、都没报错、内容还是混合的。
  `load_state` 读不动时先把坏文件 `_quarantine` 改名留存再回默认（原先静默重置，`delta` / `consumed` /
  `pending_score` 一起丢，无从对账）。
- `fix(logs)` 滚动失败不再静默：`_rotate_out` 先重试 `rename`，仍失败退化为「复制 + 截断」；
  压缩路径改为 `_truncate` 而非 `unlink`（别人手里的句柄不失效）。原先 `except Exception: pass`
  的表现是"配了 5MB 上限却永远不滚"。
- `fix(adapters/trae)` 三类数据 id 加类别前缀（`snap:` / `solo:` / `mem:`）—— 它们共用
  `source="trae"`，而库主键是 `(source, id)`，同名直接整会话覆盖；`_load_memory` 的硬编码 `[:20]`
  提为 `MAX_MEM_FILES`（第 21 个文件原先静默丢）。
- `fix(adapters/qwen)` 清单与 `load` 统一用 `_peek_sid(fp) or fp.stem` —— 首行没有 `sessionId` 时
  两边算出的 id 不同，增量比对永远判"已变"，该会话每轮全量重读；缺 `parts` 的 user 行改为跳过
  （原先把**文件名**当用户说的话入库，污染检索且标题变成一串哈希）。
- `fix(snapshot)` `restore` 前检查索引库 `-shm` / 非空 `-wal`，被占用即拒绝并提示先停其它进程 ——
  直接覆盖的后果不是"回滚失败"而是**索引分叉**：对方旧连接会继续按自己的页缓存 checkpoint，
  把刚恢复的库写成混合状态且全程无报错。CLI 加 `--force` 出口；面板调用传 `force=True`
  （面板自己长期持有连接，检查必然误报，面板侧靠 `tasks` 单槽互斥）。
  顺带修 `--wiki-only` 与 `--db-only` 同时给时"什么都没恢复却打印回滚完成"。

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

**2026-10-27 第三轮（`rag/` 包深度审计）**
- `fix(rag)` 删会话 / 排除轮次会**静默报废 ANN 索引**：`delete_units_for_conversation` /
  `delete_units_for_turn` 用 `sqlite_master WHERE type='table' AND name LIKE 'vec_%'` 找向量表，
  vec0 的影子表（`vec_*_chunks` / `vec_*_rowids` / `vec_*_info` / `vec_*_vector_chunks00`）
  同样满足条件，于是被按 unit id 删 rowid —— 而 `_chunks` 是 `INTEGER PRIMARY KEY`，
  一行对应一整块向量。删掉 = 该块全部向量从 ANN 结构消失，但普通行查询仍查得到，
  `run_stats` 的覆盖率照样显示"满"。内存库实测：删 2 行后 `COUNT(*)` 仍 3，
  `MATCH ? AND k=5` 返回 **0**。改为只删虚拟表本身（`LIKE '%USING vec0%'`）。
  守卫：`tests/rag/test_rag_ingest.py` 里直接断言 KNN 而不只断行数。
- `fix(rag)` `--rebuild` 原先**先清索引、后开源库**：源库路径写错或缺表时
  `DELETE FROM units` 已执行才抛错 —— units 与全部模型向量不可逆丢失，
  且 units 是 AUTOINCREMENT，重灌后 id 更大，`unit_values` 按旧 id 成永久孤儿。
  改为先 `open_source_ro` + `validate_source_schema`，失败必须发生在破坏之前。
- **待修**（已确认，未动）：源端 `events.content` 变更后索引**永不刷新** ——
  幂等键只有 `(source, conversation_id, seq)`，命中即 `skipped_known`，且只写向量
  不 UPDATE `text/title/time/role`。`store.upsert_sessions` 在会话 `updated_at` 变化时
  会整会话重插（同键正文被改写），此后任何 sync/ingest 都跳过它；若该模型向量曾缺失，
  会用**新文本**写向量而 `units.text` 仍是旧文本 → 向量与正文语义错位。
  现实修法只能是 `--rebuild`（代价见上一条），需要设计「known 命中后比对 content hash」。
- **待修**：`rag.retrieval.models` 里非 active 模型文件缺失时 `search.py:744-751` 无 try，
  整个检索 500（`config.py` 只校验 active 模型的文件存在）。
- **待修**：`seq = MAX(seq)+1` 在两个 MCP 客户端同时写时撞 `UNIQUE(source,conversation_id,seq)`，
  `legacy_id` 唯一索引的"先查后插"同样非原子；应改 `BEGIN IMMEDIATE` + 冲突重试。
- **待修**：`rag.write.order` 漏掉 active 模型时，新记忆/页面的 active 向量永不写入，
  而召回主通道正是 active 表 —— 配置检查脚本应加「active ∈ write_order」校验。
