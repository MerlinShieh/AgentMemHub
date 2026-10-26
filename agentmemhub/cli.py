"""AgentMemHub 命令行入口。

统一提取各 Agent Harness 对话历史 → 入库 SQLite → 检索 / 导出。

用法：
    python agentmemhub.py ingest                  # 提取所有 adapter 并入库
    python agentmemhub.py ingest --source opencode   # 只提取某个 source
    python agentmemhub.py list [--source opencode]
    python agentmemhub.py show <source> <id>
    python agentmemhub.py search <query> [--source opencode] [--role tool] [--limit 20]
    python agentmemhub.py export --format jsonl|markdown [--source x] [--out dir]
    python agentmemhub.py stats
    python agentmemhub.py adapters                 # 列出可用的 adapter 及状态
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from agentmemhub.store import Store
from agentmemhub import adapters
from agentmemhub.models import events_to_markdown


def _stdout(s: str) -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print(s)


def _force_utf8_stdio() -> None:
    """CLI 入口统一强制 UTF-8（errors=replace 兜底）。

    Windows 控制台默认 GBK，输出含 ✓ ✅ ¥ 这类字符直接 UnicodeEncodeError，
    把本已算完、正要打印的结果整个丢掉 —— 此坑已在脚本/审计/MCP 三处踩过，
    这里在唯一入口一次修掉，各命令不再各自设防。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _cli_log(msg: str, level: str = "info") -> None:
    """CLI/控制台操作落盘（logs/cli.log），不干扰终端输出。"""
    try:
        from agentmemhub import logs
        logs.record(msg, level=level, actor="cli", dest="cli")
    except Exception:
        pass


#: 清单 mtime 与库内 updated_at 的对比容差（秒）：文件落盘/时钟偏差
_MTIME_TOL = 5.0


def run_ingest(sources: list[str], signature: str = "", *,
               full: bool = False, stdout: Any = None) -> tuple[int, int]:
    """提取指定 source 列表并入库（CLI 与控制台共用）。返回 (会话数, 事件数)。

    默认会话级增量：adapter.list_sessions 轻量清单 vs 库内 updated_at 对比，
    只重读变化会话并 upsert_sessions（源端已删除的会话保留在库 = 历史保全）。
    三级回退（扩展点，见 base.py）：
    - source_freshness 信号 > 上次水位 → 整源重扫（force upsert），兜底清单
      看不见的变更（如 workbuddy 审计日志追加）；
    - 无轻量清单的源（trae）→ 整源重扫，upsert 按 updated_at 幂等对比；
    - full=True → 旧的整源 replace_source（--full 逃生口 / 库修复用）。
    成功后变更集写入 <data_dir>/watermarks.json，供 clean/push/score 增量消费。
    """
    from agentmemhub import config, watermarks
    out = stdout or _stdout
    store = Store()
    total_conv = 0
    total_ev = 0
    changed: list[dict[str, str]] = []
    source_counts: dict[str, dict] = {}
    now = time.time()
    wm_state = watermarks.load_state(config.config().data_dir)

    for src in sources:
        a = adapters.get_adapter(src)
        if a is None:
            out(f"[{src}] 未知 source")
            continue
        p = a.locate()
        if p is None:
            out(f"[{src}] 未找到数据源")
            continue
        sig = signature or ""
        if full:
            sessions = a.load(p)
            n = store.replace_source(src, sessions, signature=sig)
            total_conv += len(sessions)
            total_ev += n
            changed.extend({"source": src, "id": str(s["id"]), "status": "updated"}
                           for s in sessions)
            source_counts[src] = {"ts": now, "counts": {
                "added": len(sessions), "updated": 0, "unchanged": 0}}
            out(f"[{src}] 全量重建 {len(sessions)} 会话, {n} 事件")
            continue

        wm_ts = float((wm_state.get("last_ingest", {}).get("sources", {})
                       .get(src, {}) or {}).get("ts") or 0)
        fresh = a.source_freshness(p)
        listed = a.list_sessions(p)
        force = False
        only_ids: Optional[set[str]] = None
        plan: dict[str, str] = {}          # sid -> 预期状态（对比阶段）
        if fresh is not None and wm_ts and fresh > wm_ts + 1.0:
            force = True                    # 清单看不见的变更 → 整源重扫
        elif listed is None:
            pass                            # 无轻量清单 → 整源重扫（幂等对比）
        else:
            stored = {r["id"]: (r["updated_at"] or 0)
                      for r in store.list_conversations(src)}
            for entry in listed:
                sid = str(entry["id"])
                ts = entry.get("updated_at") or 0
                if sid not in stored:
                    plan[sid] = "added"
                elif ts > stored[sid] + _MTIME_TOL:
                    plan[sid] = "updated"
            if not plan:
                source_counts[src] = {"ts": now, "counts": {
                    "added": 0, "updated": 0, "unchanged": len(listed)}}
                out(f"[{src}] 无变化（{len(listed)} 会话跳过重读）")
                continue
            only_ids = set(plan)

        sessions = a.load(p, only_ids=only_ids)
        r = store.upsert_sessions(src, sessions, signature=sig, force=force)
        total_conv += r["added"] + r["updated"]
        total_ev += r["events"]
        if force or listed is None:
            changed.extend({"source": src, "id": str(s["id"]), "status": "updated"}
                           for s in sessions)
            label = "信号触发整源重扫" if force else "整源重扫"
            out(f"[{src}] {label}: 新增 {r['added']}, 更新 {r['updated']}, "
                f"未变 {r['unchanged']}（{r['events']} 事件）")
        else:
            changed.extend({"source": src, "id": sid, "status": stt}
                           for sid, stt in plan.items())
            skipped = (len(listed) - len(plan)) + r["unchanged"]
            out(f"[{src}] 增量: 新增 {r['added']}, 更新 {r['updated']}, "
                f"未变 {skipped}（{r['events']} 事件）")
        source_counts[src] = {"ts": now, "counts": {
            "added": r["added"], "updated": r["updated"],
            "unchanged": r["unchanged"]}}

    store.close()
    watermarks.record_ingest(wm_state, ts=now, source_counts=source_counts,
                             changed=changed)
    watermarks.save_state(config.config().data_dir, wm_state)
    _cli_log(f"ingest sources={sources} full={full} → {total_conv} 会话, "
             f"{total_ev} 事件, delta {len(changed)} 条")
    return total_conv, total_ev


def cmd_ingest(args) -> None:
    sources = [args.source] if args.source else [a.source for a in adapters.all_adapters()]
    total_conv, total_ev = run_ingest(sources, signature=args.signature,
                                      full=args.full)
    _stdout(f"完成: {total_conv} 会话, {total_ev} 事件")


def cmd_list(args) -> None:
    store = Store()
    convs = store.list_conversations(args.source)
    if not convs:
        _stdout("（空）")
        return
    for c in convs:
        title = (c["title"] or "")[:40]
        _stdout(f"[{c['source']}] {c['id'][:24]} | {title} | {c['event_count']} 事件")
    store.close()


def cmd_show(args) -> None:
    store = Store()
    conv = store.get_conversation(args.source, args.id)
    if conv is None:
        _stdout("未找到会话")
        store.close()
        return
    events = store.get_events(args.source, args.id)
    header = f"# [{conv['source']}] {conv['title'] or conv['id']}\n- 会话ID: {conv['id']}\n- 事件数: {len(events)}\n"
    _stdout(header + events_to_markdown(events))
    store.close()


def run_search_text(query: str, *, source: str = "", role: str = "", limit: int = 20) -> None:
    """关键字检索并打印结果（CLI 与控制台共用）。"""
    store = Store()
    hits = store.search(query, source=source or None, role=role or None, limit=limit)
    if not hits:
        _stdout("（无命中）")
        store.close()
        return
    _stdout(f"命中 {len(hits)} 条事件:\n")
    for h in hits:
        snippet = h.get("snippet") or h.get("content") or ""
        _stdout(f"[{h.get('source', source)}] {h.get('conversation_id')} | {h.get('role')} | {snippet[:80]}")
    store.close()


def cmd_search(args) -> None:
    run_search_text(args.query, source=args.source, role=args.role, limit=args.limit)


def cmd_export(args) -> None:
    from agentmemhub.export import export_jsonl, export_markdown
    store = Store()
    out = Path(args.out)
    if args.format == "jsonl":
        n = export_jsonl(store, out, args.source)
    else:
        n = export_markdown(store, out, args.source)
    _stdout(f"已导出 {n} 个会话 → {out}")
    store.close()


def cmd_stats(args) -> None:
    store = Store()
    s = store.stats()
    _stdout(f"总会话数: {s['conversations']}, 总事件数: {s['events']}")
    for row in s["sources"]:
        _stdout(f"  [{row['source']}] {row['conversations']} 会话")
    store.close()


def cmd_folders(args) -> None:
    """按文件夹（cwd 最后一段）统计各 Agent 的会话数。"""
    from collections import defaultdict
    store = Store()
    convs = store.list_conversations(args.source)
    groups: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for c in convs:
        cwd = c["cwd"] or "(unknown)"
        ws = cwd.rstrip("\\/").rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
        groups[ws][c["source"]] += 1

    rows = sorted(groups.items(), key=lambda x: -sum(x[1].values()))
    if not rows:
        _stdout("（空）")
        store.close()
        return
    _stdout(f"按文件夹统计的各 Agent 会话数（共 {len(rows)} 个文件夹）:\n")
    for ws, srcs in rows[: args.limit]:
        total = sum(srcs.values())
        detail = ", ".join(f"{s}={n}" for s, n in sorted(srcs.items()))
        _stdout(f"{total:4d} 会话 | {ws[:44]:44s} | {detail}")
    store.close()


