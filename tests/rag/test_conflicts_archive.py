"""归档的「两层退场」与编译输入口径测试。

2026-11-07 修复的三个问题：
1. `wiki_compile.load_memories()` / `list_sessions()` 没排除 `wiki_ignore_at`
   → 软删记忆仍被编译进页面（align 基线早就排除了，两处口径不一致）；
2. `superseded` 只写 `manual_value=0`（召回退场），wiki 编译层不知道
   → "归档了但页面还在教旧结论"，且全量重跑也不会自动消失；
3. `_delete_empty_session_outputs` 的"清空"判据同样没排除软删
   → 会话被判成非空，旧页面留着不删。
"""
from __future__ import annotations

import sqlite3

import pytest

from agentmemhub.distill import ensure_distill_schema
from agentmemhub.rag.ingest import ensure_vec_table, open_index


def _mk(tmp_path, project_settings):
    import dataclasses

    active = project_settings.active_spec
    st = dataclasses.replace(project_settings,
                            index_db=tmp_path / "idx_wiki_exclude.db")
    conn = open_index(st.index_db)
    ensure_vec_table(conn, active)
    ensure_distill_schema(conn)
    return st, conn


def _mem(conn, mem_id: int, *, content: str, ignore_at: int | None = None):
    with conn:
        conn.execute(
            "INSERT INTO distilled_memories(id, source, conversation_id,"
            " content_hash, type, topic, content, confidence, status,"
            " prompt_ver, created_at, wiki_ignore_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (mem_id, "s", "conv1", f"h{mem_id}", "fact", "t", content,
             "high", "new", 1, 1_700_000_000 + mem_id, ignore_at))


# ── 编译输入排除软删/归档 ──────────────────────────────────────────────

def test_load_memories_excludes_ignored(tmp_path, project_settings):
    """编译取正文时必须排除 `wiki_ignore_at`（否则软删在产出上无效）。"""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    import wiki_compile

    _, conn = _mk(tmp_path, project_settings)
    try:
        _mem(conn, 1, content="正常记忆")
        _mem(conn, 2, content="已软删/归档的记忆", ignore_at=1_800_000_000)
        mems = wiki_compile.load_memories(conn, "s", "conv1")
        contents = [m["content"] for m in mems]
        assert "正常记忆" in contents
        assert "已软删/归档的记忆" not in contents
    finally:
        conn.close()


def test_list_sessions_counts_only_compilable(tmp_path, project_settings):
    """会话计数同样只算可编译的记忆——否则空会话仍被当候选。"""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    import wiki_compile

    _, conn = _mk(tmp_path, project_settings)
    try:
        _mem(conn, 1, content="唯一一条，但已归档", ignore_at=1_800_000_000)
        rows = wiki_compile.list_sessions(conn, min_n=1)
        assert rows == [], "唯一的一条被归档后，该会话不再有可编译输入"
    finally:
        conn.close()


def test_ignore_clause_is_safe_without_column(tmp_path, project_settings):
    """老库没有 wiki_ignore_at 列时不该报错（回落为不过滤）。"""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    import wiki_compile

    st = _mk(tmp_path, project_settings)[0]
    conn = sqlite3.connect(str(tmp_path / "old.db"))
    try:
        conn.execute("CREATE TABLE distilled_memories(id INTEGER PRIMARY KEY,"
                     " source TEXT, conversation_id TEXT, type TEXT,"
                     " confidence TEXT, content TEXT, created_at INTEGER,"
                     " topic TEXT, status TEXT)")
        assert wiki_compile._wiki_ignore_clause(conn) == ""
    finally:
        conn.close()


# ── superseded 双写 ────────────────────────────────────────────────────

def _conflict(conn, *, new_uid: int, old_uid: int) -> int:
    from agentmemhub import conflicts as cf

    cf.ensure_conflicts_schema(conn)
    rec = cf._insert_conflict(conn, unit_new=new_uid, unit_old=old_uid,
                              kind="evolution", rule_hit="test", similarity=0.9,
                              detail={})
    conn.commit()
    return rec["id"]


