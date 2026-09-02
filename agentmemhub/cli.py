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


def _cli_log(msg: str, level: str = "info") -> None:
    """CLI/控制台操作落盘（logs/cli.log），不干扰终端输出。"""
    try:
        from agentmemhub import logs
        logs.record(msg, level=level, actor="cli", dest="cli")
    except Exception:
        pass


def run_ingest(sources: list[str], signature: str = "") -> tuple[int, int]:
    """提取指定 source 列表并入库（CLI 与控制台共用）。返回 (会话数, 事件数)。"""
    store = Store()
    total_conv = 0
    total_ev = 0
    for src in sources:
        a = adapters.get_adapter(src)
        if a is None:
            _stdout(f"[{src}] 未知 source")
            continue
        p = a.locate()
        if p is None:
            _stdout(f"[{src}] 未找到数据源")
            continue
        sessions = a.load(p)
        n = store.replace_source(src, sessions, signature=signature or "")
        total_conv += len(sessions)
        total_ev += n
        _stdout(f"[{src}] {len(sessions)} 会话, {n} 事件")
    store.close()
    _cli_log(f"ingest sources={sources} → {total_conv} 会话, {total_ev} 事件")
    return total_conv, total_ev


def cmd_ingest(args) -> None:
    sources = [args.source] if args.source else [a.source for a in adapters.all_adapters()]
    total_conv, total_ev = run_ingest(sources, signature=args.signature)
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


def cmd_memos_daemon(args) -> None:
    """MemOS 记忆引擎 daemon 管理（启动/停止/巡检/日志/配置）。"""
    import json as _json
    from agentmemhub import memos_daemon
    if args.set_dir:
        try:
            p = memos_daemon.save_plugin_dir(args.set_dir)
            _stdout(f"已保存 MemOS 插件目录 → {p}")
        except Exception as e:
            _stdout(f"保存失败: {e}")
        return
    if args.lightweight is not None:
        from agentmemhub import memos_daemon as _md
        p = _md.set_lightweight(args.lightweight == "on")
        _stdout(f"已写入引擎配置 → {p}（轻量模式={'ON' if args.lightweight=='on' else 'OFF（完整进化链）'}；"
                f"引擎重启后生效，如引擎在运行请先 [7] 停止再启动）")
        return
    if args.set_password:
        memos_daemon.save_password(args.set_password)
        ok = memos_daemon._login()
        _stdout("密码已保存" + ("，登录成功 ✓" if ok else "（暂未能验证登录：引擎可能离线或密码不符）"))
        return
    if args.action == "start":
        r = memos_daemon.daemon_start(agent=args.agent, plugin_dir=args.plugin_dir or None)
    elif args.action == "stop":
        r = memos_daemon.daemon_stop()
    elif args.action == "logs":
        lf = memos_daemon._log_file()
        if not lf.exists():
            _stdout("（暂无日志）")
            return
        text = lf.read_text(encoding="utf-8", errors="replace")
        _stdout("\n".join(text.splitlines()[-int(args.lines):]))
        return
    else:  # status
        r = memos_daemon.daemon_status()
    _stdout(_json.dumps(r, ensure_ascii=False, indent=1))


