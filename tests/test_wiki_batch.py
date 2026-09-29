# -*- coding: utf-8 -*-
"""离线批处理编排层（`agentmemhub/wiki_batch.py`）的单元测试。

全程用**假 client**（不发真请求）与**假通知器**（不碰通知库）。
"""
from __future__ import annotations

import json

from agentmemhub.llm_batch import (
    CONSUME,
    FALLBACK,
    PARTIAL,
    STATE_NAME,
    WAIT,
    BatchJob,
    load_state,
    save_state,
)
from agentmemhub.wiki_batch import import_results, poll


class _Client:
    """假批量客户端：按预设返回状态与文件内容，可指定某些调用失败。"""

    def __init__(self, *, batches=None, files=None,
                 fail_retrieve=(), fail_download=()):
        self.batches = batches or {}
        self.files = files or {}
        self.fail_retrieve = set(fail_retrieve)
        self.fail_download = set(fail_download)
        self.calls: list[tuple] = []

    def retrieve(self, batch_id):
        self.calls.append(("retrieve", batch_id))
        if batch_id in self.fail_retrieve:
            raise RuntimeError("网络断了")
        return self.batches.get(batch_id, {})

    def download(self, file_id):
        self.calls.append(("download", file_id))
        if file_id in self.fail_download:
            raise RuntimeError("下载失败")
        return self.files.get(file_id, "")


def _job(**kw) -> BatchJob:
    base = dict(batch_id="batch-1", stage="l2plan", submitted_at=1000.0,
                items={"l2plan|域A-0|0": {"domain": "域A", "batch_no": 0}},
                members={"域A": [{"title": "P1"}]})
    base.update(kw)
    return BatchJob(**base)


def _state(tmp_path, *jobs):
    save_state(tmp_path / STATE_NAME, list(jobs))


def _notifier():
    """假通知器：记下每次通知，返回 True。"""
    seen: list[tuple] = []

    def f(level, category, title, body="", meta=None):
        seen.append((level, category, title, dict(meta or {})))
        return True

    return seen, f


def _ok_line(cid: str, content: str = "{}") -> str:
    return json.dumps({"custom_id": cid,
                       "response": {"status_code": 200,
                                    "body": {"choices": [{"message": {"content": content}}]}},
                       "error": None})


# ---------------------------------------------------------------------------
# 空与等待
# ---------------------------------------------------------------------------

def test_没有在飞任务时什么都不做(tmp_path):
    c = _Client()

    out = import_results(c, tmp_path)

    assert out == {"jobs": [], "decisions": {}, "results": {},
                   "consumed": [], "voided": [], "waited": []}
    assert c.calls == [], "没有在飞任务就不该发任何请求"


def test_还在跑就继续等且不打扰人(tmp_path):
    _state(tmp_path, _job())
    c = _Client(batches={"batch-1": {"status": "in_progress",
                                     "request_counts": {"total": 1, "completed": 0}}})
    seen, note = _notifier()

    out = import_results(c, tmp_path, now=1000.0 + 60, deadline_s=3600,
                         notify_fn=note)

    assert out["decisions"]["batch-1"] == WAIT
    assert out["waited"] == ["batch-1"]
    assert ("download", "file-out") not in c.calls, "还没好就不该下载"
    assert seen == [], "还在等属正常，不该发通知"


def test_查状态失败不抛_保持原状留给下轮(tmp_path):
    _state(tmp_path, _job())
    c = _Client(fail_retrieve={"batch-1"})

    out = import_results(c, tmp_path, now=2000.0)

    assert out["jobs"][0].provider_status == "", "查不到就保持原状"
    assert out["decisions"]["batch-1"] == WAIT, "状态未知时保守地继续等"


# ---------------------------------------------------------------------------
# 消费结果
# ---------------------------------------------------------------------------

def test_完成后消费结果并落回现场(tmp_path):
    _state(tmp_path, _job())
    c = _Client(batches={"batch-1": {"status": "completed",
                                     "output_file_id": "file-out"}},
                files={"file-out": _ok_line(
                    "l2plan|域A-0|0",
                    '{"pages": [{"title": "页1", "members": [1]}]}') + "\n"})

    out = import_results(c, tmp_path, now=2000.0)

    assert out["decisions"]["batch-1"] == CONSUME
    assert out["consumed"] == ["batch-1"]
    got = out["results"]["batch-1"]["l2plan|域A-0|0"]
    assert got["ok"] is True and got["data"]["pages"][0]["title"] == "页1"
    assert load_state(tmp_path / STATE_NAME)[0].results["l2plan|域A-0|0"]["ok"] is True


