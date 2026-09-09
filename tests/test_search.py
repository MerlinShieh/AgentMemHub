"""混合召回测试：FTS 懒建/触发器同步/trigram+LIKE、RRF、向量路、端到端与溯源日志。"""
from __future__ import annotations

import dataclasses
import logging

import pytest

from asrag.embedder import OnnxEmbedder
from asrag.ingest import open_index, run_ingest
from asrag.search import (
    ensure_search_schema,
    expand_turn,
    fts_search,
    hybrid_search,
    rrf_fuse,
    vector_search,
)

_qlog = logging.getLogger("asrag.test.search")
_qlog.addHandler(logging.NullHandler())


@pytest.fixture()
def rag_db(project_settings, embedder, fixture_source_db, tmp_path):
    """已摄取 fixture 数据的索引库（Settings 指向 tmp）。"""
    idx = tmp_path / "rag.db"
    run_ingest(project_settings, embedder=embedder, source_db=fixture_source_db,
               index_db=idx, batch_size=4, log=_qlog)
    return dataclasses.replace(project_settings, index_db=idx)


@pytest.fixture()
def embedder(project_settings):
    return OnnxEmbedder(project_settings.active_spec, batch_size=4, log=_qlog)


# ── FTS 基建 ────────────────────────────────────────────────────────────

def test_ensure_fts_backfill_idempotent(rag_db):
    conn = open_index(rag_db.index_db)
    try:
        ensure_search_schema(conn, log=_qlog)
        n1 = conn.execute("SELECT COUNT(*) FROM units_fts").fetchone()[0]
        ensure_search_schema(conn, log=_qlog)  # 二次无重复无报错
        n2 = conn.execute("SELECT COUNT(*) FROM units_fts").fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM units").fetchone()[0]
        assert n1 == n2 == total
    finally:
        conn.close()


def test_trigger_keeps_fts_in_sync(rag_db):
    conn = open_index(rag_db.index_db)
    try:
        ensure_search_schema(conn, log=_qlog)
        conn.execute(
            "INSERT INTO units(source, conversation_id, seq, role, text, chars)"
            " VALUES('t','c',99,'user','触发器同步验证专用串',10)")
        conn.commit()
        assert fts_search(conn, "触发器同步", 5), "新 units 行必须自动进 FTS"
        conn.execute("DELETE FROM units WHERE seq=99")
        conn.commit()
        assert not fts_search(conn, "触发器同步", 5), "删除行必须同步出 FTS"
    finally:
        conn.close()


def test_fts_trigram_and_like_fallback(rag_db):
    conn = open_index(rag_db.index_db)
    try:
        ensure_search_schema(conn, log=_qlog)
        hits3 = fts_search(conn, "批处理", 10)   # ≥3 字符 → trigram
        hits2 = fts_search(conn, "括号", 10)     # 2 字符 → LIKE 兜底
        assert hits3 and hits2
        text = conn.execute(
            "SELECT text FROM units WHERE id=?", (hits2[0][0],)).fetchone()[0]
        assert "括号" in text
        hit_units = {i for i, _ in hits3}
        texts = [conn.execute("SELECT text FROM units WHERE id=?", (i,)).fetchone()[0]
                 for i in hit_units]
        assert any("批处理" in t for t in texts)
    finally:
        conn.close()


def test_fts_empty_query(rag_db):
    conn = open_index(rag_db.index_db)
    try:
        ensure_search_schema(conn, log=_qlog)
        assert fts_search(conn, "   ", 5) == []
    finally:
        conn.close()


# ── RRF 纯函数 ─────────────────────────────────────────────────────────

def test_rrf_scores_and_order():
    fused = rrf_fuse([[10, 20, 30], [20, 10]], k=60)
    ids = [i for i, _ in fused]
    scores = dict(fused)
    assert ids[0] == 20 or ids[0] == 10  # 两路共有的高分
    s20 = 1 / (60 + 2) + 1 / (60 + 1)
    assert scores[20] == pytest.approx(s20)
    assert scores[30] == pytest.approx(1 / 63)
    # 只有单路命中的必须排在双路命中之后
    assert ids[:2] == sorted(ids[:2], key=lambda x: -scores[x])


def test_rrf_deterministic_tiebreak():
    a = rrf_fuse([[1, 2], [2, 1]])
    b = rrf_fuse([[1, 2], [2, 1]])
    assert a == b
    # 平分则按 unit_id 升序（tie-break 稳定，检索可回放）
    assert [i for i, _ in a] == [1, 2]


# ── 端到端 ─────────────────────────────────────────────────────────────

def test_hybrid_end_to_end(rag_db, embedder):
    hits = hybrid_search(rag_db, "bat 批处理报 unexpected 错误怎么办",
                         embedder=embedder, k=5, log=_qlog)
    assert hits
    top = hits[0]
    joined = " ".join(h.text for h in hits[:3])
    assert ("批处理" in joined) or ("括号" in joined)
    assert 0 < top.score <= (1 / 61 + 1 / 61)  # 双路 rank1 的最高 RRF 分


def test_hybrid_modes(rag_db, embedder):
    for mode in ("hybrid", "vector", "fts"):
        hits = hybrid_search(rag_db, "嵌入模型维度切换", embedder=embedder,
                             k=5, mode=mode, log=_qlog)
        assert hits, f"mode={mode} 应有结果"
        if mode == "vector":
            assert all(h.vec_rank for h in hits)
        if mode == "fts":
            assert all(h.fts_rank for h in hits)


def test_turn_expansion(rag_db, embedder):
    hits = hybrid_search(rag_db, "384 维切换到 512 维怎么做", embedder=embedder,
                         k=5, mode="hybrid", log=_qlog)
    conv_b = [h for h in hits if h.conversation_id == "conv-b"]
    assert conv_b
    ctx = conv_b[0].turn_context
    roles = {r for r, _ in ctx}
    assert {"user", "assistant"} <= roles, "同轮 user+assistant 都应展开"
    assert len(ctx) == 3  # conv-b/turn_key=1 共 3 单元


def test_search_logs_traceability(rag_db, embedder, tmp_log_dir):
    """溯源：日志必须能回放查询、两路命中与融合结果。"""
    from asrag.logkit import get_logger
    log = get_logger("search", tmp_log_dir, console=False)
    hits = hybrid_search(rag_db, "水位推进幂等", embedder=embedder, k=3, log=log)
    for h in log.handlers:
        h.flush()
    content = (tmp_log_dir / "asrag-search.log").read_text(encoding="utf-8")
    assert "search q=" in content and "vec_ids=" in content
    assert "fused_ids=" in content
    if hits:
        assert str(hits[0].unit_id) in content
