"""召回命中统计与低价值记忆报表——记忆生命周期闭环的「最后一块」。

背景（2026-11-06 用户决策）：数据只增不减。软删除有了（`wiki_pending`）、
wiki 增量有了，缺的是「低价值记忆定期清理」的**依据**。本模块两件事：

1. **记事实**：`record_hits()`——每次检索的最终命中记入独立命中库
   `recall_hits.db`（与索引库同目录、**分库**，聚合式一行一 unit）。
   为什么分库而不是写进 `session_rag.db`：检索是读路径，不该在索引库上
   制造写锁竞争；命中统计是"观测事实"，快照回滚索引也不该把它一起回滚。
2. **出报表**：`stale_memories()`——「创建已满 N 天（默认 90）且最近 N 天
   零召回命中」的记忆清单，按最老优先，供人工清理决策（只报告、不删除）。

冷启动说明：命中统计自本模块落地才开始积累，**此前没有任何命中数据**——
报表把"无统计"视为"从未命中"，因此上线首日报表≈全部老记忆，属预期；
跑一段时间后它才开始有区分度。

口径约定：
- 「记忆」= `role='distilled'`（蒸馏投影）或 `source='memory'`（Agent 直写），
  与评分枚举的口径一致；消息层（对话轮）不参与本报表。
- 「命中」= `hybrid_search` **最终返回**的条目（准入+多样性+精判之后），
  不是候选池——候选只说明"进过海选"，返回才算真被用过。
- 双投影（`mcp_<hash>` 与 `dst_<hash>` 同内容各占一行 unit）本报表**不去重**，
  清理时由人工按内容判断；后续如需可按 src_hash 归并。
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS unit_recall_stats(
  unit_id     INTEGER PRIMARY KEY,
  hit_count   INTEGER NOT NULL DEFAULT 0,
  first_hit_at INTEGER NOT NULL,
  last_hit_at  INTEGER NOT NULL
)
"""


def hits_db_path(index_db: Path) -> Path:
    """命中库路径：与索引库同目录（回滚/快照都不覆盖它）。"""
    return Path(index_db).with_name("recall_hits.db")


def _connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")   # 统计库丢最后一两次可接受
    return conn


def record_hits(index_db: Path, unit_ids: list[int],
                ts: float | None = None) -> int:
    """把一次检索的最终命中记入统计库（聚合 upsert，一次事务）。

    **绝不抛错**：统计是旁路观测，任何失败只返回 0——检索结果不能被它影响
    （这也是埋点处不必再包 try 的契约；外层再包一层也无妨）。
    """
    ids = [int(i) for i in unit_ids if i is not None]
    if not ids:
        return 0
    now = int(ts if ts is not None else time.time())
    db = hits_db_path(index_db)
    try:
        conn = _connect(db)
        try:
            conn.execute(_SCHEMA)
            conn.executemany(
                "INSERT INTO unit_recall_stats(unit_id, hit_count,"
                " first_hit_at, last_hit_at) VALUES(?,1,?,?)"
                " ON CONFLICT(unit_id) DO UPDATE SET"
                " hit_count=hit_count+1, last_hit_at=excluded.last_hit_at",
                [(i, now, now) for i in ids])
            conn.commit()
        finally:
            conn.close()
        return len(ids)
    except Exception:
        return 0


def stale_memories(index_db: Path, *, days: int = 90,
                    limit: int = 200) -> list[dict[str, Any]]:
    """低价值记忆报表：创建已满 `days` 天且最近 `days` 天零召回命中的记忆。

    排序：最老优先（清理候选通常从"最老且从未被用"开始）。
    只读、不删除——清理动作由人工经既有入口（软删除/硬删除）执行。
    """
    cutoff = int(time.time()) - int(days) * 86400
    conn = sqlite3.connect(index_db, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        # unit_values 由 memstore 在首次写入时建；空库里 LEFT JOIN 会直接
        # 报错——用"存在才 JOIN"避免报表依赖写入侧先跑过。
        if "unit_values" in tables:
            v_join = "LEFT JOIN unit_values v ON v.unit_id = u.id"
            v_cols = ("COALESCE(v.manual_value, v.value) AS value,"
                      " COALESCE(v.priority, 0) AS priority")
        else:
            v_join = ("LEFT JOIN (SELECT NULL AS unit_id) v ON v.unit_id = u.id")
            v_cols = "NULL AS value, 0 AS priority"
        # 命中统计在**独立命中库**（见模块头"为什么分库"）——ATTACH 过来 JOIN；
        # 文件尚不存在 = 从未记录过命中，全部按"零命中"处理，无需 JOIN。
        hdb = hits_db_path(index_db)
        if hdb.exists():
            conn.execute("ATTACH DATABASE ? AS h", (str(hdb),))
            conn.execute("CREATE TABLE IF NOT EXISTS h.unit_recall_stats("
                         "unit_id INTEGER PRIMARY KEY, hit_count INTEGER"
                         " NOT NULL DEFAULT 0, first_hit_at INTEGER NOT NULL,"
                         " last_hit_at INTEGER NOT NULL)")
            s_join = "LEFT JOIN h.unit_recall_stats s ON s.unit_id = u.id"
            s_cols = "COALESCE(s.hit_count, 0) AS hit_count, s.last_hit_at"
            s_cond = "AND (s.unit_id IS NULL OR s.last_hit_at <= ?)"
            args: list[Any] = [cutoff, cutoff]
        else:
            s_join = s_cond = ""
            s_cols = "0 AS hit_count, NULL AS last_hit_at"
            args = [cutoff]
        rows = conn.execute(
            f"""SELECT u.id, u.src_id, u.source, u.role, u.time, u.title,
                       {v_cols},
                       {s_cols},
                       substr(u.text, 1, 160) AS text
                FROM units u
                {v_join}
                {s_join}
                WHERE (u.role='distilled' OR u.source='memory')
                  AND u.time <= ?
                  {s_cond}
                ORDER BY u.time ASC
                LIMIT ?""",
            args + [max(1, int(limit))]).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
