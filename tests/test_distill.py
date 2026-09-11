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
    Slice,
    Turn,
    build_slices,
    check_memory_fields,
    conversation_turns,
    ensure_distill_schema,
    fingerprint,
    turns_from_events,
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


# ══════════════════════════════════════════════════════════════════════
# S0 切片器
# ══════════════════════════════════════════════════════════════════════

def _turn(key: str, *msgs: tuple[str, str]) -> Turn:
    return Turn(turn_key=key, messages=tuple(msgs),
                chars=sum(len(c) for _, c in msgs))


def test_turns_from_events_groups_by_turn_key():
    rows = [
        (1, "t1", "user", "问题一"),
        (2, "t1", "assistant", "回答一"),
        (3, "t2", "user", "问题二"),
        (4, "t2", "assistant", "回答二"),
    ]
    turns = turns_from_events(rows)
    assert [t.turn_key for t in turns] == ["t1", "t2"]
    assert turns[0].messages == (("user", "问题一"), ("assistant", "回答一"))
    assert turns[0].chars == len("问题一") + len("回答一")


def test_turns_from_events_merges_noncontiguous_same_key():
    """同一 turn_key 的消息即使被其它事件隔开也归为一轮（保证"一轮就是一轮"）。"""
    rows = [
        (1, "t1", "user", "A"),
        (2, "t2", "user", "B"),
        (3, "t1", "assistant", "C"),
    ]
    turns = turns_from_events(rows)
    assert len(turns) == 2
    t1 = next(t for t in turns if t.turn_key == "t1")
    assert t1.messages == (("user", "A"), ("assistant", "C"))


def test_turns_from_events_handles_missing_turn_key():
    """无 turn_key 的事件按自身 seq 成轮，不丢内容（实测占比 0.1%）。"""
    rows = [(7, None, "user", "孤儿消息"), (8, "", "assistant", "另一条")]
    turns = turns_from_events(rows)
    assert [t.turn_key for t in turns] == ["seq:7", "seq:8"]
    assert turns[0].chars == len("孤儿消息")


def test_whole_conversation_is_single_slice():
    """未超双预算 → 单片（slice_key='whole'，首尾轮为 None）。"""
    turns = [_turn("t1", ("user", "短问题"), ("assistant", "短回答"))]
    slices = build_slices("zcode", "sess_1", turns)
    assert len(slices) == 1
    s = slices[0]
    assert s.slice_key == SLICE_WHOLE
    assert s.turn_first is None and s.turn_last is None
    assert s.turns == ("t1",)
    assert "短问题" in s.text and "短回答" in s.text


def test_empty_conversation_no_slices():
    assert build_slices("zcode", "sess_1", []) == []


def test_turn_budget_triggers_slicing():
    """轮数触顶即切（不依赖字符量）。"""
    turns = [_turn(f"t{i}", ("user", "内容" * 5)) for i in range(10)]
    slices = build_slices("zcode", "sess_1", turns, max_turns=3,
                          max_chars=10_000_000, topic_boundary=False)
    assert len(slices) == 4                      # ceil(10/3)
    assert slices[0].turns == ("t0", "t1", "t2")
    assert all(s.slice_key == f"s{i}" for i, s in enumerate(slices))


def test_char_budget_triggers_slicing():
    """字符触顶即切，且后续片从下一轮重新累积。"""
    turns = [_turn(f"t{i}", ("user", "x" * 100)) for i in range(6)]
    slices = build_slices("zcode", "sess_1", turns, max_chars=250,
                          max_turns=999, topic_boundary=False)
    # 每片最多 2 轮（100+100=200 ≤ 250，3 轮=300 > 250）
    assert [s.turns for s in slices] == [
        ("t0", "t1"), ("t2", "t3"), ("t4", "t5")]


def test_topic_boundary_moves_cut_earlier():
    """话题切换处优先成界：粗切点落在话题中间时前移到切换处。

    构造依据：t1/t2 共享 3-gram（火锅店/烤肉店）→ 同话题；t0 与 t1 无任何
    3-gram 交集 → 话题切换。故 t0|t1 之间是最佳切点（预算硬上限在 t1|t2 之间）。
    """
    t0 = _turn("t0", ("user", "记忆蒸馏方案按轮数和字符双预算切片避免超上下文"))
    t1 = _turn("t1", ("user", "晚饭吃火锅店还是烤肉店比较好呢给个建议"))
    t2 = _turn("t2", ("user", "火锅店吧上次那家烤肉店有点腻想换换口味"))
    with_refine = build_slices("zcode", "s", [t0, t1, t2], max_chars=60,
                               max_turns=99, topic_boundary=True,
                               boundary_window=2)
    without = build_slices("zcode", "s", [t0, t1, t2], max_chars=60,
                           max_turns=99, topic_boundary=False)
    assert without[0].turns == ("t0", "t1")          # 不细化：切在预算上限
    assert with_refine[0].turns == ("t0",)           # 细化：前移到话题切换处
    assert with_refine[1].turns == ("t1", "t2")      # 剩余同话题内容成一片


