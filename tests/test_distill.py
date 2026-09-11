"""蒸馏存储层测试：DDL 幂等、幂等键约束、枚举校验。

（切片器/蒸馏/合并/去重逻辑的单测在 D3–D5 追加到本文件。）
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from agentmemhub.distill import (
    CONFIDENCES,
    DISTILLED_ROLE,
    DISTILLED_SRC_PREFIX,
    MEMORY_STATUSES,
    MEMORY_TYPES,
    MERGE_SLICE_KEY,
    PROMPT_VER,
    SLICE_WHOLE,
    TOPIC_MAX,
    DistillError,
    DistillResult,
    Slice,
    Turn,
    build_slices,
    check_memory_fields,
    conversation_turns,
    distill_slice,
    ensure_distill_schema,
    fingerprint,
    load_conversation_memories,
    mark_slice_done,
    merge_entries,
    merge_input_hash,
    normalize_memories,
    project_memories,
    run_distill,
    save_memories,
    save_merged,
    slice_done,
    turns_from_events,
)

# 假凭据样本：运行时拼接（源码不出现完整格式，避免敏感扫描器误报）
_FAKE_KEY = "sk-" + "X" * 20


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
    assert DISTILLED_ROLE == "distilled"
    assert DISTILLED_SRC_PREFIX == "dst_"
    assert SLICE_WHOLE == "whole"
    assert MERGE_SLICE_KEY == "*merge*"


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


# ══════════════════════════════════════════════════════════════════════
# S1 段级蒸馏
# ══════════════════════════════════════════════════════════════════════

class _FakeLLM:
    """假 LLM 客户端：按序返回预设响应（dict=成功；Exception=抛出）。"""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0
        self.cfg = type("Cfg", (), {"model": "fake-model"})()

    def complete_json(self, system: str, user: str, **kw) -> dict:
        self.calls += 1
        r = self.responses.pop(0) if self.responses else RuntimeError("无预设响应")
        if isinstance(r, Exception):
            raise r
        return r


def _mem(mtype="decision", content="用户决定采用 RRF 融合", confidence="high",
         topic="检索"):
    return {"type": mtype, "topic": topic, "content": content,
            "confidence": confidence}


def _slice(key: str = "s0", *, text: str = "对话内容", turns=("t1",)) -> Slice:
    from agentmemhub.distill import fingerprint as fp
    return Slice(source="zcode", conversation_id="conv-a", slice_key=key,
                 turn_first=turns[0] if turns else None,
                 turn_last=turns[-1] if turns else None,
                 chars=len(text), content_hash=fp(text), text=text, turns=turns)


# ── normalize_memories ────────────────────────────────────────────────

def test_normalize_accepts_valid():
    mems, rejected = normalize_memories({"memories": [_mem()]})
    assert len(mems) == 1 and rejected == []
    assert mems[0]["type"] == "decision" and mems[0]["topic"] == "检索"


def test_normalize_drops_invalid_entries_but_keeps_valid():
    """非法条目逐条丢弃，不毁掉整片产出。"""
    raw = {"memories": [
        _mem(),
        _mem(mtype="bogus"),
        _mem(confidence="very-sure"),
        _mem(content="   "),
        "不是对象",
    ]}
    mems, rejected = normalize_memories(raw)
    assert len(mems) == 1
    assert len(rejected) == 4
    assert any("type 非法" in r for r in rejected)
    assert any("confidence 非法" in r for r in rejected)
    assert any("content 为空" in r for r in rejected)
    assert any("不是对象" in r for r in rejected)


def test_normalize_handles_bad_shapes():
    assert normalize_memories([])[1] == ["顶层不是对象：list"]
    assert normalize_memories({})[1] == ["缺少 memories 字段"]
    assert "不是数组" in normalize_memories({"memories": {}})[1][0]


def test_normalize_empty_array_is_valid_skip():
    """空数组 = LLM 明确说没内容（合法的"无可沉淀"出口）。"""
    mems, rejected = normalize_memories({"memories": []})
    assert mems == [] and rejected == []


def test_normalize_truncates_overlong_topic():
    mems, _ = normalize_memories({"memories": [_mem(topic="主" * 100)]})
    assert len(mems[0]["topic"]) == TOPIC_MAX


# ── distill_slice ─────────────────────────────────────────────────────

def test_distill_slice_success():
    client = _FakeLLM({"memories": [_mem()]})
    r = distill_slice(client, _slice(), title="标题")
    assert len(r.memories) == 1 and r.rejected == []
    assert client.calls == 1


def test_distill_slice_retries_once_on_parse_failure():
    """解析失败重试一次（网关偶发截断常见）。"""
    client = _FakeLLM(ValueError("无法解析"), {"memories": [_mem()]})
    r = distill_slice(client, _slice())
    assert len(r.memories) == 1
    assert client.calls == 2


def test_distill_slice_raises_after_two_parse_failures():
    client = _FakeLLM(ValueError("bad"), ValueError("bad"))
    with pytest.raises(DistillError, match="无法解析"):
        distill_slice(client, _slice())
    assert client.calls == 2


def test_distill_slice_raises_when_all_entries_invalid():
    """有输出但全非法 → 视为失败（不登记 hash，重跑可补）。"""
    client = _FakeLLM({"memories": [_mem(mtype="bogus")]})
    with pytest.raises(DistillError, match="全部非法"):
        distill_slice(client, _slice())
    assert client.calls == 1


def test_distill_slice_llm_error_propagates():
    """LLM 网络/审核错误向上抛，由编排层 fail-open 处理。"""
    client = _FakeLLM(RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        distill_slice(client, _slice())


# ── 幂等（distill_hashes）─────────────────────────────────────────────

def test_slice_done_and_mark(conn):
    sl = _slice()
    assert not slice_done(conn, sl, prompt_ver=1)
    mark_slice_done(conn, sl, prompt_ver=1, model="m")
    assert slice_done(conn, sl, prompt_ver=1)
    mark_slice_done(conn, sl, prompt_ver=1, model="m")     # 重复登记安全
    n = conn.execute("SELECT COUNT(*) FROM distill_hashes").fetchone()[0]
    assert n == 1


def test_slice_done_distinguishes_prompt_ver(conn):
    sl = _slice()
    mark_slice_done(conn, sl, prompt_ver=1)
    assert slice_done(conn, sl, prompt_ver=1)
    assert not slice_done(conn, sl, prompt_ver=2)          # 升版 → 需重蒸


def test_slice_done_distinguishes_content_change(conn):
    mark_slice_done(conn, _slice(text="原文"), prompt_ver=1)
    assert not slice_done(conn, _slice(text="改过的内容"), prompt_ver=1)


# ── save_memories ─────────────────────────────────────────────────────

def _result(*mems):
    return DistillResult(memories=list(mems), rejected=[])


def test_save_memories_inserts_and_registers(conn):
    sl = _slice()
    st = save_memories(conn, sl, _result(_mem()), model="m", created_at=1)
    assert st == {"inserted": 1, "sanitized": 0, "dropped": 0}
    assert slice_done(conn, sl, prompt_ver=PROMPT_VER)
    row = conn.execute(
        "SELECT source, conversation_id, turn_key, type, topic, status"
        " FROM distilled_memories").fetchone()
    assert row == ("zcode", "conv-a", "t1", "decision", "检索", "new")


def test_save_memories_dedupes_identical_content(conn):
    """同会话同内容重复出现 → 只落一条（条目级 hash 去重）。"""
    st1 = save_memories(conn, _slice(), _result(_mem()), created_at=1)
    st2 = save_memories(conn, _slice("s1"), _result(_mem()), created_at=1)
    assert st1["inserted"] == 1 and st2["inserted"] == 0
    assert conn.execute("SELECT COUNT(*) FROM distilled_memories").fetchone()[0] == 1


def test_save_memories_redacts_secrets(conn):
    """脱敏兜底：命中敏感内容 → 剥离后入库，计数上报。"""
    sl = _slice()
    st = save_memories(conn, sl,
                       _result(_mem(content=f"配置里的 key 是 {_FAKE_KEY}")),
                       created_at=1)
    assert st["sanitized"] == 1 and st["dropped"] == 0
    saved = conn.execute("SELECT content FROM distilled_memories").fetchone()[0]
    assert _FAKE_KEY not in saved
    assert "已脱敏" in saved


def test_save_memories_drops_pure_secret(conn):
    """整条都是敏感内容 → 剥空后丢弃（不留空壳记忆）。"""
    sl = _slice()
    st = save_memories(conn, sl, _result(_mem(content=_FAKE_KEY)),
                       created_at=1)
    assert st["dropped"] == 1 and st["inserted"] == 0
    assert conn.execute("SELECT COUNT(*) FROM distilled_memories").fetchone()[0] == 0


def test_save_memories_sanitize_can_be_disabled(conn):
    sl = _slice()
    st = save_memories(conn, sl,
                       _result(_mem(content=f"key {_FAKE_KEY}")),
                       sanitize_enabled=False, created_at=1)
    assert st["sanitized"] == 0 and st["inserted"] == 1


def test_save_memories_records_prompt_ver_and_model(conn):
    save_memories(conn, _slice(), _result(_mem()), prompt_ver=3, model="deepseek-flash",
                  created_at=1)
    row = conn.execute(
        "SELECT prompt_ver, model FROM distilled_memories").fetchone()
    assert row == (3, "deepseek-flash")


# ══════════════════════════════════════════════════════════════════════
# 编排：run_distill（幂等 / fail-open / dry-run）
# ══════════════════════════════════════════════════════════════════════

class _CfgStub:
    model = "fake-model"

    def complete(self) -> bool:
        return True

    def missing_hint(self) -> str:
        return ""


class _SeqLLM:
    """按调用序号产出不同内容（避免条目级去重掩盖统计）。"""

    def __init__(self, *, always_fail: bool = False):
        self.calls = 0
        self.always_fail = always_fail
        self.cfg = _CfgStub()

    def complete_json(self, system: str, user: str, **kw) -> dict:
        self.calls += 1
        if self.always_fail:
            raise RuntimeError("模拟 LLM 故障")
        return {"memories": [{"type": "fact", "topic": "切片", "confidence": "high",
                              "content": f"第 {self.calls} 条蒸馏结论"}]}


class _HubCfgStub:
    """替身统一配置：只提供 run_distill 需要的 distillation 段。"""

    def __init__(self, **over):
        d = {
            "enabled": True, "prompt_ver": 1,
            "llm": {"endpoint": "http://x/v1", "api_key": "k", "model": "fake"},
            "slice": {"max_chars": 24000, "max_turns": 16, "topic_boundary": False,
                      "boundary_window": 4, "per_message_cap": 2000},
            "sanitize": {"enabled": True},
            "runtime": {"max_concurrent": 2, "timeout": 10},
        }
        d.update(over)
        self.distillation = d


@pytest.fixture()
def distill_env(tmp_path, monkeypatch):
    """临时源库 + 索引库 + 替身 LLM 配置（投影阶段需真实模型注册表）。"""
    import dataclasses

    from agentmemhub.rag.config import load_settings

    src_path = tmp_path / "agentmemhub.db"
    c = sqlite3.connect(str(src_path))
    c.executescript(_SRC_DDL)
    c.executemany(
        "INSERT INTO events(source, conversation_id, seq, role, content,"
        " turn_key, is_system) VALUES(?,?,?,?,?,?,?)",
        [("zcode", "conv-a", i, "user" if i % 2 else "assistant",
          f"会话A第{i}轮的内容，讨论记忆蒸馏与切片预算", f"t{i}", 0)
         for i in range(1, 5)] +
        [("zcode", "conv-b", i, "user",
          f"会话B第{i}轮的内容，讨论召回融合与去重阈值", f"t{i}", 0)
         for i in range(1, 3)])
    c.commit()
    c.close()

    monkeypatch.setattr("agentmemhub.config.config", _HubCfgStub)
    # 数据路径指向临时库，模型注册表用真实配置（投影要真的向量化）
    return dataclasses.replace(load_settings(), source_db=src_path,
                               index_db=tmp_path / "session_rag.db")


def _install_llm(monkeypatch, fake):
    monkeypatch.setattr("agentmemhub.llm.LLMClient", lambda cfg, **kw: fake)
    return fake


def _count_memories(idx_path) -> int:
    c = sqlite3.connect(str(idx_path))
    try:
        return c.execute("SELECT COUNT(*) FROM distilled_memories").fetchone()[0]
    finally:
        c.close()


def test_run_distill_end_to_end(distill_env, monkeypatch):
    fake = _install_llm(monkeypatch, _SeqLLM())
    st = run_distill(distill_env)
    assert st["conversations"] == 2
    assert st["slices"] == 2 and st["skipped_done"] == 0
    assert st["distilled"] == 2 and st["failed"] == 0
    assert st["memories_new"] == 2
    assert _count_memories(distill_env.index_db) == 2


def test_run_distill_idempotent_second_pass(distill_env, monkeypatch):
    """幂等：同内容重跑 → 全部跳过、零新增、零 LLM 调用。"""
    fake = _install_llm(monkeypatch, _SeqLLM())
    run_distill(distill_env)
    calls_after_first = fake.calls
    st2 = run_distill(distill_env)
    assert st2["skipped_done"] == st2["slices"] == 2
    assert st2["memories_new"] == 0 and st2["distilled"] == 0
    assert fake.calls == calls_after_first          # 未再调用 LLM
    assert _count_memories(distill_env.index_db) == 2


def test_run_distill_fail_open_then_recover(distill_env, monkeypatch):
    """fail-open：失败不登记 hash → 换可用客户端重跑即补齐（不丢记忆）。"""
    _install_llm(monkeypatch, _SeqLLM(always_fail=True))
    st1 = run_distill(distill_env)
    assert st1["failed"] == 2 and st1["distilled"] == 0
    assert st1["memories_new"] == 0
    assert _count_memories(distill_env.index_db) == 0

    _install_llm(monkeypatch, _SeqLLM())            # 故障恢复后重跑
    st2 = run_distill(distill_env)
    assert st2["skipped_done"] == 0                 # 失败片未被登记
    assert st2["distilled"] == 2 and st2["memories_new"] == 2


def test_run_distill_dry_run_does_not_persist(distill_env, monkeypatch):
    fake = _install_llm(monkeypatch, _SeqLLM())
    st = run_distill(distill_env, dry_run=True)
    assert st["distilled"] == 2 and st["samples"]
    assert _count_memories(distill_env.index_db) == 0     # 未落库

    st2 = run_distill(distill_env, dry_run=True)
    assert st2["skipped_done"] == 0                       # 也未登记 hash
    assert fake.calls == 4


def test_run_distill_limit_and_only(distill_env, monkeypatch):
    _install_llm(monkeypatch, _SeqLLM())
    st = run_distill(distill_env, limit=1)
    assert st["conversations"] == 1

    _install_llm(monkeypatch, _SeqLLM())
    st2 = run_distill(distill_env, only={("zcode", "conv-b")})
    assert st2["conversations"] == 1


def test_run_distill_disabled_returns_error(distill_env, monkeypatch):
    monkeypatch.setattr("agentmemhub.config.config",
                        lambda: _HubCfgStub(enabled=False))
    assert "未启用" in run_distill(distill_env)["error"]


def test_run_distill_missing_llm_config_returns_error(distill_env, monkeypatch):
    monkeypatch.setattr("agentmemhub.config.config",
                        lambda: _HubCfgStub(llm={"endpoint": "", "api_key": "",
                                                 "model": ""}))
    assert "未配置完整" in run_distill(distill_env)["error"]


# ══════════════════════════════════════════════════════════════════════
# S2 同会话合并沉淀
# ══════════════════════════════════════════════════════════════════════

def _add_memory(conn, *, content, source="zcode", cid="conv-a", mtype="fact",
                topic="主题", status="new", slice_key="s0"):
    h = fingerprint(content)
    cur = conn.execute(
        "INSERT INTO distilled_memories"
        "(source, conversation_id, slice_key, type, topic, content, confidence,"
        " status, content_hash, prompt_ver, created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (source, cid, slice_key, mtype, topic, content, "high", status, h, 1, 0))
    conn.commit()
    return {"id": cur.lastrowid, "source": source, "conversation_id": cid,
            "type": mtype, "topic": topic, "content": content,
            "confidence": "high", "turn_key": None, "created_at": 0,
            "content_hash": h}


def test_merge_input_hash_stable_and_sensitive():
    a = [{"content": "甲"}, {"content": "乙"}]
    assert merge_input_hash(a) == merge_input_hash([{"content": "甲"}, {"content": "乙"}])
    assert merge_input_hash(a) != merge_input_hash([{"content": "甲"}, {"content": "丙"}])


def test_load_conversation_memories_excludes_archived(conn):
    _add_memory(conn, content="有效条目", status="new")
    _add_memory(conn, content="相似条目", status="similar")
    _add_memory(conn, content="已被合并的旧稿", status="merged")
    _add_memory(conn, content="判重丢弃的", status="duplicate")
    mems = load_conversation_memories(conn, "zcode", "conv-a")
    assert {m["content"] for m in mems} == {"相似条目", "有效条目"}


def test_merge_entries_calls_llm_with_all_entries():
    client = _FakeLLM({"memories": [_mem(content="合并后的结论")]})
    r = merge_entries(client, [{"type": "fact", "confidence": "high", "content": "甲"},
                               {"type": "fact", "confidence": "high", "content": "乙"}],
                      title="会话标题")
    assert len(r.memories) == 1
    assert client.calls == 1


def test_save_merged_archives_sources_and_writes_final(conn):
    m1 = _add_memory(conn, content="条目甲，关于切片预算")
    m2 = _add_memory(conn, content="条目乙，关于话题边界")
    result = DistillResult(memories=[_mem(content="合并后的终稿结论")], rejected=[])
    st = save_merged(conn, "zcode", "conv-a", result,
                     source_ids=[m1["id"], m2["id"]], created_at=1)
    assert st["inserted"] == 1
    # 来源条目归档为 merged（不再参与召回的候选）
    rows = conn.execute(
        "SELECT id, status FROM distilled_memories WHERE id IN (?,?)",
        (m1["id"], m2["id"])).fetchall()
    assert [r[1] for r in rows] == ["merged", "merged"]
    # 新终稿记录来源，可溯源
    final = conn.execute(
        "SELECT status, merged_from_json, slice_key FROM distilled_memories"
        " WHERE status='new'").fetchone()
    assert final[0] == "new" and final[2] == MERGE_SLICE_KEY
    assert json.loads(final[1]) == [m1["id"], m2["id"]]


def test_save_merged_redacts(conn):
    m = _add_memory(conn, content="条目甲")
    result = DistillResult(memories=[_mem(content=f"key 是 {_FAKE_KEY}")],
                           rejected=[])
    st = save_merged(conn, "zcode", "conv-a", result, source_ids=[m["id"]], created_at=1)
    assert st["sanitized"] == 1
    saved = conn.execute(
        "SELECT content FROM distilled_memories WHERE status='new'").fetchone()[0]
    assert _FAKE_KEY not in saved


# ══════════════════════════════════════════════════════════════════════
# S3 跨会话去重 + S4 投影
# ══════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def real_settings():
    """真实模型注册表（投影要真的向量化，用随项目分发的 bge-small）。"""
    from agentmemhub.rag.config import load_settings
    return load_settings()


@pytest.fixture()
def idx_conn(tmp_path):
    from agentmemhub.rag.ingest import open_index
    c = open_index(tmp_path / "session_rag.db")
    ensure_distill_schema(c)
    yield c
    c.close()


def _unit_count(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM units WHERE role=?", (DISTILLED_ROLE,)).fetchone()[0]


def _mem_dict(conn, **kw):
    m = _add_memory(conn, **kw)
    return m


def test_project_first_time_writes_units(idx_conn, real_settings):
    m = _mem_dict(idx_conn, content="用户决定记忆引擎采用 RRF 融合向量与 trigram 两路召回")
    st = project_memories(idx_conn, real_settings, [m])
    assert st == {"projected": 1, "similar": 0, "duplicate": 0}
    row = idx_conn.execute(
        "SELECT source, conversation_id, seq, role, src_id, text, title"
        " FROM units").fetchone()
    # source/conversation_id 沿用原会话 → 排除与删除机制天然覆盖
    assert row[0] == "zcode" and row[1] == "conv-a"
    assert row[3] == DISTILLED_ROLE
    assert row[2] < 0                                   # 负 seq 不与事件冲突
    assert row[4].startswith(DISTILLED_SRC_PREFIX)
    assert "RRF" in row[5]
    # 状态回写为 new
    assert idx_conn.execute(
        "SELECT status FROM distilled_memories WHERE id=?", (m["id"],)
    ).fetchone()[0] == "new"


def test_project_idempotent(idx_conn, real_settings):
    m = _mem_dict(idx_conn, content="幂等性验证用的记忆内容甲")
    project_memories(idx_conn, real_settings, [m])
    project_memories(idx_conn, real_settings, [m])
    assert _unit_count(idx_conn) == 1


def test_project_duplicate_across_conversations(idx_conn, real_settings):
    """跨会话同内容 → 第二条判重复、不投影、互链到已有条目。"""
    text = "用户决定记忆引擎采用 RRF 融合向量与 trigram 两路召回"
    m1 = _mem_dict(idx_conn, content=text, cid="conv-a")
    m2 = _mem_dict(idx_conn, content=text, cid="conv-b")
    project_memories(idx_conn, real_settings, [m1])
    st2 = project_memories(idx_conn, real_settings, [m2])
    assert st2["duplicate"] == 1 and st2["projected"] == 0
    assert _unit_count(idx_conn) == 1                   # 重复的不占召回面
    row = idx_conn.execute(
        "SELECT status, dedup_of FROM distilled_memories WHERE id=?",
        (m2["id"],)).fetchone()
    assert row[0] == "duplicate" and row[1] == m1["id"]


def test_project_similar_branch(idx_conn, real_settings):
    """相似（低于重复阈值）→ 投影 + 打标互链（阈值化验证分支）。"""
    m1 = _mem_dict(idx_conn, content="用户决定采用 RRF 融合两路召回", cid="conv-a")
    m2 = _mem_dict(idx_conn, content="采用 RRF 向量与 trigram 融合的召回策略", cid="conv-b")
    project_memories(idx_conn, real_settings, [m1])
    st = project_memories(idx_conn, real_settings, [m2],
                          duplicate_threshold=0.999, similar_threshold=0.0)
    assert st["similar"] == 1 and st["projected"] == 1
    assert _unit_count(idx_conn) == 2                   # 相似条目仍入召回面
    row = idx_conn.execute(
        "SELECT status, dedup_of FROM distilled_memories WHERE id=?",
        (m2["id"],)).fetchone()
    assert row[0] == "similar" and row[1] == m1["id"]


def test_project_unrelated_is_new(idx_conn, real_settings):
    m1 = _mem_dict(idx_conn, content="记忆蒸馏按轮数和字符双预算切片", cid="conv-a")
    m2 = _mem_dict(idx_conn, content="晚饭吃火锅店还是烤肉店比较好呢", cid="conv-b")
    project_memories(idx_conn, real_settings, [m1])
    st = project_memories(idx_conn, real_settings, [m2])
    assert st == {"projected": 1, "similar": 0, "duplicate": 0}


def test_projected_memory_searchable_via_fts(idx_conn, real_settings):
    """投影后 FTS 触发器自动同步 → 立刻可被关键词召回。"""
    m = _mem_dict(idx_conn, content="用户决定记忆引擎采用 RRF 融合向量与 trigram 两路召回")
    project_memories(idx_conn, real_settings, [m])
    rows = idx_conn.execute(
        "SELECT rowid FROM units_fts WHERE units_fts MATCH ?", ('"RRF"',)).fetchall()
    assert rows, "投影后 FTS 未同步"
    # 标题也进 FTS（供 title 命中）
    m2 = _mem_dict(idx_conn, content="另一条内容完全不同的结论", cid="conv-c")
    m2["title"] = "向量迁移专题"
    project_memories(idx_conn, real_settings, [m2])
    rows2 = idx_conn.execute(
        "SELECT rowid FROM units_fts WHERE units_fts MATCH ?", ('"向量迁移"',)).fetchall()
    assert rows2


def test_project_needs_units_table_and_vec(idx_conn, real_settings):
    """投影会自动建向量表与 FTS（首次运行无需手工准备）。"""
    m = _mem_dict(idx_conn, content="自动建表验证")
    project_memories(idx_conn, real_settings, [m])
    names = {r[0] for r in idx_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert real_settings.active_spec.vec_table in names
    assert "units_fts" in names
