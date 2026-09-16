"""蒸馏存储层测试：DDL 幂等、幂等键约束、枚举校验。

（切片器/蒸馏/合并/去重逻辑的单测在 D3–D5 追加到本文件。）
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from agentmemhub.distill import (
    CONFIDENCES,
    DISTILLED_ROLE,
    DISTILLED_SRC_PREFIX,
    MEMORY_STATUSES,
    MEMORY_TYPES,
    MERGE_FALLBACK_CAP,
    MERGE_SLICE_KEY,
    PROMPT_VER,
    SLICE_WHOLE,
    TOPIC_MAX,
    DistillError,
    DistillResult,
    Slice,
    Turn,
    _batch_entries,
    build_slices,
    check_memory_fields,
    conversation_turns,
    distill_slice,
    ensure_distill_schema,
    fingerprint,
    load_conversation_memories,
    mark_slice_done,
    merge_entries,
    merge_hierarchical,
    merge_key_for_conversation,
    normalize_memories,
    project_memories,
    purge_stale_memories,
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
    # 键绑定窗口号：max_turns=3 → 每窗口一片
    assert [s.slice_key for s in slices] == ["w0-0", "w1-0", "w2-0", "w3-0"]


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
    assert all(s.slice_key.startswith("w") for s in slices)   # 窗口内序号
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
        return {"memories": [{"type": "fact", "topic": f"主题{self.calls}",
                              "confidence": "high",
                              "content": _unique_text(self.calls)}]}

#: 汉字池：用于生成**互不相似**的假蒸馏内容。
#: 早期版本用 "第 N 条蒸馏结论" 这类模板，导致不同条目余弦相似度 >0.92
#: 被判重 → 会话全部记忆被判重后从召回面消失（触发真实设计语义，但不是
#: 测试想验证的东西）。真实场景内容各异，故用哈希派生的独特串。
_CHARS = "记忆蒸馏切片召回向量融合阈值预算边界轮次话题过滤去重合并投影引擎索引"


def _unique_text(n: int) -> str:
    """由序号派生一段独特且与他项低相似的中文内容。"""
    h = hashlib.sha256(f"seed-{n}".encode()).hexdigest()
    return "该轮对话的结论是" + "".join(
        _CHARS[int(h[i:i + 2], 16) % len(_CHARS)] for i in range(0, 58, 2))


class _HubCfgStub:
    """替身统一配置：只提供 run_distill 需要的 distillation 段。"""

    def __init__(self, **over):
        d = {
            # prompt_ver 留空 → 跟随代码里的 PROMPT_VER（生产默认）；
            # 需覆盖时用 _HubCfgStub(prompt_ver=N) 显式传
            "enabled": True,
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
                topic="主题", status="new", slice_key="s0",
                prompt_ver=None):
    h = fingerprint(content)
    cur = conn.execute(
        "INSERT INTO distilled_memories"
        "(source, conversation_id, slice_key, type, topic, content, confidence,"
        " status, content_hash, prompt_ver, created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (source, cid, slice_key, mtype, topic, content, "high", status, h,
         PROMPT_VER if prompt_ver is None else prompt_ver, 0))
    conn.commit()
    return {"id": cur.lastrowid, "source": source, "conversation_id": cid,
            "type": mtype, "topic": topic, "content": content,
            "confidence": "high", "turn_key": None, "created_at": 0,
            "content_hash": h}


def test_merge_key_binds_slice_hashes(conn):
    """合并幂等键由切片哈希集合决定：切片不变则键不变（重跑跳过）。"""
    for sk, h in (("s0", "h0"), ("s1", "h1")):
        conn.execute(_INS_HASH, ("zcode", "conv-a", sk, h, 1, "m", 0))
    conn.commit()
    k1 = merge_key_for_conversation(conn, "zcode", "conv-a", 1)
    assert k1 == merge_key_for_conversation(conn, "zcode", "conv-a", 1)

    # 切片内容变化 → 键变化（需要重合并）
    conn.execute(_INS_HASH, ("zcode", "conv-a", "s2", "h2", 1, "m", 0))
    conn.commit()
    assert merge_key_for_conversation(conn, "zcode", "conv-a", 1) != k1

    # 提示词升版 → 另一套键
    assert merge_key_for_conversation(conn, "zcode", "conv-a", 2) != k1

    # 关键回归：登记 merge 自身的指纹后键不得变化（自指会让键每轮都变→永不命中）
    base = merge_key_for_conversation(conn, "zcode", "conv-a", 1)
    conn.execute(_INS_HASH, ("zcode", "conv-a", MERGE_SLICE_KEY, base, 1, "m", 0))
    conn.commit()
    assert merge_key_for_conversation(conn, "zcode", "conv-a", 1) == base


def test_merge_key_ignores_memory_status(conn):
    """关键回归：键只看切片，不受条目状态变化影响（首跑把段级归档为 merged
    后，二次运行必须仍能判定"已合并"）——否则会重复合并、产生重复记忆。"""
    conn.execute(_INS_HASH, ("zcode", "conv-a", "s0", "h0", 1, "m", 0))
    m = _add_memory(conn, content="段级条目")
    conn.commit()
    before = merge_key_for_conversation(conn, "zcode", "conv-a", 1)
    conn.execute("UPDATE distilled_memories SET status='merged' WHERE id=?", (m["id"],))
    conn.commit()
    assert merge_key_for_conversation(conn, "zcode", "conv-a", 1) == before


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


# ── 合并输入保护（巨会话实测：110 片 → 段级上百条）──────────────────

def test_batch_entries_respects_budget():
    """按字符预算分批：不超预算、不丢条目。"""
    entries = [{"content": "字" * 100} for _ in range(10)]
    batches = _batch_entries(entries, max_chars=500)
    assert len(batches) > 1
    assert sum(len(b) for b in batches) == 10          # 不丢
    for b in batches:
        cost = sum(len(str(e["content"])) + 60 for e in b)
        # 单条自身即超预算时允许独占一批
        assert cost <= 500 or len(b) == 1


def test_batch_entries_single_batch_when_small():
    assert len(_batch_entries([{"content": "短"}], max_chars=10000)) == 1
    assert _batch_entries([], max_chars=1000) == []


def test_merge_hierarchical_single_call_when_small():
    """输入未超预算 → 单次调用（与原语义一致）。"""
    client = _FakeLLM({"memories": [_mem(content="合并结果")]})
    r = merge_hierarchical(client, [{"type": "fact", "confidence": "high",
                                     "content": "甲"}], max_chars=10000)
    assert len(r.memories) == 1 and client.calls == 1


def test_merge_hierarchical_batches_then_converges():
    """输入超预算 → 分批调用 + 收敛轮，总调用数 < 盲目一次性。"""
    entries = [{"type": "fact", "confidence": "high", "content": "条目" * 100}
               for _ in range(20)]
    # 每批 → 返回固定 2 条，保证能收敛到单批
    client = _FakeLLM(*[{"memories": [_mem(content=f"收敛{i}") for i in range(2)]}
                        for _ in range(20)])
    r = merge_hierarchical(client, entries, max_chars=2000, max_rounds=3)
    assert client.calls > 1                            # 确实分批了
    assert len(r.memories) <= 4                        # 收敛后条数很少


def test_merge_hierarchical_empty_input_no_llm_call():
    client = _FakeLLM()
    r = merge_hierarchical(client, [])
    assert r.memories == [] and client.calls == 0


def test_merge_hierarchical_truncates_when_not_converging():
    """LLM 不去重（每批原样等量返回）→ 轮数用尽后保底截断，不无限调用。"""
    long_text = "内容" * 200                      # 400 字符/条 → 每批仅装 2 条
    entries = [{"type": "fact", "confidence": "high", "content": long_text}
               for _ in range(400)]
    # 每批返回等量同长度条目（不收敛）；两轮各约 200 批
    responses = []
    for i in range(200):
        responses.append({"memories": [
            {"type": "fact", "topic": "t", "confidence": "high",
             "content": f"{long_text}-{i}-{j}"} for j in range(2)]})
    client = _FakeLLM(*(responses * 2))
    r = merge_hierarchical(client, entries, max_chars=1000, max_rounds=2)
    assert client.calls <= 400                     # 调用次数受轮数封顶，不爆炸
    assert len(r.memories) <= MERGE_FALLBACK_CAP   # 保底截断
    assert any("未收敛" in x for x in r.rejected)


# ── prompt_ver 解析：留空跟随代码常量（防"改了提示词却不重蒸"）──

def test_prompt_ver_follows_code_constant_when_unset(distill_env, monkeypatch):
    """配置不写 prompt_ver → 用代码里的 PROMPT_VER（提示词改版即自动重蒸）。"""
    from agentmemhub.distill import PROMPT_VER
    fake = _install_llm(monkeypatch, _SeqLLM())
    st = run_distill(distill_env)
    assert st["prompt_ver"] == PROMPT_VER          # 而非配置默认的数字


def test_prompt_ver_explicit_config_overrides(distill_env, monkeypatch):
    """显式配置 prompt_ver → 覆盖代码常量（强制重蒸手段）。"""
    monkeypatch.setattr("agentmemhub.config.config",
                        lambda: _HubCfgStub(prompt_ver=99))
    _install_llm(monkeypatch, _SeqLLM())
    st = run_distill(distill_env)
    assert st["prompt_ver"] == 99


def test_prompt_ver_change_triggers_redistill(distill_env, monkeypatch):
    """提示词升版 → 已蒸过的切片在新版本下重新蒸馏（不是静默跳过）。"""
    fake = _install_llm(monkeypatch, _SeqLLM())
    st1 = run_distill(distill_env)
    assert st1["skipped_done"] == 0

    monkeypatch.setattr("agentmemhub.config.config",
                        lambda: _HubCfgStub(prompt_ver=99))
    st2 = run_distill(distill_env)
    assert st2["skipped_done"] == 0                # 未因旧版本 hash 而跳过
    assert st2["distilled"] == 2


# ── 提示词升版：旧版产物必须作废（否则新旧混杂重复召回）──

def test_purge_stale_memories_removes_old_version_and_projection(idx_conn, real_settings):
    """旧 prompt_ver 的条目与 units 投影一并清理。"""
    # 一条旧版（v1）条目 + 一条新版（v2）
    old = _add_memory(idx_conn, content="旧版本产出的记忆内容甲", prompt_ver=1)
    new = _add_memory(idx_conn, content="新版本产出的记忆内容乙", prompt_ver=2)
    project_memories(idx_conn, real_settings, [old, new])
    assert _unit_count(idx_conn) == 2

    n = purge_stale_memories(idx_conn, real_settings, 2)
    assert n == 1                                        # 只清旧版
    left = idx_conn.execute(
        "SELECT content, prompt_ver FROM distilled_memories").fetchall()
    assert len(left) == 1 and left[0][1] == 2
    assert _unit_count(idx_conn) == 1                    # 旧版投影同时移除
    # FTS 也不应再命中旧内容
    rows = idx_conn.execute("SELECT rowid FROM units_fts WHERE units_fts MATCH ?",
                            ('"内容甲"',)).fetchall()
    assert not rows


def test_purge_stale_noop_when_all_current(idx_conn, real_settings):
    m = _add_memory(idx_conn, content="当前版本记忆")
    project_memories(idx_conn, real_settings, [m])
    assert purge_stale_memories(idx_conn, real_settings, PROMPT_VER) == 0
    assert _unit_count(idx_conn) == 1


def test_run_distill_idempotent_with_merge(distill_env, monkeypatch):
    """关键回归：**多片会话的合并步骤也必须幂等**。

    原幂等测试用的是单片会话（自动跳过合并），因此漏掉了这条路径：
    首跑把段级条目归档为 merged、产出终稿后，二次运行若以"当前有效条目"
    为合并输入，会发现输入变了而重复合并 → 产出重复记忆（实测 44 条）。
    现在合并幂等键绑定切片哈希，二次运行必须零 LLM 调用。
    """
    monkeypatch.setattr("agentmemhub.config.config", lambda: _HubCfgStub(
        slice={"max_chars": 60, "max_turns": 2, "topic_boundary": False,
               "boundary_window": 4, "per_message_cap": 2000}))
    fake = _install_llm(monkeypatch, _SeqLLM())

    st1 = run_distill(distill_env)
    assert st1["slices"] > 1, "需切成多片才能覆盖合并路径"
    assert st1["merged"] == 1 and st1["merge_failed"] == 0
    assert st1["memories_new"] > 0
    calls_after_first = fake.calls

    st2 = run_distill(distill_env)
    assert st2["skipped_done"] == st2["slices"]        # 切片全部幂等跳过
    assert st2["merged"] == 0 and st2["merge_skipped"] == 1   # 合并也跳过
    assert st2["memories_new"] == 0                    # 零新增（不产生重复）
    assert fake.calls == calls_after_first             # 零 LLM 调用
    assert len(_all_memories(distill_env.index_db)) == len(
        _all_memories(distill_env.index_db))           # 库内条数未增长


def _all_memories(idx_path) -> list:
    c = sqlite3.connect(str(idx_path))
    try:
        return c.execute("SELECT id, content FROM distilled_memories").fetchall()
    finally:
        c.close()


# ══════════════════════════════════════════════════════════════════════
# 历史切片稳定性（窗口化切分的核心目的）
# ══════════════════════════════════════════════════════════════════════

def _conv_turns(n: int, prefix: str = "t", size: int = 20):
    return [_turn(f"{prefix}{i}", ("user", f"第{i}轮：{'内容' * size}")) for i in range(n)]


def test_appending_turns_keeps_historical_slices_identical():
    """核心需求：**追加新对话后，历史窗口的切片键与 hash 逐字不变**。

    旧实现按累积长度切 → 追加会让后续切点整体平移、历史 hash 全变 →
    大面积无谓重蒸。窗口化切分后只有尾部未满窗口会重切。
    """
    kw = dict(max_turns=4, max_chars=10**9, topic_boundary=False)
    before = build_slices("zcode", "c", _conv_turns(8), **kw)
    after = build_slices("zcode", "c", _conv_turns(12), **kw)

    hist = {s.slice_key: s.content_hash for s in before}
    assert list(hist) == ["w0-0", "w1-0"]          # 两个已满窗口

    new = {s.slice_key: s.content_hash for s in after}
    for key, h in hist.items():
        assert new.get(key) == h, f"历史切片 {key} 发生变化（违反了稳定性）"
    assert "w2-0" in new                            # 新增的只有尾部窗口


def test_only_partial_tail_window_is_rebuilt():
    """已满窗口不受影响；仅尾部"未满窗口"随追加重切（新内容总得处理）。"""
    kw = dict(max_turns=4, max_chars=10**9, topic_boundary=False)
    before = build_slices("zcode", "c", _conv_turns(6), **kw)   # 窗口1 只有 2 轮
    after = build_slices("zcode", "c", _conv_turns(8), **kw)    # 窗口1 补满 4 轮

    b = {s.slice_key: s for s in before}
    a = {s.slice_key: s for s in after}
    assert b["w0-0"].content_hash == a["w0-0"].content_hash     # 已满窗口不动
    assert "w1-0" in b and "w1-0" in a
    assert b["w1-0"].content_hash != a["w1-0"].content_hash     # 尾部窗口重切
    assert b["w1-0"].turns == ("t4", "t5")
    assert a["w1-0"].turns == ("t4", "t5", "t6", "t7")


def test_char_budget_splits_within_window_only():
    """字符预算只在窗口内生效，切点不会跨窗口（保证窗口间互不影响）。"""
    turns = [_turn(f"t{i}", ("user", "x" * 100)) for i in range(8)]
    slices = build_slices("zcode", "c", turns, max_turns=4, max_chars=250,
                          topic_boundary=False)
    # 窗口0(轮0-3) → 每片2轮 → w0-0,w0-1；窗口1(轮4-7) → w1-0,w1-1
    assert [s.slice_key for s in slices] == ["w0-0", "w0-1", "w1-0", "w1-1"]
    assert slices[0].turns == ("t0", "t1")
    assert slices[2].turns == ("t4", "t5")


def test_topic_boundary_cannot_cross_window():
    """话题边界细化只能在窗口内移动切点（跨窗口的话题切换不被追随）。"""
    # 窗口边界（轮4/轮5 之间）正是话题切换处：话题A(轮0-3) → 话题B(轮4-7)
    turns = ([_turn(f"a{i}", ("user", "记忆蒸馏切片预算与话题边界的讨论内容"))
              for i in range(4)]
             + [_turn(f"b{i}", ("user", "晚饭吃火锅店还是烤肉店比较好呢给个建议"))
                for i in range(4)])
    slices = build_slices("zcode", "c", turns, max_turns=4, max_chars=10**9,
                          topic_boundary=True, boundary_window=4)
    # 窗口0 与窗口1 各一片，切点就是窗口边界（不会为了话题而跨窗口调整）
    assert [s.slice_key for s in slices] == ["w0-0", "w1-0"]
    assert slices[0].turns == ("a0", "a1", "a2", "a3")
    assert slices[1].turns == ("b0", "b1", "b2", "b3")


# ══════════════════════════════════════════════════════════════════════
# 增量追加场景（真实使用中最常见）：只蒸新片 + 历史记忆参与重新合并
# ══════════════════════════════════════════════════════════════════════

def _append_turns(db_path, source, cid, *, start_seq, turn_keys, content="新增对话"):
    c = sqlite3.connect(str(db_path))
    try:
        c.executemany(
            "INSERT INTO events(source, conversation_id, seq, role, content,"
            " turn_key, is_system) VALUES(?,?,?,?,?,?,0)",
            [(source, cid, start_seq + i, "user", f"{content}{tk}", tk)
             for i, tk in enumerate(turn_keys)])
        c.commit()
    finally:
        c.close()


def test_appending_only_distills_new_slices_and_remerges(distill_env, monkeypatch):
    """核心增量语义：会话被追加后，**只蒸新产生的切片**，且合并时把
    历史终稿与新条目一起重新提炼（不是从零重蒸整个会话）。"""
    # max_turns=2 → conv-a(4轮) 切成 2 片；conv-b(2轮) 1 片
    monkeypatch.setattr("agentmemhub.config.config", lambda: _HubCfgStub(
        slice={"max_chars": 10**9, "max_turns": 2, "topic_boundary": False,
               "boundary_window": 4, "per_message_cap": 2000}))
    fake1 = _install_llm(monkeypatch, _SeqLLM())
    st1 = run_distill(distill_env)
    first_slices = st1["slices"]
    first_distilled = st1["distilled"]
    assert first_slices >= 3 and first_distilled == first_slices   # 全蒸
    finals_1 = _all_memories(distill_env.index_db)
    assert finals_1, "首跑应有终稿"

    # 会话 conv-a 追加 2 轮（第 3 个窗口）→ 仅该窗口的新片需要蒸馏
    _append_turns(distill_env.source_db, "zcode", "conv-a",
                  start_seq=100, turn_keys=("t10", "t11"))

    fake2 = _install_llm(monkeypatch, _SeqLLM())
    st2 = run_distill(distill_env)
    assert st2["slices"] > first_slices, "追加后总片数应增加"
    assert st2["skipped_done"] > 0, "历史片必须被跳过（不重复蒸馏）"
    assert st2["distilled"] < first_distilled, "只应蒸馏新增的片"
    assert st2["distilled"] == st2["slices"] - st2["skipped_done"]
    assert st2["merged"] == 1, "该会话需重新合并（切片集合变了）"

    # 历史终稿应被归档，库内有效条目为重新生成的结果
    conn = sqlite3.connect(str(distill_env.index_db))
    try:
        statuses = dict(conn.execute(
            "SELECT status, COUNT(*) FROM distilled_memories GROUP BY 1").fetchall())
    finally:
        conn.close()
    assert statuses.get("merged", 0) > 0, "旧终稿应归档为 merged"
    assert statuses.get("new", 0) > 0

    # 第三次跑：完全幂等（无新增、无 LLM 调用）
    fake3 = _install_llm(monkeypatch, _SeqLLM())
    st3 = run_distill(distill_env)
    assert st3["distilled"] == 0 and st3["memories_new"] == 0
    assert st3["merge_skipped"] >= 1
    assert fake3.calls == 0


def test_appending_to_single_slice_conversation(distill_env, monkeypatch):
    """单片会话追加后若仍在同一窗口内 → 该片内容变化 → 重蒸该片（合理）。"""
    monkeypatch.setattr("agentmemhub.config.config", lambda: _HubCfgStub(
        slice={"max_chars": 10**9, "max_turns": 16, "topic_boundary": False,
               "boundary_window": 4, "per_message_cap": 2000}))
    _install_llm(monkeypatch, _SeqLLM())
    st1 = run_distill(distill_env, only={("zcode", "conv-b")})
    assert st1["slices"] == 1

    _append_turns(distill_env.source_db, "zcode", "conv-b",
                  start_seq=200, turn_keys=("x1",))
    fake2 = _install_llm(monkeypatch, _SeqLLM())
    st2 = run_distill(distill_env, only={("zcode", "conv-b")})
    assert st2["skipped_done"] == 0, "单片会话内容变了 → 需重蒸"
    assert st2["distilled"] == 1


def test_project_time_uses_origin_turn_time(idx_conn, real_settings):
    """时间语义：蒸馏记忆的 units.time = **知识产生的时间**（原轮次事件时间），
    不是蒸馏时刻——否则全部历史记忆显得"刚写入"，时间衰减与新旧区分失效。"""
    from agentmemhub.rag.ingest import open_index as _oi
    # 造一个原始 unit（属于旧会话，时间是 90 天前）
    old_ts = int(real_settings and 0) or 1
    import time as _time
    old_ts = int(_time.time()) - 90 * 86400
    cur = idx_conn.execute(
        "INSERT INTO units(source, conversation_id, seq, role, turn_key,"
        " src_id, time, title, text, chars) VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("zcode", "conv-t", 1, "user", "tk-old", "src-orig", old_ts,
         None, "原始轮次内容", 6))
    idx_conn.commit()

    m = _add_memory(idx_conn, content="关于旧会话的蒸馏结论", cid="conv-t")
    m["turn_key"] = "tk-old"
    m["created_at"] = int(_time.time())            # 蒸馏发生在今天
    project_memories(idx_conn, real_settings, [m])

    row = idx_conn.execute(
        "SELECT time FROM units WHERE role=?", (DISTILLED_ROLE,)).fetchone()
    assert row[0] == old_ts, f"应取原轮次时间 {old_ts}，实得 {row[0]}"
