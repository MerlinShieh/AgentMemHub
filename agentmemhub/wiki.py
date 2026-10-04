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

import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from agentmemhub.failures import FATAL_KINDS, FailureLog
from agentmemhub import wiki_manifest as wm

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
            Path(db), out, workers=workers, resume=False,
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
    st = wiki_aggregate.run(args) or {}
    return {
        "stage": "l2", "retried": len(bad_domains),
        # 如实回报：中止 / 跳过 / 实际写了几域几页（`--retry-failed` 也走同一条闸）
        "aborted": str(st.get("aborted") or ""),
        "written_domains": len(st.get("written_domains") or []),
        "skipped_domains": list(st.get("skipped_domains") or []),
        "pages": int(st.get("pages") or 0),
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


# ---------------------------------------------------------------------------
# 对齐审计：wiki 产物 ↔ 当前索引库
# ---------------------------------------------------------------------------

#: 正文/来源行里的全局记忆引用 `[m<id>]`
_REF_RE = re.compile(r"\[m(\d+)\]")


def collect_refs(root: Path | str) -> dict[str, set[int]]:
    """收集产出目录下所有 md 页面引用的 m-id（按文件分组，相对路径为键）。"""
    refs: dict[str, set[int]] = {}
    base = Path(root)
    if not base.exists():
        return refs
    for f in sorted(base.rglob("*.md")):
        ids = {int(x) for x in _REF_RE.findall(f.read_text(encoding="utf-8"))}
        if ids:
            refs[f.relative_to(base).as_posix()] = ids
    return refs


def _default_db() -> str:
    from agentmemhub.rag.config import load_settings
    return str(load_settings().index_db)


#: 两级 manifest 的时间差超过这个秒数就算"不同步"。
#: 一次正常的 update 会在几秒内写完两级，所以 60s 以上一定是异常中间态。
SKEW_TOL = 60


def _ts_of(s: Any) -> float:
    """把 manifest 的 `generated_at` 解析成 epoch 秒；解析不了返回 0。"""
    if isinstance(s, (int, float)):
        return float(s)
    t = str(s or "").strip().replace("/", "-")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(t[:19], fmt).timestamp()
        except Exception:               # noqa: BLE001
            continue
    try:
        return datetime.fromisoformat(t).timestamp()
    except Exception:                   # noqa: BLE001
        return 0.0


def _consistency(stages: dict[str, Any]) -> dict[str, Any]:
    """两级一致性：**L1 领先 L2** 这种中间态在单级视角下完全看不见。

    为什么必须显式算：实测 2026-09-25 出现过 L2 产物 17:11、L1 产物 21:01
    （落后近 4 小时），而 `pending` 报 0 —— 因为 `pending` 只比 L1 的 manifest。
    "L1 已重编、L2 没跟上"意味着**页面里还是旧知识**，必须有个地方能一眼看出来。
    """
    a, b = stages.get("l1") or {}, stages.get("l2") or {}
    miss = [s for s in ("l1", "l2") if not (stages.get(s) or {}).get("manifest")]
    if miss:
        return {"comparable": False,
                "reason": "两级 manifest 不齐（缺 %s），无法比较同步性" % "、".join(miss)}
    t1, t2 = _ts_of(a.get("generated_at")), _ts_of(b.get("generated_at"))
    if not t1 or not t2:
        return {"comparable": False, "reason": "manifest 的 generated_at 解析不了"}
    skew = t1 - t2                     # >0 = L1 比 L2 新 → **L2 落后**
    n1, n2 = a.get("n_inputs"), b.get("n_inputs")
    return {
        "comparable": True,
        "l1_at": a.get("generated_at"),
        "l2_at": b.get("generated_at"),
        "skew_seconds": round(skew, 1),
        "l1_inputs": n1,
        "l2_inputs": n2,
        "inputs_delta": (int(n1) - int(n2)
                         if isinstance(n1, int) and isinstance(n2, int) else None),
        # l1_ahead = L2 落后（**要重编 L2**）；l2_ahead = L1 落后（少见）；
        # ok = 两级基本同步（≤ SKEW_TOL 秒）
        "verdict": ("ok" if abs(skew) <= SKEW_TOL
                    else ("l1_ahead" if skew > 0 else "l2_ahead")),
    }


def align(out_dir: Path | str = "", stage: str = "", db: str = "",
          l1_dir: Path | str = "", l2_dir: Path | str = "") -> dict[str, Any]:
    """对齐审计：回答「wiki 和 RAG 库现在差多少」（**全程只读**）。

    三个方向：
      1. 反向有效 —— 页面引用的每个 `[m<id>]` 在库里是否仍存在、是否仍是输入；
      2. 正向覆盖 —— 当前库的输入记忆有多少被页面引用过；
      3. 变更检测 —— 有 manifest 时精确 diff（added/changed/removed + 脏会话/脏域），
         没有 manifest 则退回"引用反推"模式，只做 1、2 并明确标记缺锚点。

    **两级目录（重要）**：两级产物**分居两个目录**（配置 `wiki.out_l1` /
    `wiki.out_l2`），而 manifest 名固定是 `<目录>/manifest_<stage>.json`。
    所以"只传一个 `out_dir`"**永远只能对上其中一级**，另一级会被误报成"缺失"。
    要对齐两级：传 `l1_dir` + `l2_dir`；两者都留空且 `out_dir` 也为空时**从配置解析**。

    返回结构可直接 JSON 序列化（CLI 打印 / 面板展示共用），其中 `consistency`
    给出两级同步性判定（见 `_consistency`）。
    """
    dirs: dict[str, Path] = {}
    if l1_dir:
        dirs["l1"] = Path(l1_dir)
    if l2_dir:
        dirs["l2"] = Path(l2_dir)
    if stage:                          # 限定单级时只留那一级
        dirs = {k: v for k, v in dirs.items() if k == stage}
    if not dirs and out_dir:
        # 单目录用法：不带 stage 时**保持旧覆盖**（在该目录查两级），只是文案
        # 不再编造原因（见 CLI 的"缺失"分支）。要正确的两级视图请用 --l1/--l2。
        dirs = ({stage: Path(out_dir)} if stage
                else {"l1": Path(out_dir), "l2": Path(out_dir)})
    if not dirs:                       # 都没给 → 从配置解析两级
        from agentmemhub import config as hub_config
        w = hub_config.config().wiki or {}
        for st, key in (("l1", "out_l1"), ("l2", "out_l2")):
            if w.get(key):
                dirs[st] = Path(w[key])
    if not dirs:
        return {"out": "", "dirs": {}, "db": db, "error":
                "没给产出目录，也没配 wiki.out_l1 / wiki.out_l2"}
    out = dirs.get("l1") or next(iter(dirs.values()))
    if not db:
        db = _default_db()
    conn = sqlite3.connect("file:%s?mode=ro" % Path(db).as_posix(), uri=True)
    try:
        cur_inputs, _cur_sessions = wm.snapshot_inputs(conn)
        cur_inputs = set(cur_inputs)
        # 全库存在性（merged/duplicate 也算存在，只是不再是输入 —— 单独归类）
        exist: dict[int, str] = {
            mid: st for mid, st in conn.execute(
                "SELECT id, status FROM distilled_memories")}
    finally:
        conn.close()

    refs = collect_refs(out)
    all_refs: set[int] = set()
    for ids in refs.values():
        all_refs |= ids

    missing = sorted(i for i in all_refs if i not in exist)
    not_input = sorted(i for i in all_refs
                       if i in exist and exist[i] not in ("new", "similar"))
    covered = all_refs & cur_inputs
    # **变更检测看不见的那一半**：manifest 说"已编译"、页面里却没有引用的输入。
    # 实测库里 1781 条输入有 61 条无任何页面引用（最早追到 09-10），而 align 只
    # 比 manifest diff，一律报"✓ 库未变化" —— 静默缺口必须有出口。
    uncovered = sorted(cur_inputs - covered)

    stages: dict[str, Any] = {}
    for st, sdir in dirs.items():
        mpath = wm.manifest_path(sdir, st)
        mf = wm.load_manifest(mpath)
        if mf is None:
            # 带上**期望路径**：调用方据此能说出"是没这个文件，还是它在别的目录"
            # （早先只回一个 False，CLI 就编出了"编译于该功能上线前"这种假原因）
            stages[st] = {"manifest": False, "dir": str(sdir),
                          "path": str(mpath)}
            continue
        d = wm.diff_manifest(mf, db)
        extra: dict[str, Any] = {
            "manifest": True,
            "dir": str(sdir),
            "path": str(mpath),
            "generated_at": mf.get("generated_at"),
            "src_db": mf.get("src_db"),
            "n_inputs": mf.get("n_inputs"),
        }
        extra.update(d)
        extra["needs_recompile"] = bool(
            d["added_total"] or d["changed_total"] or d["removed_total"])
        stages[st] = extra

    needs = any(s.get("needs_recompile") for s in stages.values())
    parts = []
    for st, s in stages.items():
        if s.get("manifest"):
            parts.append("%s: 新增 %d / 变更 %d / 失去输入 %d（脏会话 %d 个）"
                         % (st, s["added_total"], s["changed_total"],
                            s["removed_total"], s["dirty_session_total"]))
        else:
            parts.append("%s: 无编译清单（只能做引用反推）" % st)
    message = ("；".join(parts)
               + "；失效引用 %d 个" % (len(missing) + len(not_input))
               + ("；⚠️ 未覆盖 %d 条输入（页面里没有任何引用）" % len(uncovered)
                  if uncovered and any(s.get("manifest") for s in stages.values())
                  else "")
               + ("；⚠️ 需要重编译" if needs else "；✓ 库未变化"))

    return {
        "out": str(out),
        "dirs": {k: str(v) for k, v in dirs.items()},
        "consistency": _consistency(stages),
        "db": db,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "refs": {"files": len(refs), "ids": len(all_refs)},
        "invalid_refs": {
            "missing": missing[:wm._LIST_CAP],
            "missing_total": len(missing),
            "not_input": not_input[:wm._LIST_CAP],
            "not_input_total": len(not_input),
        },
        "coverage": {
            "current_inputs": len(cur_inputs),
            "covered": len(covered),
            # 未被任何页面引用的输入数（变更检测的口径看不见它们）
            "uncovered": len(uncovered),
            "uncovered_ids": uncovered[:wm._LIST_CAP],
            "ratio": round(len(covered) / len(cur_inputs), 4) if cur_inputs else None,
        },
        "stages": stages,
        "needs_recompile": needs,
        "message": message,
    }


# ---------------------------------------------------------------------------
# 增量更新：align 脏清单 → 只重编受影响的 L1/L2
# ---------------------------------------------------------------------------

def _delete_empty_session_outputs(l1_dir: Path, sessions: set, db: str) -> list[str]:
    """输入已清空的会话，其 L1 产物文件要删掉（重编流程不会再碰到它们）。"""
    conn = sqlite3.connect("file:%s?mode=ro" % Path(db).as_posix(), uri=True)
    removed: list[str] = []
    try:
        import wiki_compile
        for s, c in sessions:
            n = conn.execute(
                "SELECT COUNT(*) FROM distilled_memories"
                " WHERE source=? AND conversation_id=? AND status IN ('new','similar')",
                (s, c)).fetchone()[0]
            if n == 0:
                stem = wiki_compile._out_stem(s, c)
                for ext in (".md", ".json"):
                    f = l1_dir / (stem + ext)
                    if f.exists():
                        f.unlink()
                        removed.append(f.name)
    finally:
        conn.close()
    return removed


def _audit(entry: dict) -> None:
    """写一条 wiki 流水线日志（**全程旁路**：日志失败绝不影响更新本身）。"""
    try:
        from agentmemhub.logs import audit_wiki
        audit_wiki(entry)
    except Exception:                       # noqa: BLE001
        pass


def _verify_links(l2: Path | str) -> dict[str, Any]:
    """产物链接验收（**Obsidian 权威口径**）—— 旁路，绝不抛。

    产物最终是给人看的：`[[…]]` 必须点得开，否则点击会**新建一个空白笔记**。
    `available=False` 表示"没法验"（Obsidian 没运行 / CLI 没注册）——**不是失败**；
    `ok=True` 才是唯一通过标准（未解析链接为 0）。
    """
    try:
        from agentmemhub import obsidian
        r = obsidian.verify(vault=Path(l2).name)
    except Exception as e:                  # noqa: BLE001 —— 验收绝不能把更新搞挂
        r = {"ok": False, "available": False, "total_targets": 0,
             "total_links": 0, "items": [],
             "error": "%s: %s" % (type(e).__name__, str(e)[:160])}
    _audit({"event": "link_check", "ok": bool(r.get("ok")),
            "available": bool(r.get("available")),
            "unresolved_targets": r.get("total_targets"),
            "unresolved_links": r.get("total_links"),
            "error": r.get("error", "")})
    return r


def _l2_skip_ids(dom_out: dict[str, Any], dirty_domains: list[str],
                 written: set[str], skipped: list[str]) -> set[int]:
    """算出「**没真正写出来**的脏域」覆盖的记忆 id —— 它们必须被剔除出 L2 新基线。

    没编译却记成已编译 = **永久掩盖**（align 下轮不再报脏、这批数据再也不会重编）。
    三类都算在内：
      · **页数崩塌保护跳过的域**（`skipped`）
      · **逐最终页编译整域失败的域**（聚合的 `by_domain_pages` 里根本没有它）
      · **聚合什么都没写的路径**（只归域的 dry-run、异常提前返回…）

    宁可"多剔除、下轮重试"，也不能漏剔除 —— 重试只是多花一次钱，漏剔除是静默丢数据。
    """
    unwritten = set(skipped) | {n for n in (dirty_domains or [])
                                if n not in written}
    out: set[int] = set()
    for n in unwritten:
        out.update(dom_out.get(n, {}).get("mids") or [])
    return out


# ---------------------------------------------------------------------------
# 离线模式：回读 → 按在飞任务推进一段
# ---------------------------------------------------------------------------

def _offline_ready(job, now: float, deadline_s: float) -> bool:
    """这个离线任务的结果够不够"往下走一步"。

    齐了当然走；不齐但**厂商已终局 / 已完成**也要走（否则永远卡在"部分到位"）；
    再不然只有超预算才将就 —— 与 `decide()` 的 PARTIAL 语义一致。
    """
    if not job.results:
        return job.is_terminal() or job.provider_status == "completed"
    if job.results_state == "complete":
        return True
    if job.is_terminal():
        return True
    return deadline_s > 0 and (now - job.submitted_at) >= deadline_s


def _inputs_since(db: str, since: float) -> set[int]:
    """`created_at > since` 的输入 id（**只读**；读不到就返回空集，保守不剔除）。

    只按 `created_at` 判"新增"：`distilled_memories` **没有 `updated_at`**，而"内容
    变更"不需要靠剔除兜底 —— 基线存的是**内容指纹**，内容一变指纹就变，下次 `diff`
    自然认它是脏的 ✓。
    """
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
        try:
            return {int(r[0]) for r in conn.execute(
                "SELECT id FROM distilled_memories WHERE created_at > ?",
                (float(since),))}
        finally:
            conn.close()
    except Exception:                       # noqa: BLE001 —— 读不到就不剔除
        return set()


def _rebuild_l2_manifest(l2: Path, db: str, *, src_dir: str = "",
                         since: float = 0.0) -> dict[str, Any]:
    """按**磁盘产物**重建 L2 清单（离线落盘轮用）。

    与实时路的差别只是"从哪算"：实时路从本轮内存里的编译结果算，这里从磁盘上的
    页面 json 算 —— **口径一致**（都是"这些输入确实编译出来了"）。不刷它的话，
    下轮 `align` 仍认为这批数据是脏的，会重复提交、重复付费。

    `since` = **批次提交时刻**。晚于它出现的输入（`created_at > since`）**一律不记入
    基线**。为什么必须这样：离线是异步的 —— "提交时的快照"与"落盘时的库"之间隔了
    几小时甚至一天（实测 18 分钟 ~ 24 小时），期间新写入的记忆**并没有被这次编译
    覆盖**；若照"当前库"记进基线，它们会被**静默掩盖**（下次增量更新不再带上它们，
    这些记忆永远进不了 wiki）。**实时通道没有这个问题** —— 它提交即落盘，中间没有
    时间窗（用户 2026-10-03 提出的正是这个场景）。

    ⚠️ **域名必须走 `_domain_dir_map()`（权威域表 + slug 反查）**，绝不能读各域
    `index.md` 的首行：老产物的首行是**目录名**，照它定域名会造出 18 个"假域名"
    写进 manifest，下一轮 `domain_map` 会把它当真域 → 又新建一套重复目录
    （2026-10-01 实测：manifest 从 18 个域涨到 21 个，产物里出现
    `20-04-AgentMemHub-记忆系统` 这种双重前缀目录）。
    """
    import wiki_aggregate as wa

    dir_of = wa._domain_dir_map(l2)         # 权威域名 → 磁盘目录名
    dom_out: dict[str, dict[str, Any]] = {}
    keep: set[int] = set()                  # 磁盘产物里**确实被引用**的输入
    n_pages = 0
    for name, dirname in dir_of.items():
        d = l2 / dirname
        files: set[str] = set()
        mids: set[int] = set()
        cnt = 0
        for j in sorted(d.glob("*.json")):
            if j.name == "index.json":
                continue
            try:
                p = json.loads(j.read_text(encoding="utf-8"))
            except Exception:               # noqa: BLE001
                continue
            if not isinstance(p, dict) or not p.get("title"):
                continue
            cnt += 1
            files.update(str(x) for x in (p.get("from_files") or []) if x)
            mids.update(int(x) for x in re.findall(r"\[m(\d+)\]", p.get("body") or ""))
            for t in (p.get("sources") or []):
                s = str(t)
                if s[:1] == "m" and s[1:].isdigit():
                    mids.add(int(s[1:]))
        if cnt:
            dom_out[name] = {"dir": dirname, "pages": cnt, "pages_source": "l2",
                             "l1_files": sorted(files), "mids": sorted(mids)}
            keep |= mids
            n_pages += cnt
    # **批次期间新增的输入不许进基线**（详见 docstring）。已被页面引用的当然保留 ——
    # 理论上"晚于 since"的不可能出现在产物里，这里做双保险。
    drop_ids = (_inputs_since(db, since) - keep) if since > 0 else set()
    mf2 = wm.build_manifest("l2", db, drop_ids=drop_ids or None,
                            extra={"src_dir": src_dir, "n_pages": n_pages,
                                   "domains": dom_out,
                                   "dropped_new_inputs": len(drop_ids)})
    wm.write_manifest(wm.manifest_path(l2, "l2"), mf2)
    return mf2


def _offline_advance(*, l1: Path, l2: Path, db: str, runner, client,
                     deadline_s: float) -> dict[str, Any] | None:
    """离线模式下**先回读再决定**：能推进就推进并返回结果；否则返回 `None`。

    `None` 的意思是"没有在飞任务"—— 上层据此走**提交轮**。返回 dict（包括
    "还在等"）则表示本轮到此为止，**不再提交新任务**（避免重复付费）。

    三段式里"回读"这半边全在这里：

    · `l2compile` 结果到位 → **落盘**（写页 → linkfix → 验收 → 刷基线）= 终点；
    · `l2plan` 结果到位    → 合并 → **交编译段**（第二次离线提交）；
    · 都还没到位           → 如实报"还在等"（带进度与已等多久）。
    """
    import time

    import wiki_aggregate as wa
    from agentmemhub import wiki_batch

    def _land(job, members: dict, results: dict) -> dict[str, Any]:
        """落盘 + linkfix + 验收 + 刷基线 —— 三段式的**终点**（两条路径共用）。"""
        status = wa.write_offline_stage(l2, members, results,
                                        FailureLog(l2 / "failures.jsonl"))
        fix: dict[str, Any] = {}
        link_check: dict[str, Any] = {}
        try:
            import wiki_linkfix
            fix = wiki_linkfix.run_fix(l2, dry_run=False)
            link_check = _verify_links(l2)
        except Exception as e:              # noqa: BLE001 —— 产物已写，验收失败不该回滚
            fix = {"error": "%s: %s" % (type(e).__name__, str(e)[:160])}
        # `since=job.submitted_at`：**批次提交时刻** —— 晚于它新增的记忆没有被这次
        # 编译覆盖，绝不能进基线（否则被静默掩盖，见 `_rebuild_l2_manifest`）。
        mf2 = _rebuild_l2_manifest(l2, db, src_dir=str(l1),
                                   since=float(job.submitted_at or 0.0))
        wiki_batch.finish(l2, job, "落盘完成")
        hints = ["离线批次 %s 已落盘：%d 域 / %d 页（降级拼接 %d 页）"
                 % (job.batch_id, len(status.get("written_domains") or []),
                    int(status.get("pages") or 0), int(status.get("degraded") or 0))]
        # **落盘完成要主动通知人**：离线是异步的（实测等 18 分钟 ~ 24 小时），
        # 用户不会盯着看 —— "跑完了"这件事只能靠通知。开启了 Pushplus 就顺手推微信
        # （推送是旁路：`notify()` 本身永不抛，这里再兜一层只是保险）。
        try:
            from agentmemhub import notify as _notify
            _notify.info("batch", "离线批次已落盘",
                    "批次 %s：%d 域 / %d 页（降级拼接 %d 页）"
                    % (job.batch_id, len(status.get("written_domains") or []),
                       int(status.get("pages") or 0),
                       int(status.get("degraded") or 0)),
                    meta={"batch_id": job.batch_id, "stage": job.stage,
                          "pages": status.get("pages"),
                          "degraded": status.get("degraded"),
                          "domains": len(status.get("written_domains") or [])})
        except Exception:                   # noqa: BLE001 —— 旁路
            pass
        if link_check.get("available") and not link_check.get("ok"):
            hints.append("**产物链接验收未通过**：Obsidian 报 %d 个未解析目标"
                         % link_check.get("total_targets", 0))
        return {"updated": True, "mode": "offline", "step": "landed",
                "l2": {**status, "linkfix": fix, "link_check": link_check},
                "manifest": {"l2_inputs": mf2.get("n_inputs", 0)},
                "hints": hints}

    now = time.time()
    st = wiki_batch.import_results(client, l2, now=now, deadline_s=deadline_s)
    jobs = [j for j in st["jobs"] if not j.voided]
    if not jobs:
        return None

    for job in jobs:                        # ① 编译结果到位 → 落盘（三段式的终点）
        if job.stage != "l2compile" or not _offline_ready(job, now, deadline_s):
            continue
        return _land(job, job.members,
                     st["results"].get(job.batch_id) or dict(job.results))

    for job in jobs:                        # ② 细分结果到位 → 交编译段
        if job.stage != "l2plan" or not _offline_ready(job, now, deadline_s):
            continue
        res = st["results"].get(job.batch_id) or dict(job.results)
        try:
            all_titles = [p["title"] for p in wa.load_pages(l1, {})]
        except Exception:                   # noqa: BLE001 —— 取不到只影响 related 建议
            all_titles = []
        status = wa.submit_offline_compile(l2, runner, job, res, all_titles)
        sub = status.get("offline_submitted") or {}
        if sub:
            wiki_batch.finish(l2, job, "细分结果已消费，已交编译段")
            return {"updated": False, "mode": "offline", "step": "compile_submitted",
                    "l2": status,
                    "hints": ["细分结果已消费，编译任务已提交（batch=%s，%d 条请求）"
                              "—— 等厂商跑完后再回读即可落盘"
                              % (sub.get("batch_id") or "(未知)",
                                 sub.get("n_requests", 0))]}
        # **一条请求都没交出去**（全是单成员页，本地就能生成）→ 直接落盘。
        # 不能"提交一个不存在的任务然后空等"：那样下次调用会因为"没有在飞任务"
        # 而重新走提交轮，于是永远到不了落盘。
        return _land(job, status.get("compile_members") or job.members, res)

    waiting = [{"batch_id": j.batch_id, "stage": j.stage,
                "status": j.provider_status or "(未知)",
                "got": len(j.results), "total": len(j.items),
                "waited_s": round(now - j.submitted_at, 1)} for j in jobs]
    return {"updated": False, "mode": "offline", "step": "waiting",
            "waiting": waiting,
            "hints": ["离线任务仍在等厂商（闲时调度）：%s"
                      % "；".join("%s(%s) %d/%d 条，已等 %.0f 分钟"
                                  % (w["batch_id"], w["status"], w["got"],
                                     w["total"], (w["waited_s"] or 0) / 60)
                                  for w in waiting)]}


def update(*, l1_dir: Path | str, l2_dir: Path | str, db: str = "",
           workers: int = 0, on_progress: Callable[[str], None] | None = None,
           offline_runner=None, offline_client=None,
           offline_deadline_s: float = 0.0, offline_plan: bool = False,
           full: bool = False) -> dict[str, Any]:
    """增量更新：RAG 库变了，只重编受影响的 L1 页与 L2 域（**不全量重来**）。

    **离线模式**（`offline_client` 非空时启用）：L2 的细分与编译不再当场调 LLM，
    而是交给厂商的批量接口（价格是实时的 50%，闲时调度）。每次调用**只推进一段**
    —— 段与段之间要等厂商（可能几小时），所以这是个跨多次调用的状态机：

      无在飞任务        → 提交轮（归域 → 细分请求 → 一次提交）
      细分结果到位      → 合并 → 交编译段
      编译结果到位      → 组装页面 → 落盘 → linkfix → 刷基线

    传入 `offline_client`（`batch_client.MiMoBatchClient`）与 `offline_runner`
    （`llm_batch.OfflineRunner`）即可；返回值里的 `step` 说明这次推进到哪一段。

    链路（对齐"会话为脏单位"的设计）：
      1. align 两级 manifest → 脏会话清单；
      2. 重编脏会话的 L1 页（会话隔离，整页重编无副作用）；
         输入已清空的会话 → 直接删其产物；
      3. 按 manifest_l2 的 域→L1文件 映射定位脏域；新 L1 文件归入现有域
         （一次便宜的归类调用，不重跑全量归域 —— 归域缓存按批次序号键控，
         页序漂移会让它全错位）；
      4. 只重编脏域（partial 模式：不清其它域目录、顶层索引磁盘重建、
         _domains.json 成员按最新序号修正）；
      5. linkfix（L2 标题可能变，[[标题]] 互链要重修）；
      6. 刷新两级 manifest（此后 align 归零，形成新基线）。

    前提：两级都有 manifest（没有锚点就无法增量 —— 先全量编译一次）。
    """
    import time
    root = _ensure_scripts_on_path()
    l1 = Path(l1_dir)
    l2 = Path(l2_dir)
    if not db:
        db = _default_db()
    t0 = time.time()

    # **离线模式先回读**：有在飞任务时，这一次要推进的是"回读"那一段（交编译段 /
    # 落盘），而不是再提交一批新请求 —— 否则就是重复付费。只有**没有**在飞任务时
    # 才落到下面的正常流程（此时 `args.offline_runner` 会让聚合层走**提交轮**）。
    if offline_client is not None:
        _step = _offline_advance(l1=l1, l2=l2, db=db, runner=offline_runner,
                                 client=offline_client,
                                 deadline_s=offline_deadline_s)
        if _step is not None:
            return _step

    # ---- 1. 对齐审计 ----
    a1 = align(l1, "l1", db)
    a2 = align(l2, "l2", db)
    # **全量重跑（`full=True`）要跳过这两道早退**：它本来就是"显式要求重来一遍"，
    # 与"库有没有变化""有没有现成基线"无关 —— 尤其是"缺 manifest"时，全量重跑
    # 恰恰是**建立锚点**的手段，被它挡住就自相矛盾了。
    if not full and not (a1["stages"].get("l1", {}).get("manifest")
                         and a2["stages"].get("l2", {}).get("manifest")):
        return {"updated": False,
                "message": "缺少编译清单（manifest_*.json）—— 先全量编译一次建立锚点"}
    if not full and not (a1["needs_recompile"] or a2["needs_recompile"]):
        return {"updated": False, "message": "库未变化，无需更新"}

    # 回滚锚：增量更新会覆盖产物与 manifest——动手前先快照（实测教训：
    # 测试污染导致 79 个 L1 产物丢失，只能重编恢复；有快照就是一次拷贝）
    snap: dict[str, Any] = {}
    try:
        from agentmemhub import snapshot
        snap = snapshot.create(reason="增量更新前自动快照")
    except Exception as e:                      # noqa: BLE001 —— 旁路，快照失败不阻塞
        snap = {"id": None, "error": str(e)[:200]}

    dirty_sessions = set()
    for sess in (a1["stages"]["l1"].get("dirty_sessions") or {}):
        s, _, c = sess.partition("/")
        if s and c:
            dirty_sessions.add((s, c))
    if not dirty_sessions and not full:
        # ⚠️ `full=True` 时必须放过这一道：全量**不依赖脏会话** —— 它要重来的是
        # "域结构"（这正是"『待整理』域撑不住"时该做的事）。若在这里早退，全量
        # 就永远跑不起来（实测：2026-10-04 写测试时正是被这道挡住）。
        return {"updated": False, "message": "没有可定位的脏会话"}

    import wiki_aggregate
    import wiki_compile

    # ---- 2. 重编脏会话的 L1 ----
    fl1 = FailureLog(l1 / "failures.jsonl")
    l1_results = wiki_compile.compile_all(
        Path(db), l1, workers=workers, resume=False,
        only=dirty_sessions, failures=fl1)
    l1_ok = [r for r in l1_results if not r.get("error") and not r.get("skipped")]
    l1_bad = [r for r in l1_results if r.get("error")]
    removed_files = _delete_empty_session_outputs(l1, dirty_sessions, db)

    # ---- 3. 定位脏域 + 新文件归域 ----
    l2m = wm.load_manifest(wm.manifest_path(l2, "l2")) or {}
    dom_meta: dict[str, dict] = l2m.get("domains") or {}
    file2dom: dict[str, str] = {}
    for name, meta in dom_meta.items():
        for f in (meta.get("l1_files") or []):
            file2dom[f] = name

    seq_map = wiki_aggregate.build_seq_map(Path(db))
    pages = wiki_aggregate.load_pages(l1, seq_map)
    all_files = {p["file"] for p in pages}
    new_files = all_files - set(file2dom)

    domain_map: dict[str, list[str]] = {
        n: list((m.get("l1_files") or [])) for n, m in dom_meta.items()}
    if new_files:
        client = wiki_aggregate.make_client("")
        # 归类视野增强：给每域几个现有页的真实标题，避免新页只凭"域名+一句话
        # why"被塞进语义不准的域（归错了要等下次全量才会修正）
        rep = {n: [p["title"] for p in pages if p["file"] in set(fs)]
               for n, fs in domain_map.items()}
        new_assign = wiki_aggregate.assign_new_pages(
            client, [{"name": n} for n in dom_meta],
            [p for p in pages if p["file"] in new_files], rep_pages=rep)
        for n, ps in new_assign.items():
            domain_map.setdefault(n, []).extend(p["file"] for p in ps)

    # 本次动过的 L1 文件 = 重编会话的产物 + 新文件 + 已删除会话的旧文件名
    touched = {"%s.md" % wiki_compile._out_stem(s, c) for s, c in dirty_sessions}
    touched |= new_files
    touched |= {f.rsplit(".", 1)[0] + ".md" for f in removed_files}
    dirty_domains = sorted(n for n, fs in domain_map.items() if set(fs) & touched)
    # **全量重跑：不预设任何域** —— 交给聚合层重新归域（`partial=False` 会覆盖写盘
    # 并清掉多余域目录、`no_resume` 会忽略既有 `_domains.json` 的域结构）。
    # 这正是"全量"存在的理由：增量归类**撑不住**时（hint 会提示"『待整理』域已有
    # N 页"），该重新归纳的是**域结构本身**，而不是再多塞几个域进去。
    if full:
        dirty_domains = []

    # ---- 4. 只重编脏域（partial 模式）----
    from types import SimpleNamespace
    # 域目录名：优先沿用 manifest 记录的现有目录；manifest 缺失时**回退到按
    # slug 在磁盘上找同名域目录**（早期实现直接兜底 "99-"+slug，导致 manifest
    # 信息一丢就新建 99-* 目录、与既有域目录并存——实测踩坑，L2 目录里一度
    # 躺着 9 个 99-* 残留目录）。真的找不到才新建（此时用 99- 前缀标记待整理）。
    existing_dirs = {d.name: d.name for d in l2.iterdir() if d.is_dir()}

    def _dir_for(name: str) -> str:
        rec = (dom_meta.get(name) or {}).get("dir")
        if rec:
            return rec
        slug = wiki_aggregate._slug(name)
        for dn in existing_dirs:
            if dn.split("-", 1)[-1] == slug:
                return dn
        return "99-" + slug

    preset = []
    for n in dirty_domains:
        fs = set(domain_map[n])
        members = [p for p in pages if p["file"] in fs]
        preset.append((n, _dir_for(n), members))
    # **离线模式下没有脏域就别往下走**：`preset` 为空时聚合层会回退到"全量归域 +
    # 全库重编"（实测 449 页，极贵）。而走到这里说明 L2 基线确实落后
    # （`a2.needs_recompile` 为真）却没有可定位的脏域 —— 典型场景正是"离线批次被
    # 平台判失败、已作废回池，但 L1 之后没有新变化"（2026-09-30 实测踩到）。
    # 正确动作是**重提**（`batch-resubmit` 复用原输入文件），而不是悄悄重编整库。
    if offline_runner is not None and not preset:
        return {"updated": False, "mode": "offline", "step": "nothing",
                "hints": ["没有可提交的脏域（L2 基线落后，但 L1 没有新变化可定位到域）"
                          "—— 请用 `agentmemhub wiki --action batch-resubmit` 重提"
                          "已终局的任务；确需全量重编请显式跑实时 update"]}
    args = SimpleNamespace(
        src=str(l1), out=str(l2), db=db, stage="all", workers=workers or 0,
        limit=0, domain="", dmin=20, dmax=60, batch=120, min_pages=2,
        recompile_single=False, thinking="", no_resume=full, resume=not full,
        retry_failed=False, partial=not full, preset_domains=preset,
        domain_map=domain_map,
        # **细分离线**（默认关）：细分只有「域数 × 批数」条（实测 3 个域 = 11 条），
        # 实时跑是分钟级；离线省下的绝对值很小，却要多付**一整轮排队延迟**
        # （实测细分段排了 24 小时）。要两段都离线就传 `offline_plan=True`。
        offline_runner=offline_runner if offline_plan else None,
        # **编译离线**（默认开）：编译是「每个最终页一条」（实测 113 条），正是离线
        # 划算的场景。两段都离线 ⇒ 一轮约 48 小时；只离线编译 ⇒ 一轮约 24 小时。
        offline_compile_runner=None if offline_plan else offline_runner)
    # L1 一条都没编出来时，L2 只会拿**磁盘上的旧 L1** 重跑一遍同样的域：
    # 实测白烧 1290 秒 / 42 万 prompt token，产出还只是"同义不同措辞"。
    # 全失败就跳过 —— 时间留给真正的重试（失败会话没进新基线，下次写入会重试）。
    l1_all_failed = bool(dirty_sessions) and not l1_ok and not removed_files
    fix: dict[str, Any] = {}
    # 聚合的**如实回报**（中止 / 跳过的域 / 实际写了哪些域）。三者都必须影响下面的
    # "新基线" —— 否则没真正编译的输入会被记成已编译，永久掩盖（本函数第 6 步
    # 开头那段注释讲的正是这类静默丢失，只是此前没有把中止这条路径接上）。
    l2_run: dict[str, Any] = {}
    l2_aborted = ""
    l2_skipped: list[str] = []
    #: 离线**提交轮**的回报（`{"batch_id", "n_requests", "n_domains", "domains", ...}`）。
    #: 它与 `aborted` 一样属于"本轮没写产物"，但**性质不同**：中止是出事了，
    #: 离线提交是**按计划把活交出去了**（结果几小时后才有）。
    l2_offline: dict[str, Any] = {}
    link_check: dict[str, Any] = {}
    if l1_all_failed:
        l2_stats: dict[str, Any] = {
            "domains_recompiled": 0, "pages": 0,
            "skipped": "L1 全部失败（%d 个会话），不基于旧 L1 重跑 L2" % len(l1_bad)}
    else:
        try:
            l2_run = wiki_aggregate.run(args) or {}
        except Exception as e:              # 聚合自身炸了 —— 与"主动中止"同等对待
            l2_run = {"aborted": "%s: %s" % (type(e).__name__, str(e)[:160])}
        l2_aborted = str(l2_run.get("aborted") or "")
        l2_skipped = [str(x) for x in (l2_run.get("skipped_domains") or [])]
        l2_offline = dict(l2_run.get("offline_submitted") or {})
        if l2_aborted or l2_offline:
            # **中止 / 离线提交时都不跑 linkfix**：产物没被重写，linkfix 却会按新标题
            # 去改旧页面（去链/改写），等于绕过闸门动了磁盘。
            l2_stats = ({"domains_recompiled": 0, "pages": 0,
                         "offline_submitted": l2_offline} if l2_offline else
                        {"domains_recompiled": 0, "pages": 0, "aborted": l2_aborted})
        else:
            # ---- 5. linkfix（L2 标题可能变）----
            import wiki_linkfix
            fix = wiki_linkfix.run_fix(l2, dry_run=False)
            # ---- 5b. 产物链接验收（Obsidian 权威口径）----
            # linkfix 已把链接写成 `[[文件名|显示]]` 形态；这里再用 Obsidian 自己的
            # CLI 复核 —— 不通过说明 linkfix 没兜住（新写法 / 新边界），必须让人
            # 看见，而不是等下一个人点出空白笔记来。全程旁路，不影响产物。
            link_check = _verify_links(l2)
            l2_stats = {"domains_recompiled": len(l2_run.get("written_domains") or []),
                        "pages": int(l2_run.get("pages") or 0),
                        "skipped_domains": l2_skipped}

    # ---- 6. 刷新两级 manifest（新基线）----
    # **只写实际编译成功的输入**，两类失败都要剔除，否则 align 会认为它们已经
    # 编译过、这批脏数据被永久掩盖（2026-09-21 实测的静默丢失）：
    #   · 会话级失败（整会话没编出来）
    #   · **组级失败** —— 逐组编译是 fail-open 的，会话整体算成功，但失败那几组
    #     的记忆其实没进任何页面（切网时实测丢了几组）
    bad_sessions = {(r.get("source"), r.get("cid")) for r in l1_bad}
    failed_ids: set[int] = set()
    for r in l1_results:
        failed_ids.update(r.get("failed_ids") or [])
    mf1 = wm.build_manifest("l1", db, drop_sessions=bad_sessions,
                            drop_ids=failed_ids)
    wm.write_manifest(wm.manifest_path(l1, "l1"), mf1)
    dirs = {n: (m.get("dir") or "") for n, m in dom_meta.items()}
    for n, dn, _ in preset:
        dirs[n] = dn
    pages_by_domain = dict(l2_run.get("pages_by_domain") or {})
    dom_out = {}
    for n, files in domain_map.items():
        fs = set(files)
        mids: set[int] = set()
        for p in pages:
            if p["file"] in fs:
                mids.update(int(x) for x in re.findall(r"\[m(\d+)\]", p.get("body") or ""))
                mids.update(int(t[1:]) for t in (p.get("mids") or [])
                            if t[1:].isdigit())
        dom_out[n] = {"dir": dirs.get(n, ""),
                      # **L2 页数**（聚合如实回报）；聚合没回报时退回"该域分到的
                      # L1 页数"并标注，避免把两种口径混成一个数字而不自知。
                      "pages": int(pages_by_domain.get(n, -1)) if n in pages_by_domain
                               else sum(1 for p in pages if p["file"] in fs),
                      "pages_source": ("l2" if n in pages_by_domain else "l1_fallback"),
                      "l1_files": sorted(fs), "mids": sorted(mids)}
    mf2 = None
    if l2_aborted or l2_offline:
        # **中止 / 离线提交时都绝不刷新 L2 清单** —— 保留旧基线，脏数据保持"脏"，
        # 下次写入（或离线回读轮）自然重试。刷新了就等于告诉 align"这批脏数据
        # 已编译"，它们再也不会被重跑。
        mf2 = wm.load_manifest(wm.manifest_path(l2, "l2")) or {"n_inputs": 0}
    else:
        # **没真正写出来的脏域，其输入绝不能进新基线** —— 没编译却记成已编译就是
        # 永久掩盖。判据与理由见 `_l2_skip_ids()`。
        written = set(l2_run.get("written_domains") or [])
        skip_ids = _l2_skip_ids(dom_out, dirty_domains, written, l2_skipped)
        mf2 = wm.build_manifest("l2", db, drop_sessions=bad_sessions,
                                drop_ids=failed_ids | skip_ids, extra={
            "src_dir": str(l1),
            # 与聚合自己写的那份同口径：**本轮实际产出的 L2 页数**
            "n_pages": int(l2_run.get("pages") or 0),
            "domains": dom_out})
        wm.write_manifest(wm.manifest_path(l2, "l2"), mf2)

    # 页面层投影：知识页变了，召回面必须跟着变（否则 Agent 检索到的是旧知识）。
    # 全量对齐、幂等——页面重编/改名/删除都会自动收敛。失败旁路不阻塞更新。
    # **必须记耗时**：实测 466 页要投 **4.5 分钟**，而它发生在两级 manifest 写完
    # 之后、`update()` 返回之前 —— 这段时间 wiki.log 一片空白，外部只看到
    # "run_end 了、触发状态却还是上一轮"，极易误判成卡住或标记没打上
    # （2026-09-26 实测因此误报过一次）。
    page_index: dict[str, Any] = {}
    t_proj = time.time()
    try:
        from agentmemhub import wiki_index
        from agentmemhub.rag.ingest import open_index
        from agentmemhub.rag.config import load_settings
        conn = open_index(load_settings().index_db)
        try:
            page_index = wiki_index.project_pages(conn, l2, log=lambda *_: None)
        finally:
            conn.close()
    except Exception as e:                      # noqa: BLE001
        page_index = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
    page_index["seconds"] = round(time.time() - t_proj, 1)
    _audit({"event": "page_index", "ok": "error" not in page_index,
            "seconds": page_index["seconds"],
            "pages": page_index.get("pages"),
            "inserted": page_index.get("inserted"),
            "updated": page_index.get("updated"),
            "removed": page_index.get("removed"),
            "skipped_empty": page_index.get("skipped_empty"),
            "error": page_index.get("error", "")})

    hints: list[str] = []
    n_pending = len(domain_map.get("待整理") or [])
    if n_pending:
        hints.append("『待整理』域已有 %d 页 —— 积累过多说明增量归类撑不住了，"
                     "建议择机全量重跑（会重新归纳域结构）" % n_pending)
    if l1_bad:
        hints.append("L1 有 %d 个会话重编失败，已记入失败清单，可用 --action retry 补跑"
                     % len(l1_bad))
    if failed_ids:
        hints.append("L1 有 %d 条记忆所在的**分组**编译失败（会话整体成功，但这几组"
                     "没产出页面）—— 已剔除出新基线，下次增量更新会重试"
                     % len(failed_ids))
    if l2_offline:
        hints.append("**L2 已提交离线批量任务**（batch=%s，%d 个域 / %d 条请求）——"
                     "产物保持上一轮、**未刷新 L2 基线**；结果要等厂商闲时调度后才有，"
                     "届时回读即可产出页面"
                     % (l2_offline.get("batch_id") or "(未知 id)",
                        l2_offline.get("n_domains", 0),
                        l2_offline.get("n_requests", 0)))
    if l2_aborted:
        hints.append("**L2 聚合本轮中止**（%s）—— 产物保持上一轮，且**未刷新 L2 基线**，"
                     "这批脏域会在下次写入时自动重试。常见原因是上游 LLM 大面积超时/限流"
                     % l2_aborted)
    if l2_skipped:
        hints.append("L2 有 %d 个域因**页数崩塌**被跳过（%s）—— 已保留旧产物且"
                     "**未计入新基线**，下次会重试；若反复出现说明该域的细分仍不稳定"
                     % (len(l2_skipped), "、".join(l2_skipped[:3])))
    # 链接验收：**不通过必须让人看见**（产物是给人看的，死链点一下就出现空白笔记）
    if link_check.get("available") and not link_check.get("ok"):
        hints.append("**产物链接验收未通过**：Obsidian 报 %d 个未解析目标 / %d 条链接"
                     "（如 %s）—— linkfix 没兜住，请先跑 "
                     "`python scripts/wiki_linkfix.py --dir <L2目录>` 再复查"
                     % (link_check.get("total_targets", 0),
                        link_check.get("total_links", 0),
                        "、".join(x["link"][:24] for x in (link_check.get("items") or [])[:2])))

    return {
        "updated": True,
        "db": db,
        "dirty_sessions": ["%s/%s" % (s, c) for s, c in sorted(dirty_sessions)],
        "l1": {"recompiled": len(l1_ok), "failed": len(l1_bad),
               "failed_ids": len(failed_ids),
               "removed_files": removed_files},
        "dirty_domains": dirty_domains,
        "l2": l2_stats,
        "linkfix": ({k: fix.get(k, 0) for k in ("total", "ok", "fixed", "dropped")}
                    if fix else {"skipped": True}),
        # 产物链接验收（Obsidian 权威口径）：available=False 表示"没验成"（它没运行），
        # 不是失败；ok=True 才是唯一通过标准。见 `_verify_links()`。
        "link_check": link_check,
        "manifest": {"l1_inputs": mf1["n_inputs"], "l2_inputs": mf2["n_inputs"]},
        "page_index": page_index,
        "snapshot": {"id": snap.get("id"),
                     "error": snap.get("error")},
        "hints": hints,
        "seconds": round(time.time() - t0, 1),
    }
