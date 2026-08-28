"""MCP Streamable HTTP 传输单元测试（fastapi TestClient 直连，不启真实端口）。"""
from __future__ import annotations

from unittest import mock

from fastapi.testclient import TestClient

from agentmemhub import memos_daemon
from agentmemhub.mcp_server import build_http_app

client = TestClient(build_http_app())


def _post(msg: dict, headers: dict | None = None):
    return client.post("/mcp", json=msg, headers=headers or {})


def test_http_initialize_returns_session():
    r = _post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
               "params": {"protocolVersion": "2025-06-18",
                          "capabilities": {},
                          "clientInfo": {"name": "opencode", "version": "0.1"}}})
    assert r.status_code == 200
    body = r.json()
    assert body["result"]["protocolVersion"] == "2025-06-18"
    assert body["result"]["serverInfo"]["name"] == "agentmemhub-mcp"
    assert r.headers.get("mcp-session-id")          # 会话 id 随初始化下发


def test_http_initialized_notification_202():
    """notifications/initialized（无 id）→ 202 Accepted 空 body。"""
    r = _post({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert r.status_code == 202
    assert r.content == b""


def test_http_tools_list():
    r = _post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert r.status_code == 200
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert names == {"memory_search", "memory_recent", "memory_stats", "memory_save"}


@mock.patch.object(memos_daemon, "auth_state", return_value=None)
def test_http_tools_call_offline(_auth):
    r = _post({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
               "params": {"name": "memory_stats", "arguments": {}}})
    assert r.status_code == 200
    res = r.json()["result"]
    assert res["isError"] is True
    assert "记忆引擎未运行" in res["content"][0]["text"]


@mock.patch.object(memos_daemon, "auth_state", return_value={})
@mock.patch.object(memos_daemon, "engine_request")
def test_http_tools_call_online(engine_request, _auth):
    engine_request.return_value = {
        "hits": [{"score": 0.9, "tier": 2, "refKind": "trace",
                  "refId": "t-1", "snippet": "用 SQLite 存事件流"}]}
    r = _post({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
               "params": {"name": "memory_search",
                          "arguments": {"query": "存储设计"}}})
    assert r.status_code == 200
    res = r.json()["result"]
    assert res.get("isError") is None
    text = res["content"][0]["text"]
    assert "用 SQLite 存事件流" in text
    # 转发请求体正确（第一次调用是 search，第二次是 overview）
    _, kwargs = engine_request.call_args_list[0]
    assert kwargs["body"] == {"agent": "hermes", "query": "存储设计"}


def test_http_bad_content_type_415():
    r = client.post("/mcp", content=b"{}", headers={"content-type": "text/plain"})
    assert r.status_code == 415


def test_http_parse_error_400():
    r = client.post("/mcp", content=b"{not-json",
                    headers={"content-type": "application/json"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == -32700


def test_http_invalid_request_400():
    r = _post({"jsonrpc": "2.0", "id": 1, "foo": 1})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == -32600


def test_http_get_405():
    r = client.get("/mcp")
    assert r.status_code == 405
    assert "POST" in r.headers.get("allow", "")


def test_http_delete_session_204():
    r = client.delete("/mcp", headers={"mcp-session-id": "sess-x"})
    assert r.status_code == 204