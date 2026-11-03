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
