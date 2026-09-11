"""R2 引擎写侧：原子记忆幂等落库、feedback 聚合数学、ValueStore 读侧联动。"""
from __future__ import annotations

import dataclasses
import logging

import pytest

from agentmemhub.rag.embedder import OnnxEmbedder
from agentmemhub.rag.ingest import open_index, run_ingest
from agentmemhub.rag.memstore import (
    ValueStore,
    content_anchor,
    ensure_memstore_schema,
    list_units,
    memstore_stats,
    put_feedback,
    save_memory,
)
from agentmemhub.rag.search import fts_search, hybrid_search

_qlog = logging.getLogger("asrag.test.memstore")
_qlog.addHandler(logging.NullHandler())


@pytest.fixture()
def embedder(project_settings):
    return OnnxEmbedder(project_settings.active_spec, batch_size=4, log=_qlog)


@pytest.fixture()
def rag_db(project_settings, embedder, fixture_source_db, tmp_path):
    idx = tmp_path / "rag.db"
    run_ingest(project_settings, embedder=embedder, source_db=fixture_source_db,
               index_db=idx, batch_size=4, log=_qlog)
    return dataclasses.replace(project_settings, index_db=idx)


def test_anchor_format_matches_legacy_mcp_algorithm():
    import hashlib
    c = "某条记忆内容"
    assert content_anchor(c) == "mcp_" + hashlib.sha256(c.encode()).hexdigest()[:16]


def test_save_memory_immediate_recall(rag_db, embedder):
    """save 后立即可检索（units+vec+fts 三处同步），字段语义对齐 MemOS。"""
    content = "bat 批处理块内括号会提前展开，须改参数形式"
    r = save_memory(rag_db, content, embedder=embedder, ts=1786000999)
    assert r["created"] and r["src_id"].startswith("mcp_")
    conn = open_index(rag_db.index_db)
    try:
        row = conn.execute("SELECT source, conversation_id, role, time FROM units"
                           " WHERE id=?", (r["unit_id"],)).fetchone()
        assert tuple(row) == ("memory", "mcp", "user", 1786000999)
        vt = rag_db.active_spec.vec_table
        assert conn.execute(f"SELECT 1 FROM {vt} WHERE rowid=?",
                            (r["unit_id"],)).fetchone(), "即时嵌入必须落向量表"
    finally:
        conn.close()
    hits = hybrid_search(rag_db, "批处理括号问题怎么改", embedder=embedder, k=5,
                         mode="hybrid", log=_qlog)
    assert r["unit_id"] in {h.unit_id for h in hits}


def test_save_memory_idempotent(rag_db, embedder):
    r1 = save_memory(rag_db, "重复内容测试", embedder=embedder)
    r2 = save_memory(rag_db, "重复内容测试", embedder=embedder)
    assert r1["unit_id"] == r2["unit_id"] and not r2["created"]
    conn = open_index(rag_db.index_db)
    try:
        n = conn.execute("SELECT COUNT(*) FROM units WHERE text='重复内容测试'"
                         ).fetchone()[0]
    finally:
        conn.close()
    assert n == 1


def test_save_memory_rejects_blank(rag_db, embedder):
    with pytest.raises(ValueError):
        save_memory(rag_db, "   ", embedder=embedder)


def test_feedback_aggregation_math(rag_db):
    """2 正 1 负：value=r_human=(2-1)/3=1/3；magnitude 越界钳制；priority 单调。"""
    r = save_memory(rag_db, "反馈聚合目标记忆", ts=1)
    conn = open_index(rag_db.index_db)
    try:
        ensure_memstore_schema(conn)
        p1 = put_feedback(conn, r["unit_id"], "positive")
        assert p1["value"] == 1.0 and p1["priority"] == 1.0
        put_feedback(conn, r["unit_id"], "negative")
        out = put_feedback(conn, r["unit_id"], "positive", magnitude=2.0)  # 钳到 1.0
        assert out["value"] == pytest.approx((1 - 1 + 1) / 3)
        assert out["r_human"] == out["value"]
        assert out["priority"] == 1.0, "priority 单调不回退（max(old,|v|)）"
        # 再一个负反馈 → value 更负但 priority 保持峰值
        out2 = put_feedback(conn, r["unit_id"], "negative")
        assert out2["value"] < out["value"]
        assert out2["priority"] == 1.0
    finally:
        conn.close()


def test_feedback_rejects_unknown_and_bad_polarity(rag_db):
    conn = open_index(rag_db.index_db)
    try:
        with pytest.raises(KeyError):
            put_feedback(conn, 999999, "positive")
        r = save_memory(rag_db, "极性校验", ts=1)
        with pytest.raises(ValueError):
            put_feedback(conn, r["unit_id"], "positive-ish")
    finally:
        conn.close()


