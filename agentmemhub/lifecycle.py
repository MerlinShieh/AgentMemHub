"""记忆生命周期的 service 层（M3）。

**为什么单独一层**：面板（HTTP）与 CLI 必须说同一套话。此前对齐审计踩过
「同一段 SQL 在两处各写一遍、然后悄悄漂移」的坑，这里只写一次，两侧都调它。

职责边界：

- **读**：候选清单（stale + 证据分层）、冲突队列、概览数字
- **写**：归档 / 恢复 / 冲突处置
- **不做**：不自动归档、不自动删除、不给 MCP 入口（处置是人工决策，
  与"投喂不给删除"同一纪律，见 `docs/memory-lifecycle.md` §6）

**归档语义**见 `conflicts.archive_unit()`：召回层（`manual_value=0`）与
wiki 编译层（`wiki_ignore_at`）**双层退场**，且按 `content_hash` 把
`dst_` / `mcp_` 两侧投影**一起**锁 —— 只锁一层等于没归档（2026-11-07 实测）。
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

from agentmemhub import conflicts
from agentmemhub.rag.recall_stats import (TIER_NEVER_HIT, TIER_CONFLICT_OLD,
                                          stale_memories)

#: 候选清单默认窗口（天）：创建满 N 天 + 最近 N 天零命中。
DEFAULT_DAYS = 90

#: 单次批量操作上限 —— 防一手「误点全选」把半个知识库归档掉。
#: 归档会改变 wiki 基线，一次影响太多条目会导致下次增量重编范围过大。
MAX_BATCH = 100

__all__ = [
    "MAX_BATCH", "DEFAULT_DAYS", "TIER_NEVER_HIT", "TIER_CONFLICT_OLD",
    "stale_candidates", "archived_units", "summary", "archive_units",
    "restore_units", "list_conflicts", "resolve_conflict", "scan_conflicts",
]


def _connect(index_db: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(index_db), timeout=15.0)
    conn.row_factory = sqlite3.Row
    return conn


def _audit(event: str, **fields: Any) -> None:
    """写一条生命周期事实到 `logs/memory.log`（旁路，失败绝不影响操作）。"""
    try:
        from agentmemhub.logs import audit_memory
        audit_memory({"event": event, **fields})
    except Exception:                                    # noqa: BLE001
        pass


def _norm_ids(unit_ids: Iterable[Any]) -> list[int]:
    out: list[int] = []
    for raw in unit_ids:
        try:
            uid = int(raw)
        except (TypeError, ValueError):
            continue
        if uid not in out:
            out.append(uid)
    return out


def _check_batch(ids: list[int]) -> None:
    if len(ids) > MAX_BATCH:
        raise ValueError(f"单次最多处理 {MAX_BATCH} 条，收到 {len(ids)} 条")


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------

def stale_candidates(index_db: Path | str, *, days: int = DEFAULT_DAYS,
                     limit: int = 200) -> list[dict]:
    """候选清单（已排除归档/锁定条目），每条带 `tier`。

    排除规则与理由见 `recall_stats.stale_memories()`：用户表过态
    （`manual_value` 非空）的一律不出现 —— 归档过的再列出来等于让人
    重复确认一件做过的事。
    """
    return stale_memories(index_db, days=days, limit=limit)


def summary(index_db: Path | str, *, days: int = DEFAULT_DAYS) -> dict:
    """面板概览：候选数、已归档数、锁定数、冲突分状态。"""
    conn = _connect(index_db)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        archived = pinned = 0
        if "unit_values" in tables:
            archived = int(conn.execute(
                "SELECT COUNT(*) FROM unit_values WHERE manual_value=0"
            ).fetchone()[0])
            pinned = int(conn.execute(
                "SELECT COUNT(*) FROM unit_values"
                " WHERE manual_value IS NOT NULL AND manual_value<>0"
            ).fetchone()[0])
        cf_stats = (conflicts.stats(conn) if "memory_conflicts" in tables
                    else {"total": 0, "by_status": {}})
    finally:
        conn.close()

    cands = stale_memories(index_db, days=days, limit=5000)
    return {
        "days": days,
        "candidates": len(cands),
        "candidates_conflict_old": sum(
            1 for c in cands if c.get("tier") == TIER_CONFLICT_OLD),
        "archived": archived,
        "pinned": pinned,
        "conflicts_total": cf_stats.get("total", 0),
        "conflicts_by_status": cf_stats.get("by_status", {}),
    }


def list_conflicts(index_db: Path | str, *, status: str = "open",
                   limit: int = 50) -> list[dict]:
    """冲突队列（带两侧正文摘要）。`status=""` 表示不过滤。"""
    conn = _connect(index_db)
    try:
        return conflicts.list_conflicts(conn, status=status, limit=limit)
    finally:
        conn.close()


def archived_units(index_db: Path | str, *, limit: int = 200) -> list[dict]:
    """已归档条目（`manual_value = 0`），最近归档的在前 —— 供面板"恢复"用。

    双投影去重：归档会把 `dst_` / `mcp_` 两侧**一起**锁 0，所以原始查询会
    返回两条同内容记录；这里复用候选清单的去重口径（保留 `dst_`）。
    """
    from agentmemhub.rag.recall_stats import dedup_projections

    conn = _connect(index_db)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "unit_values" not in tables:
            return []
        rows = conn.execute(
            "SELECT u.id, u.src_id, u.source, u.role, u.time, u.title,"
            "       substr(u.text, 1, 160) AS text,"
            "       v.manual_value, v.updated_at"
            "  FROM unit_values v JOIN units u ON u.id = v.unit_id"
            " WHERE v.manual_value = 0"
            " ORDER BY v.updated_at DESC, u.id DESC"
            " LIMIT ?", (max(1, int(limit)),)).fetchall()
    finally:
        conn.close()
    return dedup_projections([dict(r) for r in rows])


# ---------------------------------------------------------------------------
# 写
# ---------------------------------------------------------------------------

def archive_units(index_db: Path | str, unit_ids: Iterable[Any], *,
                  by: str = "cli", reason: str = "") -> dict:
    """归档若干记忆：召回层 + wiki 层双层退场，双投影一起锁。

    返回三段式结果，**部分失败必须能看见**：
    `{"changed": [{id, distilled_tagged, archived_units}], "skipped": [...], "errors": [...]}`

    幂等：已归档（`manual_value=0`）的进 `skipped`，不重复操作。
    """
    ids = _norm_ids(unit_ids)
    _check_batch(ids)
    if not ids:
        return {"changed": [], "skipped": [], "errors": []}

    conn = _connect(index_db)
    changed: list[dict] = []
    skipped: list[dict] = []
    errors: list[dict] = []
    try:
        now = int(time.time())
        for uid in ids:
            try:
                if conn.execute("SELECT 1 FROM units WHERE id=?",
                                (uid,)).fetchone() is None:
                    skipped.append({"id": uid, "why": "不存在"})
                    continue
                mv = conn.execute(
                    "SELECT manual_value FROM unit_values WHERE unit_id=?",
                    (uid,)).fetchone()
                if mv is not None and mv[0] == 0:
                    skipped.append({"id": uid, "why": "已归档"})
                    continue
                arch = conflicts.archive_unit(conn, uid, ts=now)
                changed.append({"id": uid, **arch})
            except Exception as e:                       # noqa: BLE001
                errors.append({"id": uid, "error": f"{type(e).__name__}: {e}"})
        conn.commit()
    finally:
        conn.close()

    _audit("lifecycle_archive", ok=not errors, by=by, reason=reason,
           requested=len(ids), changed=len(changed), skipped=len(skipped),
           errors=len(errors), unit_ids=[c["id"] for c in changed])
    return {"changed": changed, "skipped": skipped, "errors": errors}


def restore_units(index_db: Path | str, unit_ids: Iterable[Any], *,
                  by: str = "cli") -> dict:
    """恢复归档：清 `manual_value` 与 `wiki_ignore_at`（双投影两侧一起）。

    恢复后 align 会把该记忆判成 `added`，**下次增量会重编受影响页面**。
    """
    ids = _norm_ids(unit_ids)
    _check_batch(ids)
    if not ids:
        return {"changed": [], "skipped": [], "errors": []}

    from agentmemhub.rag.memstore import set_manual_value

    conn = _connect(index_db)
    changed: list[dict] = []
    skipped: list[dict] = []
    errors: list[dict] = []
    try:
        for uid in ids:
            try:
                row = conn.execute("SELECT src_id FROM units WHERE id=?",
                                   (uid,)).fetchone()
                if row is None:
                    skipped.append({"id": uid, "why": "不存在"})
                    continue
                targets = [uid]
                head, _, h = (row[0] or "").partition("_")
                if head in ("dst", "mcp") and h:
                    # 双投影：两侧都要恢复，否则另一行仍被锁着（与归档对称）
                    for prefix in ("dst", "mcp"):
                        r = conn.execute(
                            "SELECT id FROM units WHERE src_id=?",
                            (f"{prefix}_{h}",)).fetchone()
                        if r is not None and r[0] not in targets:
                            targets.append(int(r[0]))
                for t in targets:
                    set_manual_value(conn, t, None)
                if head in ("dst", "mcp") and h:
                    cols = {r[1] for r in conn.execute(
                        "PRAGMA table_info(distilled_memories)")}
                    if "wiki_ignore_at" in cols:
                        conn.execute(
                            "UPDATE distilled_memories SET wiki_ignore_at=NULL"
                            " WHERE content_hash=?", (h,))
                changed.append({"id": uid, "restored_units": targets})
            except Exception as e:                       # noqa: BLE001
                errors.append({"id": uid, "error": f"{type(e).__name__}: {e}"})
        conn.commit()
    finally:
        conn.close()

    _audit("lifecycle_restore", ok=not errors, by=by, requested=len(ids),
           changed=len(changed), skipped=len(skipped), errors=len(errors),
           unit_ids=[c["id"] for c in changed])
    return {"changed": changed, "skipped": skipped, "errors": errors}


def resolve_conflict(index_db: Path | str, conflict_id: int, action: str, *,
                     reason: str = "", by: str = "cli") -> dict:
    """处置一条冲突：`superseded` / `kept_both` / `dismissed`。

    `superseded` 会归档旧方（与 `archive_units()` 同一条底层路径）。
    """
    conn = _connect(index_db)
    try:
        r = conflicts.resolve(conn, int(conflict_id), action,
                              reason=reason, by=by)
    finally:
        conn.close()
    _audit("lifecycle_conflict_resolve", ok=True, by=by,
           conflict_id=int(conflict_id), action=action,
           changed=bool(r.get("changed")))
    return r


def scan_conflicts(index_db: Path | str, *, k: int = 10,
                   min_similarity: float = 0.90, limit: int = 0) -> dict:
    """存量扫描（纯 SQL KNN，不加载模型，2 万条约分钟级）。"""
    from agentmemhub.rag.config import load_settings
    from agentmemhub.rag.ingest import open_index

    settings = load_settings()
    conn = open_index(settings.index_db)
    try:
        return conflicts.scan(conn, vec_table=settings.active_spec.vec_table,
                              k=k, min_similarity=min_similarity, limit=limit)
    finally:
        conn.close()
