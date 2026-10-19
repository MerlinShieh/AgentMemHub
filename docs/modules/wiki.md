> 覆盖模块：`agentmemhub/wiki.py`、`agentmemhub/wiki_index.py`、`agentmemhub/wiki_manifest.py`、`agentmemhub/wiki_pending.py`、`agentmemhub/wiki_triggers.py`、`agentmemhub/obsidian.py`、`scripts/wiki_compile.py`、`scripts/wiki_aggregate.py`、`scripts/wiki_linkfix.py`、`scripts/obsidian.py` · 最后核对：2026-09-27 · 上游设计见 [llm-wiki.md](../llm-wiki.md)、[batch-offline-plan.md](../batch-offline-plan.md)、[data-architecture.md](../data-architecture.md)

## 1. 职责边界

| 做什么 | 落点 |
| --- | --- |
| 第一级（L1）逐会话编译：把同一会话的碎片记忆归纳成会话内页面（一文件多页） | `scripts/wiki_compile.py` |
| 第二级（L2）跨会话聚合：归域 → 域内细分 → 逐最终页编译，产出主题实体页 | `scripts/wiki_aggregate.py` |
| 增量更新：脏会话 → 只重编受影响的 L1 页与 L2 域 → linkfix → 刷新 manifest → 页面层投影 | `agentmemhub/wiki.py` `update()` |
| 对齐审计（只读）：引用有效性、正向覆盖、变更 diff、两级同步判定 | `agentmemhub/wiki.py` `align()` |
| 失败清单查询与定向补跑（不全量重来） | `agentmemhub/wiki.py` `failures_summary()` / `retry_targets()` / `retry_failed()` / `retry_all()` |
| wikilink 修复与权威验收 | `scripts/wiki_linkfix.py`；`agentmemhub/obsidian.py` |
| 页面层投影：L2 页整页写进 `units`，参与召回 | `agentmemhub/wiki_index.py` `project_pages()` |
| 待更新记忆的查看 / 软删除 / 硬删除 | `agentmemhub/wiki_pending.py` |
| 写入时触发判定（三条规则 + 单飞行 + 状态文件） | `agentmemhub/wiki_triggers.py` |

明确不做：

| 不做什么 | 归属 |
| --- | --- |
| 不做召回排序与融合。wiki 只负责把 L2 页写进召回面（`units` 里 `source='wiki'` / `role='wiki'`），检索、六路通道、价值加权都在别处 | `agentmemhub/rag/`，见 [data-architecture.md](../data-architecture.md) |
| 不做定时调度。没有后台定时任务、没有 daemon；`update.schedule` 是"写入时补触发已越过的时点档"，一天不写入就一天不触发 | `agentmemhub/wiki_triggers.py` |
| **不做离线的自动触发**：离线批次（Batch API）**只能手动**跑 `wiki --action batch-run`（一次提交、一次落盘）。实时通路的自动触发是有的（三条规则），但**当前被 `logs/wiki_trigger_state.json` 的 `overrides.enabled=false` 关着**；让离线可自动需要计划里的 M4（`update_mode` 开关）+ M5（触发器改造），**均未开始** | [batch-offline.md](batch-offline.md) §3.4、[batch-offline-plan.md](../batch-offline-plan.md) §13 |
| 不改蒸馏表内容。对 `distilled_memories` 的写入只有两处：软删除写 `wiki_ignore_at`、`drop(mode="hard")` 真删 | `agentmemhub/wiki_pending.py` |
| 不读源库。两条编译链路与审计都只读索引库（`sqlite3` `file:...?mode=ro`） | 索引库路径来自 `agentmemhub.rag.config.load_settings().index_db` |
| 不处理"已进 wiki"记忆的删除（页面里的 `[m<id>]` 会变死引用，需要墓碑 + 引用重映射） | [memory-deletion.md](../memory-deletion.md)；`wiki_pending._filter_pending()` 会直接拒绝这类 id |
| L1 页不进召回面（只作下钻），只有 L2 页投影 | `wiki_index.project_pages()` 只扫 L2 目录 |
| 不做外部素材的解析/翻译，只按 frontmatter 的 `origin: external` / `doc:` 标注来源维度 | [external-ingest.md](../external-ingest.md) |

## 2. 对外入口

### 2.1 CLI 子命令（`agentmemhub wiki`）

控制台入口 `agentmemhub`（`pyproject.toml` 的 `[project.scripts]`）与 `python -m agentmemhub` 等价；实现是 `agentmemhub/cli.py` `cmd_wiki()`。

`--action` 的全部合法取值（来自 argparse 的 `choices`）：`failures`（默认）、`retry`、`align`、`update`、`triggers`、`trigger`、`backfill-manual`、`index-pages`、`pending`、`drop`、`ignored`、`restore`。

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--out` | `""` | 产出目录。`failures` / `retry` 必需；`pending` / `ignored` / `drop` / `restore` 用它当 L1 目录；`index-pages` 可回落配置 |
| `--stage` | `""` | `""` / `l1` / `l2`，限定阶段 |
| `--src` | `""` | 第二级补跑必需：第一级产出目录 |
| `--l1` / `--l2` | `""` | `update` 必需；`align` 两个都给才能判两级同步性 |
| `--workers` | `0` | 0 = 用配置 |
| `--db` | `""` | 索引库路径，默认读配置 |
| `--force` | 关 | `trigger`：绕过规则强制执行一次 |
| `--ids` | `""` | `drop` / `restore`：逗号分隔的记忆 id |
| `--mode` | `soft` | `drop`：`soft`（写 `wiki_ignore_at`）/ `hard`（真删） |
| `--confirm` | 关 | `drop --mode hard` 必须显式加 |
| `--limit` | `0` | `pending` / `ignored`：最多显示几条 |

真实命令示例：

```bash
# 对齐审计（两级一起给，才有 consistency 判定）
agentmemhub wiki --action align \
  --l1 exports/wiki_work/out_l1_mimo --l2 exports/wiki_work/out_l2_mimo

# 增量更新（脏会话 → 脏域，长任务）
agentmemhub wiki --action update \
  --l1 exports/wiki_work/out_l1_mimo --l2 exports/wiki_work/out_l2_mimo