def test_topic_boundary_keeps_cut_when_no_clear_topic_shift():
    """无明显话题切换时保持在预算上限（不多切，控制片数）。"""
    turns = [_turn(f"t{i}", ("user", "记忆蒸馏切片预算与话题边界的讨论内容"))
             for i in range(4)]
    slices = build_slices("zcode", "s", turns, max_chars=40, max_turns=99,
                          topic_boundary=True, boundary_window=3)
    # 全部同话题（相似度并列）→ 不因细化而多切片，首片仍吃满预算
    assert slices[0].turns == ("t0", "t1")
    assert len(slices) == 2


def test_giant_single_turn_hard_split():
    """单轮超预算（导入型巨会话）→ 轮内硬切，内容不丢、每片不超预算。"""
    body = "\n".join(f"第{i}行内容：这是很长的导入会话正文" for i in range(700))
    turns = [_turn("giant", ("assistant", body))]
    assert len(body) > 10_000
    slices = build_slices("qwen", "sess_big", turns, max_chars=1000,
                          max_turns=16, per_message_cap=2000)
    assert len(slices) >= 10                     # 确实被切开
    assert all(s.slice_key.startswith("s") for s in slices)
    # 渲染前缀（[轮次 N | tag] / [role] ）会带来少量开销，放宽 200 字符
    assert all(s.chars <= 1000 + 200 for s in slices)
    # 内容不丢：所有片合起来覆盖全部行
    joined = "\n".join(s.text for s in slices)
    assert "第0行内容" in joined and "第699行内容" in joined
    # 巨轮硬切不施加 per_message_cap（否则 2000 字符截断会丢掉绝大部分内容）
    assert len(joined) > 10_000


def test_giant_turn_slice_keeps_turn_key():
    turns = [_turn("giant", ("assistant", "字" * 5000))]
    slices = build_slices("qwen", "c", turns, max_chars=1000, max_turns=16)
    assert all("giant" in s.turns for s in slices)


def test_slice_hash_stable_and_sensitive():
    """幂等：同输入 → 同 hash；内容变 → hash 变。"""
    turns = [_turn("t1", ("user", "内容甲")), _turn("t2", ("assistant", "内容乙"))]
    a = build_slices("zcode", "c", turns, max_chars=10**9, max_turns=99)
    b = build_slices("zcode", "c", turns, max_chars=10**9, max_turns=99)
    assert [s.content_hash for s in a] == [s.content_hash for s in b]

    changed = [_turn("t1", ("user", "内容丙")), _turn("t2", ("assistant", "内容乙"))]
    c = build_slices("zcode", "c", changed, max_chars=10**9, max_turns=99)
    assert c[0].content_hash != a[0].content_hash


def test_fingerprint_ignores_blank_line_differences():
    assert fingerprint("a\n\nb") == fingerprint("a\nb\n")
    assert fingerprint("a\nb") != fingerprint("a\nc")


def test_render_marks_turn_and_role():
    turns = [_turn("t1", ("user", "问"), ("assistant", "答"))]
    s = build_slices("zcode", "c", turns)[0]
    assert "[轮次 1 |" in s.text
    assert "[user] 问" in s.text and "[assistant] 答" in s.text


def test_per_message_cap_truncates_long_message():
    long_msg = "字" * 5000
    turns = [_turn("t1", ("user", "问")), _turn("t2", ("assistant", long_msg))]
    s = build_slices("zcode", "c", turns, max_chars=10**9, max_turns=99,
                     per_message_cap=100)[0]
    assert "…" in s.text
    assert s.chars < 400                          # 已被截断


def test_slices_cover_all_turns_without_loss():
    """切片不得丢轮：所有片覆盖的 turn 并集 = 原轮序列（顺序一致）。"""
    turns = [_turn(f"t{i}", ("user", "内容" * (i + 1))) for i in range(9)]
    slices = build_slices("zcode", "c", turns, max_chars=50, max_turns=2,
                          topic_boundary=True)
    covered = [k for s in slices for k in s.turns]
    assert covered == [t.turn_key for t in turns]