def cmd_serve(args) -> None:
    """启动本地 Web 页面加载统一会话库。"""
    from agentmemhub.web import run_server
    run_server(port=args.port, open_browser=args.open,
               db=args.db or None)


def cmd_adapters(args) -> None:
    for a in adapters.all_adapters():
        d = a.describe()
        _stdout(f"[{d['source']}] {d['label']}: {'✓ ' + (d['path'] or '') if d['located'] else '✗ 未找到'}")


def _vectorize_stage(*, stdout=None) -> dict:
    """rag 后端 sync 终点：源库（agentmemhub.db）→ 引擎索引增量嵌入。

    asrag 自带 keyset 水位 + 主键幂等，无需 delta 过滤（全序扫描 <3s）；
    评分队列不再由本阶段填充——rag 下 scoring 走 unscored 全量 diff。
    """
    import time as _t
    from agentmemhub import rag_bridge
    from agentmemhub.rag.ingest import run_ingest_multi

    st = _stdout if stdout is None else stdout
    settings = rag_bridge.settings()
    t0 = _t.perf_counter()
    order = settings.write_order

    def _on_done(mid, summ):
        st("  [%(model)s] 扫描 %(scanned)d / 新嵌入 %(embedded)d / 跳过 %(skipped_known)d"
           "（%(seconds).1fs）" % summ)

    try:
        r = run_ingest_multi(settings, on_model_done=_on_done, log=_rag_log())
    except Exception as e:
        st(f"向量化失败：{e}")
        _cli_log(f"vectorize 失败：{e}", level="error")
        return {"failed": 1, "pushed_ids": []}

    first = r["summary"] or {}
    st("向量化完成可检索：模型 %s（%.1fs）；多模型顺序：%s"
       % (r["model"], _t.perf_counter() - t0, " → ".join(order)))
    if r["background"]:
        st("提示：%s（%s）" % (r["background_hint"], "、".join(r["background"])))
    _cli_log("vectorize %s completed=%s background=%s" % (
        {k: first.get(k) for k in ("scanned", "embedded", "skipped_known", "seconds")},
        r["completed"], r["background"]))
    return {"failed": 0, "pushed_ids": [], **first,
            "completed": r["completed"], "background": r["background"],
            "background_hint": r["background_hint"]}


def _rag_log():
    import logging
    return logging.getLogger("asrag.cli-sync")


def run_sync(*, source: str = "", vectorize: bool = True,
             full: bool = False) -> None:
    """增量同步：ingest（会话级增量）→ 清洗 delta 注入事件 → 只推送变更会话。

    全链路围绕 <data_dir>/watermarks.json 的变更集（delta）：
    - ingest：list_sessions 对比库内 updated_at，只重读变化会话并 upsert；
    - clean：只清 delta 会话的系统注入事件（--full / oversized 时跳过，
      手动 `clean --apply` 仍为全库清理）；
    - vectorize：向量化阶段（源库→session_rag.db 增量嵌入，keyset 水位自带幂等）；
    - 推送成功的 trace id 入 pending_score 队列，评分入口（score / 面板 / 控制台）
      增量优先消费（sync 不自动评分——LLM 成本由用户显式触发）。
    delta 缺失/oversized 时回退全量（幂等兜底）。索引不可用：ingest 照常
    完成，向量化跳过（delta 保留，下次 sync 补做）。向量化有失败时 delta
    不标记消费，下次 sync 重试。
    """
    from agentmemhub import adapters, config, engine_api, watermarks

    sources = [source] if source else [a.source for a in adapters.all_adapters()]
    run_ingest(sources, full=full)
    if not vectorize:
        return
    if engine_api.auth_state() is None:
        _stdout("记忆索引不可用（检查 database/session_rag.db 与 models/）——"
                "ingest 已完成，跳过向量化。")
        _cli_log("sync 跳过向量化（索引不可用）", level="warn")
        return
    data_dir = config.config().data_dir
    state = watermarks.load_state(data_dir)
    pending = watermarks.pending_for(state, "push")
    if full:
        pending = None                     # --full 强制全量

    # 内置引擎：清洗变更会话（注入事件不入库）→ keyset 水位向量化
    #（自带幂等，不依赖 delta）→ 消费登记；评分不自动跑（LLM 成本用户显式触发）
    store = Store()
    try:
        if pending is not None:
            pairs = [(c["source"], c["id"]) for c in pending]
            if pairs:
                deleted, convs = store.delete_system_events(conversations=pairs)
                if deleted:
                    _stdout(f"已清洗 {deleted} 条注入事件（{convs} 个会话）")
        r = _vectorize_stage(stdout=_stdout)
        if r.get("failed", 0) == 0:
            watermarks.mark_consumed(state, "push")
            if pending is not None:        # clean 只在 delta 模式实际执行过
                watermarks.mark_consumed(state, "clean")
            watermarks.save_state(data_dir, state)
        else:
            _stdout("存在失败——变更集未标记消费，下次 sync 会重试")
        _cli_log(f"sync → embedded={r.get('embedded', 0)}, "
                 f"skipped_known={r.get('skipped_known', 0)}, "
                 f"failed={r.get('failed', 0)}")
    finally:
        store.close()


def cmd_sync(args) -> None:
    run_sync(source=args.source, vectorize=not args.skip_vectorize, full=args.full)


def run_clean(store, *, source: str = "", apply: bool = False,
              stdout: Any = None,
              only: Optional[list[dict[str, str]]] = None) -> None:
    """记忆清洗：删除系统注入事件（is_system）。

    默认只预览（dry-run）；apply=True 才物理删除并重建 FTS/event_count。
    only：可选变更集 [{source, id}]——只清洗这些会话（watermarks delta
    模式）；None = 全库。apply 成功后登记 clean 消费水位。
    """
    from agentmemhub import config, watermarks
    out = stdout or _stdout

    def _mark_clean_consumed() -> None:
        """apply 成功后登记 clean 消费水位（失败不影响清洗结果）。"""
        try:
            st = watermarks.load_state(config.config().data_dir)
            watermarks.mark_consumed(st, "clean")
            watermarks.save_state(config.config().data_dir, st)
        except Exception:
            pass

    pairs = ([(c["source"], c["id"]) for c in only] if only else None)
    rows = store.system_event_counts(source or None, conversations=pairs)
    total = sum(r["n"] for r in rows)
    if not rows:
        out("（无系统注入事件——库已经干净）")
        if apply:
            _mark_clean_consumed()
        return
    out(f"系统注入事件共 {total} 条（按 source）:")
    for r in rows:
        out(f"  [{r['source']}] {r['n']} 条（{r['convs']} 个会话）")
    if not apply:
        out("以上为预览——加 --apply 才会物理删除（删除后重建 FTS 索引与会话计数）")
        return
    deleted, convs = store.delete_system_events(source or None, conversations=pairs)
    out(f"已删除 {deleted} 条注入事件（{convs} 个会话受影响，FTS/计数已重建）")
    _cli_log(f"clean(source={source or 'all'}, only={len(pairs) if pairs else 'ALL'})"
             f" → 删除 {deleted} 条注入事件")
    _mark_clean_consumed()


def cmd_clean(args) -> None:
    store = Store()
    try:
        run_clean(store, source=args.source, apply=args.apply)
    finally:
        store.close()


def cmd_rebuild(args) -> None:
    """补向量：repair=只给缺向量的记忆补嵌（rag 内置引擎）。

    rag 后端下写入即嵌入，全量重算由 `reembed --model` 负责；rebuild 模式
    在本引擎里等价于 repair（无历史向量需重算）。
    """
    from agentmemhub import engine_api, rag_bridge
    if engine_api.auth_state() is None:
        _stdout("记忆索引不可用——检查 database/session_rag.db 与 models/ 是否就绪")
        return

    def _emit(s: str) -> None:
        _stdout(f"  {s}")
    _stdout(f"补向量（{args.mode}，内置引擎）…")
    r = rag_bridge.rebuild_embeddings(mode=args.mode, on_progress=_emit)
    _stdout(f"完成: {r}")
    _cli_log(f"rebuild({args.mode}) → {r}")


def _csv_ids(v: str) -> set[str]:
    return {x.strip() for x in v.split(",") if x.strip()}


