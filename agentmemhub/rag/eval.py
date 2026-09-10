"""召回评测（P1-4 双口径）：

- 素材可达率 recall@k：top-k 内任一结果 text/title 含任一期望关键词；
- 自动金集（P1-4）：期望关键词在语料中的落点会话集合
  gold = DISTINCT (source, conversation_id) where text/title LIKE 任一关键词；
  据此派生 conv_recall@k（金集会话被触达比例）与 precision@k
  （top-k 中来自金集会话的比例）——把"含关键词的宽松口径"与
  "会话级相关性"分开，防止泛关键词题虚高。

queries.yaml 格式：
  - id: xxx
    query: 改写后的自然语言提问（与标题/原文不同形，公平对比三档）
    expect_any: [关键词, ...]
"""
from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import Settings
from .embedder import Embedder, OnnxEmbedder
from .runtime import get_embedder
from .search import hybrid_search


@dataclass(frozen=True)
class EvalCase:
    id: str
    query: str
    expect_any: tuple[str, ...]

    def matched(self, texts: list[str]) -> bool:
        return any(k in t for t in texts for k in self.expect_any)


def load_cases(path: Path | str) -> list[EvalCase]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    cases = [
        EvalCase(id=str(c["id"]), query=str(c["query"]),
                 expect_any=tuple(str(k) for k in c["expect_any"]))
        for c in raw["cases"]
    ]
    ids = [c.id for c in cases]
    assert len(ids) == len(set(ids)), "eval 用例 id 必须唯一"
    return cases


def gold_conversations(index_db: Path | str,
                       case: EvalCase) -> set[tuple[str, str]]:
    """自动金集：期望关键词在语料中落点的全部会话（source, conversation_id）。"""
    conn = sqlite3.connect(
        f"file:{Path(index_db).as_posix()}?mode=ro", uri=True)
    try:
        gold: set[tuple[str, str]] = set()
        for kw in case.expect_any:
            like = "%" + kw.replace("%", "\\%").replace("_", "\\_") + "%"
            for r in conn.execute(
                "SELECT DISTINCT source, conversation_id FROM units"
                " WHERE text LIKE ? ESCAPE '\\'"
                "    OR IFNULL(title,'') LIKE ? ESCAPE '\\'", (like, like)):
                gold.add((r[0], r[1]))
        return gold
    finally:
        conn.close()


def first_hit_rank(hits, case: EvalCase) -> int | None:
    for i, h in enumerate(hits, 1):
        if case.matched([h.text, h.title or ""]):
            return i
    return None


def run_eval(
    settings: Settings,
    cases: list[EvalCase],
    *,
    embedder: Embedder | None = None,
    k: int = 10,
    modes: tuple[str, ...] = ("vector", "fts", "hybrid"),
    log: logging.Logger | None = None,
) -> dict:
    log = log or logging.getLogger("asrag.eval")
    spec = settings.active_spec
    embedder = embedder or get_embedder(spec, settings=settings)
    gold_map = {c.id: gold_conversations(settings.index_db, c) for c in cases}
    report: dict = {
        "model": spec.id, "dim": spec.dim, "k": k,
        "cases": len(cases), "modes": {},
    }
    for mode in modes:
        t0 = time.perf_counter()
        per_case: dict[str, int | None] = {}
        conv_recalls: list[float] = []
        precisions: list[float] = []
        for c in cases:
            hits = hybrid_search(settings, c.query, embedder=embedder,
                                 k=k, mode=mode, expand_turns=False, log=log)
            per_case[c.id] = first_hit_rank(hits, c)
            gold = gold_map[c.id]
            if gold:
                hit_convs = {(h.source, h.conversation_id) for h in hits}
                conv_recalls.append(len(gold & hit_convs) / len(gold))
                precisions.append(
                    sum(1 for h in hits
                        if (h.source, h.conversation_id) in gold) / max(len(hits), 1))
        ranks = [r for r in per_case.values() if r is not None]
        recall = len(ranks) / len(cases) if cases else 0.0
        mrr = (sum(1.0 / r for r in ranks) / len(cases)) if cases else 0.0
        report["modes"][mode] = {
            "recall@k": round(recall, 3),
            "mrr": round(mrr, 3),
            "conv_recall@k": round(
                sum(conv_recalls) / len(conv_recalls), 3) if conv_recalls else None,
            "precision@k": round(
                sum(precisions) / len(precisions), 3) if precisions else None,
            "gold_cases": len(conv_recalls),
            "first_hit_ranks": per_case,
        }
        log.info(
            "eval mode=%s k=%d cases=%d recall@k=%.3f conv_recall=%.3f"
            " precision@k=%.3f mrr=%.3f secs=%.1f",
            mode, k, len(cases), recall,
            conv_recalls and sum(conv_recalls) / len(conv_recalls) or 0.0,
            precisions and sum(precisions) / len(precisions) or 0.0,
            mrr, time.perf_counter() - t0)
    return report


def format_report(report: dict) -> str:
    lines = [
        f"# 召回评测  model={report['model']} dim={report['dim']} "
        f"cases={report['cases']} k={report['k']}",
        f"{'mode':<10} {'recall@k':>9} {'conv-rec':>9} {'prec@k':>8} "
        f"{'MRR':>7}   未命中用例",
    ]
    for mode, m in report["modes"].items():
        misses = [cid for cid, r in m["first_hit_ranks"].items() if r is None]
        cr = "-" if m["conv_recall@k"] is None else f"{m['conv_recall@k']:.3f}"
        pr = "-" if m["precision@k"] is None else f"{m['precision@k']:.3f}"
        lines.append(
            f"{mode:<10} {m['recall@k']:>9.3f} {cr:>9} {pr:>8} "
            f"{m['mrr']:>7.3f}   {','.join(misses) if misses else '-'}")
    return "\n".join(lines)
