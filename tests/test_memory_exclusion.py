"""R6 记忆排除：意图落库 + 索引即时收敛 + 摄取防回流 + 轮次级精确性。

关键验收（对应用户需求）：
- 会话被排除 → 该会话全部记忆不召回；
- 会话内部分轮次被排除 → 只有那部分不召回，其余照常；
- 排除后重跑 ingest 不回流（摄取层过滤）；
- 取消排除 → 原文仍在采集库，重跑 ingest 即恢复。
"""
from __future__ import annotations

import dataclasses
import logging
import sqlite3

import pytest

from agentmemhub.store import Store
from agentmemhub.rag.config import load_settings
from agentmemhub.rag.embedder import OnnxEmbedder
from agentmemhub.rag.ingest import (
    delete_units_for_turn,
    open_index,
    run_ingest,
)
from agentmemhub.rag.search import hybrid_search

_qlog = logging.getLogger("asrag.test.excl")
_qlog.addHandler(logging.NullHandler())


@pytest.fixture(scope="module")
def project_settings():
    """项目注册表（本文件在 tests/ 顶层，需自带；与 tests/rag/conftest 同源）。"""
    return load_settings()


@pytest.fixture()
def fixture_source_db(tmp_path):
    """微型源库：含中英混合内容 + 多轮（供排除机制验证）。"""
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
        [("zcode", "conv-a", 1, "user", "bat 批处理为什么报 was unexpected 错误",
          "1", "msg:1", 1786000001),
         ("zcode", "conv-a", 2, "assistant", "括号出现在代码块内部，需要改写参数形式",
          "1", "msg:2", 1786000002),
         ("hermes", "conv-b", 1, "user", "嵌入模型从 384 维切换到 512 维怎么做",
          "1", "msg:10", 1786000010),
         ("hermes", "conv-b", 2, "assistant", "直写迁移 512 维向量并同步重建偏移契约",
          "1", "msg:11", 1786000011),
         ("hermes", "conv-b", 3, "assistant", "全新环境记得重跑模型下载脚本",
          "1", "msg:12", 1786000012)])
    conn.executemany("INSERT INTO conversations(source, id, title) VALUES(?,?,?)",
                     [("zcode", "conv-a", "批处理调试"),
                      ("hermes", "conv-b", "向量迁移")])
    conn.commit()
    conn.close()
    return p


@pytest.fixture()
def embedder(project_settings):
    return OnnxEmbedder(project_settings.active_spec, batch_size=4, log=_qlog)


@pytest.fixture()
def src_db(fixture_source_db):
    """fixture 源库 + 排除表（模拟 AgentMemHub 采集库的完整 schema）。"""
    conn = sqlite3.connect(str(fixture_source_db))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS memory_exclusions("
        " source TEXT NOT NULL, conversation_id TEXT NOT NULL,"
        " turn_key TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL,"
        " note TEXT, PRIMARY KEY(source, conversation_id, turn_key))")
    conn.commit()
    conn.close()
    return fixture_source_db


def _ingest(settings, embedder, src, idx, **kw):
    kw.setdefault("batch_size", 4)
    return run_ingest(settings, embedder=embedder, source_db=src,
                      index_db=idx, log=_qlog, **kw)


def _add_excl(src_db, source, cid, turn_key=""):
    conn = sqlite3.connect(str(src_db))
    conn.execute(
        "INSERT OR IGNORE INTO memory_exclusions"
        "(source, conversation_id, turn_key, created_at) VALUES(?,?,?,1)",
        (source, cid, turn_key))
    conn.commit()
    conn.close()


# ── 会话级 ───────────────────────────────────────────────────────────────

def test_whole_conversation_exclusion_blocks_ingest(
    project_settings, embedder, src_db, tmp_path
):
    idx = tmp_path / "rag.db"
    _ingest(project_settings, embedder, src_db, idx)
    _add_excl(src_db, "hermes", "conv-b")          # conv-b 有 3 个单元
    s = _ingest(project_settings, embedder, src_db, idx)
    assert s["embedded"] == 0, "排除会话不得新增"
    conn = open_index(idx)
    try:
        n = conn.execute("SELECT COUNT(*) FROM units WHERE conversation_id='conv-b'"
                         ).fetchone()[0]
    finally:
        conn.close()
    assert n == 3, "已有单元不会自动消失（须显式删除）——这正是标记时要删的原因"