def cmd_score(args) -> None:
    """LLM 批量自动评分历史记忆（三轴评估 → feedback 写入）。"""
    if args.pending:
        from agentmemhub import config, watermarks
        from agentmemhub.scoring import run_score_all

        def _emit(s: str) -> None:
            _stdout(s)
        data_dir = config.config().data_dir
        st = watermarks.load_state(data_dir)
        ids = watermarks.take_pending_score(st)
        if not ids:
            _stdout("无待评分记忆（sync 推送成功后自动入队，此处消费）")
            return
        _stdout(f"待评分 {len(ids)} 条（pending_score 队列）…")
        try:
            r = run_score_all(emit=_emit,
                              dry_run=args.dry_run, workers=args.workers,
                              only_ids=set(ids))
            _stdout(f"评分完成: evaluated={r['evaluated']} skipped={r['skipped']} "
                    f"positive={r['positive']} neutral={r['neutral']} "
                    f"negative={r['negative']} errors={r['errors']}"
                    + (f" missing={r['missing']}" if r.get("missing") else "")
                    + ("（dry-run，未写入、队列保留）" if r["dryRun"] else ""))
            _cli_log(f"score --pending → {r}")
            if not r["dryRun"]:
                watermarks.save_state(data_dir, st)   # 消费并清空队列
        except Exception as e:
            _stdout(f"评分失败（队列保留，重跑 score --pending 即可重试）：{e}")
            _cli_log(f"score --pending 失败 → {e}", level="error")
        return
    if args.unscored_count:
        from agentmemhub.scoring import count_unscored
        try:
            n = count_unscored()
            _stdout(f"未评分记忆：{n} 条")
            _cli_log(f"score --unscored-count → {n}")
        except Exception as e:
            _stdout(f"统计失败：{e}")
            _cli_log(f"score --unscored-count 失败 → {e}", level="error")
        return
    if args.sync_episodes:
        from agentmemhub.scoring import sync_episode_r_task
        try:
            n = sync_episode_r_task()
            _stdout(f"已同步 {n} 个 episode 的 r_task（viewer 评分标签）")
            _cli_log(f"score --sync-episodes → {n}")
        except Exception as e:
            _stdout(f"同步失败：{e}")
            _cli_log(f"score --sync-episodes 失败 → {e}", level="error")
        return
    from agentmemhub.scoring import run_score_all, run_score_incremental

    def _emit(s: str) -> None:
        _stdout(s)
    try:
        if args.ids:
            r = run_score_all(emit=_emit,
                              limit=args.limit, dry_run=args.dry_run,
                              workers=args.workers, only_ids=args.ids)
            mode = "ids"
        else:
            # 默认走增量优先：pending_score 队列非空只评队列，空则全量筛未评定点读
            r = run_score_incremental(emit=_emit,
                                      limit=args.limit, dry_run=args.dry_run,
                                      workers=args.workers)
            mode = r.get("mode", "?")
        _stdout(f"评分完成[{mode}]: evaluated={r['evaluated']} skipped={r['skipped']} "
                f"positive={r['positive']} neutral={r['neutral']} "
                f"negative={r['negative']} errors={r['errors']}"
                + (f" missing={r['missing']}" if r.get("missing") else "")
                + ("（dry-run，未写入）" if r["dryRun"] else ""))
        _cli_log(f"score({mode}) → {r}")
    except Exception as e:
        _stdout(f"评分失败：{e}")
        _cli_log(f"score 失败 → {e}", level="error")


def cmd_distill(args) -> None:
    """记忆蒸馏：原始会话 → 结构化记忆（LLM 提炼，幂等 + fail-open）。

    详见 docs/memory-distillation.md；用 --dry-run 可先预览产物与成本。
    """
    from agentmemhub import rag_bridge
    from agentmemhub.distill import run_distill
    only = None
    if getattr(args, "only", ""):
        only = set()
        for item in args.only.split(","):
            item = item.strip()
            if ":" in item:
                s, c = item.split(":", 1)
                only.add((s.strip(), c.strip()))
        if not only:
            _stdout("--only 格式应为 source:conversation_id（逗号分隔多个）")
            return
    settings = rag_bridge.settings()
    mode = "dry-run（不落库）" if args.dry_run else "落库"
    _stdout(f"记忆蒸馏开始（{mode}）…")
    r = run_distill(settings, source=args.source, only=only,
                    limit=args.limit, dry_run=args.dry_run,
                    on_progress=_stdout)
    if r.get("error"):
        _stdout(f"蒸馏未执行：{r['error']}")
        _cli_log(f"distill 未执行 → {r['error']}", level="warn")
        return
    _stdout(json.dumps(r, ensure_ascii=False, indent=1))
    _cli_log(f"distill({mode}) → {r}")


def cmd_weight(args) -> None:
    """记忆手动加权：锁定价值分（优先于自动聚合值，且不随时间衰减）。

    ref 支持三种引用：内容锚（mcp_*）、蒸馏锚（dst_*）、unit 编号（unit:N 或纯数字）。
    不带 --value 时仅查看当前权重；--clear 清除手动设定（回到自动演化）。
    """
    import sqlite3
    from agentmemhub import rag_bridge
    from agentmemhub.rag import memstore
    st = rag_bridge.settings()
    conn = sqlite3.connect(str(st.index_db), timeout=15.0)
    try:
        memstore.ensure_memstore_schema(conn)   # 幂等迁移（如补 manual_value 列）
        uid = rag_bridge.resolve_unit_id(conn, args.ref)
        if uid is None:
            _stdout(f"未找到记忆：{args.ref}")
            return
        row = conn.execute(
            "SELECT value, manual_value FROM unit_values WHERE unit_id=?",
            (uid,)).fetchone()
        auto = row[0] if row else None
        manual = row[1] if row else None
        if args.clear:
            r = memstore.set_manual_value(conn, uid, None)
            _stdout(f"✓ 已清除手动加权（unit={uid}，回到自动值 "
                    f"{r['effective']:.2f}）")
        elif args.value is not None:
            if not 0.0 <= args.value <= 1.0:
                _stdout("--value 须在 0~1 之间")
                return
            r = memstore.set_manual_value(conn, uid, args.value)
            _stdout(f"✓ 已锁定权重 {args.value}（unit={uid}，"
                    f"自动值 {auto if auto is not None else '无'}）")
        else:
            state = f"手动={manual:.2f}" if manual is not None else "未手动加权"
            eff = manual if manual is not None else auto
            eff_s = f"{eff:.2f}" if eff is not None else "无"
            auto_s = f"{auto:.2f}" if auto is not None else "无"
            _stdout(f"unit={uid}  自动值={auto_s}  {state}  生效值={eff_s}")
        _cli_log(f"weight {args.ref} → value={args.value} clear={args.clear}")
    finally:
        conn.close()


def cmd_mcp(args) -> None:
    """启动 MCP 记忆网关。

    stdio 模式由 Agent host（ZCode/OpenCode 等）拉起子进程：stdin/stdout
    走 MCP 协议，不能向 stdout 打印任何非协议内容。--http 模式常驻为
    Streamable HTTP 服务（团队共享用，默认只监听本机）。
    """
    from agentmemhub.mcp_server import run_http, run_stdio
    if args.http:
        run_http(host=args.bind, port=args.port)
    else:
        run_stdio()


