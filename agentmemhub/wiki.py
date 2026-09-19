# -*- coding: utf-8 -*-
"""LLM Wiki 编译的**服务层**：失败清单查询 + 定向补跑。

为什么要有这一层
================
两级 wiki 编译是**长任务**（几十分钟、数百次 LLM 调用），失败不可避免。
但此前"补跑"只存在于脚本的 `--retry-failed` 参数里 —— 意味着**只能靠人敲命令**，
面板、定时任务、其它程序化调用方都够不到，Agent 也只能自己 exec 一遍。

这一层把三件事提升为可编程接口，CLI 与 Web 面板都只是薄薄包一层：

    failures_summary(out_dir)   查：哪些失败未解决、分几类、要不要人工介入
    retry_targets(out_dir, st)  查：具体该重跑哪些目标
    retry_failed(...)           做：只重跑这些目标（长任务，支持进度回调）

实现上直接复用 `scripts/` 里的编译实现（**不重写第二份逻辑**）——
脚本加 `sys.path` 后即可 import。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

from agentmemhub.failures import FATAL_KINDS, FailureLog

#: 失败清单里的 stage 名 → 人可读标签（与两个脚本写入时保持一致）
STAGES: dict[str, str] = {
    "l1": "第一级 · 逐会话编译",
    "l2-plan": "第二级 · 域内细分",
    "l2-compile": "第二级 · 页面编译",
}


def _ensure_scripts_on_path() -> Path:
    """把 `scripts/` 加进 sys.path，让服务层能复用编译实现（单一份逻辑）。"""
    root = Path(__file__).resolve().parents[1]
    scripts = root / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    return root


def _log(out_dir: Path | str) -> FailureLog:
    return FailureLog(Path(out_dir) / "failures.jsonl")


# ---------------------------------------------------------------------------
# 查
# ---------------------------------------------------------------------------

def failures_summary(out_dir: Path | str, stage: str = "") -> dict[str, Any]:
    """读失败清单，返回**结构化摘要**（可直接 JSON 序列化给接口用）。

    stage 留空 = 汇总全部阶段。`needs_manual=True` 表示存在 quota / auth / model
    这类**重试无意义**的错误 —— 调用方应当直接提示用户去处理，而不是继续补跑。
    """
    out = Path(out_dir)
    log = _log(out)
    rows = log.entries(stage or None, unresolved_only=True)

    by_kind: dict[str, list[dict]] = {}
    for e in rows:
        by_kind.setdefault(e.get("kind") or "unknown", []).append(e)

    return {
        "out": str(out),
        "log": str(out / "failures.jsonl"),
        "exists": (out / "failures.jsonl").exists(),
        "total": len(rows),
        "by_kind": {k: len(v) for k, v in sorted(by_kind.items())},
        "fatal": sorted(k for k in by_kind if k in FATAL_KINDS),
        "needs_manual": any(k in FATAL_KINDS for k in by_kind),
        "stages": {s: len(log.targets(s)) for s in STAGES},
        "items": [
            {"stage": e.get("stage"), "target": e.get("target"),
             "kind": e.get("kind"), "attempts": e.get("attempts"),
             "error": (e.get("error") or "")[:300], "ts": e.get("ts")}
            for e in rows[:200]
        ],
    }


def retry_targets(out_dir: Path | str, stage: str) -> list[str]:
    """该阶段尚未解决的失败目标（去重、保序）——补跑的实际输入。"""
    return _log(out_dir).targets(stage)


# ---------------------------------------------------------------------------
# 做
# ---------------------------------------------------------------------------

def retry_failed(*, stage: str, out_dir: Path | str, src: str = "",
                 db: str = "", workers: int = 0,
                 on_progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """只重跑失败清单里未解决的目标（**不再全量重来**）。

    stage：
      · `"l1"`  第一级 —— 从索引库读会话，只需 `out_dir`
      · `"l2"`  第二级 —— 需要 `src` 指向第一级产出目录
    其余 `l2-plan` / `l2-compile` 与 `"l2"` 等价（第二级一次跑完两个阶段）。

    `on_progress`：进度回调，供面板后台任务把输出接到日志（长任务必须可见）。

    返回：`{"stage", "targets", "ok", "failed", "seconds", "summary"}`；
    没有待重跑项时返回 `retried=0`，调用方据此提示用户"没有需要补的"。
    """
    import time
    root = _ensure_scripts_on_path()
    out = Path(out_dir)
    log = _log(out)
    t0 = time.time()

    if stage == "l1":
        import wiki_compile
        only = set()
        for t in log.targets("l1"):
            s, _, c = t.partition("/")
            if s and c:
                only.add((s, c))
        if not only:
            return {"stage": "l1", "retried": 0,
                    "message": "没有待重跑的第一级失败项"}
        if not db:
            from agentmemhub.rag.config import load_settings
            db = str(load_settings().index_db)
        results = wiki_compile.compile_all(
            Path(db), out, workers=workers or 4, resume=False,
            only=only, failures=log)
        ok = [r for r in results if not r.get("error") and not r.get("skipped")]
        bad = [r for r in results if r.get("error")]
        return {
            "stage": "l1", "retried": len(only),
            "ok": len(ok), "failed": len(bad),
            "seconds": round(time.time() - t0, 1),
            "summary": failures_summary(out, "l1"),
        }

    # ---- 第二级：复用 wiki_aggregate.run（它已支持 --retry-failed）----------
    import wiki_aggregate
    from types import SimpleNamespace

    if not src:
        return {"stage": "l2", "retried": 0,
                "message": "第二级补跑需要 src（第一级产出目录）"}
    bad_domains = set(log.targets("l2-plan")) | set(log.targets("l2-compile"))
    if not bad_domains:
        return {"stage": "l2", "retried": 0,
                "message": "没有待重跑的第二级失败项"}

    # wiki_aggregate.run 目前以 argparse 命名空间为入参；这里构造等价对象复用，
    # 避免把它的编排逻辑抄第二份（抄一份就是两处要同步维护）。
    args = SimpleNamespace(
        src=src, out=str(out), db=db, stage="all", workers=workers or 0,
        limit=0, domain="", dmin=20, dmax=60, batch=120, min_pages=2,
        recompile_single=False, thinking="", no_resume=False, resume=True,
        retry_failed=True,
    )
    wiki_aggregate.run(args)
    return {
        "stage": "l2", "retried": len(bad_domains),
        "seconds": round(time.time() - t0, 1),
        "summary": failures_summary(out),
    }


def retry_all(*, out_dir: Path | str, src: str = "", db: str = "",
              workers: int = 0,
              on_progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """按依赖顺序补跑两级（第一级在前 —— 它的产出是第二级的输入）。"""
    out = Path(out_dir)
    res: dict[str, Any] = {}
    if retry_targets(out, "l1"):
        res["l1"] = retry_failed(stage="l1", out_dir=out, db=db,
                                 workers=workers, on_progress=on_progress)
    if src and (retry_targets(out, "l2-plan") or retry_targets(out, "l2-compile")):
        res["l2"] = retry_failed(stage="l2", out_dir=out, src=src, db=db,
                                 workers=workers, on_progress=on_progress)
    if not res:
        res["message"] = "没有待重跑的失败项"
    return res