def push_to_memos(store: Store, *, sources: list[str], base_url: str,
                  no_rebuild: bool = False, rebuild_mode: str = "repair",
                  stdout: Any = None,
                  since_ts: Optional[float] = None) -> dict[str, Any]:
    """构建并幂等推送 MemOS bundle（按 source 分批，失败批次继续），可选补向量。

    MemOS /api/v1/import 上限 64 MiB——全量 bundle 可能超限（实测 90+MB），
    因此按 source 分批 POST。trace id 由 src_id 派生 → 幂等（重复=skipped）。
    since_ts：增量——只构建 updated_at >= since_ts 的会话（避免全量重复
    提取/构建/传输；无锚时由调用方传 None 走全量）。
    每批推送与 rebuild 进度**实时**经 stdout 输出（web 任务据此实时回显，
    避免"导入汇总已完成"后看起来卡住）；lines 同时收集终态文本供日志。
    返回 {imported, skipped, lines, rebuilt}；rebuilt 失败/跳过时为 None。
    """
    from agentmemhub.memos import build_bundle, push_bundle, rebuild_embeddings
    out = stdout or print
    lines: list[str] = []

    def emit(s: str) -> None:
        lines.append(s)
        try:
            out(s)
        except Exception:
            pass

    total_ok = total_skip = 0
    failed = 0                                # 失败批次计数（决定增量锚是否可推进）
    chunk_size = 200                          # 单批 trace 上限：大 source 整包导入会超引擎
                                              # HTTP 请求窗口（~300s）被服务端断连（实测 10053）
    for src in sources:
        b = build_bundle(store, src, since_ts=since_ts)
        traces = b.get("traces") or []
        if not traces:
            continue
        chunks = [traces[i:i + chunk_size] for i in range(0, len(traces), chunk_size)]
        for ci, chunk in enumerate(chunks):
            payload = dict(b, traces=chunk)
            label = f"[{src}]" if len(chunks) == 1 else f"[{src}/{ci + 1}]"
            try:
                resp = push_bundle(payload, base_url)
                total_ok += resp.get("imported", 0)
                total_skip += resp.get("skipped", 0)
                emit(f"{label} 推送 ok: imported={resp.get('imported')} "
                     f"skipped={resp.get('skipped')}")
            except Exception as e:
                failed += 1
                emit(f"{label} 推送失败（继续下一批）: {e}")
    rebuilt = None
    if total_ok and not no_rebuild:
        try:
            # rebuild 是任务最耗时阶段（本地 embedding 计算可数分钟）——
            # 先提示再逐轮打印进度，杜绝"看起来卡住"
            emit(f"正在补向量（embedding {rebuild_mode}，本地计算可能耗时数分钟）…")
            rebuilt = rebuild_embeddings(base_url, mode=rebuild_mode,
                                         on_progress=lambda s: emit(s))
            emit(f"已补向量({rebuild_mode}): {rebuilt}")
        except Exception as e:
            emit(f"embedding rebuild 失败（可用 --no-rebuild 跳过）: {e}")
    emit(f"MemOS 导入汇总: imported={total_ok}, skipped={total_skip}"
         + (f"，{failed} 个批次失败" if failed else ""))
    return {"imported": total_ok, "skipped": total_skip,
            "lines": lines, "rebuilt": rebuilt, "failed": failed}


def run_memos(*, source: str = "", out: str = "exports/memos_bundle.json",
              push: str = "", no_rebuild: bool = False,
              rebuild_mode: str = "repair") -> None:
    """提取记忆 bundle + 可选推送 MemOS（CLI 与控制台共用）。"""
    from agentmemhub.memos import build_bundle, write_bundle
    store = Store()
    bundle = build_bundle(store, source)
    _stdout(f"生成 bundle: {len(bundle['traces'])} traces")
    write_bundle(bundle, Path(out))
    _stdout(f"已写入 → {out}")
    if push:
        try:
            if source:
                batches = [source]
            else:
                # bundle 从 store 构建——按 store 中实际存在的源推送。
                # 不能用 adapter.locate() 过滤：恢复/迁移环境下源文件可能已不在
                # 磁盘，但 store 里的数据必须完整推送（否则整源静默丢失）。
                batches = sorted({c["source"] for c in store.list_conversations()})
            # 进展已实时输出（stdout=_stdout），无需再循环打印 lines
            r = push_to_memos(store, sources=batches, base_url=push,
                              no_rebuild=no_rebuild, rebuild_mode=rebuild_mode,
                              stdout=_stdout)
            _cli_log(f"memos push → imported={r['imported']}, skipped={r['skipped']}")
        except Exception as e:
            _stdout(f"推送失败（MemOS 可能在运行?）: {e}")
            _cli_log(f"memos push 失败 → {e}", level="error")
    store.close()
    _cli_log(f"memos bundle 生成 → {out}（{len(bundle['traces'])} traces）")


def _sync_anchor_path() -> Path:
    from agentmemhub import config
    return config.config().data_dir / "last_sync.json"


def _read_sync_anchor() -> Optional[float]:
    """上次成功推送的时间锚（None=从未同步 → 首次全量）。"""
    try:
        import json as _j
        return float(_j.loads(_sync_anchor_path().read_text(encoding="utf-8"))["ts"])
    except Exception:
        return None


def _save_sync_anchor(ts: Optional[float] = None) -> None:
    import json as _j
    _sync_anchor_path().write_text(
        _j.dumps({"ts": ts or time.time()}, ensure_ascii=False), encoding="utf-8")


