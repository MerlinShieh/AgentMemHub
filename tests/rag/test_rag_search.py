"""混合召回测试：FTS 懒建/触发器同步/trigram+LIKE、RRF、向量路、端到端与溯源日志。"""
from __future__ import annotations

import dataclasses
import logging

import pytest

from agentmemhub.rag.embedder import OnnxEmbedder
from agentmemhub.rag.ingest import open_index, run_ingest
from agentmemhub.rag.search import (
    _query_chunks,
    ensure_search_schema,
    expand_turn,
    extract_identifiers,
    fts_search,
    fuse_channels,
    hybrid_search,
    identifier_search,
    rank_by_relevance,
    relevance_of,
    select_diverse,
    threshold_filter,
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


# ── P0-1 标题通道 ──────────────────────────────────────────────────────

def test_fts_title_column_channel(rag_db):
    """「向量迁移」只存在于 conv-b 的标题（正文含"迁移…向量"但语序不同）——
    标题列必须让纯标题命中成为可能。"""
    conn = open_index(rag_db.index_db)
    try:
        ensure_search_schema(conn, log=_qlog)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(units_fts)")]
        assert cols == ["text", "title"]
        ids = {i for i, _ in fts_search(conn, "向量迁移", 10)}
        conv_b = {r[0] for r in conn.execute(
            "SELECT id FROM units WHERE conversation_id='conv-b'")}
        assert ids & conv_b, "标题通道应命中 conv-b"
    finally:
        conn.close()


def test_fts_migrates_single_column_legacy(rag_db):
    """旧版单列 units_fts 必须被自动迁移成双列且保留可检索性。"""
    conn = open_index(rag_db.index_db)
    try:
        conn.executescript("""
            DROP TRIGGER IF EXISTS units_ai_fts;
            DROP TABLE IF EXISTS units_fts;
            CREATE VIRTUAL TABLE units_fts USING fts5(text, tokenize='trigram');
        """)
        conn.commit()
        ensure_search_schema(conn, log=_qlog)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(units_fts)")]
        assert cols == ["text", "title"], "ensure 必须完成旧结构迁移"
        assert fts_search(conn, "向量迁移", 10), "迁移后标题通道生效"
    finally:
        conn.close()


# ── P0-2 精确标识符通道 ────────────────────────────────────────────────

def test_extract_identifiers_rules():
    assert extract_identifiers("修复 timeout_seconds 字段") == ["timeout_seconds"]
    assert extract_identifiers("retry_handler_v2_max 报错") == ["retry_handler_v2_max"]
    assert extract_identifiers("端口 7897 太短") == []          # <12 字符
    assert extract_identifiers("helloworld") == []              # 10 纯字母
    ids = extract_identifiers("A1-b2_c3-d4:e5 混合")            # 14 含分隔 → 强
    assert ids == ["A1-b2_c3-d4:e5"]


def test_identifier_channel_hits_exact(rag_db):
    """conv-e 正文的 retry_handler_v2_max 必须被标识符通道 rank1 精确锁定。"""
    conn = open_index(rag_db.index_db)
    try:
        idents = extract_identifiers("retry_handler_v2_max 是什么东西")
        hits = identifier_search(conn, idents, 10)
        assert hits, "标识符通道必须命中"
        uid, score = hits[0]
        assert score == 1.0, "精确匹配恒强证据"
        text = conn.execute(
            "SELECT text FROM units WHERE id=?", (uid,)).fetchone()[0]
        assert "retry_handler_v2_max" in text
    finally:
        conn.close()


def test_hybrid_ident_rank_populated(rag_db, embedder):
    hits = hybrid_search(rag_db, "retry_handler_v2_max 死字段处理",
                         embedder=embedder, k=5, mode="hybrid", log=_qlog)
    top = [h for h in hits if h.ident_rank is not None]
    assert top, "hybrid 下标识符命中必须携带 ident_rank"
    assert "retry_handler_v2_max" in top[0].text


# ── P0-3 滑动窗口 + 噪音过滤 ───────────────────────────────────────────

def test_query_chunks_sliding_coverage_and_noise():
    # 步长1滑窗覆盖跨块边界："理报错" 这种旧版非重叠切分会漏
    chunks = _query_chunks("批处理报错")
    assert "批处理" in chunks and "理报错" in chunks
    # 全噪音块被剔除（"的了呢"类对话桥不给它 OR 匹配灌噪声的机会）
    assert all(not all(c in "我你他她它的了呢吗么" for c in ch)
               for ch in _query_chunks("我们还喜欢它的了呢功能"))
    # 去重
    assert len(_query_chunks("aa aaaa")) == len(set(_query_chunks("aa aaaa")))


# ── P0-4 多样性选择 ────────────────────────────────────────────────────

def test_select_diverse_caps_and_variety():
    import numpy as np

    fused = [(1, 0.0328), (2, 0.0320), (3, 0.0300), (4, 0.0164)]
    conv_of = {1: ("z", "c1"), 2: ("z", "c1"), 3: ("z", "c2"), 4: ("z", "c3")}
    same = np.array([1.0, 0, 0, 0], dtype=np.float32)
    emb = {i: same for i in (1, 2, 3, 4)}
    sel = select_diverse(fused, conv_of, emb, k=3, max_per_conversation=1)
    ids = [i for i, _ in sel]
    assert len(ids) == 3
    assert len({conv_of[i][1] for i in ids}) == 3, "每会话限 1 席后必须换会话取材"


def test_hybrid_diversity_cap_real(rag_db, embedder):
    """针对 conv-b（3 个单元同一会话）出题：top-k 里 conv-b 不得超过 2 席。"""
    hits = hybrid_search(rag_db, "嵌入模型维度切换 直写迁移 向量 重建 偏移 契约",
                         embedder=embedder, k=5, mode="hybrid", log=_qlog)
    from collections import Counter

    cc = Counter(h.conversation_id for h in hits)
    assert cc.get("conv-b", 0) <= 2


# ── P1-1 候选级通道纯函数 ──────────────────────────────────────────────

def test_fuse_and_relevance_best_channel_plus_rrf_lift():
    cand = fuse_channels({
        "vec": [(1, 0.60), (2, 0.55)],
        "fts": [(2, 1.0), (3, 0.5)],
        "ident": [(2, 1.0)],
    })
    assert cand[2] == {"vec": (2, 0.55), "fts": (1, 1.0), "ident": (1, 1.0)}
    r2 = relevance_of(cand[2])
    # best=1.0 + 0.4·(1/62 + 1/61 + 1/61)
    assert r2 == pytest.approx(1.0 + 0.4 * (1 / 62 + 1 / 61 + 1 / 61))
    r1 = relevance_of(cand[1])
    assert r1 == pytest.approx(0.60 + 0.4 * (1 / 61))
    assert r2 > r1, "三通道一致命中必须明显 lift"
    # 关键词 rank-0（1.0）与 cosine 同尺度起跑线
    assert r1 < relevance_of({"fts": (1, 1.0)})


def test_threshold_floor_with_multichannel_bypass():
    cand = {
        1: {"vec": (1, 0.62), "fts": (1, 1.0)},   # 强双通道（top）
        2: {"vec": (7, 0.05)},                    # 弱单通道 → 剔除（<0.2×top）
        3: {"vec": (20, 0.10), "ident": (1, 1.0)},  # ident 强证据自保
    }
    rel = {i: relevance_of(e) for i, e in cand.items()}
    assert 0.2 * max(rel.values()) > rel[2], "前提：2 确实低于阈值线"
    keep, bypassed = threshold_filter(rel, cand, floor=0.2, strong=0.35)
    assert 1 in keep and 2 not in keep and 3 in keep

    # bypass 机制本体：人为拉高 floor 使"弱但多通道一致"的候选落入线下
    cand2 = {
        1: {"vec": (1, 0.95), "fts": (1, 1.0)},       # top ≈ 1.013
        9: {"vec": (20, 0.40), "fts": (9, 0.10)},     # rel≈0.413 < 0.8×top，但双通道且 best≥0.35
        8: {"vec": (21, 0.30)},                        # 双通道不满足 → 仍被剔
    }
    rel2 = {i: relevance_of(e) for i, e in cand2.items()}
    keep2, byp2 = threshold_filter(rel2, cand2, floor=0.8, strong=0.35)
    assert 9 in keep2 and 9 in byp2, "多通道强信号必须旁路阈值"
    assert 8 not in keep2
    ordered = rank_by_relevance(rel)
    assert [i for i, _ in ordered] == [1, 3, 2], "按 rel 降序 + id 升序破平分"


def test_threshold_rank_deterministic():
    a = [(1, 0.5), (2, 0.5)]
    assert rank_by_relevance({1: 0.5, 2: 0.5}) == a, "平分按 id 升序，可回放"


# ── 端到端 ─────────────────────────────────────────────────────────────

def test_hybrid_end_to_end(rag_db, embedder):
    hits = hybrid_search(rag_db, "bat 批处理报 unexpected 错误怎么办",
                         embedder=embedder, k=5, log=_qlog)
    assert hits
    top = hits[0]
    joined = " ".join(h.text for h in hits[:3])
    assert ("批处理" in joined) or ("括号" in joined)
    # relevance 尺度：best-channel ≤1.0 + rrf lift ≤0.4·3/61
    assert 0 < top.score <= 1.0 + 0.4 * 3 / 61 + 1e-6


def test_exclude_session(rag_db, embedder):
    """P1-3：接线必备——当前会话（防重复注入自己）从三路全部消失。"""
    q = "批处理括号报错怎么解决"
    baseline = hybrid_search(rag_db, q, embedder=embedder, k=7,
                             mode="hybrid", log=_qlog)
    assert any(h.source == "zcode" and h.conversation_id == "conv-a"
               for h in baseline), "对照：不排除时应命中 conv-a"
    hits = hybrid_search(rag_db, q, embedder=embedder, k=7, mode="hybrid",
                         exclude_session=("zcode", "conv-a"), log=_qlog)
    assert not any(h.source == "zcode" and h.conversation_id == "conv-a"
                   for h in hits), "被排除会话不得出现在任何通道"
    hits_v = hybrid_search(rag_db, q, embedder=embedder, k=7, mode="vector",
                           exclude_session=("zcode", "conv-a"), log=_qlog)
    assert not any(h.conversation_id == "conv-a" for h in hits_v)


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
    from agentmemhub.rag.logkit import get_logger
    log = get_logger("search", tmp_log_dir, console=False)
    hits = hybrid_search(rag_db, "水位推进幂等", embedder=embedder, k=3, log=log)
    for h in log.handlers:
        h.flush()
    content = (tmp_log_dir / "asrag-search.log").read_text(encoding="utf-8")
    assert "search q=" in content and "vec_n=" in content
    assert "fused_ids=" in content
    if hits:
        assert str(hits[0].unit_id) in content


def test_multi_model_vector_recall(project_settings, embedder, tmp_path):
    """retrieval.models 多模型召回：只在「第二模型表」里有向量的单元也能被召回。

    背景（2026-09-11 实测缺陷）：写入与召回各自按进程内 active 选表，
    换模型后旧记忆会落进另一张表、在召回侧"隐形"。配置语义本该是多模型
    融合（active 通道名保持 "vec"，其余为 "vec:<id>"）。
    """
    import dataclasses

    from agentmemhub.rag.ingest import ensure_vec_table, open_index
    from agentmemhub.rag.search import hybrid_search

    m2 = dataclasses.replace(project_settings.active_spec, id="probe-m2")
    st = dataclasses.replace(
        project_settings,
        index_db=tmp_path / "idx_multi.db",
        models={**project_settings.models, m2.id: m2},
        retrieval={**project_settings.retrieval, "models": [m2.id]},  # 仅第二模型参与
    )
    text = "多模型召回探针：这条记忆只在第二模型的向量表里有向量"
    conn = open_index(st.index_db)
    try:
        ensure_vec_table(conn, m2)
        ensure_vec_table(conn, project_settings.active_spec)   # active 表存在但为空（真实态）
        with conn:
            cur = conn.execute(
                "INSERT INTO units(source, conversation_id, seq, role, turn_key,"
                " src_id, time, title, text, chars)"
                " VALUES('probe','conv',1,'user','tk','probe-multi-1',1786000000,"
                "'探针',?,?)", (text, len(text)))
            v = embedder.encode_passages([text])[0]
            conn.execute(
                f"INSERT INTO {m2.vec_table}(rowid, embedding) VALUES(?,?)",
                (cur.lastrowid, bytes(v)))
    finally:
        conn.close()

    hits = hybrid_search(st, "多模型召回探针记忆", mode="vector", k=5,
                         embedder=embedder, log=_qlog)
    assert any("探针" in (h.text or "") for h in hits), \
        "第二模型表里的向量必须能通过 retrieval.models 被召回"
