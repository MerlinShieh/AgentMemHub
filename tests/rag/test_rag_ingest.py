"""摄取管道测试：白名单/幂等/水位/重建/维度守门/确定性。

全部基于微型 fixture 源库 + tmp 索引库，不依赖真实 agentmemhub.db。
"""
from __future__ import annotations

import dataclasses
import logging

import pytest

from agentmemhub.rag.embedder import OnnxEmbedder
from agentmemhub.rag.ingest import (
    ensure_vec_table,
    open_index,
    run_ingest,
    run_stats,
)
from agentmemhub.rag.source import DEFAULT_ROLES
from conftest import ELIGIBLE_COUNT

_qlog = logging.getLogger("asrag.test.ingest")
_qlog.addHandler(logging.NullHandler())


@pytest.fixture()
def embedder(project_settings):
    return OnnxEmbedder(project_settings.active_spec, batch_size=4, log=_qlog)


def _ingest(settings, embedder, source_db, index_db, **kw):
    kw.setdefault("batch_size", 4)
    return run_ingest(
        settings, embedder=embedder, source_db=source_db,
        index_db=index_db, log=_qlog, **kw,
    )


def test_ingest_role_whitelist(project_settings, embedder, fixture_source_db, tmp_path):
    idx = tmp_path / "rag.db"
    s = _ingest(project_settings, embedder, fixture_source_db, idx)
    st = run_stats(dataclasses.replace(
        project_settings, index_db=idx), log=_qlog)
    assert st["units_total"] == ELIGIBLE_COUNT
    assert st["by_role"] == {"user": 4, "assistant": 3}
    assert s["embedded"] == ELIGIBLE_COUNT and s["skipped_known"] == 0
    # tool/reasoning/meta/空白内容全部未入库
    assert set(st["by_role"]) <= set(DEFAULT_ROLES)


def test_double_run_idempotent(project_settings, embedder, fixture_source_db, tmp_path):
    idx = tmp_path / "rag.db"
    _ingest(project_settings, embedder, fixture_source_db, idx)
    s2 = _ingest(project_settings, embedder, fixture_source_db, idx)
    st = run_stats(dataclasses.replace(project_settings, index_db=idx), log=_qlog)
    assert s2["embedded"] == 0
    assert s2["skipped_known"] == ELIGIBLE_COUNT
    assert st["units_total"] == ELIGIBLE_COUNT
    assert st["vec_total"] == ELIGIBLE_COUNT, "向量无重复"
    assert st["vec_coverage"] == 1.0


def test_long_message_truncated(project_settings, embedder, fixture_source_db, tmp_path):
    idx = tmp_path / "rag.db"
    _ingest(project_settings, embedder, fixture_source_db, idx)
    import sqlite3

    conn = sqlite3.connect(str(idx))
    mx = conn.execute("SELECT MAX(chars) FROM units").fetchone()[0]
    titles = dict(conn.execute(
        "SELECT conversation_id, title FROM units"))
    conn.close()
    assert mx == 4096, "超长消息粗裁到 MAX_CHARS"
    assert titles["conv-b"] == "向量迁移", "title 反规范化自 conversations"
    assert titles["conv-d"] is None, "孤儿会话也要能吃下"


def test_rebuild_reingests_everything(
    project_settings, embedder, fixture_source_db, tmp_path
):
    idx = tmp_path / "rag.db"
    _ingest(project_settings, embedder, fixture_source_db, idx)
    s = _ingest(project_settings, embedder, fixture_source_db, idx, rebuild=True)
    st = run_stats(dataclasses.replace(project_settings, index_db=idx), log=_qlog)
    assert s["embedded"] == ELIGIBLE_COUNT, "rebuild 必须全量重嵌"
    assert st["units_total"] == ELIGIBLE_COUNT, "rebuild 后无重复残留"


def test_limit_caps_work(project_settings, embedder, fixture_source_db, tmp_path):
    idx = tmp_path / "rag.db"
    _ingest(project_settings, embedder, fixture_source_db, idx, limit=4)
    st = run_stats(dataclasses.replace(project_settings, index_db=idx), log=_qlog)
    assert st["units_total"] <= 4
    # 续跑补齐
    _ingest(project_settings, embedder, fixture_source_db, idx)
    st2 = run_stats(dataclasses.replace(project_settings, index_db=idx), log=_qlog)
    assert st2["units_total"] == ELIGIBLE_COUNT


def test_reasoning_switch(project_settings, embedder, fixture_source_db, tmp_path):
    idx = tmp_path / "rag.db"
    _ingest(project_settings, embedder, fixture_source_db, idx,
            roles=DEFAULT_ROLES + ("reasoning",))
    st = run_stats(dataclasses.replace(project_settings, index_db=idx), log=_qlog)
    assert st["by_role"].get("reasoning") == 1
    assert st["units_total"] == ELIGIBLE_COUNT + 1


def test_watermark_recorded(project_settings, embedder, fixture_source_db, tmp_path):
    idx = tmp_path / "rag.db"
    _ingest(project_settings, embedder, fixture_source_db, idx)
    st = run_stats(dataclasses.replace(project_settings, index_db=idx), log=_qlog)
    wm = st["watermark"]
    # 全序 source→conv→seq，最后一条是 zcode/conv-e/1（conv-e > conv-d > conv-a）
    assert wm == {**wm, "source": "zcode", "conversation_id": "conv-e", "seq": 1}
    assert "at" in wm