def test_whole_exclusion_purge_then_no_recall(project_settings, embedder, src_db,
                                              tmp_path):
    """标记 → 物理删除 → 检索不可召回（用户需求：会话被排除则全部不召回）。"""
    from agentmemhub.rag.ingest import delete_units_for_conversation

    idx = tmp_path / "rag.db"
    _ingest(project_settings, embedder, src_db, idx)
    settings = dataclasses.replace(project_settings, index_db=idx)
    q = "嵌入模型维度切换怎么做"
    before = hybrid_search(settings, q, embedder=embedder, k=7, log=_qlog)
    assert any(h.conversation_id == "conv-b" for h in before), "对照：排除前可召回"

    _add_excl(src_db, "hermes", "conv-b")
    conn = open_index(idx)
    try:
        removed = delete_units_for_conversation(conn, "hermes", "conv-b")
    finally:
        conn.close()
    assert removed == 3
    after = hybrid_search(settings, q, embedder=embedder, k=7, log=_qlog)
    assert not any(h.conversation_id == "conv-b" for h in after), "排除后不得召回"
    # 重跑 ingest 不回流（摄取层 NOT EXISTS 过滤）
    s = _ingest(project_settings, embedder, src_db, idx)
    assert s["embedded"] == 0
    again = hybrid_search(settings, q, embedder=embedder, k=7, log=_qlog)
    assert not any(h.conversation_id == "conv-b" for h in again), "防回流"


# ── 轮次级（最关键验收点） ────────────────────────────────────────────────

def test_turn_exclusion_keeps_other_turns(project_settings, embedder, src_db,
                                          tmp_path):
    """排除某轮 → 该轮不召回，同会话其他轮照常召回。"""
    from agentmemhub.rag.ingest import delete_units_for_turn as del_turn
    from agentmemhub.rag.ingest import open_index as oi

    idx = tmp_path / "rag.db"
    # conv-a 的 turn_key 全为 "1"，另加一轮验证隔离性
    conn = sqlite3.connect(str(src_db))
    conn.execute(
        "INSERT INTO events(source, conversation_id, seq, role, content,"
        " turn_key, src_id, time) VALUES(?,?,?,?,?,?,?,?)",
        ("zcode", "conv-a", 10, "user", "第二轮独立话题：向量检索评测集怎么扩",
         "tk2", "msg:60", 1786000060))
    conn.execute(
        "INSERT INTO events(source, conversation_id, seq, role, content,"
        " turn_key, src_id, time) VALUES(?,?,?,?,?,?,?,?)",
        ("zcode", "conv-a", 11, "assistant", "用改写式查询防偷题并做 grounding 校验",
         "tk2", "msg:61", 1786000061))
    conn.commit()
    conn.close()
    _ingest(project_settings, embedder, src_db, idx)
    settings = dataclasses.replace(project_settings, index_db=idx)

    # 排除 tk2 轮
    _add_excl(src_db, "zcode", "conv-a", "tk2")
    c = oi(idx)
    try:
        removed = del_turn(c, "zcode", "conv-a", "tk2")
    finally:
        c.close()
    assert removed == 2

    h2 = hybrid_search(settings, "向量检索评测集怎么扩", embedder=embedder, k=7,
                       log=_qlog)
    assert not any(h.turn_key == "tk2" for h in h2), "被排除轮次不得召回"
    # 同会话 turn_key="1" 的内容仍可召回（残量语义）
    h1 = hybrid_search(settings, "bat 批处理括号报错", embedder=embedder, k=7,
                       log=_qlog)
    assert any(h.conversation_id == "conv-a" and h.turn_key == "1" for h in h1), \
        "同会话未排除轮次必须仍可召回"
    # 重跑不回流
    s = _ingest(project_settings, embedder, src_db, idx)
    assert s["embedded"] == 0
    h2b = hybrid_search(settings, "向量检索评测集怎么扩", embedder=embedder, k=7,
                        log=_qlog)
    assert not any(h.turn_key == "tk2" for h in h2b)


# ── 恢复：取消标记 → 重跑 ingest 即回到索引 ───────────────────────────────

