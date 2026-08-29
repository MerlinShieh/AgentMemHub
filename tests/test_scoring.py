"""批量自动评分（scoring）单元测试：verdict 解析 / LLM 调用 / 批量循环 / 面板端点。"""
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from agentmemhub.web import tasks
from agentmemhub.scoring import _parse_verdict, evaluate_trace, run_score_all
from agentmemhub.models import Event, renumber
from agentmemhub.store import Store


def test_parse_verdict_variants():
    assert _parse_verdict('{"verdict": "positive", "reason": "x"}') == "positive"
    assert _parse_verdict('{"verdict":"negative"}') == "negative"
    assert _parse_verdict("verdict 是 neutral，一般") == "neutral"
    assert _parse_verdict("positive 好") == "positive"
    assert _parse_verdict("看不懂") == "neutral"     # 解析失败兜底


def test_evaluate_trace_calls_llm_endpoint():
    llm = {"endpoint": "https://example.com/v1/chat/completions",
           "api_key": "k-123", "model": "glm-x"}
    trace = {"id": "t1", "userText": "帮我修登录", "agentText": "已修复监听器"}
    class R:
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False
        def read(self):
            return json.dumps({"choices": [{"message": {"content":
                '{"verdict":"positive","reason":"目标达成"}'}}]}).encode()
    def fake_urlopen(req, timeout):
        assert req.get_method() == "POST"
        body = json.loads(req.data.decode("utf-8"))
        assert body["model"] == "glm-x"
        assert body["messages"][0]["role"] == "system"
        assert "记忆质量评估器" in body["messages"][0]["content"]
        assert "修登录" in body["messages"][1]["content"]
        assert "k-123" in req.headers["Authorization"]
        return R()
    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        assert evaluate_trace(trace, llm) == "positive"


def test_evaluate_trace_empty_returns_neutral():
    with mock.patch("urllib.request.urlopen", side_effect=AssertionError("不应调用")):
        assert evaluate_trace({"id": "t", "userText": "", "agentText": ""},
                              {"endpoint": "x", "api_key": "k", "model": "m"}) == "neutral"


def test_run_score_all_batch_loop():
    """批量循环：分页取 traces → LLM 评估 → feedback 写入；neutral 跳过写入；limit 生效。"""
    traces = [
        {"id": "t1", "userText": "修复登录", "agentText": "监听器问题"},
        {"id": "t2", "userText": "无聊寒暄", "agentText": "嗯嗯"},
        {"id": "t3", "userText": "部署踩坑", "agentText": "权限问题"},
    ]
    llm = {"endpoint": "https://x/chat/completions", "api_key": "k", "model": "m"}
    lines: list[str] = []
    verdicts = iter(["positive", "neutral", "negative"])
    page_calls = 0
    def fake_er(method, path, *a, **k):
        nonlocal page_calls
        if path.startswith("/api/v1/traces"):
            page_calls += 1
            return {"total": 3, "traces": traces} if page_calls == 1 else {"total": 3, "traces": []}
        if path == "/api/v1/feedback":
            return {"id": "fb"}
        raise AssertionError(f"unexpected call: {method} {path}")
    with mock.patch("agentmemhub.scoring.read_engine_llm", return_value=llm), \
         mock.patch("agentmemhub.scoring.evaluate_trace",
                    side_effect=lambda t, c: next(verdicts)), \
         mock.patch("agentmemhub.memos_daemon.engine_request", side_effect=fake_er) as er:
        r = run_score_all(emit=lines.append, base_url="http://127.0.0.1:1")
    assert r["evaluated"] == 3 and r["positive"] == 1 and r["neutral"] == 1
    assert r["negative"] == 1 and r["errors"] == 0 and r["dryRun"] is False
    # feedback 只写了 positive 与 negative（neutral 跳过）
    fb_calls = [c for c in er.call_args_list
                if c[0][0] == "POST" and c[0][1] == "/api/v1/feedback"]
    assert len(fb_calls) == 2
    assert fb_calls[0].kwargs["body"]["polarity"] == "positive"
    assert fb_calls[0].kwargs["body"]["traceId"] == "t1"
    assert fb_calls[1].kwargs["body"]["polarity"] == "negative"
    assert fb_calls[1].kwargs["body"]["traceId"] == "t3"
    # 实时进度行已产生
    assert any("评估 t1" in l for l in lines)


