# -*- coding: utf-8 -*-
"""待更新记忆的查看与删除（软删除 = 不进 wiki；硬删除 = 真删）。

与 `docs/memory-deletion.md` 的边界（务必分清）
================================================
* **本模块**：面向**尚未进 wiki** 的记忆 —— 即 `align` 报出来的"待更新"
  （added / changed）。它们**没有被任何页面引用**，所以删除**不产生死链**。
  这是最简单的那个场景，也是日常唯一的入口。
* `memory-deletion.md`：面向**已经进 wiki** 的记忆。硬删会让页面正文里的
  `[m<id>]` 变成**死引用**，需要墓碑表 + 引用重映射/占位，复杂度高一个量级。
  本模块**刻意不处理它** —— `drop()` 会拒绝任何"已进 wiki"的 id（见
  `_filter_pending`），以免在缺少那套机制时把引用打断。

两种删除的语义
==============
* **软删除**（写 `distilled_memories.wiki_ignore_at`）：记忆**仍然存在、仍然
  能被召回**，只是**不再参与 wiki 编译**。用于"我看过这条，它不该进知识库"。
  可取消（`restore`）。软删一条**待更新**的记忆**不会触发任何重编** ——
  它本来就不在 manifest 基线里，编译输入里去掉它，diff 依然是零。
* **硬删除**：真删 —— 蒸馏表行 + units 投影 + 向量（FTS 由触发器同步）。
  **不可恢复**（除非从源库重蒸），所以接口层要求显式 `confirm`。
  删完必须**重建 manifest**，否则 align 会把这批 id 报成"失去输入"而触发
  一次毫无意义的重编（页面里本来就引用不到它们）。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

from agentmemhub import wiki_manifest as wm

#: 明细里正文摘要的截断长度（够判断"这条该不该删"即可）
SUMMARY_CHARS = 160

#: 删除模式
MODE_SOFT = "soft"
MODE_HARD = "hard"
MODES = (MODE_SOFT, MODE_HARD)


def _index_db() -> str:
    from agentmemhub.rag.config import load_settings
    return str(load_settings().index_db)


def _resolve(l1_dir: Path | str = "", db: str = "") -> tuple[str, str] | dict[str, Any]:
    """解析 (l1_dir, db)；缺一不可时返回带 error 的 dict 供调用方直接回。"""
    if not db:
        db = _index_db()
    if not l1_dir:
        from agentmemhub import config as hub_config
        l1_dir = (hub_config.config().wiki.get("out_l1") or "").strip()
    if not l1_dir:
        return {"error": "未配置 wiki.out_l1（也未传 l1_dir），无法定位编译清单"}
    return str(l1_dir), db


def _pending_ids(l1_dir: str, db: str) -> dict[str, Any]:
    """待更新 id 集合 + diff 原始结果（added + changed，去重保序）。"""
    mf = wm.load_manifest(wm.manifest_path(l1_dir, "l1"))
    if mf is None:
        return {"error": "缺少 manifest_l1.json（编译清单）—— 先跑一次编译建立锚点"}
    d = wm.diff_manifest(mf, db)
    ids = list(dict.fromkeys(list(d["added"]) + list(d["changed"])))
    return {"ids": ids, "diff": d, "manifest": mf}


def _readonly(db: str) -> sqlite3.Connection:
    return sqlite3.connect("file:%s?mode=ro" % Path(db).as_posix(), uri=True)


def _rows(conn: sqlite3.Connection, ids: list[int]) -> list[dict[str, Any]]:
    if not ids:
        return []
    q = ",".join("?" * len(ids))
    out: list[dict[str, Any]] = []
    for mid, src, cid, mtype, status, conf, chars, created, summary in conn.execute(
            "SELECT id, source, conversation_id, type, status, confidence, "
            "length(content), created_at, substr(content,1,?) "
            "FROM distilled_memories WHERE id IN (%s) ORDER BY created_at, id"
            % q, [SUMMARY_CHARS] + ids):
        out.append({"id": mid, "source": src, "conversation_id": cid,
                    "type": mtype, "status": status, "confidence": conf,
                    "chars": chars, "created_at": created,
                    "summary": (summary or "").replace("\n", " ")})
    return out


def pending(*, l1_dir: Path | str = "", db: str = "",
            limit: int = 0) -> dict[str, Any]:
    """列出**待 wiki 更新**的记忆明细（align 的 added + changed）。

    复用 `wiki_manifest.diff_manifest`（与 align 同一份口径），再按 id 取明细 ——
    光有 id 列表没法判断"该不该删"，必须带来源、类型、时间与正文摘要。
    """
    r = _resolve(l1_dir, db)
    if isinstance(r, dict):
        return r
    l1, db = r
    st = _pending_ids(l1, db)
    if "error" in st:
        return st
    ids: list[int] = st["ids"]
    d = st["diff"]
    conn = _readonly(db)
    try:
        items = _rows(conn, ids)
    finally:
        conn.close()
    if limit and limit > 0:
        items = items[:limit]
    return {
        "l1": l1, "db": db,
        "added_total": d["added_total"], "changed_total": d["changed_total"],
        "count": len(ids), "shown": len(items),
        "dirty_sessions": d["dirty_sessions"],
        "items": items,
    }


def ignored(*, l1_dir: Path | str = "", db: str = "",
            limit: int = 0) -> dict[str, Any]:
    """列出已被软删除（`wiki_ignore_at` 非空）的记忆 —— 供取消忽略用。

    按忽略时间倒序（最近忽略的在前）。
    """
    r = _resolve(l1_dir, db)
    if isinstance(r, dict):
        return r
    l1, db = r
    conn = _readonly(db)
    try:
        cols = {x[1] for x in conn.execute("PRAGMA table_info(distilled_memories)")}
        if "wiki_ignore_at" not in cols:
            return {"l1": l1, "db": db, "count": 0, "shown": 0, "items": [],
                    "note": "尚无 wiki_ignore_at 列（还没有人软删除过）"}
        ids = [x[0] for x in conn.execute(
            "SELECT id FROM distilled_memories WHERE wiki_ignore_at IS NOT NULL "
            "ORDER BY wiki_ignore_at DESC, id DESC")]
        items = _rows(conn, ids)
    finally:
        conn.close()
    # _rows 按 created_at 排，这里重排成"忽略时间序"（与 ids 一致）
    order = {mid: i for i, mid in enumerate(ids)}
    items.sort(key=lambda x: order.get(x["id"], 1 << 30))
    if limit and limit > 0:
        items = items[:limit]
    return {"l1": l1, "db": db, "count": len(ids), "shown": len(items),
            "items": items}


def _filter_pending(ids: list[int], l1: str, db: str) -> tuple[list[int], list[dict]]:
    """只保留**待更新**的 id；其余按原因回报（不静默丢弃）。"""
    st = _pending_ids(l1, db)
    if "error" in st:
        return [], [{"reason": st["error"]}]
    ok_set = set(st["ids"])
    accepted = [i for i in ids if i in ok_set]
    rejected = [{"id": i, "reason": "不在待更新列表里（已进 wiki 或本来就没有变更）"}
                for i in ids if i not in ok_set]
    return accepted, rejected


def drop(*, ids: list[int], mode: str = MODE_SOFT, confirm: bool = False,
         l1_dir: Path | str = "", db: str = "") -> dict[str, Any]:
    """删除待更新的记忆。

    mode=soft：写 `wiki_ignore_at`（仍可召回，只是不进 wiki）；可 `restore()`。
    mode=hard：真删蒸馏表行 + units 投影 + 向量；**需 `confirm=True`**。
    """
    if not ids:
        return {"error": "ids 不能为空"}
    if mode not in MODES:
        return {"error": "mode 必须是 %s 之一" % " / ".join(MODES)}
    if mode == MODE_HARD and not confirm:
        return {"error": "硬删除不可恢复，需显式 confirm=true",
                "hint": "soft=只标记不进 wiki（可恢复）；hard=真删"}
    r = _resolve(l1_dir, db)
    if isinstance(r, dict):
        return r
    l1, db = r

    accepted, rejected = _filter_pending(list(dict.fromkeys(ids)), l1, db)
    if not accepted:
        return {"dropped": 0, "mode": mode, "rejected": rejected,
                "error": "没有任何 id 属于待更新列表（拒绝原因见 rejected）"}

    if mode == MODE_SOFT:
        res = _soft_drop(accepted, db)
    else:
        res = _hard_drop(accepted, db, l1)
    res.update({"mode": mode, "rejected": rejected})
    return res


def _soft_drop(ids: list[int], db: str) -> dict[str, Any]:
    """打忽略标记。**不重建 manifest** —— 待更新的记忆本来就不在基线里，
    编译输入里去掉它，align 的 diff 依然是零，不会触发重编。"""
    from agentmemhub.distill import ensure_distill_schema
    from agentmemhub.rag.ingest import open_index
    idx = open_index(db)
    try:
        ensure_distill_schema(idx)              # 保证列存在（幂等）
        q = ",".join("?" * len(ids))
        cur = idx.execute(
            "UPDATE distilled_memories SET wiki_ignore_at=? WHERE id IN (%s)" % q,
            [int(time.time())] + ids)
        idx.commit()
        n = cur.rowcount
    finally:
        idx.close()
    return {"dropped": n, "ids": ids,
            "note": "已标记为「不进 wiki」；记忆仍存在、仍可召回，可 restore 取消"}


def _hard_drop(ids: list[int], db: str, l1: str) -> dict[str, Any]:
    """真删：蒸馏表行 + **两份召回投影** + 向量；然后**重建 manifest**。

    投影有两份，只清一份等于"删了还能搜到"：
      · `dst_<hash>` —— 蒸馏投影（`drop_projection`）
      · `mcp_<hash>` —— **Agent 直写落账单元**（`drop_agent_write_unit`），
        由 `memory_save` 写引擎时产生。两者用**同一个内容 hash**。
        2026-09-22 实测踩到：只清 `dst_` 时，被删记忆仍能被检索命中。

    重建 manifest 是关键一步：库里行没了而 manifest 还留着 → align 会报"失去输入"→
    明明页面里引用不到它们，却触发一次无意义的重编。
    """
    from agentmemhub.distill import drop_agent_write_unit, drop_projection
    from agentmemhub.rag.ingest import open_index
    idx = open_index(db)
    hashes: list[str] = []
    proj = 0
    try:
        q = ",".join("?" * len(ids))
        hashes = [h for (h,) in idx.execute(
            "SELECT content_hash FROM distilled_memories WHERE id IN (%s)" % q,
            ids) if h]
        for h in hashes:
            proj += drop_projection(idx, h)          # dst_（蒸馏投影）
            proj += drop_agent_write_unit(idx, h)    # mcp_（直写落账）
        cur = idx.execute(
            "DELETE FROM distilled_memories WHERE id IN (%s)" % q, ids)
        idx.commit()
        n = cur.rowcount
    finally:
        idx.close()

    # 重建 manifest（从库当前状态）。此时这些 id 已不存在，自然不会进基线。
    mf = wm.load_manifest(wm.manifest_path(l1, "l1"))
    snap: dict[str, Any] = {}
    if mf is not None:
        try:
            mf2 = wm.build_manifest("l1", db)
            wm.write_manifest(wm.manifest_path(l1, "l1"), mf2)
            snap = {"manifest_inputs": mf2["n_inputs"]}
        except Exception as e:                  # noqa: BLE001 —— 重建失败不回滚删除
            snap = {"manifest_error": "%s: %s" % (type(e).__name__, str(e)[:160])}
    return {"dropped": n, "ids": ids, "projections_removed": proj,
            "note": "已从蒸馏表与召回面彻底删除（不可恢复）", **snap}


def restore(*, ids: list[int], l1_dir: Path | str = "", db: str = "") -> dict[str, Any]:
    """取消软删除（清空 `wiki_ignore_at`）→ 它们重新变回待更新。"""
    if not ids:
        return {"error": "ids 不能为空"}
    r = _resolve(l1_dir, db)
    if isinstance(r, dict):
        return r
    l1, db = r
    from agentmemhub.distill import ensure_distill_schema
    from agentmemhub.rag.ingest import open_index
    idx = open_index(db)
    try:
        ensure_distill_schema(idx)
        cols = {x[1] for x in idx.execute("PRAGMA table_info(distilled_memories)")}
        if "wiki_ignore_at" not in cols:
            return {"restored": 0, "note": "尚无 wiki_ignore_at 列"}
        q = ",".join("?" * len(ids))
        cur = idx.execute(
            "UPDATE distilled_memories SET wiki_ignore_at=NULL WHERE id IN (%s)" % q,
            ids)
        idx.commit()
        n = cur.rowcount
    finally:
        idx.close()
    return {"restored": n, "ids": ids,
            "note": "已取消忽略；它们会重新出现在待更新列表里"}
