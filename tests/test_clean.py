"""记忆清洗（clean）与增量同步（sync）单元测试。"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

import pytest

from agentmemhub import cli
from agentmemhub.models import Event, renumber
from agentmemhub.store import Store


@pytest.fixture(autouse=True)
def _isolate_logs(tmp_path, monkeypatch):
    """防 CLI 日志落盘污染真实数据目录（log_dir 指到临时目录）。"""
    monkeypatch.setattr("agentmemhub.logs.log_dir", lambda: tmp_path)


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


class _FakeStore:
    """带两个会话（一个旧一个新）的最小 store 假件。"""
    def __init__(self):
        import time as _t
        self.new_ts = _t.time() - 100          # 新会话（锚后）
        self.old_ts = _t.time() - 2000         # 旧会话（锚前）
        self.closed = False

    def list_conversations(self, src=None):
        return [{"source": "zcode", "updated_at": self.new_ts},
                {"source": "zcode", "updated_at": self.old_ts}]

    def delete_system_events(self, source=None, *, conversations=None):
        return (0, 0)

    def close(self):
        self.closed = True


def _wm_setup(tmp_path, monkeypatch, changed):
    """config.data_dir → tmp_path 并预置一份含 changed 变更集的水位状态。"""
    from agentmemhub import watermarks

    class _CfgStub:
        data_dir = tmp_path

    monkeypatch.setattr("agentmemhub.config.config", lambda: _CfgStub())
    st = watermarks.load_state(tmp_path)
    watermarks.record_ingest(st, ts=1000.0, changed=changed)
    watermarks.save_state(tmp_path, st)
    return st


@mock.patch.object(cli, "run_ingest", return_value=(0, 0))
@mock.patch("agentmemhub.memos_daemon.auth_state", return_value={})
def test_run_sync_incremental_pushes_only_delta(_auth, _ingest, tmp_path, monkeypatch, capsys):
    """增量：delta 会话以 only= 精确下推 push_to_memos；成功后标记消费并入队评分。"""
    from agentmemhub import watermarks
    _wm_setup(tmp_path, monkeypatch,
              [{"source": "zcode", "id": "conv_new", "status": "added"}])
    fake = _FakeStore()
    with mock.patch.object(cli, "Store", return_value=fake), \
         mock.patch.object(cli, "push_to_memos", return_value={
             "imported": 5, "skipped": 0, "lines": [], "rebuilt": None,
             "failed": 0, "pushed_ids": ["trac_1", "trac_2"]}) as pm:
        cli.run_sync(push="http://127.0.0.1:18800")
    assert pm.called
    assert pm.call_args.kwargs["only"] == {"zcode": {"conv_new"}}   # delta 精确过滤
    assert pm.call_args.kwargs.get("since_ts") is None
    assert fake.closed
    st = watermarks.load_state(tmp_path)
    assert st["consumed"]["push"] >= 1000.0                # delta 已标记消费
    assert st["pending_score"] == ["trac_1", "trac_2"]     # 推送 trace 入队评分


@mock.patch.object(cli, "run_ingest", return_value=(0, 0))
@mock.patch("agentmemhub.memos_daemon.auth_state", return_value={})
def test_run_sync_incremental_skips_when_nothing_pending(_auth, _ingest, tmp_path, monkeypatch, capsys):
    """增量：delta 已消费/为空 → 跳过推送（不调 push_to_memos）。"""
    from agentmemhub import watermarks
    st = _wm_setup(tmp_path, monkeypatch,
                   [{"source": "zcode", "id": "conv_new", "status": "added"}])
    watermarks.mark_consumed(st, "push")
    watermarks.save_state(tmp_path, st)

    with mock.patch.object(cli, "Store", return_value=_FakeStore()), \
         mock.patch.object(cli, "push_to_memos") as pm:
        cli.run_sync(push="http://127.0.0.1:18800")
    out = capsys.readouterr().out
    assert "无待推送变更" in out
    assert not pm.called


@mock.patch.object(cli, "run_ingest", return_value=(0, 0))
@mock.patch("agentmemhub.memos_daemon.auth_state", return_value={})
def test_run_sync_failure_keeps_delta(_auth, _ingest, tmp_path, monkeypatch, capsys):
    """推送有失败 → 不标记 delta 消费（下次 sync 重试失败批次，不丢数据）。"""
    from agentmemhub import watermarks
    _wm_setup(tmp_path, monkeypatch,
              [{"source": "zcode", "id": "conv_new", "status": "added"}])
    with mock.patch.object(cli, "Store", return_value=_FakeStore()), \
         mock.patch.object(cli, "push_to_memos", return_value={
             "imported": 0, "skipped": 0, "lines": [], "rebuilt": None,
             "failed": 1, "pushed_ids": []}) as pm:
        cli.run_sync(push="http://127.0.0.1:18800")
    out = capsys.readouterr().out
    assert "变更集未标记消费" in out
    st = watermarks.load_state(tmp_path)
    assert st["consumed"].get("push", 0) < 1000.0          # 未标记消费
    assert st["pending_score"] == []                        # 失败批次不入评分队列


@mock.patch.object(cli, "run_ingest", return_value=(0, 0))
@mock.patch("agentmemhub.memos_daemon.auth_state", return_value={})
def test_run_sync_full_ignores_delta(_auth, _ingest, tmp_path, monkeypatch, capsys):
    """--full 强制全量推送（only=None），并跳过 delta 清洗。"""
    from agentmemhub import watermarks
    _wm_setup(tmp_path, monkeypatch,
              [{"source": "zcode", "id": "conv_new", "status": "added"}])
    fake = _FakeStore()
    with mock.patch.object(cli, "Store", return_value=fake), \
         mock.patch.object(cli, "push_to_memos", return_value={
             "imported": 5, "skipped": 0, "lines": [], "rebuilt": None,
             "failed": 0, "pushed_ids": []}) as pm:
        cli.run_sync(push="http://127.0.0.1:18800", full=True)
    assert _ingest.call_args.kwargs.get("full") is True
    assert pm.call_args.kwargs.get("only") is None          # 全量，不走 delta 过滤