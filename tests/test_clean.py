"""记忆清洗（clean）与增量同步（sync）单元测试。"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

from agentmemhub import cli
from agentmemhub.models import Event, renumber
from agentmemhub.store import Store


def _store() -> Store:
    tmp = Path(tempfile.mkdtemp()) / "test_clean.db"
    s = Store(tmp)
    evs1 = renumber([
        Event(role="user", content="帮我修登录", time=1),
        Event(role="user", content="TodoWrite 注入的消息", time=2, is_system=True),
        Event(role="reasoning", content="看下代码", time=3),
        Event(role="assistant", content="是监听器问题", time=4),
    ])
    evs2 = renumber([
        Event(role="user", content="system-reminder 注入", time=5, is_system=True),
        Event(role="user", content="正常第二问", time=6),
        Event(role="assistant", content="答复2", time=7),
    ])
    evs3 = renumber([
        Event(role="user", content="qwen 无注入会话", time=8),
        Event(role="assistant", content="ok", time=9),
    ])
    s.replace_source("zcode", [
        {"source": "zcode", "id": "s1", "title": "t1", "cwd": "w",
         "created_at": 1, "updated_at": 4, "model": "m", "meta": {},
         "events": evs1},
        {"source": "zcode", "id": "s2", "title": "t2", "cwd": "w",
         "created_at": 5, "updated_at": 7, "model": "m", "meta": {},
         "events": evs2},
    ], signature="t")
    s.replace_source("qwen", [
        {"source": "qwen", "id": "q1", "title": "q", "cwd": "w",
         "created_at": 8, "updated_at": 9, "model": "m", "meta": {},
         "events": evs3},
    ], signature="t")
    return s


# ---------------------------------------------------------------------------
# store 层
# ---------------------------------------------------------------------------

def test_system_event_counts_by_source():
    s = _store()
    try:
        rows = s.system_event_counts()
        by_src = {r["source"]: r["n"] for r in rows}
        assert by_src == {"zcode": 2}
    finally:
        s.close()


def test_system_event_counts_source_filter():
    s = _store()
    try:
        assert s.system_event_counts("qwen") == []
    finally:
        s.close()


def test_delete_system_events_rebuilds_fts_and_counts():
    s = _store()
    try:
        # FTS 里目前能搜到注入内容
        assert s.search("TodoWrite")
        deleted, convs = s.delete_system_events()
        assert deleted == 2 and convs == 2
        # 事件表：注入行已删除，非注入保留
        evs = s.get_events("zcode", "s1")
        assert [e.role for e in evs] == ["user", "reasoning", "assistant"]
        assert not any(e.is_system for e in evs)
        qs = s.get_events("qwen", "q1")
        assert len(qs) == 2
        # FTS：注入内容不可再搜到，正常内容仍在
        assert not s.search("TodoWrite")
        assert s.search("监听器")
        # event_count 修正
        row = s.get_conversation("zcode", "s1")
        assert row["event_count"] == 3
        row2 = s.get_conversation("zcode", "s2")
        assert row2["event_count"] == 2
    finally:
        s.close()


def test_delete_system_events_source_filter():
    s = _store()
    try:
        deleted, convs = s.delete_system_events("zcode")
        assert deleted == 2 and convs == 2
        # 幂等：再删为 0
        assert s.delete_system_events("zcode") == (0, 0)
    finally:
        s.close()


# ---------------------------------------------------------------------------
# cli 层（预览 vs 执行）
# ---------------------------------------------------------------------------

def test_run_clean_preview_does_not_delete(capsys):
    s = _store()
    try:
        cli.run_clean(s)
        out = capsys.readouterr().out
        assert "共 2 条" in out and "--apply" in out
        # 预览不删除
        assert s.system_event_counts()[0]["n"] == 2
    finally:
        s.close()


def test_run_clean_apply_deletes(capsys):
    s = _store()
    try:
        cli.run_clean(s, apply=True)
        out = capsys.readouterr().out
        assert "已删除 2 条注入事件" in out
        assert s.system_event_counts() == []
    finally:
        s.close()


@mock.patch.object(cli, "run_ingest", return_value=(0, 0))
@mock.patch("agentmemhub.memos_daemon.auth_state", return_value=None)
def test_run_sync_offline_skips_push(_auth, _ingest, capsys):
    """引擎离线：ingest 照常，推送跳过并提示，不触碰 bundle/push。"""
    with mock.patch.object(cli, "Store") as mstore:
        cli.run_sync(push="http://127.0.0.1:18800")
        out = capsys.readouterr().out
        assert "ingest 已完成，跳过推送" in out
        # 未进入 push 阶段（Store 未再打开）
        assert not mstore.called


@mock.patch.object(cli, "run_ingest", return_value=(0, 0))
def test_run_sync_without_push_only_ingests(_ingest, capsys):
    cli.run_sync()
    out = capsys.readouterr().out
    # 无 --push 时只 ingest（不 probe 引擎、不推）
    assert "跳过推送" not in out