"""记忆库一致性巡检：指标采集与滞留投影回收。

为什么单独成模块：同一套判据要被三处复用 —— 面板「记忆报表」的健康卡片、
`scripts/health_check.py` 的命令行巡检、以及测试。三处各写一遍 SQL 迟早会
漂移，而"一致性指标本身漂移"是最糟的一种漂移。

三项判据（对应两次修复建立的不变量）：
1. **updated_at 覆盖率** —— NULL 行说明写它的进程仍在跑旧代码（MCP server /
   面板都是常驻进程，改完代码必须重启才生效）。
2. **蒸馏投影一致性** —— `role='distilled'` 的投影数应等于「有投影的活跃记
   忆数」。不等即为滞留投影（S2 归档时未回收），会与新终稿同时被召回。
3. **索引一致性** —— `units` 与 `units_fts` 行数相等；vec0 表无孤儿行。
"""
from __future__ import annotations

import sqlite3
from typing import Any


def _has(conn: sqlite3.Connection, name: str) -> bool:
    """表是否存在（全新索引库可能还没建 distill / FTS 表）。"""
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


def collect(conn: sqlite3.Connection) -> dict[str, Any]:
    """采集一致性指标（**只读**，不做任何修复）。"""

    def one(sql: str, *args) -> Any:
        return conn.execute(sql, args).fetchone()[0]

    vec_tables: dict[str, dict[str, int]] = {}
    for (t,) in conn.execute("SELECT name FROM sqlite_master"
                             " WHERE type='table' AND sql LIKE '%USING vec0%'"):
        vec_tables[t] = {
            "rows": one(f"SELECT COUNT(*) FROM {t}"),
            "orphans": one(f"SELECT COUNT(*) FROM {t} v"
                           " WHERE v.rowid NOT IN (SELECT id FROM units)"),
        }

    has_fts = _has(conn, "units_fts")
    m: dict[str, Any] = {
        "units": one("SELECT COUNT(*) FROM units"),
        "updated_at_null": one("SELECT COUNT(*) FROM units"
                               " WHERE updated_at IS NULL"),
        "has_fts": has_fts,
        "fts_rows": one("SELECT COUNT(*) FROM units_fts") if has_fts else 0,
        "vec_tables": vec_tables,
        "projections": 0,
        "active_with_projection": 0,
        "stale_projections": 0,
    }
    if _has(conn, "distilled_memories"):
        m["projections"] = one("SELECT COUNT(*) FROM units WHERE role='distilled'")
        # 有投影的活跃记忆：status 有效 且 units 里确有对应 dst_<content_hash>
        m["active_with_projection"] = one(
            "SELECT COUNT(*) FROM distilled_memories m"
            " WHERE m.status IN ('new','similar')"
            "   AND EXISTS (SELECT 1 FROM units u"
            "        WHERE u.src_id = 'dst_' || m.content_hash)")
        # 滞留投影：该内容 hash 已无任何有效条目，投影却还在（仍在被召回）
        m["stale_projections"] = one(
            "SELECT COUNT(*) FROM units u"
            " WHERE u.role='distilled' AND u.src_id LIKE 'dst_%'"
            "   AND NOT EXISTS (SELECT 1 FROM distilled_memories m"
            "        WHERE m.content_hash = substr(u.src_id, 5)"
            "          AND m.status IN ('new','similar'))")

    m["recent"] = [
        dict(zip(("id", "src_id", "time", "updated_at"), r))
        for r in conn.execute("SELECT id, src_id, time, updated_at"
                              " FROM units ORDER BY id DESC LIMIT 5")
    ]
    m["issues"] = diagnose(m)
    m["healthy"] = not m["issues"]
    return m


def diagnose(m: dict[str, Any]) -> list[str]:
    """把指标翻译成**可执行**的问题清单（空列表 = 全部正常）。"""
    issues: list[str] = []
    if m["updated_at_null"]:
        issues.append(
            f"{m['updated_at_null']} 行 units.updated_at 为空——写入它的进程仍在"
            "跑旧代码（重启 DSH/面板），或调 ensure_bridge_schema 自愈补齐")
    if m["stale_projections"]:
        issues.append(
            f"{m['stale_projections']} 条滞留投影仍留在召回面上——"
            "执行「回收滞留投影」，或跑一次蒸馏（会自动回收）")
    if m["projections"] != m["active_with_projection"]:
        issues.append(
            f"蒸馏投影数({m['projections']}) 与有投影的活跃记忆数"
            f"({m['active_with_projection']}) 不一致")
    if not m["has_fts"]:
        issues.append("units_fts 缺失——全文检索会退化为 LIKE 兜底，"
                      "重建一次检索索引即可恢复")
    elif m["fts_rows"] != m["units"]:
        issues.append(
            f"units_fts({m['fts_rows']}) 与 units({m['units']}) 行数不同步")
    for t, v in m["vec_tables"].items():
        if v["orphans"]:
            issues.append(f"向量表 {t} 有 {v['orphans']} 条孤儿行，需 GC")
    return issues


def reclaim(conn: sqlite3.Connection) -> dict[str, Any]:
    """回收滞留投影（幂等），返回回收条数与回收后的指标。

    与 `run_distill` 里自动执行的是同一个函数，所以手动点一次和跑一次蒸馏
    效果一致、可反复执行。
    """
    from agentmemhub.distill import reclaim_stale_projections

    n = reclaim_stale_projections(conn)
    return {"reclaimed": n, "health": collect(conn)}
