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


def _empty_cache(tmp_path, monkeypatch):
    """隔离已评清单到临时目录（防真实 data_dir 污染 + 互相干扰）。"""
    monkeypatch.setattr("agentmemhub.scoring._cache_path",
                        lambda: tmp_path / "scored_traces.json")
    from agentmemhub import scoring
    scoring.clear_scored()
    return scoring


@pytest.fixture(autouse=True)
def _isolate_scored(tmp_path, monkeypatch):
    _empty_cache(tmp_path, monkeypatch)


def test_scored_cache_persist(tmp_path, monkeypatch):
    """已评清单：mark/load/清空均持久化到文件。"""
    scoring = _empty_cache(tmp_path, monkeypatch)
    assert scoring._load_scored() == set()
    scoring.mark_scored("t1")
    scoring.mark_scored("t2")
    scoring.mark_scored("t1")               # 已存在不重复
    assert scoring._load_scored() == {"t1", "t2"}
    assert (tmp_path / "scored_traces.json").exists()
    scoring.clear_scored()
    assert scoring._load_scored() == set()


def test_run_score_all_skips_scored(tmp_path, monkeypatch):
    """默认跳过已评清单（含手动 👍/👎 的），写入成功后才 mark。"""
    scoring = _empty_cache(tmp_path, monkeypatch)
    scoring.mark_scored("t1")               # 模拟手动已评
    llm = {"endpoint": "https://x", "api_key": "k", "model": "m"}
    traces = [{"id": "t1", "userText": "a", "agentText": "b"},
              {"id": "t2", "userText": "c", "agentText": "d"}]
    lines: list[str] = []
    def fake_er(method, path, *a, **k):
        assert (method, path) == ("POST", "/api/v1/feedback")
        return {"id": "fb"}
    with mock.patch("agentmemhub.scoring.read_engine_llm", return_value=llm), \
         mock.patch("agentmemhub.scoring.evaluate_trace", return_value="positive"), \
         mock.patch("agentmemhub.memos_daemon.engine_request", side_effect=fake_er) as er:
        r = run_score_all(emit=lines.append, base_url="http://127.0.0.1:1",
                          workers=2, traces=traces)
    assert r["evaluated"] == 2 and r["skipped"] == 1      # t1 跳过，t2 评估
    assert r["positive"] == 1
    assert any("已评过，跳过" in l for l in lines)
    assert "t2" in scoring._load_scored()                 # 写入成功 → 记入清单
    assert "t1" in scoring._load_scored()