def test_value_store_filters_low_in_hybrid(rag_db, embedder):
    """读侧联动：判负单元在带 provider 的 hybrid 检索中消失，include_low 找回。"""
    bad = save_memory(rag_db, "错误结论已被推翻的记忆内容", ts=1)
    conn = open_index(rag_db.index_db)
    try:
        put_feedback(conn, bad["unit_id"], "negative")
    finally:
        conn.close()
    q = "错误结论已被推翻的记忆"
    vs = ValueStore(rag_db.index_db)
    assert vs.values([bad["unit_id"]])[bad["unit_id"]] == -1.0
    hits = hybrid_search(rag_db, q, embedder=embedder, k=10,
                         value_provider=vs, log=_qlog)
    assert bad["unit_id"] not in {h.unit_id for h in hits}
    hits2 = hybrid_search(rag_db, q, embedder=embedder, k=10,
                          value_provider=vs, include_low_value=True, log=_qlog)
    assert bad["unit_id"] in {h.unit_id for h in hits2}


def test_list_units_time_order_with_values(rag_db):
    save_memory(rag_db, "较早的记忆", ts=100)
    late = save_memory(rag_db, "较新的记忆", ts=200)
    conn = open_index(rag_db.index_db)
    try:
        ensure_memstore_schema(conn)
        put_feedback(conn, late["unit_id"], "positive")
        rows = list_units(conn, limit=5, source="memory")
        assert rows[0]["time"] == 200, "时间倒序"
        assert rows[0]["value"] == 1.0, "值已 join"
        # 写入即有来源初始分（0.6）：另一条未被反馈 → 保持初始值
        assert abs(rows[1]["value"] - 0.6) < 1e-9
    finally:
        conn.close()


def test_memstore_stats(rag_db):
    conn = open_index(rag_db.index_db)
    try:
        save_memory(rag_db, "统计验证条目", ts=1)
        st = memstore_stats(conn)
        assert st["memory_units"] == 1
        assert st["units"] >= 8
        # 写入即带来源初始分 → valued=1；feedback 仍为 0（初始分不是反馈）
        assert st["valued"] == 1 and st["feedback"] == 0
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════
# 来源初始置信度 + 用户手动加权（权重规则二）
# ══════════════════════════════════════════════════════════════════════

def test_save_memory_initial_value_is_agent_tier(project_settings):
    """Agent 主动写入（MCP memory_save）→ 初始 value=0.6（高于蒸馏的 0.3）。"""
    from agentmemhub.rag.memstore import BASE_VALUE_AGENT_WRITE
    r = save_memory(project_settings, "用户决定所有服务统一用 uv 管理虚拟环境")
    conn = open_index(project_settings.index_db)
    try:
        row = conn.execute(
            "SELECT value FROM unit_values WHERE unit_id=?", (r["unit_id"],)).fetchone()
        assert row and abs(row[0] - BASE_VALUE_AGENT_WRITE) < 1e-9
    finally:
        conn.close()


def test_manual_value_overrides_and_clears(project_settings):
    """手动加权优先于自动聚合值；清除后回到聚合值。"""
    from agentmemhub.rag.memstore import set_manual_value
    r = save_memory(project_settings, "手动加权测试的记忆条目甲")
    conn = open_index(project_settings.index_db)
    try:
        uid = r["unit_id"]
        vstore = ValueStore(project_settings.index_db)

        # 未手动加权：读到自动聚合值（初始 0.6）
        assert abs(vstore.values([uid])[uid] - 0.6) < 1e-9
        assert vstore.no_decay_ids([uid]) == set()

        # 锁定 1.0 → 优先返回；且进入不衰减名单
        set_manual_value(conn, uid, 1.0)
        assert abs(vstore.values([uid])[uid] - 1.0) < 1e-9
        assert uid in vstore.no_decay_ids([uid])

        # 清除 → 回到自动值，退出不衰减名单
        set_manual_value(conn, uid, None)
        assert abs(vstore.values([uid])[uid] - 0.6) < 1e-9
        assert uid not in vstore.no_decay_ids([uid])
    finally:
        conn.close()


def test_manual_value_no_decay_in_boost(project_settings):
    """衰减豁免：手动加权的记忆不随时间衰减，未加权的照常衰减。"""
    import time as _t
    from agentmemhub.rag.ext import apply_value_boost
    rel = {11: 0.5, 22: 0.5}
    meta = {11: (1, None), 22: (1, None)}          # 两个都"很老"（epoch=1）
    values = {11: 0.9, 22: 0.9}
    out, _ = apply_value_boost(rel, meta, values, now=_t.time(),
                               no_decay={11})
    # 11（手动）：拿满 boost；22（自动）：被 30 天半衰期衰到接近 0
    assert out[11] - 0.5 > 0.25
    assert out[22] - 0.5 < 0.05


def test_base_value_never_overwrites_feedback(project_settings):
    """初始分只在无记录时写；已有反馈演化结果的记忆不被覆盖。"""
    from agentmemhub.rag.memstore import ensure_base_value
    r = save_memory(project_settings, "反馈演化保护的记忆条目乙")
    conn = open_index(project_settings.index_db)
    try:
        uid = r["unit_id"]
        put_feedback(conn, uid, "positive", magnitude=1.0)
        before = conn.execute("SELECT value FROM unit_values WHERE unit_id=?",
                              (uid,)).fetchone()[0]
        ensure_base_value(conn, uid, 0.6)          # 再写初始分 → 应跳过
        after = conn.execute("SELECT value FROM unit_values WHERE unit_id=?",
                             (uid,)).fetchone()[0]
        assert abs(after - before) < 1e-9          # 演化结果保留
    finally:
        conn.close()
