"""评分枚举范围（2026-10-29）。

背景：`score` 评的是「units 里聚合成一轮的东西」，但其中一大批**没有正文**：
wiki 页面 636、`dst_` 蒸馏投影 236（正文在 `distilled_memories`，`units.text`
为空）、纯工具轮次 325 —— 实测 3867 条里 1197 条是空壳。

它们进 `evaluate_trace` 第一句就 `return "neutral"`：不烧 LLM 钱，但会被写进
已评清单，还让 `score --unscored-count` 虚高。这里守的就是「枚举阶段就剔掉」。
"""
from agentmemhub import rag_bridge, scoring

_FAKE = [
    {"id": "line:a", "userText": "问一句", "agentText": "答一句"},
    {"id": "wiki_x", "userText": "", "agentText": ""},
    {"id": "line:b", "userText": "   ", "agentText": "\n"},
    {"id": "mcp_y", "userText": "只有用户侧", "agentText": ""},
    {"id": "dst_z", "userText": "", "agentText": ""},
]


def test_turn_refs剔除无正文(monkeypatch):
    """判据是「有没有正文」，不是按 source 硬列白名单。"""
    monkeypatch.setattr(rag_bridge, "all_traces", lambda: list(_FAKE))
    assert rag_bridge.turn_refs() == ["line:a", "mcp_y"]


def test_turn_refs可关闭过滤(monkeypatch):
    """显式点名评某条（`score --ids`）时不该被这层挡掉。"""
    monkeypatch.setattr(rag_bridge, "all_traces", lambda: list(_FAKE))
    assert rag_bridge.turn_refs(scorable_only=False) == [
        "line:a", "wiki_x", "line:b", "mcp_y", "dst_z"]


def test_count_unscored与评分枚举同口径(monkeypatch):
    """`--unscored-count` 与 `run_score_all` 全量模式必须数同一批。"""
    monkeypatch.setattr(rag_bridge, "all_traces", lambda: list(_FAKE))
    monkeypatch.setattr(scoring, "_load_scored", lambda: {"line:a"})
    assert scoring.count_unscored() == 1        # 只剩 mcp_y 未评


def test_turn_refs去重(monkeypatch):
    """多个轮次组可能共享同一个 trace id（锚点的 legacy_id/src_id 相同）。

    下游 `traces_by_ids` 是按 turn_key 去重的 —— 同一个 id 只会评一次。
    这里不去重的话 `--unscored-count` 会虚高（实测 2642 vs 真实 2340）。
    """
    fake = [
        {"id": "line:a", "userText": "问", "agentText": "答"},
        {"id": "line:a", "userText": "问", "agentText": "答"},
        {"id": "line:b", "userText": "问2", "agentText": "答2"},
    ]
    monkeypatch.setattr(rag_bridge, "all_traces", lambda: list(fake))
    assert rag_bridge.turn_refs() == ["line:a", "line:b"]


def _mem_conn():
    """最小 units/unit_values 库，用来喂 `_rows_to_traces`。"""
    import sqlite3
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(
        "CREATE TABLE units(id INTEGER PRIMARY KEY, source TEXT, conversation_id TEXT,"
        " seq INT, role TEXT, turn_key TEXT, time INT, title TEXT, src_id TEXT,"
        " legacy_id TEXT, text TEXT)")
    c.execute("CREATE TABLE unit_values(unit_id INT, value REAL, manual_value REAL)")
    return c


def test_蒸馏记忆独立成轮且有正文():
    """`role='distilled'` 的一行必须单独成轮，且正文要能被评分拿到。

    若按 turn_key 并进原对话轮：一次评分会覆盖同轮的多条记忆，而且评的是对话
    质量而非这条记忆的价值；若两个正文字段都空，`turn_refs` 又会把它当空壳剔除
    —— 两个错误方向都让「蒸馏记忆从没被评过」。
    """
    c = _mem_conn()
    c.execute("INSERT INTO units VALUES(1,'hermes','c1',1,'distilled','t1',100,"
              "'标题','dst_h1',NULL,'一条结论')")
    c.execute("INSERT INTO units VALUES(2,'hermes','c1',2,'user','t1',101,"
              "NULL,NULL,NULL,'用户提问')")
    c.execute("INSERT INTO units VALUES(3,'hermes','c1',3,'assistant','t1',102,"
              "NULL,NULL,NULL,'助手回答')")
    rows = c.execute(rag_bridge._SELECT_TRACE + " ORDER BY u.id").fetchall()
    traces = rag_bridge._rows_to_traces(rows)
    mem = [t for t in traces if t["kind"] == "memory"]
    dia = [t for t in traces if t["kind"] == "dialogue"]
    assert len(mem) == 1 and len(dia) == 1          # 没被并进同一轮
    assert mem[0]["agentText"] == "一条结论"          # 正文进得以评分
    assert mem[0]["userText"] == ""
    assert dia[0]["userText"] == "用户提问"
    assert dia[0]["agentText"] == "助手回答"          # 对话轮不受影响


def test_原子记忆也标记为memory类():
    """`source='memory'` 的 Agent 直写记忆同样是「一条记忆一轮」。"""
    c = _mem_conn()
    c.execute("INSERT INTO units VALUES(9,'memory','mcp',1,'user','mcp',200,"
              "'记忆','mcp_abc',NULL,'一条 Agent 存入的记忆')")
    rows = c.execute(rag_bridge._SELECT_TRACE).fetchall()
    t = rag_bridge._rows_to_traces(rows)[0]
    assert t["kind"] == "memory"
    assert t["userText"] == "一条 Agent 存入的记忆"
