"""asrag 命令行入口：ingest / stats / reembed / search / eval。"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys

from .config import load_settings
from .ingest import run_ingest, run_reembed, run_stats
from .logkit import get_logger
from .search import hybrid_search
from .source import DEFAULT_ROLES


def main(argv: list[str] | None = None) -> int:
    # Windows 坑：控制台默认 GBK，中文会话内容会变乱码；强制 stdout UTF-8
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="asrag", description="会话向量化 + 记忆召回")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ingest", help="增量摄取会话消息并嵌入")
    p.add_argument("--limit", type=int, default=None, help="最多扫描候选条数（试跑用）")
    p.add_argument("--batch-size", type=int, default=32)
    p.add_argument("--include-reasoning", action="store_true",
                   help="将 reasoning 消息一并嵌入（默认不嵌）")
    p.add_argument("--rebuild", action="store_true",
                   help="清空 units 与该模型向量表后全量重建")

    sub.add_parser("stats", help="索引规模/水位统计")

    p = sub.add_parser("reembed", help="units 不动，按指定/激活模型重建向量表（模型切换）")
    p.add_argument("--model", default=None, help="models.json 中的模型 id")
    p.add_argument("--batch-size", type=int, default=32)

    p = sub.add_parser("eval", help="评测集 recall@k（vector/fts/hybrid 三档对比）")
    p.add_argument("--file", default=None, help="评测集 yaml（默认 eval/queries.yaml）")
    p.add_argument("-k", type=int, default=10)
    p.add_argument("--model", default=None, help="A/B 对比：指定注册表中的模型 id")

    p = sub.add_parser("search", help="混合召回（向量+trigram 全文，RRF 融合）")
    p.add_argument("query")
    p.add_argument("--model", default=None, help="指定模型 id（默认 active）")
    p.add_argument("-k", type=int, default=10)
    p.add_argument("--mode", choices=("hybrid", "vector", "fts"), default="hybrid")
    p.add_argument("--no-expand", action="store_true", help="不做轮次上下文展开")
    p.add_argument("--json", action="store_true", help="输出 JSON（eval 用）")

    args = ap.parse_args(argv)
    settings = load_settings()

    if args.cmd == "ingest":
        log = get_logger("ingest", settings.log_dir, console=False)
        roles = DEFAULT_ROLES + ("reasoning",) if args.include_reasoning else DEFAULT_ROLES
        s = run_ingest(settings, roles=roles, batch_size=args.batch_size,
                       limit=args.limit, rebuild=args.rebuild, log=log)
        print(json.dumps(s, ensure_ascii=False, indent=2))
    elif args.cmd == "stats":
        print(json.dumps(run_stats(settings), ensure_ascii=False, indent=2))
    elif args.cmd == "reembed":
        log = get_logger("ingest", settings.log_dir, console=False)
        s = run_reembed(settings, model_id=args.model,
                        batch_size=args.batch_size, log=log)
        print(json.dumps(s, ensure_ascii=False, indent=2))
    elif args.cmd == "eval":
        from .eval import format_report, load_cases, run_eval

        if getattr(args, "model", None):
            settings.model(args.model)  # 未注册即报错
            settings = dataclasses.replace(settings, active_model=args.model)
        log = get_logger("eval", settings.log_dir, console=False)
        path = args.file or (settings.root / "eval" / "queries.yaml")
        cases = load_cases(path)
        report = run_eval(settings, cases, k=args.k, log=log)
        print(format_report(report))
    elif args.cmd == "search":
        if getattr(args, "model", None):
            settings.model(args.model)
            settings = dataclasses.replace(settings, active_model=args.model)
        log = get_logger("search", settings.log_dir, console=False)
        hits = hybrid_search(settings, args.query, k=args.k, mode=args.mode,
                             expand_turns=not args.no_expand, log=log)
        if args.json:
            print(json.dumps([{
                "unit_id": h.unit_id, "source": h.source,
                "conversation_id": h.conversation_id, "seq": h.seq,
                "role": h.role, "title": h.title, "score": round(h.score, 5),
                "vec_rank": h.vec_rank, "fts_rank": h.fts_rank,
                "snippet": h.text[:120],
            } for h in hits], ensure_ascii=False, indent=2))
        else:
            for i, h in enumerate(hits, 1):
                snip = h.text.replace("\n", " ")[:90]
                print(f"{i:2d}. [{h.role}] {h.title or h.conversation_id}"
                      f" #{h.unit_id} score={h.score:.4f}"
                      f" vec={h.vec_rank} fts={h.fts_rank} | {snip}")
            print(f"-- {len(hits)} hits (mode={args.mode}, k={args.k})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