def run_sync(*, source: str = "", push: str = "", no_rebuild: bool = False,
             rebuild_mode: str = "repair", full: bool = False) -> None:
    """增量同步：ingest（幂等重跑）→ 只推送【自上次同步以来新增的会话】→ 补向量。

    增量锚：<data_dir>/last_sync.json 记录上次成功推送的时间；本次只构建
    updated_at 在锚之后的会话（默认带 5 分钟缓冲防边界漏），无新增则跳过
    推送——避免全量提取/构建/传输的重复损耗。首次同步或 --full 时全量。
    replace_source 与 MemOS import 幂等兜底，随时可重跑。引擎离线时 ingest
    照常完成、推送跳过（不推进锚，下次补推）。不做会话结束钩子：漏掉的
    同步靠幂等锚在下次启动补上（无耦合，不碰 harness）。
    """
    from agentmemhub import adapters, memos_daemon

    sources = [source] if source else [a.source for a in adapters.all_adapters()]
    run_ingest(sources)
    if not push:
        return
    if memos_daemon.auth_state() is None:
        _stdout("记忆引擎未运行——ingest 已完成，跳过推送（不推进增量锚）。"
                "启动引擎后重跑 `agentmemhub sync` 即可增量补推。")
        _cli_log("sync 跳过推送（引擎离线）", level="warn")
        return
    anchor = _read_sync_anchor()
    since = None
    if full or anchor is None:
        _stdout("全量同步（首次或 --full）…")
    else:
        since = anchor - 300         # 5 分钟缓冲，防边界会话漏推
        _stdout(f"增量同步（上次 {time.strftime('%m-%d %H:%M', time.localtime(anchor))} 之后的新会话）…")
    store = Store()
    try:
        # 推送源 = store 中实际有数据的源（同 run_memos：不能用 locate() 过滤，
        # 恢复环境下源文件不在磁盘也要能推 store 存量）
        batches = ([source] if source
                   else sorted({c["source"] for c in store.list_conversations()}))
        if since is not None:
            # 统计新增会话：没有就跳过（避免空跑）
            new_convs = 0
            for src in batches:
                new_convs += sum(1 for c in store.list_conversations(src)
                                 if (c["updated_at"] or 0) >= since)
            if new_convs == 0:
                _stdout("增量：无新增会话，跳过推送（--full 强制全量）")
                return
            _stdout(f"增量：{new_convs} 个新会话，仅推送这些…")
        # 进展实时输出（stdout=_stdout），无需再循环打印 lines
        r = push_to_memos(store, sources=batches, base_url=push,
                          no_rebuild=no_rebuild, rebuild_mode=rebuild_mode,
                          stdout=_stdout, since_ts=since)
        # 锚只在【无失败】时推进；有失败保留旧锚 → 下次增量窗口仍含失败会话，可重试
        if r.get("failed", 0) == 0:
            _save_sync_anchor()
        else:
            _stdout("存在推送失败——未推进增量锚，下次 sync 会重试失败批次")
        _cli_log(f"sync → imported={r['imported']}, skipped={r['skipped']}, "
                 f"failed={r.get('failed', 0)}")
    finally:
        store.close()


def cmd_sync(args) -> None:
    run_sync(source=args.source, push=args.push,
             no_rebuild=args.no_rebuild, rebuild_mode=args.rebuild_mode,
             full=args.full)


def run_clean(store, *, source: str = "", apply: bool = False,
              stdout: Any = None) -> None:
    """记忆清洗：删除系统注入事件（is_system）。

    默认只预览（dry-run）；apply=True 才物理删除并重建 FTS/event_count。
    """
    out = stdout or _stdout
    rows = store.system_event_counts(source or None)
    total = sum(r["n"] for r in rows)
    if not rows:
        out("（无系统注入事件——库已经干净）")
        return
    out(f"系统注入事件共 {total} 条（按 source）:")
    for r in rows:
        out(f"  [{r['source']}] {r['n']} 条（{r['convs']} 个会话）")
    if not apply:
        out("以上为预览——加 --apply 才会物理删除（删除后重建 FTS 索引与会话计数）")
        return
    deleted, convs = store.delete_system_events(source or None)
    out(f"已删除 {deleted} 条注入事件（{convs} 个会话受影响，FTS/计数已重建）")
    _cli_log(f"clean(source={source or 'all'}) → 删除 {deleted} 条注入事件")


