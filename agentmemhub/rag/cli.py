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


def _default_eval_path(root):
    """评测集默认路径：私有 eval/queries.yaml 优先，缺失则用仓库自带示例集。"""
    p = root / "eval" / "queries.yaml"
    return p if p.exists() else (root / "eval" / "queries.example.yaml")


def main(argv: list[str] | None = None) -> int:
    # Windows 坑：控制台默认 GBK，中文会话内容会变乱码；强制 stdout UTF-8
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="asrag", description="会话向量化 + 记忆召回")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ingest", help="增量摄取会话消息并嵌入")
    p.add_argument("--limit", type=int, default=None, help="最多扫描候选条数（试跑用）")
    p.add_argument("--batch-size", type=int, default=32)
    p.add_argument("--model", default=None,
                   help="用指定模型嵌入（默认 active）——多模型编排/后台子进程入口")
    p.add_argument("--roles", default=None,
                   help="逗号分隔的 role 白名单（默认 user,assistant）")
    p.add_argument("--include-reasoning", action="store_true",
                   help="将 reasoning 消息一并嵌入（默认不嵌）")
    p.add_argument("--rebuild", action="store_true",
                   help="清空 units 与该模型向量表后全量重建")

    sub.add_parser("stats", help="索引规模/水位统计")

    p = sub.add_parser("reembed", help="units 不动，按指定/激活模型重建向量表（模型切换）")
    p.add_argument("--model", default=None, help="models.json 中的模型 id")
    p.add_argument("--batch-size", type=int, default=32)

    p = sub.add_parser("eval", help="评测集 recall@k（vector/fts/hybrid 三档对比）")
    p.add_argument("--file", default=None,
                   help="评测集 yaml（默认 eval/queries.yaml，缺失则用 queries.example.yaml）")
    p.add_argument("-k", type=int, default=10)
    p.add_argument("--model", default=None, help="A/B 对比：指定注册表中的模型 id")

    p = sub.add_parser("search", help="混合召回（三路通道 + 阈值闸门 + 多样性）")
    p.add_argument("query")
    p.add_argument("--model", default=None, help="指定模型 id（默认 active）")
    p.add_argument("-k", type=int, default=10)
    p.add_argument("--mode", choices=("hybrid", "vector", "fts"), default="hybrid")
    p.add_argument("--no-expand", action="store_true", help="不做轮次上下文展开")
    p.add_argument("--exclude", default=None, metavar="SOURCE/CONV_ID",
                   help="排除的当前会话（P1-3，防重复注入自己）")
    p.add_argument("--safe-cutoff", action="store_true",
                   help="机械终审（≥0.7×top 且 ≤5 条；LLM Judge 的确定性替身）")
    p.add_argument("--json", action="store_true", help="输出 JSON（eval 用）")

    p = sub.add_parser("bench", help="召回延迟 p50/p95（用评测集查询，embedder 预热）")
    p.add_argument("--file", default=None)
    p.add_argument("-k", type=int, default=10)
    p.add_argument("--model", default=None)

    args = ap.parse_args(argv)
    settings = load_settings()

    if args.cmd == "ingest":
        log = get_logger("ingest", settings.log_dir, console=False)
        if args.roles:
            roles = tuple(r.strip() for r in args.roles.split(",") if r.strip())
        else:
            roles = (DEFAULT_ROLES + ("reasoning",)
                     if args.include_reasoning else DEFAULT_ROLES)
        s = run_ingest(settings, roles=roles, batch_size=args.batch_size,
                       model_id=args.model,
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
        path = args.file or _default_eval_path(settings.root)
        cases = load_cases(path)
        report = run_eval(settings, cases, k=args.k, log=log)
        print(format_report(report))
    elif args.cmd == "bench":
        from .eval import load_cases
        from .runtime import get_embedder
        import time as _t

        if getattr(args, "model", None):
            settings.model(args.model)
            settings = dataclasses.replace(settings, active_model=args.model)
        log = get_logger("search", settings.log_dir, console=False)
        path = args.file or _default_eval_path(settings.root)
        cases = load_cases(path)
        emb = get_embedder(settings.active_spec, settings=settings)
        lat = []
        for c in cases:
            t0 = _t.perf_counter()
            hybrid_search(settings, c.query, embedder=emb, k=args.k,
                          expand_turns=False, log=log)
            lat.append((_t.perf_counter() - t0) * 1000)
        s = sorted(lat)
        print(json.dumps({
            "queries": len(s), "mode": "hybrid", "k": args.k,
            "model": settings.active_model,
            "p50_ms": round(s[len(s) // 2], 1),
            "p95_ms": round(s[int(len(s) * 0.95)], 1),
            "max_ms": round(s[-1], 1),
        }, ensure_ascii=False, indent=2))
    elif args.cmd == "search":
        if getattr(args, "model", None):
            settings.model(args.model)
            settings = dataclasses.replace(settings, active_model=args.model)
        log = get_logger("search", settings.log_dir, console=False)
        exclude = None
        if args.exclude and "/" in args.exclude:
            src, _, cid = args.exclude.partition("/")
            exclude = (src, cid)
        judge = None
        if args.safe_cutoff:
            from .ext import safe_cutoff

            class _SafeJudge:
                def filter(self, q, hs):
                    return safe_cutoff(hs)

            judge = _SafeJudge()
        hits = hybrid_search(settings, args.query, k=args.k, mode=args.mode,
                             expand_turns=not args.no_expand,
                             exclude_session=exclude, judge=judge, log=log)
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
                tag = f" vec={h.vec_rank} fts={h.fts_rank}"
                if h.ident_rank:
                    tag += f" ident={h.ident_rank}"
                print(f"{i:2d}. [{h.role}] {h.title or h.conversation_id}"
                      f" #{h.unit_id} score={h.score:.4f}{tag} | {snip}")
            print(f"-- {len(hits)} hits (mode={args.mode}, k={args.k})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