def cmd_wiki(args) -> int:
    """LLM Wiki 运维：查看失败清单、定向补跑失败项。

    补跑不该只能靠人敲 `scripts/wiki_*.py` —— 这里与服务层
    (`agentmemhub.wiki`) 共用同一份实现，面板走的是同一个入口。
    """
    import json as _json

    from agentmemhub import wiki

    # `--out` 不是所有动作都需要：triggers/trigger/backfill-manual 完全不用目录，
    # update 用 --l1/--l2，index-pages 可回落配置。此前 argparse 把它声明成
    # `required=True`，导致 `wiki --action trigger` 直接报"缺少 --out"而走不通
    # （实测踩到）。改为按动作校验。
    if args.action in ("failures", "align", "retry") and not args.out:
        # align 例外：它可以用 --l1/--l2（两级对齐），也可以什么都不给
        # （按配置的 wiki.out_l1 / wiki.out_l2 解析）—— 强制 --out 就永远
        # 只能对上单级，正是"L1 领先 L2 查不出来"的由来。
        if args.action == "align" and (args.l1 or args.l2):
            pass
        elif args.action == "align":
            pass                      # 交给 wiki.align 按配置解析；解析不到它自己会报
        else:
            print("需要 --out 指定 wiki 产出目录（动作：%s）" % args.action)
            return 2

    if args.action == "failures":
        s = wiki.failures_summary(args.out, args.stage)
        if not s["exists"]:
            print("未找到失败清单：%s" % s["log"])
            print("（说明还没跑过该产出目录，或那一轮没有失败）")
            return 0
        print("失败清单：%s" % s["log"])
        print("未解决 %d 条：" % s["total"])
        for k, n in s["by_kind"].items():
            mark = "   ← 需人工处理" if k in s["fatal"] else ""
            print("   %-10s %4d%s" % (k, n, mark))
        if s["stages"]:
            print("按阶段：")
            for st, label in wiki.STAGES.items():
                if s["stages"].get(st):
                    print("   %-11s %4d  %s" % (st, s["stages"][st], label))
        if s["needs_manual"]:
            print()
            print("❌ 存在 %s 类错误 —— 重试与继续跑都无意义，请先解决再补跑"
                  % "、".join(s["fatal"]))
        return 0

    if args.action == "align":
        r = wiki.align(args.out, args.stage, args.db,
                       l1_dir=args.l1, l2_dir=args.l2)
        if r.get("error"):
            print(r["error"])
            return 2
        for st, d in (r.get("dirs") or {}).items():
            print("对齐审计 %s：%s" % (st, d))
        print("索引库  ：%s" % r["db"])
        print("页面引用：%d 个 m-id / %d 个文件" % (r["refs"]["ids"], r["refs"]["files"]))
        inv = r["invalid_refs"]
        if inv["missing_total"] or inv["not_input_total"]:
            print("失效引用：%d 个不存在（库中已删）、%d 个已不是输入（转 merged/duplicate）"
                  % (inv["missing_total"], inv["not_input_total"]))
        cov = r["coverage"]
        if cov["ratio"] is not None:
            print("正向覆盖：%d/%d = %.1f%%（被至少一个页面引用的当前输入）"
                  % (cov["covered"], cov["current_inputs"], cov["ratio"] * 100))
        con = r.get("consistency") or {}
        if con.get("comparable"):
            verdict = {"ok": "✓ 两级同步",
                       "l1_ahead": "⚠️ **L2 落后于 L1**（页面里可能还是旧知识）",
                       "l2_ahead": "⚠️ L1 落后于 L2（少见）"}.get(
                           con["verdict"], con["verdict"])
            print("两级同步：%s（差值 %.1f 秒；输入 %s vs %s%s）"
                  % (verdict, con["skew_seconds"], con["l1_inputs"], con["l2_inputs"],
                     "，差 %+d" % con["inputs_delta"]
                     if con.get("inputs_delta") else ""))
        elif con.get("reason"):
            print("两级同步：无法比较 —— %s" % con["reason"])
        print()
        for st, s in r["stages"].items():
            if not s.get("manifest"):
                # **区分两种情况**，别让"文件在另一个目录"被说成"从来没编译过"
                # （早先那句"编译于该功能上线前"就是凭空断因，把人带偏）
                others = [d for k, d in (r.get("dirs") or {}).items() if k != st]
                hint = ("该阶段未参与本次审计（目录未提供）" if not others
                        else "该目录下确实没有 manifest_%s.json" % st)
                print("manifest_%s：缺失（%s）" % (st, s.get("path") or "?"))
                print("    %s —— 两级产出目录不同，要一起对请传 --l1/--l2" % hint)
                continue
            print("manifest_%s（%s 编译，输入 %d 条）：" % (st, s["generated_at"], s["n_inputs"]))
            print("   新增 %d / 内容变更 %d / 失去输入 %d（其中真删除 %d、状态迁移 %d）"
                  % (s["added_total"], s["changed_total"], s["removed_total"],
                     s["removed_total"] - len(s["removed_reclassified"]),
                     len(s["removed_reclassified"])))
            if s["dirty_session_total"]:
                print("   脏会话 %d 个（受影响的会话重编 L1，其所在域重编 L2）"
                      % s["dirty_session_total"])
                for sess, d in list(s["dirty_sessions"].items())[:10]:
                    print("     %-52s +%d ~%d -%d"
                          % (sess[:52], d["added"], d["changed"], d["removed"]))
                if s["dirty_session_total"] > 10:
                    print("     … 其余 %d 个见 JSON 输出" % (s["dirty_session_total"] - 10))
        print()
        print(r["message"])
        return 0

    # ---- pending / ignored / drop / restore（待更新记忆的查看与删除）----
    # **只处理尚未进 wiki 的记忆**：已进 wiki 的硬删会让页面里的 [m<id>] 变成
    # 死引用，那属于 docs/memory-deletion.md 的范畴，wiki_pending.drop 会拒绝。
    if args.action in ("pending", "ignored"):
        from agentmemhub import wiki_pending
        fn = (wiki_pending.pending if args.action == "pending"
              else wiki_pending.ignored)
        r = fn(l1_dir=args.out, limit=args.limit)
        if r.get("error"):
            print("错误：%s" % r["error"])
            return 2
        if args.action == "pending":
            print("待 wiki 更新：%d 条（新增 %d / 变更 %d）"
                  % (r["count"], r["added_total"], r["changed_total"]))
            if r.get("dirty_sessions"):
                print("脏会话：%s" % "、".join(r["dirty_sessions"]))
        else:
            print("已软删除（不进 wiki）：%d 条" % r["count"])
            if r.get("note"):
                print("（%s）" % r["note"])
        for it in r["items"]:
            print()
            print("  id=%s  %s/%s  type=%s  %d 字  %s"
                  % (it["id"], it["source"], it["conversation_id"],
                     it["type"], it["chars"],
                     time.strftime("%Y-%m-%d %H:%M",
                                   time.localtime(it["created_at"] or 0))))
            print("     %s" % it["summary"][:100])
        if not r["items"]:
            print("（无）")
        elif args.action == "pending":
            print()
            print("软删除示例：wiki --action drop --ids %s --mode soft"
                  % ",".join(str(x["id"]) for x in r["items"][:3]))
        return 0

    if args.action in ("drop", "restore"):
        from agentmemhub import wiki_pending
        try:
            ids = [int(x) for x in (args.ids or "").replace(" ", "").split(",") if x]
        except ValueError:
            print("--ids 必须是逗号分隔的整数，例：--ids 4388,4392")
            return 2
        if not ids:
            print("需要 --ids 指定记忆 id（逗号分隔），例：--ids 4388,4392")
            return 2
        if args.action == "restore":
            r = wiki_pending.restore(ids=ids, l1_dir=args.out)
        else:
            r = wiki_pending.drop(ids=ids, mode=args.mode,
                                  confirm=args.confirm, l1_dir=args.out)
        if r.get("error"):
            print("错误：%s" % r["error"])
            if r.get("hint"):
                print("提示：%s" % r["hint"])
            for x in (r.get("rejected") or []):
                print("  拒绝 id=%s：%s" % (x.get("id"), x.get("reason")))
            return 2
        if args.action == "restore":
            print("已取消忽略：%d 条" % r["restored"])
        else:
            print("已删除：%d 条（mode=%s）" % (r["dropped"], r["mode"]))
            if r.get("projections_removed"):
                print("  同时清理召回投影：%d 条" % r["projections_removed"])
        if r.get("note"):
            print("  %s" % r["note"])
        for x in (r.get("rejected") or []):
            print("  拒绝 id=%s：%s" % (x.get("id"), x.get("reason")))
        return 0

    # ---- triggers（触发器查看/手动触发）----
    if args.action == "triggers":
        from agentmemhub import wiki_triggers
        # `--set-batch on|off`：**热改**离线触发器总闸（写 overrides，不用重启）。
        # 离线那套配置与实时并列，判定顺序是"离线优先 → 实时兜底"。
        flip = getattr(args, "set_batch", "") or ""
        if flip:
            want = (flip == "on")
            wiki_triggers.set_overrides({"batch": {"enabled": want}})
            print("离线触发器已 %s（overrides.batch.enabled = %s）"
                  % ("打开" if want else "关闭", want))
            print()
        print(_json.dumps(wiki_triggers.status(), ensure_ascii=False, indent=2, default=str))
        return 0
    if args.action == "trigger":
        from agentmemhub import wiki_triggers
        res = wiki_triggers.run_manual(force=args.force)
        print(_json.dumps(res, ensure_ascii=False, indent=2, default=str))
        return 0

    # ---- index-pages（页面层投影：L2 知识页 → 召回面）----
    if args.action == "index-pages":
        from agentmemhub import wiki_index
        from agentmemhub.rag.config import load_settings
        from agentmemhub.rag.ingest import open_index
        from agentmemhub import config as _cfg
        l2 = args.out or (_cfg.config().wiki.get("out_l2") or "")
        if not l2:
            print("需要 --out 指定 L2 产出目录（或在 yaml 配置 wiki.out_l2）")
            return 2
        idx = open_index(load_settings().index_db)
        try:
            r = wiki_index.project_pages(idx, wiki_index.page_roots(l2))
        finally:
            idx.close()
        print(_json.dumps(r, ensure_ascii=False, indent=2))
        print("页面层已进召回面：检索时与记忆、原始对话一起融合返回（kind=page）")
        return 0

    # ---- backfill-manual（历史 Agent 直写记忆回填蒸馏表）----
    if args.action == "backfill-manual":
        from agentmemhub import distill as _distill
        from agentmemhub.rag.config import load_settings
        from agentmemhub.rag.ingest import open_index
        idx = open_index(load_settings().index_db)
        try:
            res = _distill.backfill_agent_memories(idx)
        finally:
            idx.close()
        print("回填完成：", _json.dumps(res, ensure_ascii=False))
        print("（幂等，可重复执行；落账条目将由下次蒸馏统一补投影）")
        return 0

    # ---- update（增量更新）----
    if args.action == "update":
        if not (args.l1 and args.l2):
            print("增量更新需要 --l1（第一级产出目录）与 --l2（第二级产出目录）")
            return 2
        import json as _j

        from agentmemhub import wiki_triggers
        # 状态文件留痕：CLI 直调与面板、触发器共用同一套标记（详见
        # `wiki_triggers.mark_update_started` 的注释）
        wiki_triggers.mark_update_started("manual")
        try:
            res = wiki.update(l1_dir=args.l1, l2_dir=args.l2, db=args.db,
                              workers=args.workers)
        except Exception as e:                  # noqa: BLE001 —— 留痕后原样抛出
            wiki_triggers.mark_update_failed(e)
            raise
        wiki_triggers.mark_update_finished(
            hook="manual", updated=bool((res or {}).get("updated")))
        print(_j.dumps(res, ensure_ascii=False, indent=2, default=str))
        return 0

    # ---- full-run（全量重跑：重新归域 + 全部域重编 + 覆盖写盘）----
    if args.action == "full-run":
        from agentmemhub import batch_ops
        mode = "offline" if args.offline else batch_ops.full_mode()
        try:
            res = batch_ops.full_run(mode=mode, db=args.db, workers=args.workers)
        except batch_ops.BatchOpsError as e:
            print(str(e))                       # 未配产出目录 / 未配凭据
            return 2
        print(_json.dumps(res, ensure_ascii=False, indent=2, default=str))
        print("\n通道：%s（配置位 wiki.full.mode，可用 --offline 覆盖）" % mode)
        return 0

    # ---- batch-*（离线批处理的运维动作）----
    if args.action in ("batch-status", "batch-run", "batch-cancel",
                       "batch-resubmit"):
        return _cmd_wiki_batch(args)

    # ---- retry ----
    if args.stage == "l2" and not args.src:
        print("第二级补跑需要 --src（第一级产出目录）")
        return 2
    print("开始定向补跑（只跑失败项，不全量重来）…")
    if args.stage == "l1":
        res = wiki.retry_failed(stage="l1", out_dir=args.out,
                                workers=args.workers)
    elif args.stage == "l2":
        res = wiki.retry_failed(stage="l2", out_dir=args.out, src=args.src,
                                workers=args.workers)
    else:
        res = wiki.retry_all(out_dir=args.out, src=args.src,
                             workers=args.workers)
    print(_json.dumps(res, ensure_ascii=False, indent=2, default=str))
    return 0


