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


def run_memos(*, source: str = "", out: str = "exports/memos_bundle.json",
              push: str = "", no_rebuild: bool = False,
              rebuild_mode: str = "repair") -> None:
    """提取记忆 bundle + 可选推送 MemOS（CLI 与控制台共用）。"""
    from agentmemhub.memos import (build_bundle, write_bundle, push_bundle,
                                   rebuild_embeddings)
    store = Store()
    bundle = build_bundle(store, source)
    _stdout(f"生成 bundle: {len(bundle['traces'])} traces")
    write_bundle(bundle, Path(out))
    _stdout(f"已写入 → {out}")
    if push:
        try:
            # MemOS /api/v1/import 上限 64 MiB——全量 bundle 可能超限（实测 90+MB），
            # 因此推送时按 source 分批 POST，失败批次继续并汇总
            if source:
                batches = [source]
            else:
                batches = [a.source for a in adapters.all_adapters() if a.locate()]
            total_ok = total_skip = 0
            for src in batches:
                b = build_bundle(store, src)
                if not b["traces"]:
                    continue
                try:
                    resp = push_bundle(b, push)
                    total_ok += resp.get("imported", 0)
                    total_skip += resp.get("skipped", 0)
                    _stdout(f"[{src}] 推送 ok: imported={resp.get('imported')} skipped={resp.get('skipped')}")
                except Exception as e:
                    _stdout(f"[{src}] 推送失败（继续下一批）: {e}")
            _stdout(f"MemOS 导入汇总: imported={total_ok}, skipped={total_skip}")
            if not no_rebuild:
                try:
                    r = rebuild_embeddings(push, mode=rebuild_mode)
                    _stdout(f"已触发 embedding {rebuild_mode}: {r}")
                except Exception as e:
                    _stdout(f"embedding rebuild 失败（可用 --no-rebuild 跳过）: {e}")
        except Exception as e:
            _stdout(f"推送失败（MemOS 可能在运行?）: {e}")
    store.close()


def cmd_memos(args) -> None:
    run_memos(source=args.source, out=args.out, push=args.push,
              no_rebuild=args.no_rebuild, rebuild_mode=args.rebuild_mode)


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
        "serve": cmd_serve, "memos-daemon": cmd_memos_daemon,
    }
    fn = handlers.get(args.command)
    if fn is None:
        build_parser().print_help()
        return
    fn(args)


if __name__ == "__main__":
    main()
