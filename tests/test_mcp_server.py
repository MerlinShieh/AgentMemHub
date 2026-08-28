"""MCP 记忆网关单元测试。

覆盖：JSON-RPC 握手/错误、tools/list 契约、四个工具在引擎离线/在线
（mock engine API）下的行为。引擎调用一律 mock，不依赖真实引擎。
"""
from __future__ import annotations

import io
import json
from unittest import mock

from agentmemhub import memos_daemon
from agentmemhub.mcp_server import MCPHandler, _ENGINE_HINT


def _handler() -> tuple[MCPHandler, io.StringIO]:
    out = io.StringIO()
    h = MCPHandler(stdin=io.StringIO(), stdout=out)
    return h, out


def _call(h: MCPHandler, line: str) -> dict | None:
    return h._dispatch(line)


def _req(method: str, params: dict | None = None, mid: int = 1) -> str:
    msg: dict = {"jsonrpc": "2.0", "id": mid, "method": method}
    if params is not None:
        msg["params"] = params
    return json.dumps(msg, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 协议层
# ---------------------------------------------------------------------------

def test_initialize_echoes_client_version():
    h, _ = _handler()
    r = _call(h, _req("initialize", {"protocolVersion": "2025-06-18",
                                     "capabilities": {},
                                     "clientInfo": {"name": "opencode", "version": "0.1"}}))
    assert r is not None and "result" in r
    assert r["result"]["protocolVersion"] == "2025-06-18"
    assert r["result"]["capabilities"] == {"tools": {}}
    assert r["result"]["serverInfo"]["name"] == "agentmemhub-mcp"


def test_initialize_default_version():
    h, _ = _handler()
    r = _call(h, _req("initialize", {"protocolVersion": "2024-11-05"}))
    assert r["result"]["protocolVersion"] == "2024-11-05"


def test_ping():
    h, _ = _handler()
    r = _call(h, _req("ping"))
    assert r is not None and r["result"] == {}


def test_notification_gets_no_reply():
    """initialized 通知（无 id）不应产生任何响应。"""
    h, out = _handler()
    r = _call(h, json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}))
    assert r is None
    assert out.getvalue() == ""


def test_parse_error():
    h, _ = _handler()
    r = _call(h, "{not-json")
    assert r is not None and r["error"]["code"] == -32700


def test_invalid_request():
    h, _ = _handler()
    r = _call(h, json.dumps({"jsonrpc": "2.0", "id": 1, "foo": "bar"}))
    assert r is not None and r["error"]["code"] == -32600


def test_unknown_method_is_protocol_error():
    h, _ = _handler()
    r = _call(h, _req("wat/do"))
    assert r is not None and r["error"]["code"] == -32601


# ---------------------------------------------------------------------------
# tools/list
# ---------------------------------------------------------------------------

def test_tools_list_contract():
    h, _ = _handler()
    r = _call(h, _req("tools/list"))
    tools = {t["name"]: t for t in r["result"]["tools"]}
    assert set(tools) == {"memory_search", "memory_recent", "memory_stats", "memory_save"}
    # memory_search 的 query 必填
    assert tools["memory_search"]["inputSchema"]["required"] == ["query"]
    assert "topK" in tools["memory_search"]["inputSchema"]["properties"]
    assert tools["memory_save"]["inputSchema"]["required"] == ["content"]


# ---------------------------------------------------------------------------
# 引擎离线：所有工具统一 isError + 启动指引
# ---------------------------------------------------------------------------

@mock.patch.object(memos_daemon, "auth_state", return_value=None)
def test_tools_offline_return_engine_hint(_auth):
    h, _ = _handler()
    for tool, args in (("memory_search", {"query": "x"}),
                       ("memory_recent", {}),
                       ("memory_stats", {}),
                       ("memory_save", {"content": "x"})):
        r = _call(h, _req("tools/call", {"name": tool, "arguments": args}, mid=7))
        assert r["result"]["isError"] is True
        text = r["result"]["content"][0]["text"]
        assert "记忆引擎未运行" in text
        assert "memos-daemon start" in text


# ---------------------------------------------------------------------------
# 引擎在线（mock 转发）
# ---------------------------------------------------------------------------

