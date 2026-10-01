# -*- coding: utf-8 -*-
"""离线批处理的 HTTP 接口（挂在**面板服务**上）。

接口本身很薄 —— 装配与动作都在 `agentmemhub/batch_ops.py`（与 CLI 共用），所以这里
只验证：HTTP 语义正确（422 / 只读不写）、**未配凭据时不误发请求**、以及取消
真的通知了厂商并清掉本地标记。
"""
import pytest
from starlette.testclient import TestClient


class _FakeClient:
    """假批量客户端：**不发任何请求**（未配凭据的路径另有用例覆盖）。"""

    def __init__(self, status="completed"):
        self.status = status
        self.cancelled: list[str] = []

    def retrieve(self, batch_id):
        return {"status": self.status,
                "request_counts": {"total": 1, "completed": 1, "failed": 0}}

    def cancel(self, batch_id):
        self.cancelled.append(batch_id)
        return {"id": batch_id}


@pytest.fixture()
def api(tmp_path, monkeypatch):
    """隔离的 app：产出目录指向 tmp、批量客户端换成假的。"""
    from agentmemhub import batch_ops
    from agentmemhub.web.app import create_app

    monkeypatch.setattr(batch_ops, "paths",
                        lambda: (str(tmp_path), str(tmp_path), ""))
    monkeypatch.setattr(batch_ops, "set_paths", lambda *a, **k: None)
    fake = _FakeClient()
    monkeypatch.setattr(batch_ops, "make_client",
                        lambda: (fake, {"model": "mimo-v2.6-flash"}))

    c = TestClient(create_app(tmp_path / "idx.db"))
    c.fake = fake                                          # type: ignore[attr-defined]
    c.out = tmp_path                                       # type: ignore[attr-defined]
    return c


def _save_job(out, **kw):
    from agentmemhub.llm_batch import STATE_NAME, BatchJob, save_state
    base = dict(batch_id="batch-t", stage="l2compile", submitted_at=1000.0,
                items={"l2page|域A-x|1": {"domain": "域A", "page_no": 1}},
                members={"域A": [{"ti": 1, "title": "p", "needs_llm": False,
                                  "dropped": 0, "group": []}]},
                provider_status="in_progress")
    base.update(kw)
    save_state(out / STATE_NAME, [BatchJob(**base)])


def test_状态接口_没有在飞任务时返回空(api):
    r = api.get("/api/wiki/batch/status")

    assert r.status_code == 200
    assert r.json() == {"inflight": [], "n": 0}


def test_状态接口_返回厂商状态与建议(api):
    _save_job(api.out)

    r = api.get("/api/wiki/batch/status")

    body = r.json()
    assert body["n"] == 1
    row = body["inflight"][0]
    assert row["batch_id"] == "batch-t"
    assert row["provider_status"] == "completed"    # 取的是**厂商侧**状态
    assert row["counts"]["total"] == 1
    assert row["suggestion"], "要给出 decide() 的建议动作"


def test_状态接口是只读的(api):
    """只读接口**不得**修改队列状态（排查时反复调用要安全）。"""
    from agentmemhub.llm_batch import STATE_NAME, load_state
    _save_job(api.out)

    api.get("/api/wiki/batch/status")

    kept = load_state(api.out / STATE_NAME)
    assert len(kept) == 1 and kept[0].batch_id == "batch-t"


def test_取消接口_通知厂商并清掉本地标记(api):
    from agentmemhub.llm_batch import STATE_NAME, load_state
    _save_job(api.out)

    r = api.post("/api/wiki/batch/cancel")

    assert r.status_code == 200
    body = r.json()
    assert [d["batch_id"] for d in body["cancelled"]] == ["batch-t"]
    assert body["errors"] == []
    assert api.fake.cancelled == ["batch-t"], "要通知厂商取消"
    assert load_state(api.out / STATE_NAME) == [], "本地标记要清掉（域回待更新池）"


def test_重提接口_没有终局任务时不动(api):
    r = api.post("/api/wiki/batch/resubmit")

    assert r.status_code == 200 and r.json() == {"resubmitted": []}


def test_检测流水接口(api):
    from agentmemhub import task_queue

    task_queue.append_check(api.out, {"action": "harvested", "pending": 1})

    r = api.get("/api/wiki/batch/checks")

    rows = r.json()["checks"]
    assert len(rows) == 1 and rows[0]["action"] == "harvested"


def test_推进接口_未配凭据时422而不发请求(api, monkeypatch):
    """前置条件不满足要**在起任务之前**就拒绝（否则会起一个注定失败的长任务）。"""
    from agentmemhub import batch_ops

    def boom():
        raise batch_ops.BatchOpsError("未配置 llm_batch.api_key —— 离线通道用不了")

    monkeypatch.setattr(batch_ops, "make_client", boom)

    r = api.post("/api/wiki/batch/run")

    assert r.status_code == 422
    assert "api_key" in r.json()["detail"]
