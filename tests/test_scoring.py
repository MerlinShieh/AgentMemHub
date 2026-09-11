"""批量自动评分（scoring）单元测试：verdict 解析 / LLM 调用 / 批量循环 / 面板端点。"""
from __future__ import annotations

import json
import tempfile
import time
import urllib.error
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


def test_neutral_marked_scored_not_reevaluated(tmp_path, monkeypatch):
    """回归：neutral 判「不写 value」但仍要记入跳过清单——否则每次评分重评这批
    （用户症状：跑完再评还是同样 300 多条）。dry-run 例外，不记录。"""
    scoring = _empty_cache(tmp_path, monkeypatch)
    llm = {"endpoint": "https://x", "api_key": "k", "model": "m"}
    traces = [{"id": "n1", "userText": "寒暄", "agentText": "嗯"},
              {"id": "n2", "userText": "无结论", "agentText": "再说"}]
    with mock.patch("agentmemhub.scoring.read_engine_llm", return_value=llm), \
         mock.patch("agentmemhub.scoring.evaluate_trace", return_value="neutral"), \
         mock.patch("agentmemhub.memos_daemon.engine_request") as er:
        r = run_score_all(traces=traces)
    assert r["neutral"] == 2 and r["errors"] == 0
    # neutral 不写 feedback
    assert not [c for c in er.call_args_list if c[0][0] == "POST"]
    # 但已记入跳过清单 → 二次跑直接 skip、不再评估
    assert scoring._load_scored() == {"n1", "n2"}
    lines2: list[str] = []
    r2 = run_score_all(emit=lines2.append, traces=traces)
    assert r2["evaluated"] == 2 and r2["neutral"] == 0 and r2["skipped"] == 2
    assert all("已评过" in l for l in lines2)

    # dry-run：neutral 不记入跳过清单
    scoring.clear_scored()
    with mock.patch("agentmemhub.scoring.read_engine_llm", return_value=llm), \
         mock.patch("agentmemhub.scoring.evaluate_trace", return_value="neutral"), \
         mock.patch("agentmemhub.memos_daemon.engine_request"):
        run_score_all(traces=traces, dry_run=True)
    assert scoring._load_scored() == set()


import io
from email.message import Message as _Msg


def _http_error(code, body: str, url="https://x", reason="Bad Request"):
    return urllib.error.HTTPError(url, code, reason, _Msg(), io.BytesIO(body.encode()))


def test_scrub_text_strips_invisible_control():
    from agentmemhub import scoring
    s = scoring._scrub_text("零宽​甲\n正常\t制表\rother\x00")
    assert "​" not in s
    assert "\n" in s and "\t" in s         # 换行/制表保留
    assert "其他" in s or "other" in s


def test_is_content_filter_detects_bigmodel_1301():
    from agentmemhub import scoring
    assert scoring._is_content_filter(400, '{"error":{"code":"1301","message":"敏感内容"}}')
    assert scoring._is_content_filter(400, '{"contentFilter":[{"level":0}]}')
    assert not scoring._is_content_filter(400, '{"error":{"code":"1102","message":"余额不足"}}')
    assert not scoring._is_content_filter(401, '{"error":"unauthorized"}')
    assert not scoring._is_content_filter(500, "boom")


def test_evaluate_trace_content_filter_raises_typed():
    from agentmemhub import scoring
    body = '{"error":{"code":"1301","message":"系统检测到输入或生成内容可能包含不安全或敏感内容"}}'
    with mock.patch("agentmemhub.scoring._llm_opener",
                    side_effect=lambda: _FakeOpener(
                        lambda req, timeout: (_ for _ in ()).throw(_http_error(400, body)))), \
         mock.patch("agentmemhub.scoring.read_engine_llm",
                    return_value={"endpoint": "https://x", "api_key": "k", "model": "m"}):
        import pytest as _pt
        with _pt.raises(scoring.ContentFilterRejected):
            scoring.evaluate_trace({"userText": "x", "agentText": "y"},
                                   {"endpoint": "https://x", "api_key": "k", "model": "m"})