def _cmd_wiki_batch(args) -> int:
    """离线批处理（MiMo Batch API）：查状态 / 推进一段 / 取消作废 / 重提。

    与实时路径的关系：请求怎么切分、编号怎么映射、页面怎么组装**全部共用**
    （见 `scripts/wiki_aggregate.py`），差别只在"谁执行、什么时候执行"。

    `batch-run` 之所以是一个动作而不是"提交 / 回读"两个：状态机会自己判断该做
    哪一段 —— 无在飞就提交、结果到了就交下一段或落盘。

    **装配与动作都在 `agentmemhub/batch_ops.py`**（CLI 与 HTTP 面板共用），
    这里只负责取参与渲染。
    """
    import json as _j

    from agentmemhub import batch_ops

    batch_ops.set_paths(args.l1, args.l2)   # CLI 的 --l1/--l2 覆盖（面板走配置）

    try:
        if args.action == "batch-status":
            r = batch_ops.status()
            if not r["n"]:
                print("没有在飞的离线任务 —— 用 `agentmemhub wiki --action batch-run` 发起一轮")
                return 0
            print("在飞的离线任务（%d 个）：" % r["n"])
            for row in r["inflight"]:
                cnt = row.get("counts") or {}
                print("  %s  stage=%-9s 厂商状态=%-11s 进度=%s/%s  已等 %.0f 分钟  建议=%s"
                      % (row["batch_id"], row["stage"], row["provider_status"] or "?",
                         cnt.get("completed", row["results"]),
                         cnt.get("total", row["items"]),
                         row["waited_s"] / 60.0, row["suggestion"]))
                if row.get("output_file_id"):
                    print("     成功文件：%s" % row["output_file_id"])
                if row.get("error_file_id"):
                    print("     错误文件：%s" % row["error_file_id"])
                if row.get("error"):
                    print("     ⚠️ 查状态失败：%s" % row["error"])
            print("\n下一步：`agentmemhub wiki --action batch-run`"
                  "（状态机会自己判断推进哪一段）")
            return 0

        if args.action == "batch-cancel":
            r = batch_ops.cancel()
            if not r["cancelled"]:
                print("没有在飞的离线任务")
                return 0
            for d in r["cancelled"]:
                print("  已取消并作废：%s（%d 个域回到待更新池）"
                      % (d["batch_id"], d["domains"]))
            for e in r["errors"]:
                print("  取消失败（本地标记仍作废）：%s —— %s"
                      % (e["batch_id"], e["error"]))
            _cli_log("wiki batch-cancel：作废 %d 个离线任务" % len(r["cancelled"]))
            return 0

        if args.action == "batch-resubmit":
            done = batch_ops.resubmit()["resubmitted"]
            if not done:
                print("没有需要重提的已终局任务（还在飞的任务不会被重建）")
                return 0
            for d in done:
                print("  重提 %s：%s → %s（%d 条请求，复用输入文件 %s）"
                      % (d["stage"], d["old"], d["new"], d["n_requests"],
                         d["input_file_id"]))
            print("\n用原输入文件重建，**不重跑 L1/归域**（不额外花实时的钱）")
            _cli_log("wiki batch-resubmit：重建 %d 个任务" % len(done))
            return 0

        # batch-run：推进一段
        res = batch_ops.run(deadline_s=float(getattr(args, "deadline", 0) or 0),
                            db=args.db, workers=args.workers)
    except batch_ops.BatchOpsError as e:
        print(str(e))
        return 2

    print(_j.dumps(res, ensure_ascii=False, indent=2, default=str))
    step = res.get("step") or ""
    if not step:
        # 走的是 `update()` 的正常流程（提交轮），标签要取**厂商那边的 stage**，
        # 不能写死成 plan —— 默认"只离线编译"时它是 l2compile（2026-10-01 实测：
        # 写死 plan_submitted 会把"提交了 250 条编译请求"说成细分，误导排查）。
        sub = (res.get("l2") or {}).get("offline_submitted") or {}
        step = ("submitted:%s" % sub.get("stage")) if sub.get("stage") else ""
    print("\n本次推进：%s" % (step or "(未推进)"))
    _cli_log("wiki batch-run → %s" % (step or "未推进"))
    return 0


def cmd_memory_log(args) -> int:
    """记忆操作事实流查询：什么时候写了/读了什么记忆（`logs/memory.log`）。

    与 `logs/mcp.log` 的分工：那份记**协议层**（谁调了什么工具、耗时、成败），
    这份记**数据层**（哪条记忆被写/读，内容是什么、从哪条路径来）。查"某条记忆
    的来龙去脉"用这份；查"Agent 调了什么、失败没有"用 `scripts/mcp_log.py`。
    """
    import json as _json
    import time as _time
    from agentmemhub import logs as _logs

    rows = _logs.read_memory_audit()
    if args.event:
        rows = [r for r in rows if r.get("event") == args.event]
    if args.path:
        rows = [r for r in rows if (r.get("path") or "") == args.path]
    if args.grep:
        kw = args.grep.lower()
        rows = [r for r in rows
                if kw in _json.dumps(r, ensure_ascii=False).lower()]
    if args.limit:
        rows = rows[-args.limit:]
    if args.json:
        print(_json.dumps(rows, ensure_ascii=False, indent=2, default=str))
        return 0
    if not rows:
        print("无匹配的记忆操作记录（%s）" % _logs.memory_audit_file())
        return 0
    for r in rows:
        ts = _time.strftime("%m-%d %H:%M:%S",
                            _time.localtime(float(r.get("ts") or 0)))
        ev = r.get("event")
        if ev == "write":
            print("%s [写·%s] id=%s %s/%s %s ← %s  (%d 字)" % (
                ts, r.get("path") or "?", r.get("memory_id"),
                r.get("source") or "?", r.get("type") or "?",
                (r.get("content") or "").replace("\n", " ")[:64],
                r.get("result") or "", r.get("chars") or 0))
        elif ev == "read":
            print("%s [读·%s] q=%r 命中 %s 条 %s" % (
                ts, r.get("path") or "?", (r.get("query") or "")[:36],
                r.get("hits"), r.get("kinds") or ""))
        else:
            print("%s [%s] %s" % (ts, ev,
                                  _json.dumps(r, ensure_ascii=False)[:160]))
    print("\n共 %d 条（%s）" % (len(rows), _logs.memory_audit_file()))
    return 0