def test_run_score_all_dry_run_skips_write():
    llm = {"endpoint": "https://x", "api_key": "k", "model": "m"}
    with mock.patch("agentmemhub.scoring.read_engine_llm", return_value=llm), \
         mock.patch("agentmemhub.scoring.evaluate_trace", return_value="positive"), \
         mock.patch("agentmemhub.memos_daemon.engine_request") as er:
        er.side_effect = [{"total": 2, "traces": [{"id": "t1", "userText": "a", "agentText": "b"}]},
                          {"total": 2, "traces": []}]
        r = run_score_all(limit=1, dry_run=True)
    assert r["evaluated"] == 1 and r["dryRun"] is True
    # dry-run 不产生 feedback POST
    assert not [c for c in er.call_args_list if c[0][0] == "POST"]


# ---------------------------------------------------------------------------
# 面板端点
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_tasks():
    tasks.reset()
    yield
    tasks.reset()


def _client() -> TestClient:
    tmp = Path(tempfile.mkdtemp()) / "score.db"
    s = Store(tmp)
    evs = renumber([Event(role="user", content="hi", time=1)])
    s.replace_source("zcode", [{
        "source": "zcode", "id": "s1", "title": "t", "cwd": "w",
        "created_at": 1, "updated_at": 1, "model": "m", "meta": {},
        "events": evs,
    }], signature="t")
    s.close()
    from agentmemhub.web.app import create_app
    return TestClient(create_app(tmp))


def _wait_done(client: TestClient, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        j = client.get("/api/admin/job").json()["job"]
        if j and j["status"] != "running":
            return j
        time.sleep(0.05)
    raise AssertionError("job 未在超时内完成")


def test_run_score_all_concurrent_workers():
    """并发：4 worker 评估 4 条全部计入；进度行带 [N/总数]。"""
    llm = {"endpoint": "https://x", "api_key": "k", "model": "m"}
    traces = [{"id": f"t{i}", "userText": f"内容{i}", "agentText": "回复"} for i in range(4)]
    lines: list[str] = []
    def fake_er(method, path, *a, **k):
        if path.startswith("/api/v1/traces"):
            return {"total": 4, "traces": traces}
        if path == "/api/v1/feedback":
            return {"id": "fb"}
        raise AssertionError(f"unexpected: {method} {path}")
    with mock.patch("agentmemhub.scoring.read_engine_llm", return_value=llm), \
         mock.patch("agentmemhub.scoring.evaluate_trace", return_value="positive"), \
         mock.patch("agentmemhub.memos_daemon.engine_request", side_effect=fake_er) as er:
        r = run_score_all(emit=lines.append, base_url="http://127.0.0.1:1", workers=4)
    assert r["evaluated"] == 4 and r["positive"] == 4 and r["errors"] == 0
    # 进度行带 [N/总数]（并发下总数正确）
    assert any("[1/4]" in l for l in lines) and any("[4/4]" in l for l in lines)
    fb = [c for c in er.call_args_list if c[0][1] == "/api/v1/feedback"]
    assert len(fb) == 4


def test_admin_score_offline_503():
    c = _client()
    with mock.patch("agentmemhub.memos_daemon.auth_state", return_value=None):
        r = c.post("/api/admin/score")
    assert r.status_code == 503
    assert "记忆引擎未运行" in r.json()["detail"]


def test_admin_score_job_runs():
    c = _client()
    with mock.patch("agentmemhub.memos_daemon.auth_state", return_value={}), \
         mock.patch("agentmemhub.scoring.run_score_all", return_value={
             "evaluated": 2, "positive": 1, "neutral": 1, "negative": 0,
             "errors": 0, "dryRun": False}):
        r = c.post("/api/admin/score?limit=2")
        assert r.status_code == 200
        done = _wait_done(c)
        assert done["status"] == "done"
        assert "评分完成" in done["output"] and "positive=1" in done["output"]