def test_evaluate_trace_other_400_surfaces_body():
    from agentmemhub import scoring
    body = '{"error":{"code":"1102","message":"欠费"}}'
    with mock.patch("agentmemhub.scoring._llm_opener",
                    side_effect=lambda: _FakeOpener(
                        lambda req, timeout: (_ for _ in ()).throw(_http_error(400, body)))), \
         mock.patch("agentmemhub.scoring.read_engine_llm",
                    return_value={"endpoint": "https://x", "api_key": "k", "model": "m"}):
        import pytest as _pt
        with _pt.raises(RuntimeError) as ei:
            scoring.evaluate_trace({"userText": "x", "agentText": "y"},
                                   {"endpoint": "https://x", "api_key": "k", "model": "m"})
    assert "1102" in str(ei.value) or "欠费" in str(ei.value)


def test_content_filter_neutral_marked_not_error(tmp_path, monkeypatch):
    """内容审核拒评 → 归 neutral + 记跳过清单（非 error）；二次跑直接跳过。"""
    from agentmemhub import scoring
    _empty_cache(tmp_path, monkeypatch)
    llm = {"endpoint": "https://x", "api_key": "k", "model": "m"}
    traces = [{"id": "blocked", "userText": "测试 API", "agentText": "敏感"}]

    def blocked(*a, **k):
        raise scoring.ContentFilterRejected("1301")
    with mock.patch("agentmemhub.scoring.read_engine_llm", return_value=llm), \
         mock.patch("agentmemhub.scoring.evaluate_trace", side_effect=blocked), \
         mock.patch("agentmemhub.memos_daemon.engine_request") as er:
        r = scoring.run_score_all(traces=traces)
    assert r["neutral"] == 1 and r["errors"] == 0
    assert not [c for c in er.call_args_list if c[0][0] == "POST"]   # 不写 value
    assert scoring._load_scored() == {"blocked"}                     # 记入跳过清单


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
         mock.patch("agentmemhub.scoring.sync_episode_r_task", return_value=0), \
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


class _FakeOpener:
    """替换 _llm_opener 的假 opener：记录请求并回放 LLM 响应。"""
    calls: list = []

    def __init__(self, respond):
        self._respond = respond

    def open(self, req, timeout=None):
        _FakeOpener.calls.append(req)
        return self._respond(req, timeout)


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
    _FakeOpener.calls = []
    with mock.patch("agentmemhub.scoring._llm_opener",
                    return_value=_FakeOpener(fake_urlopen)):
        assert evaluate_trace(trace, llm) == "positive"
    # 请求端点 = 引擎配置的 LLM endpoint
    assert _FakeOpener.calls[0].full_url == llm["endpoint"]


def test_evaluate_trace_empty_returns_neutral():
    with mock.patch("agentmemhub.scoring._llm_opener",
                    side_effect=AssertionError("不应调用")):
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
         mock.patch("agentmemhub.scoring.sync_episode_r_task", return_value=0), \
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
         mock.patch("agentmemhub.scoring.list_trace_ids", return_value=["t1", "t2"]), \
         mock.patch("agentmemhub.scoring.list_traces_by_ids", return_value=traces), \
         mock.patch("agentmemhub.memos_daemon.engine_request") as er:
        er.side_effect = []
        r = run_score_all(limit=1, dry_run=True)
    assert r["evaluated"] == 1 and r["dryRun"] is True
    # dry-run 不产生 feedback POST
    assert not [c for c in er.call_args_list if c[0][0] == "POST"]


def test_run_score_all_full_reads_only_unscored():
    """全量模式：先廉价筛 id、减去已评，再只定点读未评正文——不触发 list_all_traces。"""
    llm = {"endpoint": "https://x", "api_key": "k", "model": "m"}
    with mock.patch("agentmemhub.scoring.read_engine_llm", return_value=llm), \
         mock.patch("agentmemhub.scoring.evaluate_trace", return_value="positive"), \
         mock.patch("agentmemhub.scoring.mark_scored"), \
         mock.patch("agentmemhub.scoring.sync_episode_r_task", return_value=0), \
         mock.patch("agentmemhub.scoring.list_all_traces",
                    side_effect=AssertionError("全量模式不应调用 list_all_traces 全表枚举")), \
         mock.patch("agentmemhub.scoring.list_trace_ids",
                    return_value=["t1", "t2", "t3"]), \
         mock.patch("agentmemhub.scoring.list_traces_by_ids") as by_ids, \
         mock.patch("agentmemhub.memos_daemon.engine_request") as er:
        er.return_value = {"id": "fb"}
        by_ids.return_value = [{"id": "t2", "userText": "c", "agentText": "d"},
                               {"id": "t3", "userText": "e", "agentText": "f"}]
        # t1 视为已评（monkeypatch _load_scored 返回含 t1）
        with mock.patch("agentmemhub.scoring._load_scored", return_value={"t1"}):
            r = run_score_all()
    # 只定点读未评的 t2/t3（t1 在 id 层就被筛掉，不读其正文）
    read_ids = by_ids.call_args[0][0]
    assert read_ids == {"t2", "t3"}
    assert r["evaluated"] == 2 and r["skipped"] == 1
    assert r["errors"] == 0