@mock.patch.object(memos_daemon, "auth_state", return_value={})
@mock.patch.object(memos_daemon, "engine_request")
def test_memory_search_online(engine_request, _auth):
    engine_request.side_effect = [
        {"hits": [{"score": 0.87, "tier": "traces", "refKind": "episode",
                   "refId": "ep-1", "snippet": "用户偏好 TypeScript"}],
         "injectedContext": "【相关记忆】偏好 TS"},
        {"episodes": 12, "traces": 340},
    ]
    h, _ = _handler()
    r = _call(h, _req("tools/call", {"name": "memory_search",
                                     "arguments": {"query": "语言偏好", "topK": 5}}))
    assert r["result"].get("isError") is None
    text = r["result"]["content"][0]["text"]
    assert "记忆检索「语言偏好」" in text
    assert "0.87" in text and "用户偏好 TypeScript" in text
    assert "episodes=12" in text
    assert "注入上下文" in text
    # 转发请求体正确（body 走关键字参数）
    args, kwargs = engine_request.call_args_list[0]
    assert args == ("POST", "/api/v1/memory/search")
    assert kwargs["body"] == {"agent": "hermes", "query": "语言偏好"}


@mock.patch.object(memos_daemon, "auth_state", return_value={})
@mock.patch.object(memos_daemon, "engine_request")
def test_memory_recent_online(engine_request, _auth):
    engine_request.return_value = {"traces": [
        {"ts": 1750000000000, "value": 0.6, "userText": "修好了登录 bug",
         "agentText": "根因是事件监听器未解绑"}]}
    h, _ = _handler()
    r = _call(h, _req("tools/call", {"name": "memory_recent", "arguments": {"limit": 3}}))
    text = r["result"]["content"][0]["text"]
    assert "最近记忆" in text and "修好了登录 bug" in text
    assert "事件监听器" in text


@mock.patch.object(memos_daemon, "auth_state", return_value={})
@mock.patch.object(memos_daemon, "daemon_status")
def test_memory_stats_online(status, _auth):
    status.return_value = {
        "online": True, "base_url": "http://127.0.0.1:18800",
        "summary": {"episodes": 12, "traces": 340,
                    "embedding_ready": True, "embedding_model": "all-MiniLM-L6-v2",
                    "llm_available": True},
        "lightweight": False, "auth": {"authenticated": True},
    }
    h, _ = _handler()
    r = _call(h, _req("tools/call", {"name": "memory_stats", "arguments": {}}))
    text = r["result"]["content"][0]["text"]
    assert "在线" in text and "episodes=12" in text
    assert "all-MiniLM-L6-v2" in text and "完整进化" in text


@mock.patch.object(memos_daemon, "auth_state", return_value={})
@mock.patch.object(memos_daemon, "engine_request")
@mock.patch("agentmemhub.memos.push_bundle")
def test_memory_save_online(push_bundle, engine_request, _auth):
    push_bundle.return_value = {"imported": 1, "skipped": 0}
    engine_request.return_value = {}          # rebuild repair
    h, _ = _handler()
    r = _call(h, _req("tools/call", {"name": "memory_save",
                                     "arguments": {"content": "登录 bug 根因：事件监听器未解绑"}}))
    text = r["result"]["content"][0]["text"]
    assert "记忆已写入" in text and "imported=1" in text
    bundle = push_bundle.call_args[0][0]
    trace = bundle["traces"][0]
    assert trace["userText"] == "登录 bug 根因：事件监听器未解绑"
    assert trace["id"].startswith("mcp_") and trace["value"] == 0.5
    # 幂等：同内容同时间 → 同 id
    assert trace["id"] == bundle["traces"][0]["id"]
    # 写入后补向量（repair 模式）
    assert engine_request.call_args[0] == ("POST", "/api/v1/embeddings/rebuild")
    assert engine_request.call_args.kwargs["body"] == {"mode": "repair"}


def test_memory_save_requires_content():
    h, _ = _handler()
    r = _call(h, _req("tools/call", {"name": "memory_save",
                                     "arguments": {"content": "  "}}))
    assert r["result"]["isError"] is True
    assert "content 参数" in r["result"]["content"][0]["text"]


def test_unknown_tool_is_protocol_error():
    h, _ = _handler()
    r = _call(h, _req("tools/call", {"name": "nope", "arguments": {}}))
    assert r is not None and r["error"]["code"] == -32601