def cmd_clean(args) -> None:
    store = Store()
    try:
        run_clean(store, source=args.source, apply=args.apply)
    finally:
        store.close()


def cmd_rebuild(args) -> None:
    """补向量：触发引擎 embedding rebuild（默认 repair 只补缺失向量）。"""
    from agentmemhub import memos_daemon
    from agentmemhub.memos import rebuild_embeddings
    if memos_daemon.auth_state() is None:
        _stdout("记忆引擎未运行（启动后重试）")
        return

    def _emit(s: str) -> None:
        _stdout(f"  {s}")
    _stdout(f"补向量（{args.mode}，本地计算可能耗时数分钟）…")
    r = rebuild_embeddings(base_url=memos_daemon.base_url(),
                           mode=args.mode, on_progress=_emit)
    _stdout(f"完成: {r}")
    _cli_log(f"rebuild({args.mode}) → {r}")


def _csv_ids(v: str) -> set[str]:
    return {x.strip() for x in v.split(",") if x.strip()}


def cmd_score(args) -> None:
    """LLM 批量自动评分历史记忆（三轴评估 → feedback 写入）。"""
    if args.unscored_count:
        from agentmemhub.scoring import count_unscored
        try:
            n = count_unscored(base_url=args.push)
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
    from agentmemhub.scoring import run_score_all

    def _emit(s: str) -> None:
        _stdout(s)
    try:
        r = run_score_all(emit=_emit, base_url=args.push,
                          limit=args.limit, dry_run=args.dry_run,
                          workers=args.workers,
                          only_ids=args.ids)
        _stdout(f"评分完成: evaluated={r['evaluated']} skipped={r['skipped']} "
                f"positive={r['positive']} neutral={r['neutral']} "
                f"negative={r['negative']} errors={r['errors']}"
                + (f" missing={r['missing']}" if r.get("missing") else "")
                + ("（dry-run，未写入）" if r["dryRun"] else ""))
        _cli_log(f"score → {r}")
    except Exception as e:
        _stdout(f"评分失败：{e}")
        _cli_log(f"score 失败 → {e}", level="error")


