"""看板数据操作后台任务（/api/admin/*）测试：job 生命周期/并发/离线。"""
from __future__ import annotations

import tempfile
import time
from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from agentmemhub.web import tasks
from agentmemhub.models import Event, renumber
from agentmemhub.store import Store


@pytest.fixture(autouse=True)
def _reset_tasks():
    tasks.reset()
    yield
    tasks.reset()


def _client() -> TestClient:
    tmp = Path(tempfile.mkdtemp()) / "admin.db"
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


def test_admin_job_empty():
    c = _client()
    assert c.get("/api/admin/job").json()["job"] is None


def test_admin_ingest_job_lifecycle():
    c = _client()
    with mock.patch("agentmemhub.cli.run_ingest") as ri, \
         mock.patch("agentmemhub.adapters.all_adapters", return_value=[]):
        r = c.post("/api/admin/ingest")
        assert r.status_code == 200
        job = r.json()["job"]
        assert job["id"] and job["name"]          # 任务已提交（mock 极快，可能已 done）
        done = _wait_done(c)
        assert done["status"] == "done" and done["id"] == job["id"]
        ri.assert_called_once()


def test_admin_ingest_source_param_passthrough():
    """?source=zcode → run_ingest 只处理该 source（agent 筛选生效）。"""
    c = _client()
    with mock.patch("agentmemhub.cli.run_ingest") as ri:
        r = c.post("/api/admin/ingest?source=zcode")
        assert r.status_code == 200
        _wait_done(c)
        args, kwargs = ri.call_args
        assert args[0] == ["zcode"] and kwargs == {"signature": ""}


def test_admin_ingest_busy_conflict_409():
    c = _client()
    def slow(*a, **k):
        time.sleep(0.4)
    with mock.patch("agentmemhub.cli.run_ingest", side_effect=slow), \
         mock.patch("agentmemhub.adapters.all_adapters", return_value=[]):
        assert c.post("/api/admin/ingest").status_code == 200
        r2 = c.post("/api/admin/ingest")
        assert r2.status_code == 409           # 串行保护：并发提交被拒
        _wait_done(c)


def test_admin_push_offline_503():
    c = _client()
    with mock.patch("agentmemhub.memos_daemon.auth_state", return_value=None):
        r = c.post("/api/admin/push")
    assert r.status_code == 503
    assert "记忆引擎未运行" in r.json()["detail"]
    assert c.get("/api/admin/job").json()["job"] is None     # 未提交任务


def test_admin_push_online_job():
    c = _client()
    fake = {"imported": 3, "skipped": 5,
            "lines": ["[zcode] 推送 ok: imported=3 skipped=5"],
            "rebuilt": {"done": True}}
    with mock.patch("agentmemhub.memos_daemon.auth_state", return_value={}), \
         mock.patch("agentmemhub.cli.push_to_memos", return_value=fake), \
         mock.patch("agentmemhub.adapters.all_adapters", return_value=[]):
        r = c.post("/api/admin/push")
        assert r.status_code == 200
        done = _wait_done(c)
        assert done["status"] == "done"
        assert "imported=3" in done["output"]
        assert "done" in done["output"]


def test_admin_task_error_reported():
    c = _client()
    with mock.patch("agentmemhub.cli.run_ingest", side_effect=RuntimeError("boom")), \
         mock.patch("agentmemhub.adapters.all_adapters", return_value=[]):
        c.post("/api/admin/ingest")
        done = _wait_done(c)
        assert done["status"] == "error"
        assert "boom" in done["error"]


def test_task_live_output_during_running():
    """emit 实时流：任务运行中 job.output 即可见部分输出（前端 loading 期显示进展）。"""
    tasks.reset()
    def fn(emit, meta):
        emit("第一批完成")
        time.sleep(0.15)
        emit("第二批完成")
    job = tasks.submit("实时任务", fn)
    assert job["status"] == "running"
    time.sleep(0.05)
    mid = tasks.status()
    assert mid["status"] == "running" and "第一批完成" in mid["output"]
    deadline = time.time() + 3
    while time.time() < deadline and tasks.status()["status"] == "running":
        time.sleep(0.02)
    done = tasks.status()
    assert done["status"] == "done"
    assert "第二批完成" in done["output"]


def test_logs_record_recent_and_endpoint(tmp_path, monkeypatch):
    """统一日志：record/recent + /api/logs 端点（文件写 tmp，防污染真实数据目录）。"""
    from agentmemhub.web import logs
    monkeypatch.setattr("agentmemhub.web.logs._path", lambda: tmp_path / "web.log")
    logs.reset()
    logs.record("提取任务完成")
    logs.record("引擎启动失败", level="error")
    c = _client()
    d = c.get("/api/logs?limit=10").json()
    entries = d["logs"]
    assert entries[0]["msg"] == "引擎启动失败" and entries[0]["level"] == "error"
    assert entries[1]["msg"] == "提取任务完成"
    assert (tmp_path / "web.log").exists()           # JSONL 留痕


def test_admin_ingest_records_log(tmp_path, monkeypatch):
    """提交任务写入统一日志（可追溯面板操作）。"""
    from agentmemhub.web import logs
    monkeypatch.setattr("agentmemhub.web.logs._path", lambda: tmp_path / "web.log")
    logs.reset()
    c = _client()
    with mock.patch("agentmemhub.cli.run_ingest") as ri, \
         mock.patch("agentmemhub.adapters.all_adapters", return_value=[]):
        c.post("/api/admin/ingest")
        _wait_done(c)
    msgs = [l["msg"] for l in logs.recent()]
    assert any("提交任务" in m for m in msgs)
    assert any("开始" in m for m in msgs) and any("完成" in m for m in msgs)


def test_task_full_log_persisted_to_file(tmp_path, monkeypatch):
    """推送任务完整输出逐行落盘到 <data_dir>/tasks/<job_id>.log，页面关掉也可追溯。"""
    from agentmemhub.web import logs
    monkeypatch.setattr("agentmemhub.web.logs.task_log_dir", lambda: tmp_path / "tasks")
    c = _client()
    fake = {"imported": 3, "skipped": 5,
            "lines": ["[zcode] 推送 ok: imported=3 skipped=5"], "rebuilt": None}
    with mock.patch("agentmemhub.memos_daemon.auth_state", return_value={}), \
         mock.patch("agentmemhub.cli.push_to_memos", return_value=fake), \
         mock.patch("agentmemhub.adapters.all_adapters", return_value=[]):
        c.post("/api/admin/push")
        done = _wait_done(c)
    p = tmp_path / "tasks" / f"{done['id']}.log"
    assert p.exists()
    text = p.read_text(encoding="utf-8")
    assert "推送 ok: imported=3" in text
    assert logs.task_log_tail(done["id"]) == text.strip()


def test_task_log_single_line_append(tmp_path, monkeypatch):
    """append_task_line 单行追加 + tail 读取。"""
    from agentmemhub.web import logs
    monkeypatch.setattr("agentmemhub.web.logs.task_log_dir", lambda: tmp_path / "tasks")
    logs.append_task_line("t1", "第一行")
    logs.append_task_line("t1", "第二行")
    text = logs.task_log_tail("t1")
    assert "第一行" in text and "第二行" in text
    assert logs.task_log_tail("no_such_job") == ""