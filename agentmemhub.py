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

from store import Store
import adapters
from event_model import events_to_markdown


def _stdout(s: str) -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print(s)


def cmd_ingest(args) -> None:
    store = Store()
    sources = [args.source] if args.source else [a.source for a in adapters.all_adapters()]
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
        n = store.replace_source(src, sessions, signature=args.signature or "")
        total_conv += len(sessions)
        total_ev += n
        _stdout(f"[{src}] {len(sessions)} 会话, {n} 事件")
    store.close()
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


def cmd_search(args) -> None:
    store = Store()
    hits = store.search(args.query, source=args.source, role=args.role, limit=args.limit)
    _stdout(f"命中 {len(hits)} 条事件:\n")
    for h in hits:
        snippet = h.get("snippet") or h.get("content") or ""
        _stdout(f"[{h.get('source', args.source)}] {h.get('conversation_id')} | {h.get('role')} | {snippet[:80]}")
    store.close()


def cmd_export(args) -> None:
    from export import export_jsonl, export_markdown
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


def cmd_adapters(args) -> None:
    for a in adapters.all_adapters():
        d = a.describe()
        _stdout(f"[{d['source']}] {d['label']}: {'✓ ' + (d['path'] or '') if d['located'] else '✗ 未找到'}")


def cmd_memos(args) -> None:
    from memos_bridge import build_bundle, write_bundle, push_bundle
    store = Store()
    bundle = build_bundle(store, args.source)
    store.close()
    _stdout(f"生成 bundle: {len(bundle['traces'])} traces")
    write_bundle(bundle, Path(args.out))
    _stdout(f"已写入 → {args.out}")
    if args.push:
        try:
            resp = push_bundle(bundle, args.push)
            _stdout(f"已推送 MemOS: {resp}")
        except Exception as e:
            _stdout(f"推送失败（MemOS 可能在运行?）: {e}")


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

    pm = sub.add_parser("memos", help="生成/推送 MemOS 导入 bundle")
    pm.add_argument("--source", default="")
    pm.add_argument("--out", default="exports/memos_bundle.json")
    pm.add_argument("--push", default="", help="MemOS base URL，例如 http://127.0.0.1:18800；非空则 POST")
    return p


def main() -> None:
    args = build_parser().parse_args()
    handlers = {
        "ingest": cmd_ingest, "list": cmd_list, "show": cmd_show,
        "search": cmd_search, "export": cmd_export, "stats": cmd_stats,
        "adapters": cmd_adapters, "memos": cmd_memos,
    }
    fn = handlers.get(args.command)
    if fn is None:
        build_parser().print_help()
        return
    fn(args)


if __name__ == "__main__":
    main()
