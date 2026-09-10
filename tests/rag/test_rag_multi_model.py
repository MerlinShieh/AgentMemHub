"""多模型并发向量化：每模型独立幂等（回归锁定 2026-09-10 实测 bug）。

Bug：后跑模型把前一个模型已入库的 units 全当成 "known" 跳过，
导致 embedded=0 白跑（因 _existing_keys 只看 units 不看各模型向量表）。
"""
from __future__ import annotations

import dataclasses
import logging
import sqlite3

import pytest

from agentmemhub.rag.config import load_settings
from agentmemhub.rag.embedder import OnnxEmbedder
from agentmemhub.rag.ingest import open_index, run_ingest, run_ingest_multi
from agentmemhub.rag.runtime import reset_embedders

_qlog = logging.getLogger("asrag.test.multi")
_qlog.addHandler(logging.NullHandler())
_qlog.propagate = False


@pytest.fixture(scope="module")
def project_settings():
    return load_settings()


@pytest.fixture()
def two_models(project_settings):
    """两个模型（真实权重）；skip 若第二个模型不可用。"""
    ids = list(project_settings.models)
    if len(ids) < 2:
        pytest.skip("需至少两个已注册模型")
    return project_settings, ids


@pytest.fixture()
def src_db(tmp_path):
    p = tmp_path / "src.db"
    conn = sqlite3.connect(str(p))
    conn.executescript("""
        CREATE TABLE conversations (
            source TEXT NOT NULL, id TEXT NOT NULL, title TEXT, cwd TEXT,
            model TEXT, created_at INTEGER, updated_at INTEGER,
            event_count INTEGER DEFAULT 0, roles_json TEXT, meta_json TEXT,
            signature TEXT, session_key TEXT, PRIMARY KEY (source, id));
        CREATE TABLE events (
            source TEXT NOT NULL, conversation_id TEXT NOT NULL, seq INTEGER NOT NULL,
            role TEXT NOT NULL, content TEXT, tool_name TEXT, tool_input_json TEXT,
            tool_output TEXT, tool_status TEXT, reasoning TEXT, patch_file TEXT,
            patch_diff TEXT, shell_cmd TEXT, shell_output TEXT, shell_cwd TEXT,
            parent_id TEXT, time INTEGER, model TEXT, raw_json TEXT, src_id TEXT,
            turn_key TEXT, is_system INTEGER DEFAULT 0,
            PRIMARY KEY (source, conversation_id, seq));
    """)
    conn.executemany(
        "INSERT INTO events(source, conversation_id, seq, role, content,"
        " turn_key, src_id, time) VALUES(?,?,?,?,?,?,?,?)",
        [("zcode", "c1", 1, "user", "第一条测试内容：向量检索", "1", "m1", 1),
         ("zcode", "c1", 2, "assistant", "第一条回答：混合召回原理", "1", "m2", 2),
         ("zcode", "c1", 3, "user", "第二条测试内容：记忆排除", "2", "m3", 3),
         ("zcode", "c1", 4, "assistant", "第二条回答：标记不写入", "2", "m4", 4)])
    conn.execute("INSERT INTO conversations(source, id, title) VALUES('zcode','c1','t')")
    conn.commit()
    conn.close()
    return p


def test_second_model_embeds_independently(two_models, src_db, tmp_path):
    """核心回归：模型 A 入库后，模型 B 必须仍然嵌入全部单元（不能为 0）。"""
    settings, ids = two_models
    m_a, m_b = ids[0], ids[1]
    idx = tmp_path / "rag.db"

    emb_a = OnnxEmbedder(settings.model(m_a), batch_size=4, log=_qlog)
    sa = run_ingest(settings, embedder=emb_a, model_id=m_a, source_db=src_db,
                    index_db=idx, batch_size=4, log=_qlog)
    assert sa["embedded"] == 4, "模型 A 应嵌入 4 条"

    emb_b = OnnxEmbedder(settings.model(m_b), batch_size=4, log=_qlog)
    sb = run_ingest(settings, embedder=emb_b, model_id=m_b, source_db=src_db,
                    index_db=idx, batch_size=4, log=_qlog)
    assert sb["embedded"] == 4, (
        f"模型 B 必须独立嵌入（实际 embedded={sb['embedded']}）"
        f"——若为 0 说明 _existing_keys 未按模型区分")

    conn = open_index(idx)
    try:
        for mid in (m_a, m_b):
            t = settings.model(mid).vec_table
            n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            assert n == 4, f"{mid} 向量表应有 4 行，实际 {n}"
        assert conn.execute("SELECT COUNT(*) FROM units").fetchone()[0] == 4, \
            "文本层不重复（两模型共用同一批 units）"
    finally:
        conn.close()


def test_same_model_rerun_is_idempotent(two_models, src_db, tmp_path):
    """同模型重跑：第二次 embedded=0（真幂等，不被多模型修复破坏）。"""
    settings, ids = two_models
    m_a = ids[0]
    idx = tmp_path / "rag.db"
    emb = OnnxEmbedder(settings.model(m_a), batch_size=4, log=_qlog)
    s1 = run_ingest(settings, embedder=emb, model_id=m_a, source_db=src_db,
                    index_db=idx, batch_size=4, log=_qlog)
    s2 = run_ingest(settings, embedder=emb, model_id=m_a, source_db=src_db,
                    index_db=idx, batch_size=4, log=_qlog)
    assert s1["embedded"] == 4 and s2["embedded"] == 0
    assert s2["skipped_known"] == 4


def test_multi_orchestration_fast_first(two_models, src_db, tmp_path, monkeypatch):
    """fast_first：首个模型同步完成，其余转后台（测试里 mock 掉子进程 spawn）。"""
    settings, ids = two_models
    spawned: list[str] = []
    monkeypatch.setattr(
        "agentmemhub.rag.ingest._spawn_background_model",
        lambda mid, **kw: spawned.append(mid))

    s = dataclasses.replace(settings, index_db=tmp_path / "rag.db",
                            write={**settings.write,
                                   "order": ids,
                                   "fast_first": True})
    reset_embedders()
    r = run_ingest_multi(s, source_db=src_db, log=_qlog)
    assert r["model"] == ids[0] and r["completed"] == [ids[0]]
    assert r["background"] == ids[1:]
    assert spawned == ids[1:], "其余模型必须以子进程方式后台启动"
    assert r["background_hint"]


def test_multi_orchestration_sync_all(two_models, src_db, tmp_path):
    """fast_first=False：全部同步跑完（用于需要严格顺序的场景）。"""
    settings, ids = two_models
    s = dataclasses.replace(settings, index_db=tmp_path / "rag.db",
                            write={**settings.write,
                                   "order": ids, "fast_first": False})
    reset_embedders()
    r = run_ingest_multi(s, source_db=src_db, log=_qlog)
    assert r["completed"] == ids and r["background"] == []
    conn = open_index(s.index_db)
    try:
        for mid in ids:
            t = s.model(mid).vec_table
            assert conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 4
    finally:
        conn.close()
