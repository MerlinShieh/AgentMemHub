"""`agentmemhub stale` 的渲染：有未处置冲突的候选要能一眼看出来。

数据侧的 `tier` 判定在 `tests/rag/test_recall_stats.py` 与 `tests/test_lifecycle.py`
已覆盖；这里只守 CLI 那一层 —— 它决定"用户看得见什么"。
"""
from __future__ import annotations

import argparse
import time
from types import SimpleNamespace

from agentmemhub import cli


def _row(**kw):
    base = {"id": 30652, "src_id": "dst_deadbeef", "source": "zcode",
            "role": "distilled", "time": int(time.time()) - 100 * 86400,
            "title": "t", "value": 0.3, "priority": 0, "hit_count": 0,
            "last_hit_at": None, "text": "一条很老的记忆",
            "freshness": 0.85, "stale_score": 0.255,
            "tier": "never_hit"}
    base.update(kw)
    return base


def _args(**kw):
    a = argparse.Namespace(days=90, limit=200, json=False)
    for k, v in kw.items():
        setattr(a, k, v)
    return a


def _patch(monkeypatch, rows):
    monkeypatch.setattr("agentmemhub.rag.config.load_settings",
                        lambda: SimpleNamespace(index_db="fake.db"))
    monkeypatch.setattr("agentmemhub.rag.recall_stats.stale_memories",
                        lambda *a, **k: rows)


def test_stale_marks_open_conflict_old(monkeypatch, capsys):
    """`conflict_old` 候选要打「有未处置冲突」，其余不打。"""
    _patch(monkeypatch, [_row(tier="conflict_old"), _row(id=2, src_id="dst_x")])
    assert cli.cmd_stale(_args()) == 0
    out = capsys.readouterr().out
    assert "[有未处置冲突]" in out
    assert out.count("[有未处置冲突]") == 1, "只有 conflict_old 那一条该带标记"


def test_stale_json_keeps_tier(monkeypatch, capsys):
    """`--json` 必须原样带 `tier`（面板/脚本靠它分组）。"""
    _patch(monkeypatch, [_row(tier="conflict_old")])
    assert cli.cmd_stale(_args(json=True)) == 0
    out = capsys.readouterr().out
    assert '"tier": "conflict_old"' in out
    assert '"age_days"' in out, "JSON 输出要补 age_days（既有行为）"