# 看某轮的失败清单，再只补跑失败项
agentmemhub wiki --action failures --out exports/wiki_work/out_l1_mimo --stage l1
agentmemhub wiki --action retry --stage l1 --out exports/wiki_work/out_l1_mimo
agentmemhub wiki --action retry --stage l2 --out exports/wiki_work/out_l2_mimo \
  --src exports/wiki_work/out_l1_mimo

# 触发器：看状态 / 手动跑一次（--force 绕过规则）
agentmemhub wiki --action triggers
agentmemhub wiki --action trigger --force

# 待更新记忆明细 → 软删除（不进 wiki，仍可召回）
agentmemhub wiki --action pending --out exports/wiki_work/out_l1_mimo --limit 10
agentmemhub wiki --action drop --ids 4388,4392 --mode soft --out exports/wiki_work/out_l1_mimo

# 页面层投影（L2 页 → 召回面）
agentmemhub wiki --action index-pages --out exports/wiki_work/out_l2_mimo
```

### 2.2 HTTP 端点（`agentmemhub/web/app.py`）

| 方法 | 路径 | 落到 |
| --- | --- | --- |
| GET | `/api/wiki/failures` | `wiki.failures_summary(out, stage)` |
| POST | `/api/wiki/retry` | 后台任务 → `retry_failed` / `retry_all`（`stage=l2` 缺 `src` 报 400） |
| GET | `/api/wiki/align` | `wiki.align(out, stage, db, l1_dir=, l2_dir=)` |
| GET | `/api/wiki/pending` | `wiki_pending.pending()` |
| POST | `/api/wiki/pending/drop` | `wiki_pending.drop()`（body 带 `ids` / `mode` / `confirm` / `out`） |
| GET | `/api/wiki/pending/ignored` | `wiki_pending.ignored()` |
| POST | `/api/wiki/pending/restore` | `wiki_pending.restore()` |
| POST | `/api/wiki/update` | 后台任务 → `wiki.update()`（长任务，已有任务在跑返回 409） |
| GET / PUT | `/api/wiki/triggers` | `wiki_triggers.status()` / `set_overrides()`（非法字段 422） |
| POST | `/api/wiki/trigger/run` | `wiki_triggers.run_manual(force=)`，同步 |

### 2.3 MCP 侧：没有 wiki 工具，只有写入钩子

MCP 五工具（`memory_search` / `memory_recent` / `memory_stats` / `memory_save` / `memory_score`）里没有 wiki 相关工具。接入点是**写入侧旁路**：`mcp_server` 在写入成功后起 daemon 线程 `_wiki_trigger_probe()`，它调 `wiki_triggers.on_memories_written()`；该函数全程旁路，异常只落 `logs/wiki.log`（`event="trigger_error"`），不影响 MCP 响应。

### 2.4 脚本 CLI（可直接独立跑，不必经过服务层）

```bash
# L1：列候选会话 / 全量编译 / 定向补跑 / 单会话
python scripts/wiki_compile.py --list --min 5
python scripts/wiki_compile.py --all --out exports/wiki_work/out_l1_mimo --workers 4
python scripts/wiki_compile.py --all --out exports/wiki_work/out_l1_mimo --retry-failed
python scripts/wiki_compile.py --source dsh --conversation-id <cid> --out DIR

# L2：只看归域质量（便宜）/ 全量三段式
python scripts/wiki_aggregate.py --src exports/wiki_work/out_l1_mimo \
  --out exports/wiki_work/out_l2_mimo --stage domains
python scripts/wiki_aggregate.py --src exports/wiki_work/out_l1_mimo \
  --out exports/wiki_work/out_l2_mimo --stage all --workers 6 --retry-failed