def cmd_snapshot(args) -> int:
    """快照与回滚：索引库整库（含蒸馏表/units/评分）+ wiki 产物目录。"""
    import json as _json
    from agentmemhub import snapshot

    if args.snapshot_action == "create":
        r = snapshot.create(reason=args.reason or "手动")
        print(_json.dumps(r, ensure_ascii=False, indent=2))
        return 0
    if args.snapshot_action == "list":
        snaps = snapshot.list_snapshots()
        if not snaps:
            print("暂无快照（保留上限 %d 份）" % snapshot.keep_count())
            return 0
        for s in snaps:
            print("%s  %s  %6.1f MB  %s" % (
                s["id"], (s["reason"] or "")[:24], s["size_mb"],
                "、".join(s["parts"])))
        print("共 %d 份；保留上限 %d 份（超出自动删最旧，改配置 snapshot.keep 调整）"
              % (len(snaps), snapshot.keep_count()))
        return 0
    if args.snapshot_action == "restore":
        if not args.snapshot_id:
            print("restore 需要 --snapshot-id（用 snapshot --snapshot-action list 查看可用快照）")
            return 2
        r = snapshot.restore(args.snapshot_id, wiki_only=args.wiki_only,
                             db_only=args.db_only)
        print(_json.dumps(r, ensure_ascii=False, indent=2))
        print("回滚完成。当前状态已自动保存为保护快照：", r["guard_snapshot"])
        return 0
    return 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agentmemhub", description="AgentMemHub 统一 Agent 会话提取")
    sub = p.add_subparsers(dest="command")

    pi = sub.add_parser("ingest", help="提取所有 adapter 并入库（默认会话级增量）")
    pi.add_argument("--source", default="")
    pi.add_argument("--signature", default="")
    pi.add_argument("--full", action="store_true",
                    help="强制全量重建（整源清空重写；默认增量只重读变化会话）")

    pl = sub.add_parser("list", help="列出会话")
    pl.add_argument("--source", default="")

    ps = sub.add_parser("show", help="查看会话（Markdown）")
    ps.add_argument("source")
    ps.add_argument("id")

    pq = sub.add_parser("search", help="搜索事件正文")
    pq.add_argument("query")
    pq.add_argument("--source", default="")
    pq.add_argument("--role", default="")
    pq.add_argument("--limit", type=int, default=20)

    pe = sub.add_parser("export", help="导出")
    pe.add_argument("--format", choices=["jsonl", "markdown"], default="jsonl")
    pe.add_argument("--source", default="")
    pe.add_argument("--out", default="exports")

    pst = sub.add_parser("stats", help="统计")
    pa = sub.add_parser("adapters", help="列出 adapter 状态")

    pf = sub.add_parser("folders", help="按文件夹统计各 Agent 会话数")
    pf.add_argument("--source", default="")
    pf.add_argument("--limit", type=int, default=20)

    pv = sub.add_parser("serve", help="启动本地 Web 页面（可选功能）")
    pv.add_argument("--port", type=int, default=8086)
    pv.add_argument("--db", default="", help="数据库路径（默认 <项目根>/database/agentmemhub.db）")
    # 成对开关：--open / --no-open（默认自动打开浏览器）
    pv.add_argument("--open", action=argparse.BooleanOptionalAction, default=True,
                    help="启动后自动打开浏览器（默认开启；用 --no-open 关闭）")

    pmc = sub.add_parser("mcp", help="启动 MCP 记忆网关（stdio 默认；--http 转 Streamable HTTP 常驻）")
    pmc.add_argument("--http", action="store_true",
                     help="以 Streamable HTTP 模式常驻（默认 stdio 由 Agent 拉起）")
    pmc.add_argument("--port", type=int, default=9100, help="HTTP 监听端口（默认 9100）")
    pmc.add_argument("--bind", default="127.0.0.1",
                     help="HTTP 监听地址（默认仅本机；团队共享用 0.0.0.0）")

    psy = sub.add_parser("sync", help="增量同步：ingest 增量 → 清洗变更会话 → 向量化写入记忆索引")
    psy.add_argument("--source", default="")
    psy.add_argument("--skip-vectorize", dest="skip_vectorize", action="store_true",
                     help="只采集入库并清洗系统注入事件，跳过向量化阶段")
    psy.add_argument("--full", action="store_true",
                     help="强制全量同步（默认按 watermarks 变更集增量处理）")

    pcl = sub.add_parser("clean", help="记忆清洗：删除系统注入事件（is_system；默认预览，--apply 执行）")
    pcl.add_argument("--source", default="")
    pcl.add_argument("--apply", action="store_true",
                     help="执行删除（不带此参数仅统计预览；删除会重建 FTS 与会话计数）")

    psc = sub.add_parser("score", help="LLM 批量自动评分历史记忆（三轴评估 → feedback 写入，检索排序生效）")
    psc.add_argument("--pending", action="store_true",
                     help="只评分 sync 处理后入队的 trace（pending_score 队列，"
                          "成功后消费清空；dry-run 不清空）")
    psc.add_argument("--limit", type=int, default=0, help="最多评分条数（0=全部）")
    psc.add_argument("--dry-run", action="store_true", help="只评估不写入（预览 verdict 分布）")
    psc.add_argument("--workers", type=int, default=4, help="并发评估线程数（默认 4；IO 密集建议 4~8）")
    psc.add_argument("--ids", type=_csv_ids, default=None,
                     help="只评分指定 trace id（逗号分隔，如 --ids trac_1,trac_2；配合写后即评/锚点）")
    psc.add_argument("--unscored-count", action="store_true",
                     help="只统计未评分记忆条数（供定时/定量触发判断），不做 LLM 评估")
    psc.add_argument("--sync-episodes", action="store_true",
                     help="只把各 episode 的 r_task 同步为受评 traces 的平均 value（让 viewer 评分标签显示真实分），不做 LLM 评估")

    prb = sub.add_parser("rebuild", help="补向量：给缺向量的记忆补嵌（repair；rag 写入即嵌入）")
    prb.add_argument("--mode", default="repair", choices=("repair", "rebuild"))

    pw = sub.add_parser("weight", help="记忆手动加权：锁定价值分（优先于自动聚合、不衰减）")
    pw.add_argument("ref", help="记忆引用：mcp_* / dst_* / unit:N（见面板或检索结果）")
    pw.add_argument("--value", type=float, default=None,
                    help="权重 0~1；不传=仅查看当前权重")
    pw.add_argument("--clear", action="store_true", help="清除手动加权（回到自动演化）")

    pd = sub.add_parser("distill", help="记忆蒸馏：原始会话 → 结构化记忆（LLM 提炼，幂等）")
    pd.add_argument("--source", default="", help="只处理某个 Agent 来源")
    pd.add_argument("--only", default="",
                    help="只处理指定会话（source:conversation_id，逗号分隔多个）")
    pd.add_argument("--limit", type=int, default=0,
                    help="最多处理多少个会话（按正文量降序，0=全部）")
    pd.add_argument("--dry-run", action="store_true",
                    help="只蒸馏不落库（预览产物与成本，可反复执行）")

    _register_snapshot_parser(sub)
    _register_memory_log_parser(sub)
    _register_llm_config_parser(sub)

    pfeed = sub.add_parser(
        "feed", help="外部投喂：把外部素材转译成统一 md 并登记（含素材库查询）")
    # 动作既支持自然写法 `feed add <文件>`，也支持项目惯例 `feed --action add`
    # —— 两种都收在 `args` 里再解析（用 `--action` 单独占一个 dest 会与位置参数打架）
    pfeed.add_argument("args", nargs="*",
                       help="动作 + 参数：add <文件…> / list / show <doc_id> / "
                            "retry <doc_id> / retract <doc_id> / forget <doc_id> / "
                            "lint / scan <目录> / batch <目录…> / formats")
    pfeed.add_argument("--action", default="",
                       choices=["", "add", "list", "show", "retry",
                                "retract", "forget", "lint",
                                "scan", "batch", "formats"],
                       help="动作（与位置写法等价；项目惯例写法）")
    pfeed.add_argument("--id", default="", help="show/retry/retract/forget 用：doc_id")
    pfeed.add_argument("--purge", action="store_true",
                       help="forget 用：**连原件一起删**（默认保留原件，因为它不可再生）")
    pfeed.add_argument("--stdin", action="store_true",
                       help="add 用：**兜底通路** —— 从标准输入读正文直接投喂"
                            "（反爬/另存失败时用）；配合 --title / --source-ref")
    pfeed.add_argument("--title", default="", help="--stdin 用：标题")
    pfeed.add_argument("--source-ref", default="",
                       help="--stdin 用：真实来源（URL 或文件路径），作溯源值")
    pfeed.add_argument("--kind", default="",
                       help="显式指定类型（text/url/wechat/pdf/image）")
    pfeed.add_argument("--force", action="store_true",
                       help="add/retry 用：同内容已投喂过也重跑")
    pfeed.add_argument("--from-raw", action="store_true",
                       help="retry 用：强制用 raw/ 里的原件重转（不碰原始来源）")
    pfeed.add_argument("--dry-run", action="store_true",
                       help="add 用：只做查重与认领，不写盘")
    pfeed.add_argument("--status", default="", help="list 用：按状态筛")
    pfeed.add_argument("--low-quality", action="store_true",
                       help="list 用：只列质量偏低或有告警的")
    pfeed.add_argument("--limit", type=int, default=0,
                       help="list/scan/batch 用：最多几条（0=全部）")
    pfeed.add_argument("--apply", action="store_true",
                       help="batch 用：**真的投喂**。不加则只扫描列清单（默认只看不写）")
    pfeed.add_argument("--no-recursive", action="store_true",
                       help="scan/batch 用：只扫目录第一层")
    pfeed.add_argument("--set-formats", default="",
                       help="formats 用：设置运行时允许的格式（逗号分隔，如 "
                            "'.pdf,.docx'）；传 'clear' 清除覆盖回到配置/默认")
    pfeed.add_argument("--json", action="store_true", help="以 JSON 输出（便于脚本消费）")
    pwk = sub.add_parser("wiki", help="LLM Wiki 运维：对齐审计 / 增量更新 / 查看失败清单 / 定向补跑")
    pwk.add_argument("--action", default="failures",
                     choices=["failures", "retry", "align", "update",
                              "triggers", "trigger", "backfill-manual",
                              "index-pages", "pending", "drop", "ignored",
                              "restore",
                              "batch-status", "batch-run", "batch-cancel",
                              "batch-resubmit", "full-run"],
                     help="failures=查看失败清单（默认）；retry=只重跑失败项；"
                          "align=审计 wiki 产物与索引库的差距（只读）；"
                          "update=增量更新（只重编脏会话 L1 与受影响 L2 域）；"
                          "triggers=查看触发器配置与状态；trigger=手动执行一次触发检测；"
                          "backfill-manual=历史 Agent 直写记忆回填蒸馏表（幂等）；"
                          "index-pages=L2 知识页投影进召回面（页级召回）；"
                          "pending=待更新记忆明细；drop=删除待更新记忆"
                          "（--mode soft=不进 wiki / hard=真删）；"
                          "ignored=已软删除的记忆；restore=取消软删除；"
                          "batch-status=离线批处理任务状态（只读）；"
                          "batch-run=离线模式推进一段（提交/交编译/落盘，状态机自判）；"
                          "batch-cancel=取消并作废所有在飞离线任务；"
                          "batch-resubmit=用**原输入文件**重建已终局的任务"
                          "（平台侧 validation_failed 时用，不重跑 L1/归域）")
    pwk.add_argument("--out", default="",
                     help="wiki 产出目录（failures/align/retry 必需；"
                          "其余从配置或 --l1/--l2 读）")
    pwk.add_argument("--set-batch", default="", choices=["", "on", "off"],
                     help="triggers 用：热改离线触发器总闸（写 overrides，不用重启）。"
                          "判定顺序：离线优先 → 实时兜底")
    pwk.add_argument("--stage", default="", choices=["", "l1", "l2"],
                     help="限定阶段：l1=第一级；l2=第二级；留空=全部")
    pwk.add_argument("--src", default="", help="第二级补跑必需：第一级产出目录")
    pwk.add_argument("--l1", default="", help="update/align 用：第一级产出目录")
    pwk.add_argument("--l2", default="", help="update/align 用：第二级产出目录（与 --l1 同时给 对齐两级的同步性）")
    pwk.add_argument("--workers", type=int, default=0, help="并发数（0=用配置）")
    pwk.add_argument("--db", default="", help="索引库路径（align/update 用；默认读配置）")
    pwk.add_argument("--force", action="store_true",
                     help="trigger 用：绕过规则强制执行一次增量更新")
    pwk.add_argument("--deadline", type=float, default=0.0,
                     help="batch-run 用：等待厂商的预算秒数（0=不限时，默认）")
    pwk.add_argument("--offline", action="store_true",
                     help="full-run 用：强制走离线通道（覆盖 wiki.full.mode）")
    pwk.add_argument("--ids", default="",
                     help="drop/restore 用：记忆 id 列表，逗号分隔（如 4388,4392）")
    pwk.add_argument("--mode", default="soft", choices=["soft", "hard"],
                     help="drop 用：soft=只标记不进 wiki（可恢复，默认）；"
                          "hard=真删（蒸馏表+投影+向量，不可恢复）")
    pwk.add_argument("--confirm", action="store_true",
                     help="drop --mode hard 必须显式加上（二次确认）")
    pwk.add_argument("--limit", type=int, default=0,
                     help="pending/ignored 用：最多显示几条（0=全部）")
    return p


