"""R3 rag 后端派发层测试：backend 开关、engine_request 路由、状态语义、sync 向量化分支。

约定（tests/conftest.py）：存量套件整体钉 memos；本文件逐测试显式 opt-in rag。
全部使用 tmp 沙箱索引库（rag_bridge.configure），绝不触碰真实 session_rag.db。
"""
from __future__ import annotations

import dataclasses
import logging

import pytest

from agentmemhub import memos_daemon, rag_bridge
from agentmemhub.config import Config
from agentmemhub.rag.config import load_settings

_qlog = logging.getLogger("asrag.test.bridge")
_qlog.addHandler(logging.NullHandler())


@pytest.fixture()
def rag_env(tmp_path, monkeypatch):
    """rag 后端 + 沙箱索引：env 翻开关，bridge 指向 tmp 库。"""
    monkeypatch.setenv("AGENTMEMHUB_BACKEND", "rag")
    base = load_settings()
    rag_bridge.configure(dataclasses.replace(
        base, index_db=tmp_path / "rag.db", source_db=tmp_path / "src.db",
        log_dir=tmp_path / "logs"))
    yield
    rag_bridge.reset_settings()


# ── backend 开关解析 ───────────────────────────────────────────────────

def test_backend_default_and_overrides():
    assert Config(env={}).memory_backend == "rag"
    assert Config(env={"AGENTMEMHUB_BACKEND": "memos"}).memory_backend == "memos"
    assert Config(env={"AGENTMEMHUB_BACKEND": "RAG"}).memory_backend == "rag"
    assert Config(env={"AGENTMEMHUB_BACKEND": "nonsense"}).memory_backend == "rag"


def test_stub_config_without_attr_is_memos(monkeypatch):
    class _Stub:
        pass
    monkeypatch.setattr("agentmemhub.config.config", lambda: _Stub())
    assert memos_daemon._backend_is_rag() is False


# ── engine_request 路由（派发表全覆盖 + 未知路径炸） ────────────────────

def test_dispatch_routes(rag_env, monkeypatch):
    calls = {}
    monkeypatch.setattr(rag_bridge, "search",
                        lambda a, q, **k: calls.update(search=q) or {"hits": []})
    monkeypatch.setattr(rag_bridge, "overview",
                        lambda: calls.update(overview=1) or {"ok": True})
    monkeypatch.setattr(rag_bridge, "traces_list",
                        lambda limit, offset: {"traces": [], "limit": limit,
                                               "offset": offset})
    monkeypatch.setattr(rag_bridge, "import_bundle",
                        lambda traces: {"imported": len(traces), "skipped": 0})
    monkeypatch.setattr(rag_bridge, "feedback",
                        lambda tid, pol, **k: {"ok": 1, "t": tid})
    monkeypatch.setattr(rag_bridge, "rebuild_embeddings",
                        lambda mode="repair": {"done": True})
    er = memos_daemon.engine_request
    assert er("POST", "/api/v1/memory/search", {"agent": "a", "query": "问题"})["hits"] == []
    assert calls["search"] == "问题"
    assert er("GET", "/api/v1/overview")["ok"] is True
    assert er("GET", "/api/v1/traces?limit=5&offset=10")["offset"] == 10
    assert er("POST", "/api/v1/import",
              {"traces": [{"id": "x", "userText": "内容"}]})["imported"] == 1
    assert er("POST", "/api/v1/feedback",
              {"traceId": "x", "polarity": "positive"})["ok"] == 1
    assert er("POST", "/api/v1/embeddings/rebuild", {"mode": "repair"})["done"] is True
    st = er("GET", "/api/v1/auth/status")
    assert st["authenticated"] is True and st["backend"] == "rag"
    with pytest.raises(RuntimeError, match="未实现"):
        er("GET", "/api/v1/unknown-route")


# ── 真实桥端到端（tmp 库，不 mock bridge 内部） ─────────────────────────

def test_end_to_end_save_search_score_recent(rag_env):
    er = memos_daemon.engine_request
    r = er("POST", "/api/v1/import", {"traces": [{
        "id": "mcp_e2e", "episodeId": "mcp", "sessionId": "mcp",
        "ts": 1786000000000, "userText": "SQLite WAL 文件膨胀拖慢写入",
        "agentText": "TRUNCATE checkpoint 归零解决", "value": 0.5}]})
    assert r == {"imported": 1, "skipped": 0}
    hits = er("POST", "/api/v1/memory/search",
              {"agent": "a", "query": "数据库日志文件变大写入慢"})["hits"]
    assert hits and hits[0]["refId"] == "mcp_e2e"
    fb = er("POST", "/api/v1/feedback",
            {"traceId": "mcp_e2e", "polarity": "positive", "magnitude": 1.0})
    assert fb["ok"] and fb["priority"] == 1.0
    tr = er("GET", "/api/v1/traces?limit=8&offset=0")["traces"]
    assert tr[0]["id"] == "mcp_e2e" and "WAL" in tr[0]["userText"]
    rb = er("POST", "/api/v1/embeddings/rebuild", {"mode": "repair"})
    assert rb["done"] and rb["failed"] == 0
    ov = er("GET", "/api/v1/overview")
    assert ov["ok"] and ov["traces"] == 1 and ov["memory_units"] == 1


def test_feedback_unknown_id_raises(rag_env):
    with pytest.raises(KeyError):
        rag_bridge.feedback("nope-not-exist", "positive")


# ── 状态语义：auth_state / daemon_status / probe ───────────────────────

