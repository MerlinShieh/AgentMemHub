"""记忆库一致性巡检测试。

`agentmemhub/health.py` 是面板「记忆库健康」卡片与 CLI `health_check.py`
**共用的判据来源**，所以这里锁定的就是"什么算正常"——判据漂移会同时让
面板和 CLI 一起骗人，比单点出错更危险。
"""
from __future__ import annotations

import pytest

from agentmemhub.health import collect, reclaim


@pytest.fixture()
def idx_conn(tmp_path):
    from agentmemhub.distill import ensure_distill_schema
    from agentmemhub.rag.ingest import open_index
    from agentmemhub.rag.search import ensure_search_schema
    c = open_index(tmp_path / "session_rag.db")
    ensure_distill_schema(c)
    ensure_search_schema(c)      # 生产库必有 FTS（触发器随 units 自动同步）
    yield c
    c.close()


def _insert_unit(conn, *, src_id=None, role="user", text="内容", updated_at=None,
                 seq=1):
    conn.execute(
        "INSERT INTO units(source, conversation_id, seq, role, src_id, text, chars,"
        " updated_at) VALUES('zcode','c1',?,?,?,?,?,?)",
        (seq, role, src_id, text, len(text), updated_at))
    conn.commit()


def _add_memory(conn, content: str, status: str = "new"):
    from agentmemhub.distill import PROMPT_VER, fingerprint
    h = fingerprint(content)
    conn.execute(
        "INSERT INTO distilled_memories(source, conversation_id, slice_key, type,"
        " content, confidence, status, content_hash, prompt_ver, created_at)"
        " VALUES('zcode','c1','whole','fact',?,'high',?,?,?,0)",
        (content, status, h, PROMPT_VER))
    conn.commit()
    return h


def test_clean_db_is_healthy(idx_conn):
    m = collect(idx_conn)
    assert m["healthy"] is True
    assert m["issues"] == []


def test_flags_missing_updated_at(idx_conn):
    """NULL 行 = 写入它的进程仍在跑旧代码，必须被报出来。"""
    _insert_unit(idx_conn, updated_at=None)
    m = collect(idx_conn)
    assert m["updated_at_null"] == 1
    assert m["healthy"] is False
    assert any("updated_at" in t for t in m["issues"])


def test_active_projection_is_not_flagged(idx_conn):
    """有效条目的投影是正常状态，不得误报为滞留。"""
    from agentmemhub.distill import DISTILLED_SRC_PREFIX
    h = _add_memory(idx_conn, "活跃记忆内容")
    _insert_unit(idx_conn, src_id=DISTILLED_SRC_PREFIX + h, role="distilled",
                 text="活跃记忆内容", updated_at=1, seq=-1)
    m = collect(idx_conn)
    assert m["projections"] == 1
    assert m["active_with_projection"] == 1
    assert m["stale_projections"] == 0
    assert m["healthy"] is True


def test_detects_and_reclaims_stale_projection(idx_conn):
    """归档后滞留的投影必须被报出，且 reclaim 能清掉（幂等）。"""
    from agentmemhub.distill import DISTILLED_SRC_PREFIX
    h = _add_memory(idx_conn, "被合并取代的旧稿", status="merged")
    _insert_unit(idx_conn, src_id=DISTILLED_SRC_PREFIX + h, role="distilled",
                 text="被合并取代的旧稿", updated_at=1, seq=-1)

    m = collect(idx_conn)
    assert m["stale_projections"] == 1
    assert m["healthy"] is False
    assert any("滞留投影" in t for t in m["issues"])

    res = reclaim(idx_conn)
    assert res["reclaimed"] == 1
    assert res["health"]["stale_projections"] == 0
    assert res["health"]["healthy"] is True

    assert reclaim(idx_conn)["reclaimed"] == 0        # 幂等：二次为空操作


def test_same_hash_still_needed_keeps_projection(idx_conn):
    """同内容 hash 仍有有效条目时，投影不算滞留（跨会话同结论的场景）。"""
    from agentmemhub.distill import DISTILLED_SRC_PREFIX
    h = _add_memory(idx_conn, "两会话产出的相同结论", status="merged")
    idx_conn.execute(
        "INSERT INTO distilled_memories(source, conversation_id, slice_key, type,"
        " content, confidence, status, content_hash, prompt_ver, created_at)"
        " VALUES('zcode','c2','whole','fact','两会话产出的相同结论','high','new',?,2,0)",
        (h,))
    idx_conn.commit()
    _insert_unit(idx_conn, src_id=DISTILLED_SRC_PREFIX + h, role="distilled",
                 text="两会话产出的相同结论", updated_at=1, seq=-1)

    m = collect(idx_conn)
    assert m["stale_projections"] == 0
    assert m["active_with_projection"] == 1
    assert m["healthy"] is True


def test_flags_fts_mismatch(idx_conn):
    """FTS 行数与 units 不等时必须报出来（触发器失效/手工改库的征兆）。"""
    _insert_unit(idx_conn, updated_at=1)
    idx_conn.execute("DELETE FROM units_fts")
    idx_conn.commit()
    m = collect(idx_conn)
    assert m["fts_rows"] == 0 and m["units"] == 1
    assert m["healthy"] is False
    assert any("units_fts" in t for t in m["issues"])


def test_flags_vector_orphans(idx_conn, tmp_path):
    """vec0 无触发器：units 删行后向量会成孤儿，必须报出来。"""
    from agentmemhub.rag.config import load_settings
    from agentmemhub.rag.ingest import ensure_vec_table
    import numpy as np

    spec = load_settings().active_spec
    ensure_vec_table(idx_conn, spec)
    _insert_unit(idx_conn, updated_at=1)
    uid = idx_conn.execute("SELECT id FROM units").fetchone()[0]
    idx_conn.execute(
        f"INSERT INTO {spec.vec_table}(rowid, embedding) VALUES(?,?)",
        (uid, np.zeros(spec.dim, dtype=np.float32).tobytes()))
    idx_conn.commit()

    assert collect(idx_conn)["healthy"] is True        # 有向量、行还在 → 正常

    idx_conn.execute("DELETE FROM units WHERE id=?", (uid,))
    idx_conn.commit()
    m = collect(idx_conn)
    assert m["vec_tables"][spec.vec_table]["orphans"] == 1
    assert m["healthy"] is False
    assert any("孤儿" in t for t in m["issues"])