# 链接修复（先 dry-run 看报告）与 Obsidian 权威验收
python scripts/wiki_linkfix.py --dir exports/wiki_work/out_l2_mimo --dry-run
python scripts/obsidian.py check
python scripts/obsidian.py unresolved
```

> A2 改动点：`scripts/wiki_aggregate.py` 的 `stage_plan(..., runner=)` 是离线批处理已落地的**唯一接缝**（默认 `SyncRunner`，见 `agentmemhub/llm_batch.py`）；`run(args)` 的返回状态将被扩成三段式（提交轮提前返回 `offline_submitted`），`PendingError` 的传播边界也在此收拢。`wiki --action` 的新增动作与配置入口不在本文档展开，见 [batch-offline-plan.md](../batch-offline-plan.md) §五 / §里程碑。

### 2.5 内部函数（可编程入口，面板与 CLI 共用同一份）

`agentmemhub/wiki.py`：

| 签名 | 作用 |
| --- | --- |
| `failures_summary(out_dir, stage="") -> dict` | 未解决失败的结构化摘要；`needs_manual=True` 表示存在 `quota` / `auth` / `model` 这类重试无意义的错误 |
| `retry_targets(out_dir, stage) -> list[str]` | 该阶段未解决的失败目标（去重保序），L1 的目标形如 `source/cid` |
| `retry_failed(*, stage, out_dir, src="", db="", workers=0, on_progress=None) -> dict` | 只重跑失败项；`stage="l1"` 只需 `out_dir`，`stage="l2"` 需要 `src`；无待重跑项返回 `retried=0` |
| `retry_all(*, out_dir, src="", db="", workers=0, on_progress=None) -> dict` | 按依赖顺序补跑两级（L1 在前） |
| `align(out_dir="", stage="", db="", l1_dir="", l2_dir="") -> dict` | 只读对齐审计，返回 `invalid_refs` / `coverage` / `stages` / `consistency` / `needs_recompile` |
| `collect_refs(root) -> dict[str, set[int]]` | 收集产出目录下所有 md 的 `[m<id>]` 引用（按文件分组） |
| `update(*, l1_dir, l2_dir, db="", workers=0, on_progress=None) -> dict` | 增量更新主流程；返回 `updated` / `dirty_sessions` / `l1` / `dirty_domains` / `l2` / `linkfix` / `link_check` / `manifest` / `page_index` / `snapshot` / `hints` |

`agentmemhub/wiki_triggers.py`：`on_memories_written()`（写入钩子）、`run_manual(*, force=False)`、`status()`、`get_effective_config()`、`set_overrides(patch)`、`evaluate(*, now, dirty, has_dirty, hook, cfg, state)`（纯函数，可单测）、`_targets()`（解析两级目录 + 索引库，沙箱熔断在此）。

`agentmemhub/wiki_manifest.py`：`manifest_path(out_dir, stage)`、`snapshot_inputs(conn)`、`build_manifest(stage, db_path, extra=None, *, drop_sessions=None, drop_ids=None)`、`write_manifest(path, manifest)`、`load_manifest(path)`、`diff_manifest(manifest, db_path)`。

`agentmemhub/wiki_index.py`：`parse_page(path)`、`project_pages(idx, l2_dir, *, embedder=None, settings=None, log=print)`、`stats(idx)`。

`agentmemhub/wiki_pending.py`：`pending(*, l1_dir="", db="", limit=0)`、`ignored(...)`、`drop(*, ids, mode="soft", confirm=False, l1_dir="", db="")`、`restore(*, ids, l1_dir="", db="")`。

`agentmemhub/obsidian.py`：`find_cli(explicit="")`、`verify(*, cli="", vault="", timeout=180, require_running=True)`（不抛版本）、`unresolved(...)`、`eval_js(...)`、`reload_app(...)`、`app_running()`。

`scripts/wiki_compile.py`：`compile_all(db, out_dir, *, workers=0, limit=0, resume=True, min_n=1, thinking="", only=None, failures=None)`、`compile_one(...)`、`compile_session(client, memories, log=print)`、`plan_groups_batched(...)`、`_out_stem(source, cid)`。

`scripts/wiki_aggregate.py`：`run(args)`、`stage_domains(client, pages, dmin, dmax, batch=120, cache_file=None)`、`stage_plan(client, domain, members, *, batch=0, runner=None, log=print)`、`stage_compile(client, title_hint, group, all_titles)`、`assign_new_pages(client, table, new_pages, rep_pages=None)`、`load_pages(src_dir, seq_map=None)`、`build_seq_map(db)`、`make_client(thinking="")`、`_slug(s)`、`_l2_cfg(key, default)`。

`scripts/wiki_linkfix.py`：`run_fix(root, dry_run=False)`、`collect_pages(root)`、`collect_redirects(root)`、`resolve(name, idx, redirects)`、`norm(s)`。

注意 `scripts/wiki_aggregate.py` 的 `run(args)` 注解是 `-> None`，**实际返回状态字典**（`aborted` / `written_domains` / `skipped_domains` / `pages` / `pages_by_domain`）；`agentmemhub/wiki.py` 与 `retry_failed()` 都依赖这个返回值。

> A2 改动点：计划把 distill 钩子 / MCP probe / CLI 三处调用点收拢到统一入口 `update_wiki(mode=...)`，`wiki.update()` 的返回状态新增离线提交态；`agentmemhub/llm_batch.py` 提供 `BatchRequest` / `BatchJob` / `SyncRunner` / `PendingError` / `decide()` / `save_state()` / `load_state()` 等编解码与状态件。

## 3. 数据流与不变量

### 3.1 数据流

```
索引库 session_rag.db
  distilled_memories（status IN ('new','similar') 且 wiki_ignore_at IS NULL）
        │
        │  L1：wiki_compile.compile_all()  —— 会话为脏单位，map-reduce 两段式
        │       （≤ single_shot_max 走单次；否则分批分组 → 逐组并发编译）
        ▼
out_l1_mimo/
  <source>__<cid>.md + .json      每会话一份，md 里多页（`# 标题` + `*type:*`）
  manifest_l1.json                编译输入快照（id → md5 指纹 + 会话归属）
  failures.jsonl                  失败清单（stage='l1'，target='source/cid'）
        │
        │  L2：wiki_aggregate.run()  —— ① 归域 ② 域内细分 ③ 逐最终页编译
        │       （源页面正文里的 [n] 先提升为全局 [m<id>]）
        ▼
out_l2_mimo/
  NN-域名/NNN-slug.md + .json     最终页（frontmatter: title/domain/aliases/type/sources/merged_from）
  NN-域名/index.md                域内索引
  index.md                        顶层索引
  _domains.json                   域 → 成员（平铺序号）
  _assign_cache.json              归域缓存（按批次序号键控）
  manifest_l2.json                编译输入快照 + 域 → L1 文件/记忆 映射
  failures.jsonl                  stage='l2-plan' / 'l2-compile' / 'l2-write'
        │
        │  收尾：linkfix（标题可能变）→ Obsidian 验收 → 刷新 manifest
        │        → wiki_index.project_pages() 全量对齐投影
        ▼
索引库 units（source='wiki'、role='wiki'、conversation_id=域名、
             src_id='wiki_<路径crc32>'、wiki_path=相对路径、origin=native|external）
        │
        ▼
