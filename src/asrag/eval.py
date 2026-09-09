"""召回评测：查询集 → top-k 命中判定（text/title 含任一期望关键词）→ 三档对比报表。

queries.yaml 格式：
  - id: bat-unicode
    query: 改写后的自然语言提问（避免与标题同形，公平对比三档）
    expect_any: [关键词, ...]
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .config import Settings
from .embedder import Embedder, OnnxEmbedder
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


def first_hit_rank(hits, case: EvalCase) -> int | None:
    """返回首个满足期望的命中位次（1 起），未命中 None。"""
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
    embedder = embedder or OnnxEmbedder(spec, log=log)
    report: dict = {
        "model": spec.id, "dim": spec.dim, "k": k,
        "cases": len(cases), "modes": {},
    }
    for mode in modes:
        t0 = time.perf_counter()
        per_case: dict[str, int | None] = {}
        for c in cases:
            hits = hybrid_search(settings, c.query, embedder=embedder,
                                 k=k, mode=mode, expand_turns=False, log=log)
            per_case[c.id] = first_hit_rank(hits, c)
        ranks = [r for r in per_case.values() if r is not None]
        recall = len(ranks) / len(cases) if cases else 0.0
        mrr = (sum(1.0 / r for r in ranks) / len(cases)) if cases else 0.0
        report["modes"][mode] = {
            "recall@k": round(recall, 3),
            "mrr": round(mrr, 3),
            "first_hit_ranks": per_case,
        }
        log.info(
            "eval mode=%s k=%d cases=%d recall@k=%.3f mrr=%.3f secs=%.1f",
            mode, k, len(cases), recall, mrr, time.perf_counter() - t0,
        )
    return report


def format_report(report: dict) -> str:
    lines = [
        f"# 召回评测  model={report['model']} dim={report['dim']} "
        f"cases={report['cases']} k={report['k']}",
        f"{'mode':<10} {'recall@k':>9} {'MRR':>7}   未命中用例",
    ]
    for mode, m in report["modes"].items():
        misses = [cid for cid, r in m["first_hit_ranks"].items() if r is None]
        lines.append(
            f"{mode:<10} {m['recall@k']:>9.3f} {m['mrr']:>7.3f}   "
            f"{','.join(misses) if misses else '-'}"
        )
    return "\n".join(lines)
