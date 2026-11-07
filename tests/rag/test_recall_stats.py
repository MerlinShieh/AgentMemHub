"""召回命中统计与低价值记忆报表（recall_stats）测试。

覆盖：命中 upsert 口径、报表过滤语义（老记忆无命中 / 老记忆近期命中过 /
年轻记忆 / 非记忆单元）、hybrid_search 埋点真的落库、统计失败不炸检索。
"""
from __future__ import annotations

import sqlite3
import time

from agentmemhub.rag.recall_stats import (
    hits_db_path, record_hits, stale_memories)
from agentmemhub.rag.ingest import ensure_vec_table, open_index


def _mk_index(tmp_path, name="idx.db"):
    """建一个带 units schema 的索引库并返回 (路径, 连接)。"""
    p = tmp_path / name
    conn = open_index(p)
    return p, conn


def _insert_unit(conn, uid, *, role, source="probe", src_id=None,
                 text="记忆内容", t=None):
    conn.execute(
        "INSERT INTO units(id, source, conversation_id, seq, role, turn_key,"
        " src_id, time, title, text, chars)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (uid, source, f"conv-{uid}", 1, role, "tk",
         src_id or f"src-{uid}", t if t is not None else int(time.time()),
         "标题", text, len(text)))


NOW = int(time.time())
OLD = NOW - 120 * 86400        # 120 天前（> 默认 90 天窗口）


def test_record_hits_upsert_counts(tmp_path):
    p, conn = _mk_index(tmp_path)
    conn.close()
    assert record_hits(p, [1], ts=NOW) == 1
    assert record_hits(p, [1, 2], ts=NOW + 10) == 2
    db = sqlite3.connect(hits_db_path(p))
    rows = dict(db.execute("SELECT unit_id, hit_count FROM unit_recall_stats"))
    last = dict(db.execute("SELECT unit_id, last_hit_at FROM unit_recall_stats"))
    db.close()
    assert rows == {1: 2, 2: 1}
    assert last[1] == NOW + 10                 # last_hit_at 更新为最近一次


def test_record_hits_never_raises_on_bad_path(tmp_path):
    """契约：统计是旁路，库不可写（路径是目录等）也不抛错、只返回 0。"""
    bad = tmp_path / "nope" / "idx.db"
    assert record_hits(bad, [1], ts=NOW) == 0


def test_stale_report_filters(tmp_path):
    """报表口径：老记忆无命中 → 入选；老记忆近期命中 / 年轻记忆 / 消息层 → 不选。"""
    p, conn = _mk_index(tmp_path)
    with conn:
        _insert_unit(conn, 1, role="distilled", src_id="dst_a",
                     text="从未被召回的老蒸馏记忆", t=OLD)
        _insert_unit(conn, 2, role="distilled", src_id="dst_b",
                     text="最近被召回过的老蒸馏记忆", t=OLD)
        _insert_unit(conn, 3, source="memory", role="user", src_id="mcp_c",
                     text="年轻直写记忆（不满窗口）", t=NOW - 5 * 86400)
        _insert_unit(conn, 4, role="assistant", src_id="msg:9",
                     text="老对话消息（不是记忆，不参与报表）", t=OLD)
    conn.close()
    record_hits(p, [2], ts=NOW - 1)           # 2 号最近命中过 → 不算 stale

    rows = stale_memories(p, days=90)
    ids = [r["id"] for r in rows]
    assert ids == [1], "只有「老且零命中」的蒸馏记忆入选"
    assert rows[0]["src_id"] == "dst_a"
    assert rows[0]["hit_count"] == 0
    assert "老蒸馏记忆" in rows[0]["text"]

    # 窗口缩短后 2 号也不满足「零命中」窗口？——2 号命中就在刚才，仍排除；
    # 反证 days 语义：days=1 时「创建满 1 天」的老记忆都候选，但 2 号最近
    # 命中过仍被排除，1 号与 4 号之外…… 3 号（5 天前创建）也进入候选。
    rows1 = stale_memories(p, days=1)
    assert {r["id"] for r in rows1} == {1, 3}  # 4 号是消息层，永远不进


def test_stale_report_with_unit_values(tmp_path):
    """unit_values 存在时带出价值列（清理决策要看得见 value/priority）。"""
    p, conn = _mk_index(tmp_path)
    with conn:
        _insert_unit(conn, 7, role="distilled", src_id="dst_v",
                     text="有价值列的老记忆", t=OLD)
        conn.execute(
            "CREATE TABLE unit_values(unit_id INTEGER PRIMARY KEY, value REAL,"
            " r_human REAL, priority REAL, updated_at INTEGER, manual_value REAL)")
        conn.execute(
            "INSERT INTO unit_values(unit_id, value, priority, manual_value)"
            " VALUES(7, 0.3, 0.3, NULL)")
    conn.close()
    rows = stale_memories(p)
    assert rows and rows[0]["value"] == 0.3 and rows[0]["priority"] == 0.3


def test_search_records_hits(project_settings, embedder, tmp_path):
    """埋点：hybrid_search 的最终命中自动落进独立命中库；检索不受统计影响。"""
    import dataclasses

    from agentmemhub.rag.search import hybrid_search

    active = project_settings.active_spec
    st = dataclasses.replace(project_settings,
                            index_db=tmp_path / "idx_hook.db")
    text = "命中统计探针：这条记忆被检索后必须在独立命中库出现"
    conn = open_index(st.index_db)
    try:
        ensure_vec_table(conn, active)
        with conn:
            cur = conn.execute(
                "INSERT INTO units(source, conversation_id, seq, role, turn_key,"
                " src_id, time, title, text, chars)"
                " VALUES('memory','conv',1,'user','tk','mcp_probe',"
                f"{OLD},'探针',?,?)", (text, len(text)))
            v = embedder.encode_passages([text])[0]
            conn.execute(
                f"INSERT INTO {active.vec_table}(rowid, embedding) VALUES(?,?)",
                (cur.lastrowid, bytes(v)))
        uid = cur.lastrowid
    finally:
        conn.close()

    hits = hybrid_search(st, "命中统计探针", mode="vector", k=3,
                         embedder=embedder)
    assert any(h.unit_id == uid for h in hits)
    db = sqlite3.connect(hits_db_path(st.index_db))
    try:
        row = db.execute(
            "SELECT hit_count FROM unit_recall_stats WHERE unit_id=?",
            (uid,)).fetchone()
    finally:
        db.close()
    assert row and row[0] == 1, "检索一次 → 命中库记一次"
    # 该 unit 已被命中 → 不再出现在零命中报表里
    assert all(r["id"] != uid for r in stale_memories(st.index_db))