def test_run_score_all_only_ids_targeted_no_full_scan():
    """score --ids/--pending 语义：只定点读队列 id，list_all_traces 不被调用。"""
    llm = {"endpoint": "https://x", "api_key": "k", "model": "m"}
    with mock.patch("agentmemhub.scoring.read_engine_llm", return_value=llm), \
         mock.patch("agentmemhub.scoring.evaluate_trace", return_value="positive"), \
         mock.patch("agentmemhub.scoring.mark_scored"), \
         mock.patch("agentmemhub.scoring.sync_episode_r_task", return_value=0), \
         mock.patch("agentmemhub.scoring.list_all_traces",
                    side_effect=AssertionError("定点路径不应全表枚举")), \
         mock.patch("agentmemhub.scoring.list_traces_by_ids",
                    return_value=[{"id": "t1", "userText": "a", "agentText": "b"}]) as by_ids, \
         mock.patch("agentmemhub.memos_daemon.engine_request") as er:
        er.return_value = {"id": "fb"}
        r = run_score_all(only_ids={"t1", "ghost"})
    by_ids.assert_called_once_with({"t1", "ghost"})
    assert r["evaluated"] == 1 and r["missing"] == 1     # ghost 库中无 → 计入 missing


def test_list_traces_by_ids_subset_and_missing(tmp_path):
    """定点读：命中子集按 ts 升序返回；不存在的 id 静默缺失（由调用方 diff missing）。"""
    import sqlite3
    from agentmemhub import scoring
    db = tmp_path / "memos.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE traces (id TEXT PRIMARY KEY, user_text TEXT, "
                 "agent_text TEXT, ts INTEGER)")
    conn.executemany("INSERT INTO traces VALUES (?,?,?,?)",
                     [("a", "A用", "A答", 1), ("b", "B用", "B答", 2),
                      ("c", "C用", "C答", 3)])
    conn.commit()
    conn.close()
    with mock.patch("agentmemhub.scoring._engine_db_path", return_value=db):
        rows = scoring.list_traces_by_ids({"c", "a", "nope"})
        assert [r["id"] for r in rows] == ["a", "c"]      # ts 升序，nope 缺失
        assert rows[0]["userText"] == "A用"
        assert scoring.list_trace_ids() == ["a", "b", "c"]


def _incr_cfg(tmp_path, monkeypatch):
    """run_score_incremental 的 config.config().data_dir 指向测试临时目录。"""
    from agentmemhub import config as cfg_mod

    class _C:
        data_dir = tmp_path
    monkeypatch.setattr(cfg_mod, "config", lambda: _C())


def test_run_score_incremental_queue_consumed_on_success(tmp_path, monkeypatch):
    """队列非空 → 只评队列（定点）；无失败 → 已处理 id 出队。"""
    from agentmemhub import scoring, watermarks
    _incr_cfg(tmp_path, monkeypatch)
    st = watermarks.load_state(tmp_path)
    watermarks.add_pending_score(st, ["t1", "t2"])
    watermarks.save_state(tmp_path, st)
    seen: dict = {}

    def fake_all(*, emit=None, only_ids=None, **kw):
        seen["only_ids"] = only_ids
        return {"evaluated": len(only_ids), "skipped": 0, "positive": 1,
                "neutral": 0, "negative": 0, "errors": 0, "missing": 0,
                "dryRun": False}
    monkeypatch.setattr(scoring, "run_score_all", fake_all)
    r = scoring.run_score_incremental()
    assert r["mode"] == "pending"
    assert seen["only_ids"] == {"t1", "t2"}
    assert watermarks.load_state(tmp_path)["pending_score"] == []   # 全部出队


