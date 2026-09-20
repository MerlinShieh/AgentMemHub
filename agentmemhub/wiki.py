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


def align(out_dir: Path | str, stage: str = "", db: str = "") -> dict[str, Any]:
    """对齐审计：回答「wiki 和 RAG 库现在差多少」（**全程只读**）。

    三个方向：
      1. 反向有效 —— 页面引用的每个 `[m<id>]` 在库里是否仍存在、是否仍是输入；
      2. 正向覆盖 —— 当前库的输入记忆有多少被页面引用过；
      3. 变更检测 —— 有 manifest 时精确 diff（added/changed/removed + 脏会话/脏域），
         没有 manifest 则退回"引用反推"模式，只做 1、2 并明确标记缺锚点。

    stage 留空 = l1、l2 都查（哪个目录有 manifest 查哪个）。
    返回结构可直接 JSON 序列化（CLI 打印 / 面板展示共用）。
    """
    out = Path(out_dir)
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

    stages: dict[str, Any] = {}
    check = ["l1", "l2"] if not stage else [stage]
    for st in check:
        mf = wm.load_manifest(wm.manifest_path(out, st))
        if mf is None:
            stages[st] = {"manifest": False}
            continue
        d = wm.diff_manifest(mf, db)
        extra: dict[str, Any] = {
            "manifest": True,
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
               + ("；⚠️ 需要重编译" if needs else "；✓ 库未变化"))

    return {
        "out": str(out),
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


def update(*, l1_dir: Path | str, l2_dir: Path | str, db: str = "",
           workers: int = 0, on_progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """增量更新：RAG 库变了，只重编受影响的 L1 页与 L2 域（**不全量重来**）。

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

    # ---- 1. 对齐审计 ----
    a1 = align(l1, "l1", db)
    a2 = align(l2, "l2", db)
    if not (a1["stages"].get("l1", {}).get("manifest")
            and a2["stages"].get("l2", {}).get("manifest")):
        return {"updated": False,
                "message": "缺少编译清单（manifest_*.json）—— 先全量编译一次建立锚点"}
    if not (a1["needs_recompile"] or a2["needs_recompile"]):
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
    if not dirty_sessions:
        return {"updated": False, "message": "没有可定位的脏会话"}

    import wiki_aggregate
    import wiki_compile

    # ---- 2. 重编脏会话的 L1 ----
    fl1 = FailureLog(l1 / "failures.jsonl")
    l1_results = wiki_compile.compile_all(
        Path(db), l1, workers=workers or 4, resume=False,
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

    # ---- 4. 只重编脏域（partial 模式）----
    from types import SimpleNamespace
    preset = []
    for n in dirty_domains:
        fs = set(domain_map[n])
        members = [p for p in pages if p["file"] in fs]
        dn = (dom_meta.get(n) or {}).get("dir") or ("99-" + wiki_aggregate._slug(n))
        preset.append((n, dn, members))
    args = SimpleNamespace(
        src=str(l1), out=str(l2), db=db, stage="all", workers=workers or 0,
        limit=0, domain="", dmin=20, dmax=60, batch=120, min_pages=2,
        recompile_single=False, thinking="", no_resume=False, resume=True,
        retry_failed=False, partial=True, preset_domains=preset,
        domain_map=domain_map)
    wiki_aggregate.run(args)

    # ---- 5. linkfix（L2 标题可能变）----
    import wiki_linkfix
    fix = wiki_linkfix.run_fix(l2, dry_run=False)

    # ---- 6. 刷新两级 manifest（新基线）----
    mf1 = wm.build_manifest("l1", db)
    wm.write_manifest(wm.manifest_path(l1, "l1"), mf1)
    dirs = {n: (m.get("dir") or "") for n, m in dom_meta.items()}
    for n, dn, _ in preset:
        dirs[n] = dn
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
                      "pages": sum(1 for p in pages if p["file"] in fs),
                      "l1_files": sorted(fs), "mids": sorted(mids)}
    mf2 = wm.build_manifest("l2", db, extra={
        "src_dir": str(l1), "n_pages": sum(len(p) for _, p in dom_out.items()),
        "domains": dom_out})
    wm.write_manifest(wm.manifest_path(l2, "l2"), mf2)

    # 页面层投影：知识页变了，召回面必须跟着变（否则 Agent 检索到的是旧知识）。
    # 全量对齐、幂等——页面重编/改名/删除都会自动收敛。失败旁路不阻塞更新。
    page_index: dict[str, Any] = {}
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

    hints: list[str] = []
    n_pending = len(domain_map.get("待整理") or [])
    if n_pending:
        hints.append("『待整理』域已有 %d 页 —— 积累过多说明增量归类撑不住了，"
                     "建议择机全量重跑（会重新归纳域结构）" % n_pending)
    if l1_bad:
        hints.append("L1 有 %d 个会话重编失败，已记入失败清单，可用 --action retry 补跑"
                     % len(l1_bad))

    return {
        "updated": True,
        "db": db,
        "dirty_sessions": ["%s/%s" % (s, c) for s, c in sorted(dirty_sessions)],
        "l1": {"recompiled": len(l1_ok), "failed": len(l1_bad),
               "removed_files": removed_files},
        "dirty_domains": dirty_domains,
        "l2": {"domains_recompiled": len(preset),
               "pages": sum(len(m) for _, _, m in preset)},
        "linkfix": {k: fix[k] for k in ("total", "ok", "fixed", "dropped")},
        "manifest": {"l1_inputs": mf1["n_inputs"], "l2_inputs": mf2["n_inputs"]},
        "page_index": page_index,
        "snapshot": {"id": snap.get("id"),
                     "error": snap.get("error")},
        "hints": hints,
        "seconds": round(time.time() - t0, 1),
    }