def _unit(conn, *, src_id: str, text: str, seq: int) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO units(source, conversation_id, seq, role, turn_key,"
            " src_id, time, title, text, chars)"
            " VALUES('memory','conv1',?,'distilled','tk',?,1700000000,'t',?,?)",
            (seq, src_id, text, len(text)))
    return cur.lastrowid


def test_superseded_writes_both_layers(tmp_path, project_settings):
    """superseded：召回层（manual_value=0）+ wiki 层（wiki_ignore_at）双写。"""
    from agentmemhub import conflicts as cf

    _, conn = _mk(tmp_path, project_settings)
    try:
        _mem(conn, 501, content="旧的结论")
        _mem(conn, 502, content="新的结论")
        old_uid = _unit(conn, src_id="dst_h501", text="旧的结论", seq=1)
        new_uid = _unit(conn, src_id="dst_h502", text="新的结论", seq=2)
        cid = _conflict(conn, new_uid=new_uid, old_uid=old_uid)
        out = cf.resolve(conn, cid, "superseded")
        assert out["changed"] and out["old_archived"]
        assert out["distilled_tagged"] is True, "必须找到蒸馏表行并标记"
        # 召回层
        mv = conn.execute("SELECT manual_value FROM unit_values WHERE unit_id=?",
                          (old_uid,)).fetchone()
        assert mv and mv[0] == 0.0
        # wiki 编译层
        ig = conn.execute("SELECT wiki_ignore_at FROM distilled_memories WHERE id=501"
                          ).fetchone()
        assert ig and ig[0], "wiki_ignore_at 必须写入，否则页面仍包含它"
        # 新方不受影响
        ig2 = conn.execute("SELECT wiki_ignore_at FROM distilled_memories WHERE id=502"
                           ).fetchone()
        assert ig2[0] is None
    finally:
        conn.close()


def test_superseded_from_mcp_projection_tags_same_hash(tmp_path, project_settings):
    """`mcp_<h>` 与 `dst_<h>` 双投影：从任一投影归档都要标到蒸馏表。"""
    from agentmemhub import conflicts as cf

    _, conn = _mk(tmp_path, project_settings)
    try:
        _mem(conn, 601, content="同一内容")
        _mem(conn, 602, content="新内容")
        old_uid = _unit(conn, src_id="mcp_h601", text="同一内容", seq=3)
        new_uid = _unit(conn, src_id="dst_h602", text="新内容", seq=4)
        cid = _conflict(conn, new_uid=new_uid, old_uid=old_uid)
        out = cf.resolve(conn, cid, "superseded")
        assert out["distilled_tagged"] is True
        ig = conn.execute("SELECT wiki_ignore_at FROM distilled_memories WHERE id=601"
                          ).fetchone()
        assert ig[0], "mcp_ 投影也要能对上 dst_ 的 content_hash"
    finally:
        conn.close()


def test_kept_both_does_not_archive(tmp_path, project_settings):
    """kept_both / dismissed 不应动任何记忆。"""
    from agentmemhub import conflicts as cf

    _, conn = _mk(tmp_path, project_settings)
    try:
        for i, action in enumerate(("kept_both", "dismissed")):
            base = 700 + i * 10
            _mem(conn, base + 1, content=f"旧{action}")
            _mem(conn, base + 2, content=f"新{action}")
            old_uid = _unit(conn, src_id=f"dst_h{base + 1}",
                            text=f"旧{action}", seq=10 + i * 2)
            new_uid = _unit(conn, src_id=f"dst_h{base + 2}",
                            text=f"新{action}", seq=11 + i * 2)
            cid = _conflict(conn, new_uid=new_uid, old_uid=old_uid)
            out = cf.resolve(conn, cid, action)
            assert out["changed"] and not out["old_archived"]
        assert not conn.execute(
            "SELECT 1 FROM unit_values WHERE manual_value=0").fetchone()
        assert conn.execute(
            "SELECT COUNT(*) FROM distilled_memories WHERE wiki_ignore_at IS NOT NULL"
        ).fetchone()[0] == 0
    finally:
        conn.close()