rag 六路通道召回（kind=page；两阶段召回的第二阶段按 wiki_path 读全文）
```

| 读写对象 | 谁写 | 谁读 |
| --- | --- | --- |
| `distilled_memories`（索引库） | 蒸馏链路；wiki 侧只写 `wiki_ignore_at`、`drop(hard)` 删行 | `snapshot_inputs()`、`build_seq_map()`、`load_memories()`、`_delete_empty_session_outputs()` |
| `units` 与每模型独立的向量表（`ModelSpec.vec_table` 形如 `vec_<模型id>`）（索引库） | `wiki_index.project_pages()`（`source='wiki'` 行，含向量与 `ensure_base_value` 起点分 0.6） | 召回侧 |
| `out_l1` / `out_l2` 目录 | 两个脚本、`wiki_linkfix.run_fix()`、`wiki.update()` | 人（Obsidian vault）、`align()`、`wiki_index`、`load_pages()` |
| `manifest_l1.json` / `manifest_l2.json` | 脚本收尾、`wiki.update()` 第 6 步、`wiki_pending._hard_drop()` | `align()`、`diff_manifest()`、`wiki_pending._pending_ids()`、`wiki.update()` |
| `failures.jsonl` | `FailureLog.record()` / `resolve()` | `failures_summary()` / `retry_targets()` / 两个脚本的 `--retry-failed` |
| `logs/wiki.log`（JSONL 事实流） | 全部 wiki 模块（`logs.audit_wiki()`，全程旁路） | 人 / `agentmemhub.logs.read_wiki_audit()` |
| `logs/wiki_trigger_state.json` | `wiki_triggers`（标记 + `overrides`） | `wiki_triggers.status()` / `get_effective_config()` |
| `logs/wiki_update.lock` | `wiki_triggers._file_lock()`（PID + 时间戳） | 跨进程单飞行判定 |

### 3.2 不能破的不变量

1. **写盘三道闸门，全部作用于"整域替换"这个动作。**写作方式是 `_swap_domain_dir()`：先把一个域的全部 md/json/index 渲进内存，写进隐藏临时目录 `.tmp_<域目录>`，再把原目录改名为 `.bak_<域目录>`、临时目录改名为正式目录，失败时尽力还原。
   - **域级原子写**：不允许原地"删旧写新"——中途失败会留下"旧页已删、新页没写"的半成品。
   - **页数崩塌拒写**：`_is_page_collapse(old_n, new_n)` 判据是 `old_n >= COLLAPSE_MIN_OLD(4)` 且 `new_n < old_n * COLLAPSE_RATIO(0.5)`；命中则该域**跳过写盘、保留旧产物**，进 `skipped_domains` 与失败清单（`stage='l2-write'`），日志 `event="domain_write_skipped"`。
   - **换入失败也只跳过该域**（2026-09-29 修，缺陷 D-1）：`_write_domain_outputs()` 是逐域写盘的唯一出口，内部用 `_write_domain()` 收口 `_swap_domain_dir()` 抛出的任何异常，返回可读原因并记 `reason="swap_failed"`。**单个域换不进去绝不中止整轮** —— 此前异常一路穿透到整轮中止闸（2026-09-28 实测：Obsidian 打开着 `04-AgentMemHub-记忆系统` 的两个笔记 → `ddir.rename(bak)` 报 `WinError 5` → 88 分钟一轮里 L1 编完、**L2 一个域都没写**）。两类跳过都进 `skipped_domains` → `_l2_skip_ids()` 剔出新基线 → 下轮自动重试；失败路径也不再残留 `.tmp_<域>`。
   - **成功换入要销账**（2026-10-08 修）：`_write_domain_outputs()` 在某域换入成功后调 `fl.resolve("l2-write", name)`，销掉该域的历史失败。**失败清单是"待办"，磁盘上既然已是最新一轮产物，待办就该消** —— 否则只进不出、攒出"幽灵失败"：2026-10-04 14:32 增量轮跳过 4 个域（进清单），当晚 17:14 全量轮按设计重划域（17→21 域）并**完整写入 396 页、零跳过**，那 4 条却一条都没销，一直挂到 4 天后人工清账才发现。根因是销账此前**只发生在 `--retry-failed` 路径**。销账粒度是 `(stage='l2-write', target=域)`，只影响本域；本轮仍被跳过的域，历史失败**照留**（否则再也无从补跑）。
     **第二条销账路径 —— 域在重划中消失**（同日补修）：全量重划域会让某些旧域消失（实测 17→21 域那轮），`_prune_stale_domain_dirs()` 把它们的目录剪掉。这类失败**无人能销**：下一轮的 `by_domain_pages` 里连它的名字都不会出现，上面那条成功销账路径永远够不着它。修法是在**剪之前**用 `_stale_domain_names()`（判据共用 `_existing_domain_dirs()`，两处不各写一份）反查出这些域的**名字**，逐条 `fl.resolve()`。反查靠 `_domain_dir_map()`，而它对"对不上 `_domains.json` 的目录**直接不收**"⇒ 那类孤儿目录被剪时**故意漏销**（宁可留一条待办让人看见，也不能因为"认不出它是谁"就误销一条真问题）；**增量模式绝不动**别人的账。
   - **整轮降级中止**：`_is_degraded_run(n_degraded, n_tasks)` 判据是降级页占比 `>= ABORT_DEGRADE_RATIO(0.5)`；命中则 `run()` 立即返回 `{"aborted": "degrade_ratio", ...}`，日志 `event="run_aborted"`。**这道闸必须在 `_prune_stale_domain_dirs()` 之前**——先删旧域目录再中止，等于"声称中止了、磁盘却已经动了"。
    - **整域失败中止**：`_domain_missing_abort(planned, produced, out)` —— **全量模式**（`partial=False`）下，“计划里有的域”却“一页都没产出”（页任务在**外层**异常、走 `page_fail` 被 `continue` 掉，不是 fail-open 降级），且磁盘上已有旧产物时，`run()` 立即返回 `{"aborted": "domain_missing"}`。**为什么不能“跳过那个域、把其余域写了”**：全量模式的目录名按**成功域**重新编号，少一个域后面整体前移 —— `01-旧域` 与 `01-新域` 并存，索引与互链全乱；而 `_prune_stale_domain_dirs()` 还会把没产出域的旧目录当成“上一轮残留”删掉，本轮却没有替代产物 ⇒ **净损失一整域**。**首次构建例外**（磁盘上还没有任何域目录）：无物可毁，能写多少写多少。（2026-10-04 体检发现：`keep` 为空那道闸只覆盖了“**全失败**”，“**整域失败**”漏在外面；它与第 3 条的 `_l2_skip_ids` 互补 —— 那条管“新基线别记成已编译”，这条管“磁盘上的旧产物别被清掉”。）
2. **manifest 的语义是「实际编译成了什么」，不是「库里现在有什么」。**`build_manifest(..., drop_sessions=, drop_ids=)` 两个剔除参数分别对应会话级失败与组级失败（L1 逐组编译是 fail-open 的，会话整体成功但失败那几组的记忆没进任何页面）。漏剔除 = 下轮 align 认为已编译 = **永久掩盖**，这是 2026-09-21 静默丢失事故的根因。
3. **`_l2_skip_ids(dom_out, dirty_domains, written, skipped)`：没真正写出来的脏域，其覆盖的记忆 id 必须从 L2 新基线里剔除。**三类都算：页数崩塌跳过的域、逐最终页编译整域失败的域、聚合什么都没写的路径（dry-run、异常提前返回）。原则是"宁可多剔除、下轮重试，也不漏剔除"。
4. **L2 中止时不刷新 L2 manifest、也不跑 linkfix。**中止时 `mf2` 读回旧清单（脏数据保持脏，下次写入自动重试）；linkfix 按"新标题"去改旧页面，等于绕过中止闸动了磁盘。
5. **L1 全部失败时跳过 L2。**`l1_all_failed = bool(dirty_sessions) and not l1_ok and not removed_files` 成立时，L2 不会拿磁盘上的旧 L1 重跑同样的域（实测白烧 1290 秒 / 42 万 prompt token，产出只是同义改写）。
6. **`pending` 只比 L1 的 manifest，看不见"L1 已重编、L2 没跟上"。**这个中间态由 `align()` 的 `consistency` 显式回答：两级 manifest 的 `generated_at` 差值绝对值 `<= SKEW_TOL(60)` 秒判 `ok`，`> 0` 判 `l1_ahead`（**L2 落后，页面里还是旧知识**），`< 0` 判 `l2_ahead`；缺任一级 manifest 或时间解析不了则 `comparable=False`。当前产出目录正是这种状态：`manifest_l1.json` 比 `manifest_l2.json` 新两天以上，`align` 会判 `l1_ahead`。
7. **页面层投影是原子的全量对齐。**`project_pages()` 扫 L2 目录 → upsert → 删掉 `units` 里 `source='wiki'` 但磁盘已不存在的行，保证"投影集合与磁盘目录精确一致"，不会新旧两版共存。锚点必须由**路径**派生（`src_id='wiki_<crc32(相对路径)>'`、`seq` 为负数固定区间）：用内容指纹会在页面重编后走 INSERT 撞 `UNIQUE(source, conversation_id, seq)`，用位置序号会在页面增删后漂移。
8. **空页面与点目录绝不进召回面。**正文为空的 md（Obsidian 点击死链会在原地自动新建 0 字节笔记）、`.obsidian/` 与 `.tmp_*` / `.bak_*` 残留目录下的 md，都在 `project_pages()` 里被跳过（计入 `skipped_empty`）。
9. **跨进程单飞行。**`on_memories_written()` / `run_manual()` 都要过 `_single_flight()`：进程内 `threading.Lock` + 跨进程 `logs/wiki_update.lock`（`O_CREAT|O_EXCL` 原子创建，内容写 PID 与时间戳，超过 `_LOCK_MAX_AGE(3h)` 或持锁进程已死则抢占）。取不到锁不是静默跳过——记 `event="trigger_skip"`。
10. **域目录名要稳定。**增量更新优先沿用 `manifest_l2.json` 里记录的 `dir`，缺失时按 `_slug(域名)` 在磁盘找同名域目录，仍找不到才新建 `99-<slug>`（早期直接兜底 `99-` 曾导致 L2 目录里躺着 9 个 `99-*` 残留）。全量模式下 `_prune_stale_domain_dirs()` 只清"非隐藏目录 + `%02d-` 数字前缀"的域目录，**partial 模式一个目录都不清**。
11. **L1 的 `[[相关]]` 不可信。**第一级死链率 15.5%（措辞不一致），L2 只在提示词里把它当"可能相关的主题名"参考，最终 `related` 由 L2 自己重写；`linkfix` 的输出形态固定为 `[[<目标文件名>|<显示文本>]]`——Obsidian 的 `[[X]]` 只按文件名/路径解析，`aliases` 不参与（实测 1136 条按标题写的链接全是死链）。有歧义的名字**宁可去链保留文字**（`[[X]]` → `X`），不猜。
12. **测试熔断是进程级的。**环境变量 `AGENTMEMHUB_TEST_SANDBOX`（`wiki_triggers.SANDBOX_ENV`）被置位后，`_targets()` 直接抛 `RuntimeError`。它挡的是"活过测试的线程"——`monkeypatch` 撤销之后线程仍会拿到真实配置。

> A2 改动点：第 1、2、3、4 条闸门与剔除逻辑都必须覆盖离线提交路径——提交轮不得刷 manifest、不得跑 linkfix，回读轮才写盘；中止判定要能区分"上游降级"与"结果还没回来"（`PendingError`）。

13. **页级增量绝不改写范围外的页。**`scope_pages` 给出后，写盘只替换那几页，其余页**原样搬运**（`_read_kept_pages()` 读文件原文，不重新 `render_page()`）—— 重新渲染会把没打算动的页顺手改一遍，渲染函数一改就全库漂移。新页只用**空闲序号**，绝不重命名保留页（重命名会打断已有的 `[[链接]]`）。页数崩塌判据在 scope 模式按**本轮范围**算，而不是整域页数（整域数会因为"只编了一部分"必然塌，属于误杀）。

### 3.3 增量粒度：两级收窄（2026-10-19）

增量的**脏单位**决定了单轮成本。这一版把两级都收窄了，背景是实测：**12 条新记忆
→ L1 重编 84 页 → L2 重编 270 页**，单轮近 1 小时，且开销随知识库规模线性增长
（越用越慢）。

**第一级：会话分桶（L1）**

`memory_save` 的直写记忆原先全部归入一个固定的虚拟会话 `direct`，实测积累 210 条 /
84 个 L1 页。L1 的脏单位是**会话**，所以任何一条新记忆都让整会话变脏。现改为按自然日
分桶 `direct-YYYY-MM-DD`（配置 `distillation.direct_bucket`，`none` 可回退）：

- 新记忆只脏当天那个桶（几十条 → 3~5 页）；
- **编译逻辑一字未改**（仍是整会话重编），只是会话变小；
- 查重范围前移到 `(source, content_hash)` —— 否则同一结论隔天再写会因 cid 不同而
  **重复入库**（唯一索引仍是三列，历史口径不动，查重前移即可）；
- 跨天的同主题记忆仍由 L2 域聚合合并 ⇒ 这是分桶**不降质量**的关键。

**第二级：页级重编（L2）**

L2 原先把"脏域"整域重编。现在按磁盘页面 json 的 `from_files`（这页由哪些 L1 页合成）
反查「L1 文件 → L2 页」，**只重编引用了变动 L1 页的那些页**：

- `wiki._l2_page_scope()` 算出 `{域名: {可替换的旧页}}` 与各页的 L1 来源；
- 聚合层 `_write_domain_outputs(scope_pages=...)` 做**合并写盘**：范围内的旧页被替换，
  范围外的页原样搬过去（文件原文、文件名、序号都不动），新页只填空闲序号；
- **定位不到就退回整域重编**（老产物没写 `from_files`、或该域输入已全失效）——
  宁可多编，不可漏页。

**顺带修掉一个静默缺陷**：manifest 的 `domains[].l1_files` 是"上一次归域"的记录，会与
磁盘页面的 `from_files` 漂移。实测 `mcp__direct.md` 在 manifest 里只记了 **3 个域**，
磁盘上却有 **11 个域**引用它 —— 那 8 个域的页面永远进不了增量范围、内容静默过时。
`_l2_page_scope()` 因此**扫描全部域**（以磁盘为准），并把结果并回 `dirty_domains`。

**量化（真实产物实测）**：

| 变动的 L1 文件 | 受影响 L2 页 |
| --- | --- |
| 普通会话（`zcode__opencode-import-…`，13 处引用） | 13 页 / 4 个域 |
| `mcp__direct.md`（210 条记忆合成的一个巨型文件） | **170 页 / 11 个域** |

分桶后新记忆的 L1 产物是**新文件**，只被少数页引用 ⇒ 日常增量落在"个位数到十几页"，
而不是整域 270 页。

**质量风险与守门**：页级重编不丢内容（每条记忆仍进某个页；manifest 的 `drop_ids` 仍
剔除失败输入），但会**少做跨页合并** → 页更多、更碎。守门指标：
`align.coverage.uncovered` 必须为 0、`manifest.n_inputs` 与库一致、页数增长与新增记忆
成比例；周期性全量重跑（`--full`）用来压实长期积累的碎片。

## 4. 配置项

键的合并规则：内置默认（`config.DEFAULT_WIKI`）< `agentmemhub.yaml`；触发规则再多一层 `logs/wiki_trigger_state.json` 的 `overrides`（`get_effective_config()`）。`wiki` 与 `wiki.l2` 的 `llm` 子段是**各自相对顶层 `llm` 的覆盖，互不牵连**（不级联继承）。

| 键 | 默认值 | 作用 |
| --- | --- | --- |
| `wiki.enabled` | `True` | 总开关。唯一消费点是 `wiki_triggers._configured()`：置 `false` 后触发器不再跑，`status()` 也如实报告目标未就绪 |
| `wiki.out_l1` | `""`（yaml 实配 `exports/wiki_work/out_l1_mimo`） | 第一级产出目录；`align` / `update` / 触发器的默认目标 |
| `wiki.out_l2` | `""`（yaml 实配 `exports/wiki_work/out_l2_mimo`） | 第二级产出目录；同时决定 Obsidian 的 vault 名（取目录名，不是路径） |
| `wiki.obsidian_cli` | `""`（yaml 实配 `D:\tools\Obsidian\Obsidian.com`） | Obsidian CLI 路径。**优先于 `OBSIDIAN_CLI` 环境变量**：进程环境是启动时捕获的，新增的 PATH 条目可能没继承到 |
| `wiki.llm` | `{"timeout": 900}` | L1 的 LLM 覆盖段（相对顶层 `llm`）。`timeout` 必须放宽：L1 是长调用，默认 60s 实测整会话读超时（60s×3 + 退避 ≈ 187 秒）后失败 |
| `wiki.single_shot_max` | `12` | 会话记忆数 ≤ 此值时单次编译（省一次分组调用）；`wiki_compile._limits()` 读取，非法值回落常量 |
| `wiki.plan_batch_max` | `40` | 分组调用的单批记忆数上限；超出即分批分组，批内编号映射回全局编号 |
| `wiki.page_workers` | `3` | 会话内组级并发。真实并发 = 会话级 `workers` × `page_workers` |
| `wiki.workers` | `4` | 会话级并发（`compile_all` 默认并发数）；显式 `--workers` 覆盖 |
| `wiki.update.enabled` | `True` | 三条触发规则的总开关（`evaluate()` 第一道短路） |
| `wiki.update.schedule` | `["09:00", "12:00", "18:00"]` | 时点档。语义是"写入时补触发已越过且今日未触发的档位"，不是时钟触发；空列表 = 禁用它 |
| `wiki.update.dirty_memories` | `10` | 脏记忆（新增 + 变更）达到此数即命中定量规则 |
| `wiki.update.first_write_daily` | `True` | 每自然日第一次蒸馏写入也触发一次（手动触发不算，走 `force`） |
| `wiki.l2.llm` | `{}`（yaml 实配 `reasoning_effort: "low"`、`timeout: 900`） | L2 的 LLM 覆盖段，**继承顶层 `llm` 而非 `wiki.llm`** |
| `wiki.l2.workers` | `6` | 聚合阶段并发（域级细分与逐最终页编译共用）；脚本侧 `--workers` 优先 |
| `wiki.l2.min_pages` | `2` | 域内页数 ≤ 此值不再细分，直接成一页；脚本侧 `--min-pages` 优先 |
| `wiki.l2.domain_max` | `60` | 归域调用的主题域数量上限；脚本侧 `--dmax` 优先（0 时才读配置） |
| `wiki.l2.plan_batch_max` | yaml 里显式配 `40` | 域内细分的单批成员上限。该键**不在 `DEFAULT_WIKI["l2"]` 里**，`_l2_cfg("plan_batch_max", L2_PLAN_BATCH_MAX)` 读不到就回落模块常量 `40`（两处默认值恰好相同） |

触发器的运行时状态（不进 yaml，程序化改写会抹掉用户注释）：`logs/wiki_trigger_state.json` 的 `fired`（今日已触发档位，只留最近 7 天）/ `first_write_date` / `last_update_at` / `update_started_at`（"更新进行中"）/ `last_error` / `overrides`。

> A2 改动点：`wiki.update_mode` 一类的新键、`wiki.triggers.enabled` 开关与阈值调整都**随实现里程碑落地**，不提前埋假开关；白名单与 override 层设计见 [batch-offline-plan.md](../batch-offline-plan.md) §四 / §八。

## 5. 调试与排障

### 5.1 `logs/wiki.log`（JSONL 事实流，真实事件名）

| 事件 | 来源 | 关键字段 |
| --- | --- | --- |
| `run_start` / `run_end` | 两个脚本 | `script`、`out` / `src`、`sessions`、`todo`、`ok`、`failed`、`pages`、`skipped`、`seconds`（墙钟）、`call_seconds`（真实等 LLM 的时间）、`usage` |
| `session_done` / `session_fail` | L1 | `source`、`cid`、`memories`、`pages`、`seconds` / `error` |
| `progress` | L1 | `target`、`message`（分组/逐组编译的进度，长任务"跑到哪了"） |
| `manifest` | 两级收尾 | `stage`、`n_inputs`、`domains` |
| `call` | L2 | `tag`（域表/归类/细分/编译）、`ok`、`attempt`、`seconds`、`error`、`usage` |
| `domain_planned` / `page_done` / `page_fail` | L2 | `name` / `domain`、`title`、`pages` / `merged` / `truncated` / `error` |
| `run_aborted` | L2 | `reason`（`degrade_ratio` / `domain_missing`）、`degraded`、`tasks`、`missing`、`domains` |
| `domain_write_skipped` | L2 | `domain`、`reason="page_collapse"`、`old_pages`、`new_pages` |
| `trigger_check` | 触发器 | `hook`（`write` / `manual`）、`should_run`、`reasons`、`due_slots`、`dirty`、`last_update_at` |
| `trigger_skip` | 触发器 | 已有更新在跑（进程内或其它进程） |
| `trigger_update_start` / `trigger_update_end` / `trigger_update_error` | 触发器 | `reasons`、`dirty`、`seconds`、`updated`、`l1_recompiled`、`l1_failed`、`l2_domains`、`l2_pages`、`l2_aborted`、`l2_skipped`、`page_index_seconds`、`hints` |
| `trigger_fired_skipped` | 触发器 | L1 有会话失败 → 不消耗触发额度 |
| `trigger_error` | 触发器 / MCP 钩子 | `hook`、`error` |
| `link_check` | `wiki._verify_links()` | `ok`、`available`、`unresolved_targets`、`unresolved_links`、`error` |
| `page_index` | `wiki.update()` 收尾 | `ok`、`seconds`（实测 466 页约 4.5 分钟）、`pages`、`inserted`、`updated`、`removed`、`skipped_empty` |

程序侧读取：`agentmemhub.logs.read_wiki_audit(limit=N)`。标准头（`ts` / `level` / `module` / `line` / `msg`）由 `logs.standardize()` 统一补齐，调用方只写业务字段。

> A2 改动点：离线批处理计划新增 `batch_upload` / `batch_create` / `batch_retrieve` / `batch_cancel` / `batch_download` 事件（含离线提交 / 回读 / 终局失败 / abort 等），随实现登记。

### 5.2 常用查询与重跑命令

```bash
# 1) 差多少：两级一起给，看 consistency（l1_ahead = L2 落后）
agentmemhub wiki --action align --l1 exports/wiki_work/out_l1_mimo \
  --l2 exports/wiki_work/out_l2_mimo

