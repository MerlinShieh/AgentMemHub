"""增量同步（会话级 upsert / ingest 增量 / delta 贯通）单元测试。"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from agentmemhub.models import Event, renumber
from agentmemhub.store import Store


@pytest.fixture(autouse=True)
def _isolate_logs(tmp_path, monkeypatch):
    """防 CLI 日志落盘污染真实数据目录（log_dir 指到临时目录）。"""
    monkeypatch.setattr("agentmemhub.logs.log_dir", lambda: tmp_path)


def _db() -> Store:
    tmp = Path(tempfile.mkdtemp()) / "test_incremental.db"
    return Store(tmp)


def _sess(source: str, sid: str, updated_at: float,
          texts: list[str], *, system_flags: list[bool] | None = None) -> dict:
    events = renumber([
        Event(role="user", content=t, time=updated_at + i,
              is_system=(system_flags[i] if system_flags else None))
        for i, t in enumerate(texts)
    ])
    return {"source": source, "id": sid, "title": f"t-{sid}", "cwd": "w",
            "created_at": updated_at - 100, "updated_at": updated_at,
            "model": "m", "meta": {}, "events": events}


# ---------------------------------------------------------------------------
# store.upsert_sessions
# ---------------------------------------------------------------------------

def test_upsert_add_then_unchanged():
    s = _db()
    try:
        sess = _sess("zcode", "s1", 100.0, ["第一问", "答一"])
        r = s.upsert_sessions("zcode", [sess])
        assert r == {"added": 1, "updated": 0, "unchanged": 0, "events": 2}

        # 幂等：同 updated_at 同事件数 → unchanged，事件不重复
        r2 = s.upsert_sessions("zcode", [_sess("zcode", "s1", 100.0, ["第一问", "答一"])])
        assert r2 == {"added": 0, "updated": 0, "unchanged": 1, "events": 0}
        assert s.stats()["events"] == 2
    finally:
        s.close()


def test_upsert_updated_rewrites_and_fts():
    s = _db()
    try:
        s.upsert_sessions("zcode", [_sess("zcode", "s1", 100.0, ["旧内容甲"])])
        # updated_at 前进 + 换内容
        r = s.upsert_sessions("zcode", [_sess("zcode", "s1", 200.0, ["新内容乙", "追问"])])
        assert r == {"added": 0, "updated": 1, "unchanged": 0, "events": 2}

        conv = s.get_conversation("zcode", "s1")
        assert conv["updated_at"] == 200.0
        assert conv["event_count"] == 2
        evs = s.get_events("zcode", "s1")
        assert [e.content for e in evs] == ["新内容乙", "追问"]
        # FTS：新内容可搜、旧内容不可搜、无重复行
        assert len(s.search("新内容乙")) == 1
        assert s.search("旧内容甲") == []
        assert len(s.search("追问")) == 1
    finally:
        s.close()


def test_upsert_same_ts_but_event_count_changed():
    s = _db()
    try:
        s.upsert_sessions("zcode", [_sess("zcode", "s1", 100.0, ["只有一条"])])
        # updated_at 未变但事件数变了 → 必须重写（否则增量会漏掉截断/追加）
        r = s.upsert_sessions("zcode", [_sess("zcode", "s1", 100.0, ["一条", "两条"])])
        assert r["updated"] == 1
        assert s.get_conversation("zcode", "s1")["event_count"] == 2
    finally:
        s.close()


def test_upsert_keeps_conversations_absent_from_source():
    s = _db()
    try:
        s.upsert_sessions("zcode", [_sess("zcode", "s1", 100.0, ["a"]),
                                    _sess("zcode", "s2", 100.0, ["b"])])
        # 源端这次只带来 s1 → s2 保留（历史保全，与 replace_source 的整源替换不同）
        s.upsert_sessions("zcode", [_sess("zcode", "s1", 150.0, ["a2"])])
        assert s.get_conversation("zcode", "s2") is not None
        assert s.get_conversation("zcode", "s1")["event_count"] == 1
    finally:
        s.close()


def test_upsert_force_rewrites_unchanged():
    s = _db()
    try:
        s.upsert_sessions("zcode", [_sess("zcode", "s1", 100.0, ["a"])])
        r = s.upsert_sessions("zcode", [_sess("zcode", "s1", 100.0, ["a"])], force=True)
        assert r == {"added": 0, "updated": 1, "unchanged": 0, "events": 1}
    finally:
        s.close()


def test_upsert_multi_source_isolation():
    s = _db()
    try:
        s.upsert_sessions("zcode", [_sess("zcode", "x", 100.0, ["hi"])])
        s.upsert_sessions("qwen", [_sess("qwen", "x", 100.0, ["你好"])])
        # 同 id 不同 source 互不干扰
        assert s.get_conversation("zcode", "x")["event_count"] == 1
        assert s.get_conversation("qwen", "x")["event_count"] == 1
        assert s.stats()["conversations"] == 2
    finally:
        s.close()


# ---------------------------------------------------------------------------
# run_ingest 增量主流程（FakeAdapter 隔离真实数据源）
# ---------------------------------------------------------------------------

from agentmemhub import adapters, cli, watermarks  # noqa: E402
from agentmemhub.adapters.base import AgentAdapter  # noqa: E402


class _FakeAdapter(AgentAdapter):
    """可控假适配器：会话列表可变，记录每次 load 的 only_ids。"""

    def __init__(self, source: str = "fake"):
        self.source = source
        self.label = "Fake"
        self.sessions: list[dict] = []
        self.list_returns_none = False
        self.freshness: float | None = None
        self.load_calls: list = []

    def candidate_paths(self) -> list[Path]:
        return []

    def locate(self):
        return Path(".")

    def list_sessions(self, path):
        if self.list_returns_none:
            return None
        return [{"id": s["id"], "updated_at": s["updated_at"]}
                for s in self.sessions]

    def source_freshness(self, path):
        return self.freshness

    def load(self, path, only_ids=None):
        self.load_calls.append(only_ids)
        rows = self.sessions if only_ids is None \
            else [s for s in self.sessions if s["id"] in only_ids]
        return [dict(s, events=list(s["events"])) for s in rows]


class _Cfg:
    """config.config() 替身：data_dir 指向测试临时目录。"""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)

    def agent_path(self, source):
        return None


def _patch(monkeypatch, tmp_path, fake: _FakeAdapter, db_name="ingest.db"):
    monkeypatch.setattr(adapters, "get_adapter",
                        lambda src: fake if src == fake.source else None)
    dbp = tmp_path / db_name
    monkeypatch.setattr(cli, "Store", lambda: Store(dbp))
    monkeypatch.setattr("agentmemhub.config.config", lambda: _Cfg(tmp_path))
    return dbp


def test_run_ingest_delta_flow(tmp_path, monkeypatch):
    fake = _FakeAdapter()
    fake.sessions = [_sess("fake", "s1", 100.0, ["hello"]),
                     _sess("fake", "s2", 100.0, ["world"])]
    dbp = _patch(monkeypatch, tmp_path, fake)
    lines: list[str] = []

    # 首轮：库空 → 全部 added，only_ids 精确下推
    conv, ev = cli.run_ingest(["fake"], stdout=lines.append)
    assert (conv, ev) == (2, 2)
    assert fake.load_calls[-1] == {"s1", "s2"}
    st = watermarks.load_state(tmp_path)
    assert {c["id"] for c in st["delta"]["conversations"]} == {"s1", "s2"}
    assert st["delta"]["oversized"] is False
    assert st["last_ingest"]["sources"]["fake"]["counts"]["added"] == 2

    # 二轮：无变化 → 不读源；上轮 delta 无人消费 → 残留合并保留（防丢）
    conv2, ev2 = cli.run_ingest(["fake"], stdout=lines.append)
    assert (conv2, ev2) == (0, 0)
    assert len(fake.load_calls) == 1             # 没有新的 load 调用
    assert any("无变化" in ln for ln in lines)
    st2 = watermarks.load_state(tmp_path)
    assert {c["id"] for c in st2["delta"]["conversations"]} == {"s1", "s2"}

    # 三轮：s2 更新 → 只重读 s2；delta = 本轮 s2 + 残留 s1
    fake.sessions[1] = _sess("fake", "s2", 200.0, ["world", "!"])
    conv3, ev3 = cli.run_ingest(["fake"], stdout=lines.append)
    assert (conv3, ev3) == (1, 2)
    assert fake.load_calls[-1] == {"s2"}
    st3 = watermarks.load_state(tmp_path)
    delta = {(c["source"], c["id"]) for c in st3["delta"]["conversations"]}
    assert delta == {("fake", "s1"), ("fake", "s2")}

    # 下游消费后：再跑一次无变化 ingest → delta 清空
    watermarks.mark_consumed(st3, "push")
    watermarks.save_state(tmp_path, st3)
    conv4, _ = cli.run_ingest(["fake"], stdout=lambda _s: None)
    assert conv4 == 0
    st4 = watermarks.load_state(tmp_path)
    assert st4["delta"]["conversations"] == []

    s = Store(dbp)
    try:
        assert s.get_conversation("fake", "s2")["event_count"] == 2
        assert s.get_conversation("fake", "s1")["event_count"] == 1
    finally:
        s.close()


def test_run_ingest_no_listing_rescan(tmp_path, monkeypatch):
    fake = _FakeAdapter()
    fake.list_returns_none = True
    fake.sessions = [_sess("fake", "s1", 100.0, ["a"])]
    _patch(monkeypatch, tmp_path, fake)
    lines: list[str] = []

    conv, ev = cli.run_ingest(["fake"], stdout=lines.append)
    assert (conv, ev) == (1, 1)
    assert fake.load_calls[-1] is None            # 整源 load
    assert any("整源重扫" in ln for ln in lines)

    # 二轮内容未变：仍整源 load，但 upsert 对比 → unchanged，不重写
    conv2, ev2 = cli.run_ingest(["fake"], stdout=lines.append)
    assert (conv2, ev2) == (0, 0)
    assert fake.load_calls[-1] is None
    lines2 = list(lines)
    assert any("未变 1" in ln for ln in lines2)


def test_run_ingest_freshness_forces_rescan(tmp_path, monkeypatch):
    fake = _FakeAdapter()
    fake.sessions = [_sess("fake", "s1", 100.0, ["a"])]
    dbp = _patch(monkeypatch, tmp_path, fake)

    cli.run_ingest(["fake"], stdout=lambda _s: None)
    # 清单与 updated_at 都没变，但源级信号（如 audit 追加）新鲜 → force 重扫
    fake.freshness = 1e12
    lines: list[str] = []
    conv, ev = cli.run_ingest(["fake"], stdout=lines.append)
    assert fake.load_calls[-1] is None
    assert any("信号触发整源重扫" in ln for ln in lines)
    s = Store(dbp)
    try:
        assert s.get_conversation("fake", "s1")["event_count"] == 1  # force 重写无残留
    finally:
        s.close()


def test_run_ingest_full_replaces_source(tmp_path, monkeypatch):
    fake = _FakeAdapter()
    dbp = _patch(monkeypatch, tmp_path, fake)
    s0 = Store(dbp)
    try:
        s0.upsert_sessions("fake", [_sess("fake", "gone", 100.0, ["旧会话"])])
    finally:
        s0.close()

    fake.sessions = [_sess("fake", "s1", 100.0, ["新会话"])]
    lines: list[str] = []
    conv, ev = cli.run_ingest(["fake"], full=True, stdout=lines.append)
    assert (conv, ev) == (1, 1)
    assert any("全量重建" in ln for ln in lines)
    s = Store(dbp)
    try:
        # full 语义：源端消失的会话被清掉（与增量模式的历史保全不同）
        assert s.get_conversation("fake", "gone") is None
        assert s.get_conversation("fake", "s1") is not None
    finally:
        s.close()
    st = watermarks.load_state(tmp_path)
    assert {c["id"] for c in st["delta"]["conversations"]} == {"s1"}


def test_run_ingest_carries_unconsumed_delta(tmp_path, monkeypatch):
    """连续两次 ingest 之间下游未消费：旧 delta 条目须并入新 delta（防丢）。"""
    fake = _FakeAdapter()
    fake.sessions = [_sess("fake", "s1", 100.0, ["a"])]
    _patch(monkeypatch, tmp_path, fake)

    cli.run_ingest(["fake"], stdout=lambda _s: None)          # delta: s1
    fake.sessions[0] = _sess("fake", "s1", 200.0, ["a", "b"])
    fake.sessions.append(_sess("fake", "s2", 200.0, ["c"]))
    cli.run_ingest(["fake"], stdout=lambda _s: None)          # delta: s1 + s2(合并)

    st = watermarks.load_state(tmp_path)
    delta = {(c["source"], c["id"]) for c in st["delta"]["conversations"]}
    assert delta == {("fake", "s1"), ("fake", "s2")}

    # clean 消费后：下一轮 ingest 不再携带残留
    watermarks.mark_consumed(st, "clean")
    watermarks.save_state(tmp_path, st)
    fake.sessions[0] = _sess("fake", "s1", 300.0, ["a", "b", "c"])
    cli.run_ingest(["fake"], stdout=lambda _s: None)
    st2 = watermarks.load_state(tmp_path)
    assert {c["id"] for c in st2["delta"]["conversations"]} == {"s1"}


def test_watermarks_pending_and_oversized(tmp_path):
    st = watermarks.load_state(tmp_path)
    assert watermarks.pending_for(st, "clean") is None       # 无 delta → 全量回退

    watermarks.record_ingest(st, ts=100.0, changed=[
        {"source": "x", "id": str(i), "status": "updated"} for i in range(3)])
    assert [c["id"] for c in watermarks.pending_for(st, "clean")] == \
        ["0", "1", "2"]
    watermarks.mark_consumed(st, "clean")
    assert watermarks.pending_for(st, "clean") == []          # 已消费
    assert [c["id"] for c in watermarks.pending_for(st, "push")] == \
        ["0", "1", "2"]                                       # push 仍待处理

    # oversized：变更超 cap → None（下游回退全量）
    st2 = watermarks.load_state(tmp_path)
    watermarks.record_ingest(st2, ts=200.0, cap=2, changed=[
        {"source": "x", "id": str(i), "status": "updated"} for i in range(3)])
    assert st2["delta"]["oversized"] is True
    assert watermarks.pending_for(st2, "clean") is None

    # 损坏文件 → 全默认（回退全量，不致命）
    (tmp_path / "watermarks.json").write_text("{broken", encoding="utf-8")
    st3 = watermarks.load_state(tmp_path)
    assert st3["delta"]["conversations"] == []
    assert watermarks.pending_for(st3, "clean") is None


# ---------------------------------------------------------------------------
# 模块 D：delta 贯通 clean / push / score --pending / sync
# ---------------------------------------------------------------------------

import argparse  # noqa: E402

from agentmemhub import memos as memos_mod  # noqa: E402
from agentmemhub import scoring as scoring_mod  # noqa: E402
from agentmemhub.memos import build_bundle  # noqa: E402


def test_build_bundle_only_ids_filter():
    s = _db()
    try:
        s.upsert_sessions("zcode", [
            _sess("zcode", "s1", 100.0, ["一问", "二问"]),
            _sess("zcode", "s2", 100.0, ["另一会话"]),
        ])
        full = build_bundle(s, "zcode")
        narrowed = build_bundle(s, "zcode", only_ids={"s1"})
        assert len(full["traces"]) > len(narrowed["traces"]) > 0
    finally:
        s.close()


def test_run_clean_delta_narrowing(tmp_path, monkeypatch):
    monkeypatch.setattr("agentmemhub.config.config", lambda: _Cfg(tmp_path))
    s = _db()
    try:
        s.upsert_sessions("zcode", [
            _sess("zcode", "s1", 100.0, ["正常", "注入甲"], system_flags=[None, True]),
            _sess("zcode", "s2", 100.0, ["正常", "注入乙"], system_flags=[None, True]),
        ])
        lines: list[str] = []
        cli.run_clean(s, apply=True, stdout=lines.append,
                      only=[{"source": "zcode", "id": "s1"}])
        # 只清 s1 的注入事件，s2 保留
        assert len(s.get_events("zcode", "s1")) == 1
        assert len(s.get_events("zcode", "s2")) == 2
        # apply 成功 → clean 消费水位已登记（本测试无 ingest，值为当前 delta 的 0）
        st = watermarks.load_state(tmp_path)
        assert "clean" in st["consumed"]
    finally:
        s.close()


def test_push_to_memos_only_and_pushed_ids(tmp_path, monkeypatch):
    s = _db()
    try:
        s.upsert_sessions("zcode", [
            _sess("zcode", "s1", 100.0, ["一问", "二问"]),
            _sess("zcode", "s2", 100.0, ["另一会话"]),
        ])
        calls: list[dict] = []

        def fake_push(payload, base_url):
            calls.append(payload)
            return {"imported": len(payload["traces"]), "skipped": 0}

        monkeypatch.setattr("agentmemhub.memos.push_bundle", fake_push)
        r = cli.push_to_memos(s, sources=["zcode"], base_url="http://fake",
                              no_rebuild=True, only={"zcode": {"s1"}})
        pushed = [t for p in calls for t in p["traces"]]
        assert len(r["pushed_ids"]) == len(pushed) > 0
        # 只构建了 s1 的 traces（s2 未入包）
        assert r["pushed_ids"] == [t["id"] for t in pushed]
    finally:
        s.close()


def test_score_pending_consumes_queue(tmp_path, monkeypatch):
    monkeypatch.setattr("agentmemhub.config.config", lambda: _Cfg(tmp_path))
    st = watermarks.load_state(tmp_path)
    watermarks.add_pending_score(st, ["trac_a", "trac_b"])
    watermarks.save_state(tmp_path, st)

    seen: dict = {}

    def fake_score(*, emit=None, base_url="", dry_run=False, workers=1,
                   only_ids=None, **kw):
        seen["only_ids"] = only_ids
        seen["dry_run"] = dry_run
        return {"evaluated": len(only_ids or []), "skipped": 0, "positive": 1,
                "neutral": 0, "negative": 0, "errors": 0, "missing": 0,
                "dryRun": dry_run}

    monkeypatch.setattr(scoring_mod, "run_score_all", fake_score)
    args = argparse.Namespace(pending=True, unscored_count=False,
                              sync_episodes=False, ids=None, limit=0,
                              dry_run=False, workers=2, push="")
    cli.cmd_score(args)
    assert seen["only_ids"] == {"trac_a", "trac_b"}
    st2 = watermarks.load_state(tmp_path)
    assert st2["pending_score"] == []            # 成功 → 队列消费清空

    # dry-run：队列保留
    watermarks.add_pending_score(st2, ["trac_c"])
    watermarks.save_state(tmp_path, st2)
    seen.clear()
    args_dry = argparse.Namespace(pending=True, unscored_count=False,
                                  sync_episodes=False, ids=None, limit=0,
                                  dry_run=True, workers=1, push="")
    cli.cmd_score(args_dry)
    assert seen["dry_run"] is True
    st3 = watermarks.load_state(tmp_path)
    assert st3["pending_score"] == ["trac_c"]


def test_run_sync_delta_end_to_end(tmp_path, monkeypatch):
    fake = _FakeAdapter()
    fake.sessions = [
        _sess("fake", "s1", 100.0, ["正常", "注入"], system_flags=[None, True]),
        _sess("fake", "s2", 100.0, ["另一会话"]),
    ]
    dbp = _patch(monkeypatch, tmp_path, fake)
    monkeypatch.setattr("agentmemhub.memos_daemon.auth_state",
                        lambda: {"ok": True})

    calls: list[dict] = []

    def fake_push(payload, base_url):
        calls.append(payload)
        return {"imported": len(payload["traces"]), "skipped": 0}

    monkeypatch.setattr("agentmemhub.memos.push_bundle", fake_push)
    cli.run_sync(source="fake", push="http://fake", no_rebuild=True)
    # 推送了全部 2 个会话的 traces；clean 已清注入事件
    assert len(calls) >= 1
    s = Store(dbp)
    try:
        assert len(s.get_events("fake", "s1")) == 1      # 注入事件已清
    finally:
        s.close()
    st = watermarks.load_state(tmp_path)
    assert st["consumed"]["push"] > 0 and st["consumed"]["clean"] > 0
    assert len(st["pending_score"]) >= 2                 # 推送 trace 全部入队

    # 二轮：无变更 → 跳过推送，不产生新的 push 调用，delta 已消费清空
    n_calls = len(calls)
    cli.run_sync(source="fake", push="http://fake", no_rebuild=True)
    assert len(calls) == n_calls
    st2 = watermarks.load_state(tmp_path)
    assert st2["delta"]["conversations"] == []
    assert st2["pending_score"] == st["pending_score"]   # 队列保留待 score --pending


def test_run_sync_offline_keeps_delta(tmp_path, monkeypatch):
    fake = _FakeAdapter()
    fake.sessions = [_sess("fake", "s1", 100.0, ["a"])]
    _patch(monkeypatch, tmp_path, fake)
    monkeypatch.setattr("agentmemhub.memos_daemon.auth_state", lambda: None)

    cli.run_sync(source="fake", push="http://fake")
    st = watermarks.load_state(tmp_path)
    assert st["delta"]["conversations"]                  # delta 保留待补推
    assert st["consumed"].get("push") in (None, 0)       # 未标记消费