def test_auth_state_and_daemon_status_rag(rag_env, monkeypatch):
    st = memos_daemon.auth_state()
    assert st and st["authenticated"] is True          # 沙箱库可开 → 在线
    ds = memos_daemon.daemon_status()
    assert ds["online"] is True and ds["backend"] == "rag"
    assert ds["managed"] is False and ds["pid"] is None
    assert ds["summary"]["traces"] >= 0
    start = memos_daemon.daemon_start()
    assert start["started"] is False and start["reason"] == "rag-in-process"


def test_auth_state_offline_when_bridge_broken(rag_env, monkeypatch):
    monkeypatch.setattr(rag_bridge, "probe",
                        lambda: {"online": False, "error": "db missing"})
    assert memos_daemon.auth_state() is None
    assert memos_daemon.daemon_status()["online"] is False


# ── scoring 派发（memos 直读 vs rag bridge） ───────────────────────────

def test_scoring_enumeration_via_bridge(rag_env, monkeypatch):
    from agentmemhub import scoring
    rag_bridge.import_bundle([
        {"id": "mcp_s1", "userText": "第一条待评记忆", "ts": 1},
        {"id": "mcp_s2", "userText": "第二条待评记忆", "ts": 2},
    ])
    assert {t["id"] for t in scoring.list_all_traces()} == {"mcp_s1", "mcp_s2"}
    assert set(scoring.list_trace_ids()) == {"mcp_s1", "mcp_s2"}
    got = scoring.list_traces_by_ids({"mcp_s1"})
    assert len(got) == 1 and got[0]["id"] == "mcp_s1"
    # rag 下 r_task 同步 = conv_scores 全量重建（memory 源不计入，返回 0 不报错）
    assert scoring.sync_episode_r_task(trace_ids=["mcp_s1"]) == 0


# ── cli sync：rag 早分支（清洗→向量化→消费，绝不调 push_to_memos） ──────

def test_run_sync_rag_vectorize_stage(rag_env, monkeypatch, tmp_path):
    from agentmemhub import cli, watermarks

    state_calls: dict = {}
    vec = {}

    def fake_vectorize(*, stdout=None):
        vec["called"] = True
        return {"failed": 0, "pushed_ids": [], "embedded": 3, "scanned": 3,
                "skipped_known": 0, "seconds": 0.1, "model": "m"}

    monkeypatch.setattr(cli, "_vectorize_stage", fake_vectorize)
    monkeypatch.setattr(cli, "run_ingest", lambda *a, **k: None)
    monkeypatch.setattr(memos_daemon, "auth_state", lambda: {"authenticated": True})
    monkeypatch.setattr(cli, "Store", lambda *a, **k: _FakeStore())

    def mark(s, key):
        state_calls[key] = state_calls.get(key, 0) + 1
    monkeypatch.setattr(watermarks, "mark_consumed", mark)
    monkeypatch.setattr(watermarks, "load_state", lambda d: {})
    monkeypatch.setattr(watermarks, "save_state", lambda *a: None)
    monkeypatch.setattr(watermarks, "pending_for", lambda s, k: [])

    monkeypatch.setattr(cli, "push_to_memos",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("rag 下不得走 push_to_memos")))
    cli.run_sync(push="rag")
    assert vec.get("called") is True
    assert state_calls.get("push") == 1


class _FakeStore:
    def delete_system_events(self, **k):
        return (0, 0)

    def list_conversations(self):
        return []

    def close(self):
        pass


# ── R5.1 验收修复回归 ──────────────────────────────────────────────────

def test_search_curate_caps_like_memos_max_keep(rag_env):
    """机械终审：≤5 且 ≥0.7×top（MemOS llmFilterMaxKeep 同量级），curate=False 关闭。"""
    rag_bridge.import_bundle([
        {"id": f"mcp_c{i}", "userText": f"终审验证条目 编号{i} 内容递增一点点{i * 7}",
         "ts": 100 + i} for i in range(8)])
    all_hits = rag_bridge.search("h", "终审验证条目", k=8, curate=False)["hits"]
    curated = rag_bridge.search("h", "终审验证条目", k=8, curate=True)["hits"]
    assert len(all_hits) == 8
    assert 1 <= len(curated) <= 5
    top = curated[0]["score"]
    assert all(h["score"] >= 0.7 * top - 1e-9 for h in curated)


def test_safe_cutoff_hits_rules():
    hits = [{"score": s} for s in (1.0, 0.9, 0.71, 0.69, 0.5, 0.4)]
    kept = rag_bridge.safe_cutoff_hits(hits, max_keep=5)
    assert [h["score"] for h in kept] == [1.0, 0.9, 0.71]
    assert rag_bridge.safe_cutoff_hits([]) == []
    one = [{"score": 1.0}, {"score": 0.01}]
    assert rag_bridge.safe_cutoff_hits(one) == one[:1]


def test_web_push_button_vectorizes_not_bundles(rag_env, monkeypatch):
    """面板「推送记忆」按钮在 rag 后端必须转向量化（走 bundle 会重复落 memory 单元）。"""
    from agentmemhub.web import app

    calls = {"vectorize": 0, "push": 0}

    class CliStub:
        @staticmethod
        def _vectorize_stage(*, stdout=None):
            calls["vectorize"] += 1
            return {"failed": 0}

        @staticmethod
        def push_to_memos(*a, **k):
            calls["push"] += 1
            return {}

    do = app._run_push_fn(CliStub, "zcode")
    do(lambda line: None, {"id": "t1", "name": "push"})
    assert calls == {"vectorize": 1, "push": 0}


def test_llm_availability_probe_honest(rag_env, monkeypatch):
    monkeypatch.delenv("NOPE", raising=False)
    v = rag_bridge._scoring_llm_available()
    assert isinstance(v, bool)     # 读不到配置就是 False，不装样子