# 2) 哪些失败了：按 kind 分类，fatal（quota/auth/model）无需重试
agentmemhub wiki --action failures --out exports/wiki_work/out_l1_mimo --stage l1

# 3) 待更新明细（id 列表会被截断，明细在这里）
agentmemhub wiki --action pending --out exports/wiki_work/out_l1_mimo

# 4) 补跑
agentmemhub wiki --action retry --stage l1 --out exports/wiki_work/out_l1_mimo
agentmemhub wiki --action retry --stage l2 --out exports/wiki_work/out_l2_mimo \
  --src exports/wiki_work/out_l1_mimo

# 5) 触发器的配置、状态、进行中标记
agentmemhub wiki --action triggers

# 6) 链接：先修再验（验收以 Obsidian 为准，未解析必须为 0）
python scripts/wiki_linkfix.py --dir exports/wiki_work/out_l2_mimo --dry-run
python scripts/obsidian.py check

# 7) 重投影召回面
agentmemhub wiki --action index-pages --out exports/wiki_work/out_l2_mimo

# 8) 查日志（JSONL，可直接筛事件）
rg '"event": "run_aborted"' logs/wiki.log
rg '"event": "domain_write_skipped"' logs/wiki.log
rg '"event": "trigger_check"' logs/wiki.log
```

### 5.3 常见故障与对策

| 现象 | 判据 / 日志 | 对策 |
| --- | --- | --- |
| 上游 HTTP 524，一轮丢几十页 | `run_aborted`（`reason="degrade_ratio"`）或 `domain_write_skipped`（`reason="page_collapse"`） | 这是**保护生效**，不是产物损坏：旧产物已保留、脏数据没进新基线，下次写入自动重试。反复出现说明该域细分不稳定，检查 `wiki.l2.plan_batch_max` 是否过大；等 LLM 恢复后重跑 |
| 页数崩塌（如 43 页 → 1 页） | `domain_write_skipped`、失败清单 `stage='l2-write'` | 同上；不要在 LLM 不可用时强行写盘——整域替换不可逆 |
| L1 有会话失败 | `session_fail`；`wiki.update()` 返回 `l1.failed > 0`；触发器 `trigger_fired_skipped`（不消耗当日额度） | 失败会话已被剔除出新基线，走 `--action retry --stage l1` 或等下次写入 |
| L2 落后 L1（页面里还是旧知识） | `align` 的 `consistency.verdict == "l1_ahead"` | 跑 `wiki --action update`（会按 manifest_l2 的域映射定位脏域） |
| 页面正文有空 `[m?12]` 这类未映射引用 | `load_pages()` 打印"未映射引用 N 处"；`--db` 没给时会警告 | 给 L2 传 `--db`：会话内序号 `[n]` 只能靠索引库算回 m-id，只靠产物 json 的 `sources` 数组会丢一批 |
| `[[…]]` 点开是空白笔记 | `link_check` 的 `available=True` 且 `ok=False`；`scripts/obsidian.py unresolved` 有清单 | 跑 `wiki_linkfix.py --dir <L2目录>`（它输出 `[[文件名|显示]]`），再用 `obsidian.py check` 复核 |
| 验收报"未验成" | `link_check.available=False` | **不是失败**：Obsidian 没运行 / CLI 没注册 / 没开「命令行界面」。`available=False` 只表示没验成，`ok=True` 才是唯一通过标准 |
| 失败清单里有 `quota` / `auth` / `model` | `failures_summary().needs_manual == True`、`fatal` 非空 | **停止补跑**，先解决余额 / 密钥 / 模型名，再重试 |
| 以为"触发没打上标记" | `wiki --action triggers` 的 `in_progress.running == True` | 更新在写完 manifest 之后还有页面层投影（实测 4.5 分钟），期间标记仍是上一轮。看 `update_started_at` 与 `page_index_seconds`，别误判成卡住 |
| 索引里的链接指向 `.json` | 顶层 `index.md` 的链接后缀 | 以 `.md` 为准枚举页面（`render_index_from_disk()`）；域级索引走另一条路径，别只改一处 |
| 并发触发互踩 | 日志里出现两份 `run_start` | 跨进程锁没生效或被误删。锁文件是 `logs/wiki_update.lock`；`logs/` 不可写时会退化为不锁（宁可承担并发风险也不让更新整体失效） |
| 测试里真实产出目录被改 | `RuntimeError: 测试环境禁止解析真实 wiki 产出目录` | 熔断生效。沙箱跑测试时 `AGENTMEMHUB_TEST_SANDBOX=1` 必须置位；新加"起线程/子进程的旁路"时要确认它也被覆盖 |

（待核实：项目约定引用了 `docs/logging.md` 作为"日志事件字典 + 排查剧本"，但本次核对时该文件在磁盘上不存在，因此 §5.1 的事件表是按代码 grep 结果整理的，不是与那份字典对齐后的结果。）

## 6. 测试在哪

| 测试文件 | 覆盖 |
| --- | --- |
| [tests/test_wiki_service.py](../../tests/test_wiki_service.py)（39 个） | `wiki.py`：失败摘要 / 补跑 / `align` 引用反推与 manifest diff / `consistency` 六个分支 / `update` 主流程（脏会话、脏域、L1 全失败跳过 L2、组级失败剔除新基线、空会话删产物、`_l2_skip_ids`） |
| [tests/test_wiki_scripts.py](../../tests/test_wiki_scripts.py)（55 个） | 两个脚本 + linkfix：`_slug`、`load_pages` 页面识别与序号提升、linkfix 五步解析顺序、分组分批映射、并发与结果重排、`_limits()` / `_l2_cfg()` 配置优先、顶层索引指向 md、`aliases` 渲染、页数崩塌与整轮降级判据、域目录整体换入与失败还原、细分 runner 接缝与 `custom_id`、**页级增量合并写盘**（`scope_pages`）与 `_read_kept_pages()` |
| [tests/test_wiki_page_scope.py](../../tests/test_wiki_page_scope.py)（5 个） | `_l2_page_scope()`：只收引用变动 L1 文件的页、新 L1 文件并入成员、定位不到退回整域、坏 json 容错、index.md 不算页 |
| [tests/test_wiki_manifest.py](../../tests/test_wiki_manifest.py)（11 个） | 输入快照口径、指纹走 md5 现算、manifest 读写、diff 的新增/变更/removed 细分/脏会话归属 |
| [tests/test_wiki_triggers.py](../../tests/test_wiki_triggers.py)（27 个） | 三条规则判定、`overrides` 校验与持久化、写入钩子与手动触发、L1 部分失败不打标记、单飞行（进程内 / 跨进程 / 锁文件异常）、更新起止留痕、`_configured()` 总开关、沙箱熔断 |
| [tests/test_wiki_index.py](../../tests/test_wiki_index.py)（15 个） | 页面解析、整页投影与全量对齐收敛、幂等、召回层级、来源维度（native/external）、空页面与点目录不入召回面、`aliases` 兼容 |
| [tests/test_wiki_pending.py](../../tests/test_wiki_pending.py)（10 个） | 待更新明细、软删除不触发重编、`restore`、硬删除清两份投影并重建 manifest、只允许删待更新 id、`confirm` 校验 |
| [tests/test_obsidian_cli.py](../../tests/test_obsidian_cli.py)（9 个） | CLI 路径优先级、vault 名归一、BOM / 英文句兜底、`available=False` 不算失败、通过标准是未解析为 0、不自动启动 Obsidian |
| [tests/test_failures.py](../../tests/test_failures.py)（17 个） | `FailureLog`：分类、累积、坏行容错、`resolve` 销账、目标去重、汇总与致命提示 |
| [tests/test_config.py](../../tests/test_config.py) / [tests/test_config_audit.py](../../tests/test_config_audit.py) | 配置默认值与深合并、僵尸键审计（含"真实仓库所有配置键都有读取点"） |
| [tests/test_logs_format.py](../../tests/test_logs_format.py) / [tests/test_snapshot.py](../../tests/test_snapshot.py) | wiki 事件的标准头与摘要、失败日志不影响主流程；`update()` 开头的快照锚点 |

回归清单（改动哪块跑哪些）：

| 改动 | 必跑 |
| --- | --- |
| `agentmemhub/wiki.py`（align / update / 闸门 / 投影收尾） | `test_wiki_service.py` + `test_wiki_manifest.py` + `test_snapshot.py` |
| `scripts/wiki_aggregate.py`（归域/细分/编译/写盘闸） | `test_wiki_scripts.py` |
| 页级增量（`_l2_page_scope` / `scope_pages` 合并写盘） | `test_wiki_page_scope.py` + `test_wiki_scripts.py` + `test_wiki_service.py` |
| `scripts/wiki_compile.py`（两段式 / 分批 / `_limits`） | `test_wiki_scripts.py` |
| `scripts/wiki_linkfix.py` | `test_wiki_scripts.py` + `test_obsidian_cli.py` |
| `agentmemhub/obsidian.py` | `test_obsidian_cli.py` |
| `agentmemhub/wiki_manifest.py`（快照口径 / diff） | `test_wiki_manifest.py` + `test_wiki_service.py` |
| `agentmemhub/wiki_triggers.py`（规则 / 锁 / 标记） | `test_wiki_triggers.py` + `test_mcp_server.py`（写入钩子入口） |
| `agentmemhub/wiki_index.py`（投影 / 召回面） | `test_wiki_index.py` |
| `agentmemhub/wiki_pending.py` | `test_wiki_pending.py` + `test_wiki_manifest.py` |
| 增删配置键（`DEFAULT_WIKI` 或 yaml 的 `wiki` 段） | `test_config.py` + `test_config_audit.py` + `scripts/check_config_keys.py` |
| 新增日志事件 | `test_logs_format.py` |
| 动的旁路会起线程 / 子进程 | 确认 `AGENTMEMHUB_TEST_SANDBOX` 熔断覆盖它（`test_wiki_triggers.py::test_沙箱熔断_进程级拦截真实产出目录`） |

> A2 改动点：离线批处理会改 `scripts/wiki_aggregate.py` 与 `wiki_triggers` 的默认行为，届时 `test_wiki_scripts.py`（细分 runner 接缝用例）与 `test_wiki_triggers.py`（27 个）需要按计划适配；新增回归见 [batch-offline-plan.md](../batch-offline-plan.md) §里程碑。