def cmd_memos(args) -> None:
    run_memos(source=args.source, out=args.out, push=args.push,
              no_rebuild=args.no_rebuild, rebuild_mode=args.rebuild_mode)


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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agentmemhub", description="AgentMemHub 统一 Agent 会话提取")
    sub = p.add_subparsers(dest="command")

    pi = sub.add_parser("ingest", help="提取所有 adapter 并入库")
    pi.add_argument("--source", default="")
    pi.add_argument("--signature", default="")

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
    pv.add_argument("--db", default="", help="数据库路径（默认 ~/.agentmemhub/agentmemhub.db）")
    # 成对开关：--open / --no-open（默认自动打开浏览器）
    pv.add_argument("--open", action=argparse.BooleanOptionalAction, default=True,
                    help="启动后自动打开浏览器（默认开启；用 --no-open 关闭）")

    pm = sub.add_parser("memos", help="生成/推送 MemOS 导入 bundle")
    pm.add_argument("--source", default="")
    pm.add_argument("--out", default="exports/memos_bundle.json")
    pm.add_argument("--push", default="", help="MemOS base URL，例如 http://127.0.0.1:18800；非空则 POST")
    pm.add_argument("--no-rebuild", action="store_true",
                    help="push 后不触发 /api/v1/embeddings/rebuild（默认自动补向量）")
    pm.add_argument("--rebuild-mode", default="repair", choices=("repair", "rebuild"),
                    help="embedding rebuild 模式：repair=只补缺失向量（默认），rebuild=全部重算")

    pmd = sub.add_parser("memos-daemon", help="MemOS 记忆引擎管理（start/stop/status/logs）")
    pmd.add_argument("action", nargs="?", default="status",
                     choices=("start", "stop", "status", "logs"))
    pmd.add_argument("--agent", default="hermes", help="daemon 的 agent 标识（决定端口/home）")
    pmd.add_argument("--plugin-dir", default="",
                     help="MemOS 插件目录（默认走 MEMOS_PLUGIN_DIR 或常见位置探测）")
    pmd.add_argument("--set-dir", default="",
                     help="持久化 MemOS 插件目录到 ~/.agentmemhub/config.json 后退出")
    pmd.add_argument("--set-password", default="",
                     help="保存 MemOS viewer 密码（引擎设了密码时网关自动登录）后退出")
    pmd.add_argument("--lightweight", choices=("on", "off"), default=None,
                     help="开关 MemOS 轻量记忆模式（off=完整进化链；写托管配置，重启引擎生效）")
    pmd.add_argument("--lines", type=int, default=40, help="logs 动作显示的行数")

    pmc = sub.add_parser("mcp", help="启动 MCP 记忆网关（stdio 默认；--http 转 Streamable HTTP 常驻）")
    pmc.add_argument("--http", action="store_true",
                     help="以 Streamable HTTP 模式常驻（默认 stdio 由 Agent 拉起）")
    pmc.add_argument("--port", type=int, default=9100, help="HTTP 监听端口（默认 9100）")
    pmc.add_argument("--bind", default="127.0.0.1",
                     help="HTTP 监听地址（默认仅本机；团队共享用 0.0.0.0）")

    psy = sub.add_parser("sync", help="增量同步：ingest → 幂等 push MemOS → 补向量（可随时重跑）")
    psy.add_argument("--source", default="")
    psy.add_argument("--push", default="", help="MemOS base URL；非空则推送到引擎（幂等，离线自动跳过）")
    psy.add_argument("--no-rebuild", action="store_true",
                     help="push 后不触发 embedding rebuild（默认自动补向量）")
    psy.add_argument("--rebuild-mode", default="repair", choices=("repair", "rebuild"))
    psy.add_argument("--full", action="store_true",
                     help="强制全量同步（默认只推送上次同步后的新增会话）")

    pcl = sub.add_parser("clean", help="记忆清洗：删除系统注入事件（is_system；默认预览，--apply 执行）")
    pcl.add_argument("--source", default="")
    pcl.add_argument("--apply", action="store_true",
                     help="执行删除（不带此参数仅统计预览；删除会重建 FTS 与会话计数）")

    psc = sub.add_parser("score", help="LLM 批量自动评分历史记忆（三轴评估 → feedback 写入，检索排序生效）")
    psc.add_argument("--limit", type=int, default=0, help="最多评分条数（0=全部）")
    psc.add_argument("--dry-run", action="store_true", help="只评估不写入（预览 verdict 分布）")
    psc.add_argument("--workers", type=int, default=4, help="并发评估线程数（默认 4；IO 密集建议 4~8）")
    psc.add_argument("--push", default="", help="引擎 base URL（默认 18800，一般不用设）")
    psc.add_argument("--ids", type=_csv_ids, default=None,
                     help="只评分指定 trace id（逗号分隔，如 --ids trac_1,trac_2；配合写后即评/锚点）")
    psc.add_argument("--unscored-count", action="store_true",
                     help="只统计未评分记忆条数（供定时/定量触发判断），不做 LLM 评估")
    psc.add_argument("--sync-episodes", action="store_true",
                     help="只把各 episode 的 r_task 同步为受评 traces 的平均 value（让 viewer 评分标签显示真实分），不做 LLM 评估")

    prb = sub.add_parser("rebuild", help="补向量：触发引擎 embedding rebuild（repair=只补缺失，rebuild=全部重算）")
    prb.add_argument("--mode", default="repair", choices=("repair", "rebuild"))
    return p


def main() -> None:
    args = build_parser().parse_args()
    if not args.command:
        # 无参数 = 进入交互式控制台（新用户入口）
        from agentmemhub.console import run_console
        run_console()
        return
    handlers = {
        "ingest": cmd_ingest, "list": cmd_list, "show": cmd_show,
        "search": cmd_search, "export": cmd_export, "stats": cmd_stats,
        "adapters": cmd_adapters, "memos": cmd_memos, "folders": cmd_folders,
        "serve": cmd_serve, "memos-daemon": cmd_memos_daemon, "mcp": cmd_mcp,
        "sync": cmd_sync, "clean": cmd_clean, "score": cmd_score, "rebuild": cmd_rebuild,
    }
    fn = handlers.get(args.command)
    if fn is None:
        build_parser().print_help()
        return
    fn(args)


if __name__ == "__main__":
    main()
