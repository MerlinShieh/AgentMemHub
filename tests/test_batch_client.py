# -*- coding: utf-8 -*-
"""批量接口客户端（`agentmemhub/batch_client.py`）的单元测试。

**全程不发真请求**：所有功能 case 注入假 opener；真网络由两道保险挡住 ——
① 本文件每个 case 都用假 opener；② `AGENTMEMHUB_TEST_SANDBOX` 熔断（专门有
一个 case 验它生效，那批调用是**真花钱**的，不能靠自觉）。
"""
from __future__ import annotations

import io
import json
import urllib.error

import pytest

from agentmemhub.batch_client import (
    SANDBOX_ENV,
    BatchAuthError,
    BatchBalanceError,
    BatchError,
    BatchNotFoundError,
    BatchTransientError,
    BatchUnknownStateError,
    MiMoBatchClient,
    classify_error,
)

CFG = {"endpoint": "https://batch.example.com/v1", "api_key": "sk-test",
       "model": "mimo-v2.6-flash", "completion_window": "24h",
       "timeout": 5, "max_retries": 3}


class _Resp:
    """够用的假响应（支持 `with` 与 `read()`）。"""

    def __init__(self, text: str, status: int = 200):
        self._t = text
        self.status = status

    def read(self) -> bytes:
        return self._t.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Opener:
    """假 opener：按预设序列响应，并记下每次请求（供断言重试次数与请求体）。"""

    def __init__(self, *items):
        self.items = list(items)
        self.calls: list = []

    def open(self, req, timeout=None):
        self.calls.append(req)
        item = self.items.pop(0) if self.items else (200, "{}")
        if isinstance(item, Exception):
            raise item
        status, text = item
        if status >= 400:
            raise urllib.error.HTTPError(
                req.full_url, status, "err", {}, io.BytesIO(text.encode("utf-8")))
        return _Resp(text, status)


def _client(opener, monkeypatch, cfg=None) -> MiMoBatchClient:
    """解除沙箱熔断（opener 是假的，不会真发）后构造客户端。"""
    monkeypatch.delenv(SANDBOX_ENV, raising=False)
    return MiMoBatchClient(cfg or CFG, opener=opener)


# ---------------------------------------------------------------------------
# 熔断与配置
# ---------------------------------------------------------------------------

def test_沙箱熔断_任何请求都直接抛且不发出去(monkeypatch):
    monkeypatch.setenv(SANDBOX_ENV, "1")
    opener = _Opener((200, "{}"))
    c = MiMoBatchClient(CFG, opener=opener)
    with pytest.raises(BatchError) as e:
        c.retrieve("batch_x")
    assert SANDBOX_ENV in str(e.value)
    assert opener.calls == [], "熔断必须发生在发请求之前（这是花钱的调用）"


def test_未配api_key直接拒绝(monkeypatch):
    monkeypatch.delenv(SANDBOX_ENV, raising=False)
    c = MiMoBatchClient({**CFG, "api_key": ""}, opener=_Opener())
    with pytest.raises(BatchAuthError) as e:
        c.upload("{}")
    assert "api_key" in str(e.value)


def test_未配endpoint构造即失败(monkeypatch):
    monkeypatch.delenv(SANDBOX_ENV, raising=False)
    with pytest.raises(BatchAuthError):
        MiMoBatchClient({**CFG, "endpoint": ""}, opener=_Opener())


# ---------------------------------------------------------------------------
# 错误分类（纯函数 —— 分类决定调用方是"重试 / 回退实时 / 通知人"）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status,body,expect", [
    (401, '{"error":{"message":"Invalid API key","code":"invalid_api_key"}}', BatchAuthError),
    (403, '{"error":{"message":"forbidden"}}', BatchAuthError),
    (402, '{"error":{"message":"insufficient balance"}}', BatchBalanceError),
    (404, '{"error":{"message":"Batch not found: x","code":"batch_not_found"}}', BatchNotFoundError),
    (500, '{"error":{"message":"oops"}}', BatchTransientError),
    (429, '{"error":{"message":"rate limit"}}', BatchTransientError),
    (400, '{"error":{"message":"bad param","code":"invalid_request"}}', BatchError),
])
def test_错误分类(status, body, expect):
    err = classify_error(status, body)
    assert type(err) is expect, "分类要精确（鉴权/余额/不存在/瞬态/其它）"
    assert err.status == status


def test_错误分类_非JSON响应也给出可读信息():
    err = classify_error(400, "<html>gateway</html>")
    assert err.status == 400 and "400" in str(err)


# ---------------------------------------------------------------------------
# 各动作
# ---------------------------------------------------------------------------

def test_上传_编码multipart并返回file对象(monkeypatch):
    opener = _Opener((200, json.dumps({"id": "file-abc", "bytes": 12, "status": "active"})))
    c = _client(opener, monkeypatch)
    out = c.upload('{"custom_id":"a"}\n', filename="in.jsonl")

    assert out["id"] == "file-abc"
    req = opener.calls[0]
    assert req.method == "POST" and req.full_url.endswith("/v1/files")
    assert req.headers["Content-type"].startswith("multipart/form-data; boundary=")
    body = req.data.decode("utf-8")
    assert 'name="purpose"' in body and "batch" in body
    assert 'filename="in.jsonl"' in body
    assert '{"custom_id":"a"}' in body, "JSONL 正文必须原样进 multipart"