def test_bitwise_determinism_same_batching(
    project_settings, embedder, fixture_source_db, tmp_path
):
    """不变量3 回归：固定 batch_size + 全序 → 两个独立索引库向量位级一致。"""
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    _ingest(project_settings, embedder, fixture_source_db, a)
    _ingest(project_settings, embedder, fixture_source_db, b)
    # 读 vec0 虚表必须走 open_index（连接需加载 sqlite-vec 扩展）；
    # 表名从 active spec 派生——测试自身也遵守"禁止硬编码模型 id"规约
    vt = project_settings.active_spec.vec_table
    ca = open_index(a).execute(
        f"SELECT embedding FROM {vt} ORDER BY rowid").fetchall()
    cb = open_index(b).execute(
        f"SELECT embedding FROM {vt} ORDER BY rowid").fetchall()
    assert ca and ca == cb, "同批次同序必须逐字节可复现"


def test_vec_orphan_gc_after_row_deletion(
    project_settings, embedder, fixture_source_db, tmp_path
):
    """回归（2026-09-10 复现）：删 units 行后 vec0 残留孤儿，重跑 ingest
    必须重新补齐缺失单元并 GC 掉孤儿向量（vec_coverage 回到 1.0）。"""
    idx = tmp_path / "rag.db"
    _ingest(project_settings, embedder, fixture_source_db, idx)
    conn = open_index(idx)
    victim = conn.execute(
        "SELECT id FROM units WHERE source='zcode' AND conversation_id='conv-a'"
        " AND seq=1").fetchone()[0]
    conn.execute("DELETE FROM units WHERE id=?", (victim,))
    conn.commit()
    conn.close()
    s = _ingest(project_settings, embedder, fixture_source_db, idx)
    assert s.get("vec_gc", 0) == 1, "孤儿向量应被清理"
    st = run_stats(dataclasses.replace(project_settings, index_db=idx), log=_qlog)
    assert st["units_total"] == ELIGIBLE_COUNT, "被删单元自动修复回补"
    assert st["vec_total"] == ELIGIBLE_COUNT
    assert st["vec_coverage"] == 1.0
    conn = open_index(idx)
    vt = project_settings.active_spec.vec_table
    assert not conn.execute(
        f"SELECT 1 FROM {vt} WHERE rowid=?", (victim,)
    ).fetchone(), "旧孤儿行必须已删除"
    conn.close()


def test_vec_dim_conflict_guard(project_settings, tmp_path):
    """注册表 dim 与既有向量表冲突必须拒绝，不许静默混库。"""
    spec = project_settings.active_spec
    idx = open_index(tmp_path / "rag.db")
    try:
        ensure_vec_table(idx, spec)
        bad = dataclasses.replace(spec, dim=384)
        with pytest.raises(RuntimeError, match="冲突"):
            ensure_vec_table(idx, bad)
    finally:
        idx.close()


def test_is_system_events_never_ingested(
    project_settings, embedder, fixture_source_db, tmp_path
):
    """R1 回归：源库 is_system=1 的注入消息不得进入向量索引。"""
    idx = tmp_path / "rag.db"
    _ingest(project_settings, embedder, fixture_source_db, idx)
    st = run_stats(dataclasses.replace(project_settings, index_db=idx), log=_qlog)
    assert st["units_total"] == ELIGIBLE_COUNT
    conn = open_index(idx)
    assert conn.execute("SELECT COUNT(*) FROM units WHERE conversation_id='conv-f'"
                        ).fetchone()[0] == 0
    conn.close()


def test_delete_units_for_conversation_cascade(
    project_settings, embedder, fixture_source_db, tmp_path
):
    """R1 级联删除：units+vec+fts 一处清光，且与幂等摄取共存（删后可重嵌）。"""
    from agentmemhub.rag.ingest import delete_units_for_conversation
    import sqlite3

    from agentmemhub.rag.search import ensure_search_schema

    idx = tmp_path / "rag.db"
    _ingest(project_settings, embedder, fixture_source_db, idx)
    vt = project_settings.active_spec.vec_table
    conn = open_index(idx)
    ensure_search_schema(conn, log=_qlog)  # 触发器路径需要 FTS 结构在场
    n = conn.execute("SELECT COUNT(*) FROM units WHERE conversation_id='conv-b'"
                     ).fetchone()[0]
    assert n == 3
    deleted = delete_units_for_conversation(conn, "hermes", "conv-b")
    assert deleted == 3
    assert conn.execute("SELECT COUNT(*) FROM units WHERE conversation_id='conv-b'"
                        ).fetchone()[0] == 0
    # fts 经触发器同步清空
    assert conn.execute(
        "SELECT COUNT(*) FROM units_fts WHERE rowid NOT IN (SELECT id FROM units)"
    ).fetchone()[0] == 0
    # 所有 vec 表无残留
    assert conn.execute(f"SELECT COUNT(*) FROM {vt} WHERE rowid NOT IN"
                        " (SELECT id FROM units)").fetchone()[0] == 0
    conn.close()
    # 重摄取会自我修复（幂等 known 集重新拉入）——这里源数据还在
    s2 = _ingest(project_settings, embedder, fixture_source_db, idx)
    assert s2["embedded"] == 3
    st = run_stats(dataclasses.replace(project_settings, index_db=idx), log=_qlog)
    assert st["vec_coverage"] == 1.0


def test_open_index_busy_timeout(project_settings, tmp_path):
    """R1 并发加固：多进程写依赖 busy_timeout（面板×MCP 不同进程）。"""
    conn = open_index(tmp_path / "rag.db")
    try:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 15000
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()