def _harvest_quietly() -> None:
    """顺手**收割一次异步任务**（旁路：永不抛、不打印、不改命令行为）。

    为什么挂在 CLI 入口而不是逐个 wiki 动作：离线批次完成时总要有人回读，而项目
    刻意不做常驻轮询 —— 挂在"用户跑命令"这个天然事件上，**任何一条命令**都会顺带
    检查。队列空时零成本立即返回（只读一次本地文件，不碰厂商），不影响启动时间。

    放在 `parse_args()` **之后**：`--help` 会 `SystemExit`，不该有副作用。
    """
    try:
        from agentmemhub import wiki_triggers
        wiki_triggers.harvest_now()
    except Exception:                       # noqa: BLE001 —— 旁路
        pass


def cmd_feed(args) -> int:
    """外部投喂（`feed`）：转译 → 登记 → 发布，以及素材库查询。

    四个动作全部走 `agentmemhub.feed.service` 的同一份编排 —— CLI / MCP / 面板
    只做参数解析与呈现，绝不在各自那里重写流程（wiki 上踩过"两处流程对不齐"的坑）。
    """
    import json as _json

    from agentmemhub import feed as _feed
    from agentmemhub.feed import service as _svc

    action = (getattr(args, "action", "") or "").strip()
    as_json = bool(getattr(args, "json", False))
    # 位置写法（`feed add X`）与 `--action add` 等价：首 token 命中动作名就取走。
    # 不这么做的话 `feed add X` 会把 "add" 当成**文件路径**去投喂 —— 能跑、
    # 但把用户意图理解错了（实测：报"找不到文件：add"，随后又真的投喂了 X）。
    tokens = [str(t) for t in (getattr(args, "args", []) or [])]
    _ACTIONS = ("add", "list", "show", "retry", "retract", "forget", "lint",
                "scan", "batch", "formats")
    if not action and tokens and tokens[0] in _ACTIONS:
        action = tokens.pop(0)
    action = action or "add"
    doc_id_arg = (getattr(args, "id", "") or "").strip()
    if not doc_id_arg and action in ("show", "retry", "retract", "forget") and tokens:
        doc_id_arg = tokens.pop(0)

    if action == "add":
        # **兜底通路**：从 stdin 读正文（反爬失败 / 只拿到正文时用）
        if getattr(args, "stdin", False):
            import sys as _sys
            text = _sys.stdin.read()
            r = _svc.ingest_text(text,
                                 title=getattr(args, "title", "") or "",
                                 source_ref=getattr(args, "source_ref", "") or "")
            if as_json:
                print(_json.dumps(r, ensure_ascii=False, indent=2))
            return 0 if r.get("ok") else 1
        paths = [p for p in tokens if p.strip()]
        if not paths:
            print("用法：agentmemhub feed add <文件路径> [更多路径…] "
                  "[--kind text|pdf|docx|pptx|xmind|html] [--dry-run] [--force]")
            return 2
        out = []
        failed = 0
        for p in paths:
            r = _svc.ingest(p, kind=getattr(args, "kind", "") or "",
                            force=bool(getattr(args, "force", False)),
                            dry_run=bool(getattr(args, "dry_run", False)))
            out.append(r)
            if not r.get("ok"):
                failed += 1
        if as_json:
            print(_json.dumps(out, ensure_ascii=False, indent=2))
        elif len(out) > 1:
            print("\n合计：成功 %d / 失败 %d" % (len(out) - failed, failed))
        return 1 if failed else 0

    if action == "formats":
        # 格式白名单：查看 / 调整（用户要求"通过调整文档格式参数控制哪些数据支持投喂"）
        from agentmemhub.feed import formats as _fmt
        spec = (getattr(args, "set_formats", "") or "").strip()
        if spec:
            if spec.lower() == "clear":
                st = _fmt.clear_overrides()
            else:
                st = _fmt.set_overrides([e for e in spec.split(",") if e.strip()])
        else:
            st = _fmt.state()
        if as_json:
            print(_json.dumps(st, ensure_ascii=False, indent=2))
            return 0
        print("当前允许投喂的格式（来源：%s）：" % st["source"])
        print("  " + "、".join(st["allowed"]) if st["allowed"] else "  （空）")
        print("  转换器支持：%s" % "、".join(st["supported"]))
        print("  覆盖文件：%s" % st["stateFile"])
        return 0

    if action == "scan":
        roots = [p for p in tokens if p.strip()]
        if not roots:
            print("用法：agentmemhub feed scan <目录或文件> [更多路径…] "
                  "[--no-recursive] [--limit N] [--json]")
            return 2
        res = _svc.scan_targets(roots=roots,
                                recursive=not bool(getattr(args, "no_recursive", False)),
                                limit=int(getattr(args, "limit", 0) or 0))
        if as_json:
            print(_json.dumps(res, ensure_ascii=False, indent=2))
            return 0
        print("扫描：可投喂 %d / 被格式挡掉 %d%s"
              % (res["allowedCount"], res["skippedCount"],
                 "（已按 --limit 截断）" if res["truncated"] else ""))
        for ext, n in sorted(res["byExt"].items(), key=lambda x: -x[1]):
            print("    %-14s %d" % (ext, n))
        for s in res["skipped"][:10]:
            print("    [挡] %s（%s）" % (s["path"], s["reason"]))
        return 0

    if action == "batch":
        roots = [p for p in tokens if p.strip()]
        if not roots:
            print("用法：agentmemhub feed batch <目录或文件> [更多路径…] "
                  "[--apply] [--dry-run] [--force] [--no-recursive] [--limit N]")
            print("      **不加 --apply 时只扫描列清单**（默认只看不写）")
            return 2
        recursive = not bool(getattr(args, "no_recursive", False))
        limit = int(getattr(args, "limit", 0) or 0)
        if not getattr(args, "apply", False):
            # 默认只看不写：批量影响面大，且用户明确"未筛选的数据禁止入库"
            res = _svc.scan_targets(roots=roots, recursive=recursive, limit=limit)
            if as_json:
                print(_json.dumps(res, ensure_ascii=False, indent=2))
                return 0
            print("（只扫描，未投喂）可投喂 %d / 被格式挡掉 %d%s"
                  % (res["allowedCount"], res["skippedCount"],
                     "（已按 --limit 截断）" if res["truncated"] else ""))
            for ext, n in sorted(res["byExt"].items(), key=lambda x: -x[1]):
                print("    %-14s %d" % (ext, n))
            print("确认无误后加 `--apply` 真的投喂"
                  "（想连盘都不写就再加 `--dry-run` 先走一遍全流程）")
            return 0
        out = _svc.ingest_batch(roots=roots, recursive=recursive,
                                force=bool(getattr(args, "force", False)),
                                dry_run=bool(getattr(args, "dry_run", False)),
                                limit=limit)
        if as_json:
            print(_json.dumps(out, ensure_ascii=False, indent=2))
        else:
            print("批量投喂：成功 %d / 幂等跳过 %d / 失败 %d（共 %d）%s"
                  % (out["ok"], out["skipped"], out["failed"], out["total"],
                     "（dry-run 未写盘）" if out["dryRun"] else ""))
            for e in out["errors"][:10]:
                print("    [x] %s：[%s] %s"
                      % (e["path"], e["error_kind"], e["error"][:80]))
        return 1 if out["failed"] else 0

    if action == "list":
        rows = _svc.list_materials(
            status=getattr(args, "status", "") or "",
            low_quality=bool(getattr(args, "low_quality", False)),
            limit=int(getattr(args, "limit", 0) or 0))
        if as_json:
            print(_json.dumps(rows, ensure_ascii=False, indent=2))
            return 0
        if not rows:
            print("（没有已投喂的素材）")
            return 0
        print("已投喂素材 %d 条：" % len(rows))
        print("  %-17s %-6s %-10s %-6s %s" % ("doc_id", "kind", "status",
                                              "质量", "标题"))
        for r in rows:
            print("  %-17s %-6s %-10s %-6.2f %s"
                  % (r.get("doc_id", ""), r.get("kind", ""), r.get("status", ""),
                     float(r.get("quality") or 0.0), (r.get("title") or "")[:40]))
            if r.get("warnings"):
                print("      ⚠️ %s" % "；".join(r["warnings"])[:100])
            if r.get("error"):
                print("      ❌ [%s] %s" % (r.get("error_kind") or "", r["error"][:100]))
        return 0

    if action == "show":
        doc_id = doc_id_arg
        if not doc_id:
            print("用法：agentmemhub feed show <doc_id>（或 --id <doc_id>）")
            return 2
        row = _svc.get_material(doc_id)
        if not row:
            print("[x] 没有这条素材：%s" % doc_id)
            return 1
        if as_json:
            print(_json.dumps(row, ensure_ascii=False, indent=2))
            return 0
        import time as _t
        print("doc_id    : %s" % row.get("doc_id"))
        print("标题      : %s" % (row.get("title") or ""))
        print("状态      : %s%s" % (row.get("status"),
                                    ("（%s）" % row["error"]) if row.get("error") else ""))
        print("类型/转换 : %s / %s" % (row.get("kind"), row.get("converter")))
        print("来源      : %s" % row.get("source_ref"))
        print("文档时间  : %s" % _t.strftime("%Y-%m-%d %H:%M",
                                            _t.localtime(int(row.get("doc_time") or 0))))
        print("质量      : %.2f%s" % (float(row.get("quality") or 0.0),
                                      ("  ⚠️ " + "；".join(row["warnings"]))
                                      if row.get("warnings") else ""))
        print("原件      : %s" % row.get("raw_path"))
        print("镜像      : %s" % (row.get("mirror_path") or "（无）"))
        print("交付物    : %s" % (row.get("kb_path") or "（无）"))
        return 0

    if action == "retract":
        doc_id = doc_id_arg
        if not doc_id:
            print("用法：agentmemhub feed retract <doc_id>")
            return 2
        r = _svc.retract(doc_id)
        if as_json:
            print(_json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if r.get("ok") else 1

    if action == "forget":
        doc_id = doc_id_arg
        if not doc_id:
            print("用法：agentmemhub feed forget <doc_id> [--purge]"
                  "（--purge 连原件一起删；默认保留原件）")
            return 2
        r = _svc.forget(doc_id, purge=bool(getattr(args, "purge", False)))
        if as_json:
            print(_json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if r.get("ok") else 1

    if action == "lint":
        from agentmemhub.feed.lint import format_report
        r = _svc.lint()
        if as_json:
            print(_json.dumps(r, ensure_ascii=False, indent=2))
        else:
            print(format_report(r))
        # P0 > 0 ⇒ 非零退出码（可直接当提交/巡检门槛，照 lint.py 的做法）
        return 1 if r.get("p0") else 0

    if action == "retry":
        doc_id = doc_id_arg
        if not doc_id:
            print("用法：agentmemhub feed retry <doc_id>（或 --id <doc_id>）")
            return 2
        r = (_svc.reconvert(doc_id) if getattr(args, "from_raw", False)
             else _svc.retry(doc_id, force=bool(getattr(args, "force", False))))
        if as_json:
            print(_json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if r.get("ok") else 1

    print("未知 feed 动作：%s" % action)
    return 2


def main() -> int:
    _force_utf8_stdio()
    parser = build_parser()
    # argparse **不允许位置参数夹在选项之间**：`feed add --force X` 里的 X 会被判成
    # "unrecognized arguments"（因为 `args` 是 nargs="*"，遇到 --force 就停止收集）。
    # 这对 feed 是硬伤（路径与 flag 的书写顺序很自然），所以只对 feed 把落下的
    # token 补回去；其余子命令**保持严格**，不能把拼错的参数吞掉。
    args, unknown = parser.parse_known_args()
    if unknown:
        if getattr(args, "command", "") == "feed":
            args.args = list(getattr(args, "args", []) or []) + list(unknown)
        else:
            parser.error("unrecognized arguments: %s" % " ".join(unknown))
    _harvest_quietly()
    if not args.command:
        # 无参数 = 进入交互式控制台（新用户入口）
        from agentmemhub.console import run_console
        run_console()
        return 0
    handlers = {
        "ingest": cmd_ingest, "list": cmd_list, "show": cmd_show,
        "search": cmd_search, "export": cmd_export, "stats": cmd_stats,
        "adapters": cmd_adapters, "folders": cmd_folders,
        "serve": cmd_serve, "mcp": cmd_mcp,
        "sync": cmd_sync, "clean": cmd_clean, "score": cmd_score, "rebuild": cmd_rebuild,
        "distill": cmd_distill, "weight": cmd_weight,
        "wiki": cmd_wiki,
        "feed": cmd_feed,
        "snapshot": cmd_snapshot,
        "memory-log": cmd_memory_log,
        "llm-config": cmd_llm_config,
    }
    fn = handlers.get(args.command)
    if fn is None:
        build_parser().print_help()
        return 0
    # 子命令用返回值表达成败（`feed lint` 的 P0 门槛、`feed add` 的部分失败等）。
    # 2026-10-26 之前这里直接丢弃返回值、`main()` 也不返回 ⇒ **所有命令退出码恒 0**，
    # 于是文档里写的"P0>0 ⇒ 退出码 1，可当提交/巡检门槛"从来没生效过。
    rc = fn(args)
    return rc if isinstance(rc, int) else 0


def cmd_llm_config(args) -> int:
    """打印各 LLM 使用点**最终生效**的配置，并标出每项是继承还是覆盖。

    为什么需要（2026-09-22）：LLM 的覆盖项是**就地**的（`wiki.llm` /
    `distillation.llm` / `wiki.l2.llm`），与 `logs.files.<name>`、召回档位的
    `retrieval.callers.<name>` 那种集中式覆盖不同 —— 想知道"某个调用方到底用
    哪个模型、哪些参数是继承来的"要翻好几处配置。本命令把结果拼好。
    """
    import json as _json

    from agentmemhub import config as hub_config

    data = hub_config.llm_effective()
    if getattr(args, "json", False):
        print(_json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    print("LLM 生效配置（%d 个使用点）" % len(data["users"]))
    print("=" * 76)
    print("\n【顶层 llm】%s —— 所有调用方的共用默认" % data["top"]["path"])
    for k, v in data["top"]["values"].items():
        print("   %-18s %s" % (k, v))

    for u in data["users"]:
        print("\n【%s】%s" % (u["name"], u["path"]))
        if not u["overrides"]:
            print("   （完全继承顶层，无覆盖）")
        for k, v in u["values"].items():
            tag = "覆盖" if k in u["overrides"] else "继承"
            print("   [%s] %-18s %s" % (tag, k, v))

    print("\n注：Wiki 第二级的继承链是「顶层 llm → wiki.l2.llm」，**刻意跳过 "
          "wiki.llm** ——\n    级联继承会让第一级换 provider 牵连第二级（实测踩过："
          "第一级切到\n    DeepSeek 官方关推理，第二级因此丢掉了 Command Code 的凭据）。")
    return 0


def _register_llm_config_parser(sub) -> None:
    pl = sub.add_parser(
        "llm-config",
        help="查看各 LLM 使用点最终生效的配置（哪项继承、哪项覆盖）")
    pl.add_argument("--json", action="store_true", help="输出原始 JSON")


def _register_memory_log_parser(sub) -> None:
    pm = sub.add_parser(
        "memory-log", help="记忆操作事实流：查什么时候写了/读了什么记忆")
    pm.add_argument("--limit", type=int, default=50,
                    help="最多显示多少条（默认 50；0=全部）")
    pm.add_argument("--event", default="", choices=["", "write", "read"],
                    help="只看写入 / 只看读取")
    pm.add_argument("--path", default="",
                    choices=["", "mcp", "http", "distill", "cli", "unknown"],
                    help="只看某个来源路径")
    pm.add_argument("--grep", default="", help="关键词过滤（匹配整条记录）")
    pm.add_argument("--json", action="store_true", help="输出原始 JSON")


def _register_snapshot_parser(sub) -> None:
    ps = sub.add_parser("snapshot", help="快照与回滚：索引库整库 + wiki 产物目录")
    ps.add_argument("--snapshot-action", dest="snapshot_action", default="list",
                    choices=["create", "list", "restore"],
                    help="create=创建快照（默认 list）；restore=回滚到指定快照")
    ps.add_argument("--reason", default="", help="create 用：快照原因（记录在 meta）")
    ps.add_argument("--snapshot-id", default="", help="restore 用：目标快照 id")
    ps.add_argument("--wiki-only", action="store_true", help="restore 时只回滚 wiki 产物")
    ps.add_argument("--db-only", action="store_true", help="restore 时只回滚索引库")


if __name__ == "__main__":
    main()