def test_unexclude_restores_on_next_ingest(project_settings, embedder, src_db,
                                           tmp_path):
    from agentmemhub.rag.ingest import delete_units_for_conversation

    idx = tmp_path / "rag.db"
    _ingest(project_settings, embedder, src_db, idx)
    _add_excl(src_db, "hermes", "conv-b")
    conn = open_index(idx)
    try:
        delete_units_for_conversation(conn, "hermes", "conv-b")
    finally:
        conn.close()
    # 取消标记
    conn = sqlite3.connect(str(src_db))
    conn.execute("DELETE FROM memory_exclusions WHERE source=? AND conversation_id=?",
                 ("hermes", "conv-b"))
    conn.commit()
    conn.close()
    s = _ingest(project_settings, embedder, src_db, idx)
    assert s["embedded"] == 3, "取消排除后原文重新嵌入（原文始终在采集库）"


# ── Store 层 API ────────────────────────────────────────────────────────

def test_store_exclusion_api(tmp_path):
    st = Store(tmp_path / "hub.db")
    try:
        assert st.add_exclusion("z", "c1") is True          # 整会话
        assert st.add_exclusion("z", "c1") is False          # 幂等
        assert st.add_exclusion("z", "c1", "tk1") is True    # 轮次
        whole, turns = st.exclusion_state("z", "c1")
        assert whole is True and turns == {"tk1"}
        assert len(st.list_exclusions("z", "c1")) == 2
        assert st.remove_exclusion("z", "c1", "tk1") is True
        whole, turns = st.exclusion_state("z", "c1")
        assert whole is True and turns == set()
        assert st.remove_exclusion("z", "c1") is True
        assert st.exclusion_state("z", "c1") == (False, set())
    finally:
        st.close()


def test_exclusion_survives_source_rebuild(tmp_path):
    """排除表独立于 conversations/events：整源重建不得冲掉排除意图。"""
    st = Store(tmp_path / "hub.db")
    try:
        st.add_exclusion("z", "c1")
        st.replace_source("z", [])            # 模拟整源重建（清空该源）
        assert st.exclusion_state("z", "c1")[0] is True, "排除意图必须存活"
    finally:
        st.close()


def test_ingest_works_without_exclusion_table(project_settings, embedder,
                                              fixture_source_db, tmp_path):
    """源库无 memory_exclusions 表（外部/旧库）时保持既有行为，不报错。"""
    idx = tmp_path / "rag.db"
    s = _ingest(project_settings, embedder, fixture_source_db, idx)
    assert s["embedded"] > 0


# ── 层级语义（2026-09-10 用户反馈修复） ─────────────────────────────────

def test_whole_exclusion_overrides_turn_exclusions(tmp_path):
    """整会话排除是父级：必须覆盖并清除所有轮次排除。

    否则残留的轮次标记会在将来取消整会话排除时继续暗中生效
    （用户以为已全部恢复，实际仍被局部排除）。
    """
    st = Store(tmp_path / "hub.db")
    try:
        st.add_exclusion("z", "c1", "tkA")
        st.add_exclusion("z", "c1", "tkB")
        whole, turns = st.exclusion_state("z", "c1")
        assert whole is False and turns == {"tkA", "tkB"}

        st.add_exclusion("z", "c1")            # 父级排除
        whole, turns = st.exclusion_state("z", "c1")
        assert whole is True, "整会话应被排除"
        assert turns == set(), f"子级轮次标记必须被清除，实际残留 {turns}"
    finally:
        st.close()


def test_remove_whole_restores_everything(tmp_path):
    """取消整会话排除 = 彻底恢复：同时清除所有轮次排除，不留残留。"""
    st = Store(tmp_path / "hub.db")
    try:
        st.add_exclusion("z", "c1", "tkA")
        st.add_exclusion("z", "c1")            # 父级（会清 tkA）
        st.add_exclusion("z", "c1", "tkC")     # 父级之后又加的子级
        st.remove_exclusion("z", "c1")         # 取消父级
        whole, turns = st.exclusion_state("z", "c1")
        assert whole is False and turns == set(), (
            f"应彻底恢复，实际 whole={whole} turns={turns}")

        # 全库无残留
        assert st.list_exclusions("z", "c1") == []
    finally:
        st.close()


def test_turn_exclusion_independent_of_parent_absent(tmp_path):
    """没有父级时，轮次排除各自独立（不影响其他轮）。"""
    st = Store(tmp_path / "hub.db")
    try:
        st.add_exclusion("z", "c1", "tkA")
        st.remove_exclusion("z", "c1", "tkA")
        whole, turns = st.exclusion_state("z", "c1")
        assert whole is False and turns == set()
    finally:
        st.close()