def test_建任务_成功时带上端点与窗口(monkeypatch):
    opener = _Opener((201, json.dumps({"id": "batch_1", "status": "created",
                                       "completion_window": "24h", "name": "n1"})))
    c = _client(opener, monkeypatch)
    b = c.create("file-abc", name="n1", n_requests=3)

    assert b["id"] == "batch_1" and b["status"] == "created"
    sent = json.loads(opener.calls[0].data.decode("utf-8"))
    assert sent["input_file_id"] == "file-abc"
    assert sent["endpoint"] == "/v1/chat/completions"
    assert sent["completion_window"] == "24h"


def test_建任务绝不重试_失败抛未知态(monkeypatch):
    """建任务不幂等：失败即抛 `BatchUnknownStateError`，**一次都不重试**。"""
    opener = _Opener((500, '{"error":{"message":"oops"}}'),
                     (201, '{"id":"batch_should_not_happen"}'))
    monkeypatch.setattr("agentmemhub.batch_client.time.sleep", lambda s: None)
    c = _client(opener, monkeypatch)
    with pytest.raises(BatchUnknownStateError):
        c.create("file-abc")
    assert len(opener.calls) == 1, "建任务绝不能自动重试（会建出第二个任务、重复计费）"


def test_幂等动作会重试(monkeypatch):
    opener = _Opener((500, '{"error":{"message":"oops"}}'),
                     (200, '{"id":"batch_1","status":"in_progress"}'))
    monkeypatch.setattr("agentmemhub.batch_client.time.sleep", lambda s: None)
    c = _client(opener, monkeypatch)
    b = c.retrieve("batch_1")
    assert b["status"] == "in_progress"
    assert len(opener.calls) == 2, "只读动作应当重试"


def test_取消_已消失的任务不报错(monkeypatch):
    opener = _Opener((404, '{"error":{"message":"Batch not found: x","code":"batch_not_found"}}'))
    monkeypatch.setattr("agentmemhub.batch_client.time.sleep", lambda s: None)
    c = _client(opener, monkeypatch)
    assert c.cancel("batch_gone")["status"] == "not_found"


def test_下载结果(monkeypatch):
    opener = _Opener((200, '{"custom_id":"a"}\n{"custom_id":"b"}\n'))
    c = _client(opener, monkeypatch)
    text = c.download("file-out")
    assert text.count("custom_id") == 2
    assert opener.calls[0].full_url.endswith("/v1/files/file-out/content")


def test_列表返回data数组(monkeypatch):
    items = [{"id": "batch_1", "name": "other", "input_file_id": "file-x"},
             {"id": "batch_2", "name": "want", "input_file_id": "file-y"}]
    opener = _Opener((200, json.dumps({"object": "list", "data": items})))
    c = _client(opener, monkeypatch)
    assert [b["id"] for b in c.list_batches()] == ["batch_1", "batch_2"]


# ---------------------------------------------------------------------------
# 对账：建任务超时后**先问服务端**，不重试、不猜
# ---------------------------------------------------------------------------

def test_对账_按name找到已建成的任务(monkeypatch):
    items = [{"id": "batch_1", "name": "agentmemhub-l2", "input_file_id": "file-a"},
             {"id": "batch_2", "name": "x", "input_file_id": "file-b"}]
    opener = _Opener((200, json.dumps({"data": items})),
                     (200, json.dumps({"data": items})))
    c = _client(opener, monkeypatch)
    assert c.find_batch(name="agentmemhub-l2")["id"] == "batch_1"
    assert c.find_batch(name="没建过") is None


def test_对账_按input_file_id兜底(monkeypatch):
    items = [{"id": "batch_9", "name": "", "input_file_id": "file-target"}]
    opener = _Opener((200, json.dumps({"data": items})))
    c = _client(opener, monkeypatch)
    assert c.find_batch(name="", input_file_id="file-target")["id"] == "batch_9"


def test_对账_列表都查不到时返回None而不是抛(monkeypatch):
    opener = _Opener((500, '{"error":{"message":"oops"}}'))
    monkeypatch.setattr("agentmemhub.batch_client.time.sleep", lambda s: None)
    c = _client(opener, monkeypatch)
    assert c.find_batch(name="x") is None


# ---------------------------------------------------------------------------
# 日志是旁路
# ---------------------------------------------------------------------------

def test_审计失败不影响主流程(monkeypatch):
    import agentmemhub.logs as logs

    def boom(*a, **kw):
        raise RuntimeError("审计炸了")

    monkeypatch.setattr(logs, "audit_wiki", boom)
    opener = _Opener((200, '{"id":"file-1"}'))
    c = _client(opener, monkeypatch)
    assert c.upload("{}")["id"] == "file-1", "日志出问题不能影响业务返回值"