# ── 采集库读取（含排除过滤）──────────────────────────────────────────

_SRC_DDL = """
CREATE TABLE conversations(
    source TEXT, id TEXT, title TEXT, PRIMARY KEY(source, id));
CREATE TABLE events(
    source TEXT NOT NULL, conversation_id TEXT NOT NULL, seq INTEGER NOT NULL,
    role TEXT NOT NULL, content TEXT, turn_key TEXT, is_system INTEGER DEFAULT 0,
    PRIMARY KEY(source, conversation_id, seq));
"""


@pytest.fixture()
def src_conn():
    c = sqlite3.connect(":memory:")
    c.executescript(_SRC_DDL)
    c.executemany(
        "INSERT INTO events(source, conversation_id, seq, role, content,"
        " turn_key, is_system) VALUES(?,?,?,?,?,?,?)",
        [
            ("zcode", "conv-a", 1, "user", "问题一", "t1", 0),
            ("zcode", "conv-a", 2, "assistant", "回答一", "t1", 0),
            ("zcode", "conv-a", 3, "reasoning", "思考过程", "t1", 0),
            ("zcode", "conv-a", 4, "user", "   ", "t2", 0),        # 空白 → 丢
            ("zcode", "conv-a", 5, "user", "", "t2", 0),           # 空 → 丢
            ("zcode", "conv-a", 6, "user", "注入的伪消息", "t3", 1),  # 系统注入 → 丢
            ("zcode", "conv-a", 7, "user", "问题二", "t4", 0),
            ("zcode", "conv-b", 1, "user", "别的会话", "t1", 0),
        ])
    c.commit()
    yield c
    c.close()


def test_conversation_turns_filters_and_orders(src_conn):
    turns = conversation_turns(src_conn, "zcode", "conv-a")
    assert [t.turn_key for t in turns] == ["t1", "t4"]
    assert turns[0].messages == (("user", "问题一"), ("assistant", "回答一"))
    # reasoning 非白名单角色、空白/空内容、系统注入事件均被过滤


def test_conversation_turns_isolates_conversation(src_conn):
    assert [t.turn_key for t in conversation_turns(src_conn, "zcode", "conv-b")] == ["t1"]


def test_conversation_turns_role_whitelist(src_conn):
    turns = conversation_turns(src_conn, "zcode", "conv-a",
                               roles=("user", "assistant", "reasoning"))
    assert turns[0].messages == (("user", "问题一"), ("assistant", "回答一"),
                                 ("reasoning", "思考过程"))


def test_conversation_turns_respects_whole_session_exclusion(src_conn):
    """整会话排除 → 该会话无轮可蒸馏（不写入记忆的也不会被蒸馏）。"""
    src_conn.executescript(
        "CREATE TABLE memory_exclusions(source TEXT, conversation_id TEXT,"
        " turn_key TEXT, created_at INTEGER, note TEXT);")
    src_conn.execute("INSERT INTO memory_exclusions VALUES(?,?,?,?,?)",
                     ("zcode", "conv-a", "", 0, ""))
    assert conversation_turns(src_conn, "zcode", "conv-a") == []
    # 其它会话不受影响
    assert conversation_turns(src_conn, "zcode", "conv-b") != []


def test_conversation_turns_respects_turn_exclusion(src_conn):
    """轮级排除 → 仅该轮消失，其余照常。"""
    src_conn.executescript(
        "CREATE TABLE memory_exclusions(source TEXT, conversation_id TEXT,"
        " turn_key TEXT, created_at INTEGER, note TEXT);")
    src_conn.execute("INSERT INTO memory_exclusions VALUES(?,?,?,?,?)",
                     ("zcode", "conv-a", "t1", 0, ""))
    turns = conversation_turns(src_conn, "zcode", "conv-a")
    assert [t.turn_key for t in turns] == ["t4"]


def test_conversation_turns_without_exclusion_table(src_conn):
    """源库无排除表（外部/旧库）→ 视作无排除，不报错。"""
    assert len(conversation_turns(src_conn, "zcode", "conv-a")) == 2


def test_slices_end_to_end_from_source(src_conn):
    """读库 → 切片 全链路：会话无排除时产出可蒸馏片段。"""
    turns = conversation_turns(src_conn, "zcode", "conv-a")
    slices = build_slices("zcode", "conv-a", turns)
    assert len(slices) == 1
    assert slices[0].slice_key == SLICE_WHOLE
    assert "问题一" in slices[0].text and "问题二" in slices[0].text
    assert "注入的伪消息" not in slices[0].text
