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