def test_错误文件也要读_才知道哪条失败(tmp_path):
    """只读成功文件的话，"哪些请求没算出来"就无从知道，只能整体重跑。"""
    _state(tmp_path, _job())
    err = json.dumps({"id": "r1", "custom_id": "l2plan|域A-0|0", "response": None,
                      "error": {"code": "inference_failed", "message": "400"}})
    c = _Client(batches={"batch-1": {"status": "completed",
                                     "error_file_id": "file-err"}},
                files={"file-err": err + "\n"})

    out = import_results(c, tmp_path, now=2000.0)

    got = out["results"]["batch-1"]["l2plan|域A-0|0"]
    assert got["ok"] is False and "inference_failed" in got["error"]


def test_下载失败不抛_结果留待下轮(tmp_path):
    _state(tmp_path, _job())
    c = _Client(batches={"batch-1": {"status": "completed",
                                     "output_file_id": "file-out"}},
                fail_download={"file-out"})

    out = import_results(c, tmp_path, now=2000.0)

    assert out["consumed"] == [], "没取到就不算消费"
    assert out["results"]["batch-1"] == {}


def test_部分到位且超预算_结论是先用已到的(tmp_path):
    """决策要在**取回结果之后重新算** —— 取之前 results 是空的，会被误判成
    "一条都没到"（FALLBACK）。"""
    _state(tmp_path, _job(items={"a": {"domain": "域A", "batch_no": 0},
                                 "b": {"domain": "域A", "batch_no": 1}}))
    c = _Client(batches={"batch-1": {"status": "in_progress",
                                     "output_file_id": "file-out"}},
                files={"file-out": _ok_line("a") + "\n"})
    seen, note = _notifier()

    out = import_results(c, tmp_path, now=1000.0 + 7200, deadline_s=3600,
                         notify_fn=note)

    assert out["decisions"]["batch-1"] == PARTIAL, "取到 1/2 条 → 先用已到的"
    assert out["consumed"] == ["batch-1"]
    assert seen and "只到了一部分" in seen[0][2]
    assert seen[0][3]["pending"] == 1


def test_超时且一条都没到_结论是整批回退(tmp_path):
    _state(tmp_path, _job())
    c = _Client(batches={"batch-1": {"status": "in_progress"}})

    out = import_results(c, tmp_path, now=1000.0 + 7200, deadline_s=3600)

    assert out["decisions"]["batch-1"] == FALLBACK


# ---------------------------------------------------------------------------
# 终局：作废 + 回池 + 通知
# ---------------------------------------------------------------------------

def test_终局即作废回池并通知人(tmp_path):
    """D2 的决策：终局（过期/失败/取消）整批作废，**不捞已完成部分**。"""
    _state(tmp_path, _job())
    c = _Client(batches={"batch-1": {"status": "expired",
                                     "output_file_id": "file-out",
                                     "expires_at": 123.0}})
    seen, note = _notifier()

    out = import_results(c, tmp_path, now=2000.0, notify_fn=note)

    assert out["voided"][0]["status"] == "expired"
    assert out["voided"][0]["domains"] == ["域A"], "作废要报出需要回池的域"
    assert ("download", "file-out") not in c.calls, "终局不捞已完成部分（D2）"
    assert load_state(tmp_path / STATE_NAME) == [], "作废的任务不再留在 state 里"
    assert seen and seen[0][0] == "warning" and "expired" in seen[0][2]


def test_poll刷新厂商侧的各个字段(tmp_path):
    job = _job()
    c = _Client(batches={"batch-1": {"status": "Completed", "output_file_id": "f1",
                                     "error_file_id": "f2", "expires_at": 55.0,
                                     "request_counts": {"total": 1, "completed": 1}}})

    info = poll(c, [job])

    assert job.provider_status == "completed", "厂商状态一律 lower()"
    assert job.output_file_id == "f1" and job.error_file_id == "f2"
    assert job.expire_at == 55.0
    assert info["batch-1"]["counts"]["completed"] == 1
