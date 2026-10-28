"""摄取管道测试：白名单/幂等/水位/重建/维度守门/确定性。

全部基于微型 fixture 源库 + tmp 索引库，不依赖真实 agentmemhub.db。
"""
from __future__ import annotations

import dataclasses
import logging
import sqlite3

import pytest

from agentmemhub.rag.embedder import OnnxEmbedder
from agentmemhub.rag.ingest import (
    ensure_bridge_schema,
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


def test_删除会话后其它会话仍能被向量召回(
    project_settings, embedder, fixture_source_db, tmp_path
):
    """删会话**不能**清掉 vec0 的影子表。

    回归点：`delete_units_for_conversation` 曾用
    `SELECT name FROM sqlite_master WHERE name LIKE 'vec_%'` 枚举"向量表"，
    而 vec0 的影子表（`_info` / `_chunks` / `_rowids` / `_vector_chunks00`）
    同样 `type='table'` 且以 `vec_` 开头，于是被按 rowid 删。表面上
    `COUNT(*) FROM vec_x` 只剩该删的行（看着是对的），实际上聚类行被删、
    ANN 索引整块失效 —— `WHERE embedding MATCH ?` 返回 0 条。
    这个缺陷不抛错、不记日志、覆盖率统计也还是满的，只有 KNN 断言能抓到。
    """
    import numpy as np

    from agentmemhub.rag.ingest import delete_units_for_conversation

    idx = tmp_path / "rag.db"
    _ingest(project_settings, embedder, fixture_source_db, idx)
    vt = project_settings.active_spec.vec_table
    conn = open_index(idx)
    victim = conn.execute(
        "SELECT conversation_id FROM units ORDER BY id LIMIT 1").fetchone()[0]
    assert delete_units_for_conversation(conn, "hermes", victim) > 0
    remaining = conn.execute("SELECT COUNT(*) FROM units").fetchone()[0]
    assert remaining > 0, "夹具应还有其它会话可以召回"
    q = np.asarray(embedder.encode_query("批量自动化脚本"), dtype=np.float32)
    hits = conn.execute(
        f"SELECT rowid FROM {vt} WHERE embedding MATCH ? AND k = ?",
        (q.tobytes(), remaining)).fetchall()
    conn.close()
    assert hits, "删了一个会话就把整个 ANN 索引打死了（影子表被误删）"


def test_rebuild_源库异常时不能清空索引(
    project_settings, embedder, tmp_path
):
    """`--rebuild` 必须先打开源库校验 schema，**再**清索引。

    反过来（先 `DELETE FROM units` 再 `open_source_ro`）意味着源库路径写错就
    已经把索引清空了才报错：units 与全部模型向量不可逆丢失，而 units 是
    AUTOINCREMENT，重灌后 id 更大 —— `unit_values`（手动加权 / 反馈）按旧 id
    全部变成永久孤儿。这条测试守的是「失败要发生在破坏之前」。
    """
    idx = tmp_path / "rag.db"
    conn = open_index(idx)
    conn.execute(
        "INSERT INTO units(source, conversation_id, seq, role, text, chars)"
        " VALUES('zcode','c1',1,'user','这条必须活下来',4)")
    conn.commit()
    conn.close()

    with pytest.raises(Exception):          # noqa: B017 —— 具体类型不重要
        run_ingest(
            dataclasses.replace(project_settings, index_db=idx),
            embedder=embedder,
            source_db=tmp_path / "不存在的源库.db",
            rebuild=True,
            log=_qlog,
        )
    conn = open_index(idx)
    n = conn.execute("SELECT COUNT(*) FROM units").fetchone()[0]
    conn.close()
    assert n == 1, "源库打不开时索引已被清空 —— rebuild 的顺序反了"


def test_open_index_busy_timeout(project_settings, tmp_path):
    """R1 并发加固：多进程写依赖 busy_timeout（面板×MCP 不同进程）。"""
    conn = open_index(tmp_path / "rag.db")
    try:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 15000
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()


def test_ensure_bridge_schema_adds_updated_at_and_backfills(tmp_path):
    """老库迁移：units 缺 updated_at 时补列，并用**迁移时刻**回填存量。

    updated_at = 该行最后一次被写入/改写的时间（秒）。不能用 units.time
    回填——那是**事件时间**，会让下游误判成"很久没变过"。
    """
    import sqlite3

    conn = sqlite3.connect(tmp_path / "legacy.db")
    # 复现补列之前的 units 结构（无 legacy_id / tags / updated_at）
    conn.execute(
        "CREATE TABLE units("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " source TEXT NOT NULL, conversation_id TEXT NOT NULL,"
        " seq INTEGER NOT NULL, role TEXT NOT NULL, turn_key TEXT,"
        " src_id TEXT, time INTEGER, title TEXT, text TEXT NOT NULL,"
        " chars INTEGER NOT NULL, UNIQUE(source, conversation_id, seq))")
    conn.execute(
        "INSERT INTO units(source, conversation_id, seq, role, text, chars, time)"
        " VALUES('zcode','c1',1,'user','历史内容',4,1000)")
    conn.commit()

    ensure_bridge_schema(conn)

    cols = [r[1] for r in conn.execute("PRAGMA table_info(units)")]
    assert "updated_at" in cols
    ts = conn.execute("SELECT updated_at FROM units").fetchone()[0]
    assert ts is not None and ts > 1000      # 迁移时刻，而非事件时间 1000

    # 自愈：迁移后由旧代码写入的 NULL 行，再次调用会被补齐
    # （真实场景：仍在跑旧代码的常驻 MCP 进程写入了新记忆）
    conn.execute(
        "INSERT INTO units(source, conversation_id, seq, role, text, chars)"
        " VALUES('zcode','c1',2,'user','后补内容',4)")
    conn.commit()
    assert conn.execute(
        "SELECT updated_at FROM units WHERE seq=2").fetchone()[0] is None
    ensure_bridge_schema(conn)
    assert conn.execute(
        "SELECT updated_at FROM units WHERE seq=2").fetchone()[0] is not None
    conn.close()


# ---------------------------------------------------------------------------
# 采集层写 origin（M0 地基 · 外部投喂通道的前置）
#
# 2026-10-10 实测缺口：`INSERT INTO units` 的列清单里**没有 origin**，于是
# source='feed' 的**原始段落**落库为 NULL，而召回侧是 `or "native"` 兜底
# ⇒ 投喂进来的原文被当成"自有沉淀"。同一条数据在原始事件层与蒸馏记忆层
# 来源标记不一致（蒸馏层由 `distill._project_one` 正确标 external）。
# ---------------------------------------------------------------------------

def _add_feed_conversation(src_db, *, content="投喂进来的一段外部资料"):
    """往 fixture 源库里加一个 source='feed' 的会话（投喂文档的会话容器形态）。"""
    conn = sqlite3.connect(str(src_db))
    conn.execute("INSERT INTO conversations(source, id, title)"
                 " VALUES('feed','doc-001','投喂文档')")
    conn.execute(
        "INSERT INTO events(source, conversation_id, seq, role, content,"
        " turn_key, src_id, time) VALUES('feed','doc-001',1,'user',?,?,?,?)",
        (content, "1", "doc:doc-001#1", 1786000200))
    conn.commit()
    conn.close()


def test_ingest_投喂会话的段落标external(project_settings, embedder,
                                   fixture_source_db, tmp_path):
    """口径与 `distill._project_one` 一致：source='feed' → origin='external'。"""
    _add_feed_conversation(fixture_source_db)
    idx = tmp_path / "rag.db"
    _ingest(project_settings, embedder, fixture_source_db, idx)

    conn = open_index(idx)
    try:
        feed = [r[0] for r in conn.execute(
            "SELECT origin FROM units WHERE source='feed'")]
        assert feed, "feed 会话的段落应已入库"
        assert set(feed) == {"external"}, \
            "投喂的原始段落必须标 external，否则会被当成自有沉淀"
    finally:
        conn.close()


def test_ingest_自有来源仍标native(project_settings, embedder,
                              fixture_source_db, tmp_path):
    """回归保护：加 origin 不能把自有来源（zcode/hermes/…）标错。"""
    _add_feed_conversation(fixture_source_db)
    idx = tmp_path / "rag.db"
    _ingest(project_settings, embedder, fixture_source_db, idx)

    conn = open_index(idx)
    try:
        native = {r[0] for r in conn.execute(
            "SELECT origin FROM units WHERE source!='feed'")}
        assert native == {"native"}, "非 feed 来源一律 native"
    finally:
        conn.close()

