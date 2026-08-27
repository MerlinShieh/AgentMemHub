"""memos 桥接器单元测试：幂等 id / turn 分组 / value 启发式 / 系统注入跳过。

直接测纯函数 _session_events_to_traces（不需要 SQLite store）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentmemhub.models import Event, renumber
from agentmemhub.memos import _session_events_to_traces as to_traces

CONV = {"id": "c1"}


def _ev(role, content=None, *, src_id=None, turn_key=None, is_system=None,
        time=None, tool_name=None, tool_status=None, tool_call_id=None,
        tool_input=None):
    return Event(role=role, content=content, src_id=src_id, turn_key=turn_key,
                 is_system=is_system, time=time, tool_name=tool_name,
                 tool_status=tool_status, tool_call_id=tool_call_id,
                 tool_input=tool_input)


def test_trace_id_idempotent_on_src_id():
    events = renumber([
        _ev("user", "帮我修 bug", src_id="u1", turn_key="u1", is_system=False, time=100.0),
        _ev("assistant", "找到了，改依赖版本", src_id="a1", turn_key="u1", time=101.0),
    ])
    t1 = to_traces("zcode", CONV, events)
    t2 = to_traces("zcode", CONV, events)
    assert len(t1) == 1 and len(t2) == 1
    assert t1[0]["id"] == t2[0]["id"], "同 src_id 重复导出 id 必须一致"
    assert t1[0]["id"].startswith("trac_")


def test_turn_split_and_grouping():
    """三条 user 消息 → 三条 trace，assistant 归入各自轮。"""
    events = renumber([
        _ev("user", "第一问", src_id="u1", turn_key="u1", is_system=False, time=100.0),
        _ev("assistant", "回答一", src_id="a1", turn_key="u1", time=101.0),
        _ev("user", "第二问", src_id="u2", turn_key="u2", is_system=False, time=200.0),
        _ev("tool", "run", src_id="t1", turn_key="u2", time=201.0,
             tool_name="bash", tool_status="completed"),
        _ev("assistant", "回答二", src_id="a2", turn_key="u2", time=202.0),
        _ev("user", "第三问", src_id="u3", turn_key="u3", is_system=False, time=300.0),
    ])
    ts = to_traces("zcode", CONV, events)
    assert [t["userText"] for t in ts] == ["第一问", "第二问", "第三问"]
    assert ts[1]["agentText"] == "回答二"
    assert [tc["name"] for tc in ts[1]["toolCalls"]] == ["bash"]
    assert ts[0]["turnId"] == 100_000 and ts[2]["ts"] == 300_000


def test_system_injected_user_skipped():
    """系统注入（TodoWrite / DSH context）user 消息不进 bundle 也不开新轮。"""
    events = renumber([
        _ev("user", "真实问题", src_id="u1", turn_key="u1", is_system=False, time=100.0),
        _ev("user", "The TodoWrite tool created a todo list",
            src_id="sys1", turn_key=None, is_system=True, time=101.0),
        _ev("assistant", "处理中", src_id="a1", turn_key="u1", time=102.0),
    ])
    ts = to_traces("dsh", CONV, events)
    assert len(ts) == 1
    assert ts[0]["userText"] == "真实问题"
    assert "TodoWrite" not in json_str(ts)
    assert ts[0]["agentText"] == "处理中"


def test_value_heuristic():
    # 有工具 + 长回复 → 正价值
    tool_events = renumber([
        _ev("user", "部署一下", src_id="u1", turn_key="u1", is_system=False, time=1.0),
        _ev("tool", None, src_id="t1", turn_key="u1", time=2.0,
             tool_name="docker", tool_status="completed"),
        _ev("assistant", "部署完成，" + "详细步骤说明。" * 20, src_id="a1",
            turn_key="u1", time=3.0),
    ])
    assert to_traces("zcode", CONV, tool_events)[0]["value"] >= 0.4

    # tool error → 负价值
    err_events = renumber([
        _ev("user", "跑一下", src_id="u1", turn_key="u1", is_system=False, time=1.0),
        _ev("tool", None, src_id="t1", turn_key="u1", time=2.0,
             tool_name="bash", tool_status="error"),
        _ev("assistant", "失败了", src_id="a1", turn_key="u1", time=3.0),
    ])
    assert to_traces("zcode", CONV, err_events)[0]["value"] < 0

    # 纯寒暄 → 0
    chat_events = renumber([
        _ev("user", "早", src_id="u1", turn_key="u1", is_system=False, time=1.0),
        _ev("assistant", "早啊", src_id="a1", turn_key="u1", time=2.0),
    ])
    assert to_traces("zcode", CONV, chat_events)[0]["value"] == 0


def test_old_events_without_src_id_fallback_seq():
    """旧数据无 src_id：id 由 seq 派生，仍稳定。"""
    events = renumber([
        Event(role="user", content="老数据", time=1.0),
        Event(role="assistant", content="回复", time=2.0),
    ])
    t1 = to_traces("zcode", CONV, events)
    t2 = to_traces("zcode", CONV, events)
    assert t1[0]["id"] == t2[0]["id"]
    assert t1[0]["userText"] == "老数据"


def test_bundle_shape():
    from agentmemhub.memos import build_bundle
    from agentmemhub.store import Store
    import tempfile
    import pathlib

    db = pathlib.Path(tempfile.mkdtemp()) / "t.db"
    st = Store(db)
    events = renumber([
        _ev("user", "你好", src_id="u1", turn_key="u1", is_system=False, time=10.0),
        _ev("assistant", "你好！有什么可以帮你", src_id="a1", turn_key="u1", time=11.0),
    ])
    st.replace_source("zcode", [{"source": "zcode", "id": "c1", "title": "t",
                                 "events": events}], signature="s")
    b = build_bundle(st)
    st.close()
    assert b["version"] == 1
    assert len(b["traces"]) == 1
    assert set(b) == {"version", "exportedAt", "traces", "policies",
                      "worldModels", "skills"}


def json_str(ts) -> str:
    import json
    return json.dumps(ts, ensure_ascii=False)