def test_run_score_incremental_failure_keeps_queue(tmp_path, monkeypatch):
    """本轮有失败 → 队列原样保留下次重试（已成功条由已评清单保护，重跑不双评）。"""
    from agentmemhub import scoring, watermarks
    _incr_cfg(tmp_path, monkeypatch)
    st = watermarks.load_state(tmp_path)
    watermarks.add_pending_score(st, ["t1", "t2", "t3"])
    watermarks.save_state(tmp_path, st)
    monkeypatch.setattr(scoring, "run_score_all", lambda **k: {
        "evaluated": 3, "skipped": 0, "positive": 2, "neutral": 0,
        "negative": 0, "errors": 1, "missing": 0, "dryRun": False})
    r = scoring.run_score_incremental()
    assert r["mode"] == "pending" and r["errors"] == 1
    assert watermarks.load_state(tmp_path)["pending_score"] == ["t1", "t2", "t3"]


def test_run_score_incremental_limit_partial(tmp_path, monkeypatch):
    """limit 为队列消费上限：取前 N 条评，其余留在队列。"""
    from agentmemhub import scoring, watermarks
    _incr_cfg(tmp_path, monkeypatch)
    st = watermarks.load_state(tmp_path)
    watermarks.add_pending_score(st, ["t1", "t2", "t3", "t4"])
    watermarks.save_state(tmp_path, st)
    seen: dict = {}

    def fake_all(*, emit=None, only_ids=None, **kw):
        seen["only_ids"] = only_ids
        return {"evaluated": len(only_ids), "skipped": 0, "positive": 0,
                "neutral": 0, "negative": 0, "errors": 0, "missing": 0,
                "dryRun": False}
    monkeypatch.setattr(scoring, "run_score_all", fake_all)
    scoring.run_score_incremental(limit=2)
    assert seen["only_ids"] == {"t1", "t2"}
    assert watermarks.load_state(tmp_path)["pending_score"] == ["t3", "t4"]


def test_run_score_incremental_empty_queue_falls_back_full(tmp_path, monkeypatch):
    """队列为空 → 回退全量扫描未评（mode=full）。"""
    from agentmemhub import scoring
    _incr_cfg(tmp_path, monkeypatch)
    called: dict = {}

    def fake_all(**kw):
        called["only_ids"] = kw.get("only_ids")
        return {"evaluated": 0, "skipped": 0, "positive": 0, "neutral": 0,
                "negative": 0, "errors": 0, "missing": 0, "dryRun": False}
    monkeypatch.setattr(scoring, "run_score_all", fake_all)
    r = scoring.run_score_incremental()
    assert r["mode"] == "full"
    assert called["only_ids"] is None      # 全量模式不带定点过滤


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
         mock.patch("agentmemhub.scoring.sync_episode_r_task", return_value=0), \
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
    assert "记忆索引不可用" in r.json()["detail"]


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

# ---------------------------------------------------------------------------
# LLM 强制直连（不受系统代理/环境变量影响）
# ---------------------------------------------------------------------------

def test_llm_opener_direct_by_default(monkeypatch):
    """默认强制直连：opener 中不注册任何 ProxyHandler。

    机制说明：build_opener 传入 ProxyHandler 实例会跳过默认「读系统代理」的
    ProxyHandler；空代理表的实例没有任何 xxx_open 方法，根本不会被注册
    （实测 handlers 无 ProxyHandler）→ 请求不经过任何代理 = 直连。
    """
    import urllib.request
    from agentmemhub import scoring
    monkeypatch.delenv("AGENTMEMHUB_LLM_PROXY", raising=False)

    def _proxy_handlers(op):
        return [h for h in op.handlers
                if isinstance(h, urllib.request.ProxyHandler)]

    assert _proxy_handlers(scoring._llm_opener()) == []
    # 显式指定才走代理（私有部署逃生口）
    monkeypatch.setenv("AGENTMEMHUB_LLM_PROXY", "http://127.0.0.1:7897")
    phs = _proxy_handlers(scoring._llm_opener())
    assert len(phs) == 1 and phs[0].proxies == {
        "http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"}