def test_run_score_all_dry_run_not_mark(tmp_path, monkeypatch):
    """dry-run 不写入也不记入已评清单。"""
    scoring = _empty_cache(tmp_path, monkeypatch)
    llm = {"endpoint": "https://x", "api_key": "k", "model": "m"}
    with mock.patch("agentmemhub.scoring.read_engine_llm", return_value=llm), \
         mock.patch("agentmemhub.scoring.evaluate_trace", return_value="positive"), \
         mock.patch("agentmemhub.memos_daemon.engine_request") as er:
        er.return_value = {"id": "fb"}                    # 仅 feedback 写入分支
        r = run_score_all(limit=1, dry_run=True,
                          traces=[{"id": "t1", "userText": "a", "agentText": "b"}])
    assert r["dryRun"] is True and r["positive"] == 1
    assert scoring._load_scored() == set()                # 未写入 → 未标记


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
    """批量：注入 traces → LLM 评估 → feedback 写入；neutral 跳过写入；limit 生效。"""
    traces = [
        {"id": "t1", "userText": "修复登录", "agentText": "监听器问题"},
        {"id": "t2", "userText": "无聊寒暄", "agentText": "嗯嗯"},
        {"id": "t3", "userText": "部署踩坑", "agentText": "权限问题"},
    ]
    llm = {"endpoint": "https://x/chat/completions", "api_key": "k", "model": "m"}
    lines: list[str] = []
    verdicts = iter(["positive", "neutral", "negative"])
    def fake_er(method, path, *a, **k):
        assert (method, path) == ("POST", "/api/v1/feedback")
        return {"id": "fb"}
    with mock.patch("agentmemhub.scoring.read_engine_llm", return_value=llm), \
         mock.patch("agentmemhub.scoring.evaluate_trace",
                    side_effect=lambda t, c: next(verdicts)), \
         mock.patch("agentmemhub.memos_daemon.engine_request", side_effect=fake_er) as er:
        r = run_score_all(emit=lines.append, base_url="http://127.0.0.1:1",
                          traces=traces)
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
    traces = [{"id": "t1", "userText": "a", "agentText": "b"},
              {"id": "t2", "userText": "c", "agentText": "d"}]
    with mock.patch("agentmemhub.scoring.read_engine_llm", return_value=llm), \
         mock.patch("agentmemhub.scoring.evaluate_trace", return_value="positive"), \
         mock.patch("agentmemhub.scoring.list_all_traces", return_value=traces), \
         mock.patch("agentmemhub.memos_daemon.engine_request") as er:
        er.side_effect = []
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
        assert (method, path) == ("POST", "/api/v1/feedback")
        return {"id": "fb"}
    with mock.patch("agentmemhub.scoring.read_engine_llm", return_value=llm), \
         mock.patch("agentmemhub.scoring.evaluate_trace", return_value="positive"), \
         mock.patch("agentmemhub.memos_daemon.engine_request", side_effect=fake_er) as er:
        r = run_score_all(emit=lines.append, base_url="http://127.0.0.1:1",
                          workers=4, traces=traces)
    assert r["evaluated"] == 4 and r["positive"] == 4 and r["errors"] == 0
    # 进度行带 [N/总数]（并发下总数正确）
    assert any("[1/4]" in l for l in lines) and any("[4/4]" in l for l in lines)
    fb = [c for c in er.call_args_list if c[0][1] == "/api/v1/feedback"]
    assert len(fb) == 4


def test_lists_all_traces_from_engine_db(tmp_path):
    """只读枚举全部 trace（绕开 listTraces 500 窗口）——列名/内容正确。"""
    import sqlite3
    from agentmemhub import scoring
    db = tmp_path / "memos.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE traces (id TEXT PRIMARY KEY, user_text TEXT, "
                 "agent_text TEXT, ts INTEGER)")
    conn.executemany("INSERT INTO traces VALUES (?,?,?,?)",
                     [("t1", "你好", "回复", 1), ("t2", None, "只有助手", 2)])
    conn.commit()
    conn.close()
    with mock.patch("agentmemhub.scoring._engine_db_path", return_value=db):
        rows = scoring.list_all_traces()
    assert len(rows) == 2
    assert rows[0] == {"id": "t1", "userText": "你好", "agentText": "回复"}
    assert rows[1] == {"id": "t2", "userText": "", "agentText": "只有助手"}   # None → 空串


def test_admin_score_offline_503():
    c = _client()
    with mock.patch("agentmemhub.memos_daemon.auth_state", return_value=None):
        r = c.post("/api/admin/score")
    assert r.status_code == 503
    assert "记忆引擎未运行" in r.json()["detail"]


def test_admin_score_job_runs():
    c = _client()
    with mock.patch("agentmemhub.memos_daemon.auth_state", return_value={}), \
         mock.patch("agentmemhub.scoring.run_score_all") as rsa:
        def fake_run(*a, **k):
            prog = k.get("on_progress")    # 面板进度条走结构化 progress
            assert prog is not None and callable(prog)
            prog(1, 2115)
            prog(500, 2115)
            print("评分完成: evaluated=500 skipped=0 positive=2 neutral=100 negative=0 errors=0")
            return {"evaluated": 500, "skipped": 0, "positive": 2, "neutral": 100,
                    "negative": 0, "errors": 0, "dryRun": False}
        rsa.side_effect = fake_run
        r = c.post("/api/admin/score")
        assert r.status_code == 200
        done = _wait_done(c)
        assert done["status"] == "done"
        assert "评分完成" in done["output"]
        # 结构进度写入 job（面板进度条依据）
        assert done["progress"] is not None and done["progress"]["pct"] == 100
        assert done["progress"]["total"] == 500