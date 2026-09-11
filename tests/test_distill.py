"""蒸馏存储层测试：DDL 幂等、幂等键约束、枚举校验。

（切片器/蒸馏/合并/去重逻辑的单测在 D3–D5 追加到本文件。）
"""
from __future__ import annotations

import sqlite3

import pytest

from agentmemhub.distill import (
    CONFIDENCES,
    DISTILLED_SOURCE,
    DISTILLED_SRC_PREFIX,
    MEMORY_STATUSES,
    MEMORY_TYPES,
    SLICE_WHOLE,
    check_memory_fields,
    ensure_distill_schema,
)

_INS_MEM = (
    "INSERT INTO distilled_memories"
    "(source, conversation_id, slice_key, turn_key, type, topic, content,"
    " confidence, status, content_hash, prompt_ver, model, created_at)"
    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
)
_INS_HASH = (
    "INSERT INTO distill_hashes"
    "(source, conversation_id, slice_key, content_hash, prompt_ver, model, created_at)"
    " VALUES (?,?,?,?,?,?,?)"
)


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    ensure_distill_schema(c)
    yield c
    c.close()


def _mem_row(*, source="zcode", conv="sess_1", content_hash="h1",
             mtype="decision", confidence="high"):
    return (source, conv, SLICE_WHOLE, "msg_a", mtype, "AgentMemHub",
            "用户决定采用 RRF 融合", confidence, "new", content_hash, 1,
            "deepseek-flash", 1789000000)


# ── DDL ───────────────────────────────────────────────────────────────

def test_schema_idempotent(conn):
    """重复调用安全（面板任务与 MCP 可能并发启动）。"""
    ensure_distill_schema(conn)
    ensure_distill_schema(conn)


def test_tables_and_indexes_created(conn):
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','index')")}
    assert {"distill_hashes", "distilled_memories"} <= names
    assert {"idx_dm_conv", "idx_dm_status", "idx_dm_hash",
            "idx_distill_hashes_conv"} <= names


def test_type_column_usable(conn):
    """`type` 是列名（SQLite 非保留字），可正常读写。"""
    conn.execute(_INS_MEM, _mem_row())
    row = conn.execute(
        "SELECT type, topic, confidence FROM distilled_memories").fetchone()
    assert row == ("decision", "AgentMemHub", "high")


# ── 幂等：memories 层 ─────────────────────────────────────────────────

def test_memory_unique_by_content_hash(conn):
    """同会话同内容 hash 只允许一条（重跑不产生重复记忆）。"""
    conn.execute(_INS_MEM, _mem_row(content_hash="same"))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(_INS_MEM, _mem_row(content_hash="same"))


def test_memory_same_content_different_conversation_allowed(conn):
    """跨会话的同 hash 内容允许共存（跨会话去重走 S3 标记，不靠 UNIQUE）。"""
    conn.execute(_INS_MEM, _mem_row(conv="sess_1", content_hash="same"))
    conn.execute(_INS_MEM, _mem_row(conv="sess_2", content_hash="same"))
    n = conn.execute("SELECT COUNT(*) FROM distilled_memories").fetchone()[0]
    assert n == 2


def test_memory_status_default_new(conn):
    conn.execute(
        "INSERT INTO distilled_memories"
        "(source, conversation_id, type, content, confidence, content_hash,"
        " prompt_ver, created_at) VALUES (?,?,?,?,?,?,?,?)",
        ("zcode", "sess_1", "fact", "内容", "medium", "h", 1, 0))
    assert conn.execute("SELECT status FROM distilled_memories").fetchone()[0] == "new"


# ── 幂等：hash 层（prompt_ver 语义）───────────────────────────────────

def test_hash_pk_blocks_same_prompt_ver(conn):
    conn.execute(_INS_HASH, ("zcode", "sess_1", SLICE_WHOLE, "h", 1, "m", 0))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(_INS_HASH, ("zcode", "sess_1", SLICE_WHOLE, "h", 1, "m", 0))


def test_hash_allows_reprompt_on_new_version(conn):
    """提示词升版：同内容 hash 在新 prompt_ver 下可登记（触发重蒸）。"""
    conn.execute(_INS_HASH, ("zcode", "sess_1", SLICE_WHOLE, "h", 1, "m", 0))
    conn.execute(_INS_HASH, ("zcode", "sess_1", SLICE_WHOLE, "h", 2, "m", 0))
    n = conn.execute("SELECT COUNT(*) FROM distill_hashes").fetchone()[0]
    assert n == 2


def test_hash_distinguishes_slices(conn):
    """同会话不同切片各自独立登记（切片级幂等）。"""
    conn.execute(_INS_HASH, ("zcode", "sess_1", "s0", "h0", 1, "m", 0))
    conn.execute(_INS_HASH, ("zcode", "sess_1", "s1", "h1", 1, "m", 0))
    n = conn.execute("SELECT COUNT(*) FROM distill_hashes").fetchone()[0]
    assert n == 2


# ── 枚举与常量 ────────────────────────────────────────────────────────

def test_check_memory_fields_accepts_valid():
    for t in MEMORY_TYPES:
        for c in CONFIDENCES:
            assert check_memory_fields(t, c) is None


def test_check_memory_fields_rejects_invalid():
    assert "type 非法" in check_memory_fields("bogus", "high")
    assert "confidence 非法" in check_memory_fields("fact", "very-sure")
    assert "type 非法" in check_memory_fields("", "high")


def test_constants_shape():
    assert MEMORY_TYPES == ("decision", "fact", "preference", "lesson")
    assert MEMORY_STATUSES == ("new", "similar", "duplicate")
    assert DISTILLED_SOURCE == "distilled"
    assert DISTILLED_SRC_PREFIX == "dst_"
    assert SLICE_WHOLE == "whole"
