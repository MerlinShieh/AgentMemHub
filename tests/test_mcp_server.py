"""MCP 记忆网关单元测试。

覆盖：JSON-RPC 握手/错误、tools/list 契约、四个工具在引擎离线/在线
（mock engine API）下的行为。引擎调用一律 mock，不依赖真实引擎。
"""
from __future__ import annotations

import io
import json
import sys
from unittest import mock

import pytest

from agentmemhub import memos_daemon
from agentmemhub.mcp_server import MCPHandler, _engine_hint, run_stdio
from agentmemhub.rag.memstore import AGENT_IMPORTANCE_VALUES, BASE_VALUE_AGENT_WRITE


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
    assert set(tools) == {"memory_search", "memory_recent", "memory_stats", "memory_save", "memory_score"}
    # memory_search 的 query 必填
    assert tools["memory_search"]["inputSchema"]["required"] == ["query"]
    assert "topK" in tools["memory_search"]["inputSchema"]["properties"]
    assert tools["memory_save"]["inputSchema"]["required"] == ["content"]
    # importance 是可选档位：枚举三档，且不得进入必填
    imp = tools["memory_save"]["inputSchema"]["properties"]["importance"]
    assert imp["enum"] == ["high", "normal", "low"]
    assert "importance" not in tools["memory_save"]["inputSchema"]["required"]
    # tags 也必须真的暴露（历史遗留：文档写了、schema 里却没有）
    tags = tools["memory_save"]["inputSchema"]["properties"]["tags"]
    assert tags["type"] == "array" and tags["items"]["type"] == "string"
    assert "tags" not in tools["memory_save"]["inputSchema"]["required"]


# ---------------------------------------------------------------------------
# 引擎离线：所有工具统一 isError + 启动指引
# ---------------------------------------------------------------------------

@mock.patch.object(memos_daemon, "auth_state", return_value=None)
def test_tools_offline_return_engine_hint(_auth):
    """引擎离线：所有工具统一 isError + 后端相关的排障指引。"""
    h, _ = _handler()
    for tool, args in (("memory_search", {"query": "x"}),
                       ("memory_recent", {}),
                       ("memory_stats", {}),
                       ("memory_save", {"content": "x"})):
        r = _call(h, _req("tools/call", {"name": tool, "arguments": args}, mid=7))
        assert r["result"]["isError"] is True
        text = r["result"]["content"][0]["text"]
        # 提示文案按后端区分（memos 后端=daemon；rag 后端=索引库/sync）
        assert ("记忆引擎未运行" in text) or ("记忆索引不可用" in text)


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
    assert trace["id"].startswith("mcp_")
    # 初始价值必须与引擎常量一致（历史 bug：此处曾硬编码 0.5，比设计值低 0.1）
    assert trace["value"] == BASE_VALUE_AGENT_WRITE
    assert trace["priority"] == BASE_VALUE_AGENT_WRITE
    # 幂等：同内容同时间 → 同 id
    assert trace["id"] == bundle["traces"][0]["id"]
    # 写入后补向量（repair 模式）
    assert engine_request.call_args[0] == ("POST", "/api/v1/embeddings/rebuild")
    assert engine_request.call_args.kwargs["body"] == {"mode": "repair"}


@mock.patch.object(memos_daemon, "auth_state", return_value={})
@mock.patch.object(memos_daemon, "engine_request")
@mock.patch("agentmemhub.memos.push_bundle")
def test_memory_save_initial_value_is_agent_tier(push_bundle, engine_request, _auth):
    """回归：MCP 写入的初始价值必须等同引擎的「Agent 主动写入」档位。

    历史 bug：``_save`` 把 value/priority 硬编码为 0.5，与
    ``memstore.BASE_VALUE_AGENT_WRITE``（0.6）分叉——MCP 走 bundle import 路径，
    绕过了 ``memstore.save_memory()``，那个常量因此从未生效。本引擎价值只做
    有界 tie-breaker（BOOST_CAP=0.3），起点低 0.1 相当于初始权重损失约 1/3。
    """
    assert BASE_VALUE_AGENT_WRITE == 0.6, "Agent 写入档位口径变更，请同步本测试与文档"

    push_bundle.return_value = {"imported": 1, "skipped": 0}
    engine_request.return_value = {}
    h, _ = _handler()
    _call(h, _req("tools/call", {"name": "memory_save",
                                 "arguments": {"content": "初始价值契约"}}))

    trace = push_bundle.call_args[0][0]["traces"][0]
    assert trace["value"] == BASE_VALUE_AGENT_WRITE
    assert trace["priority"] == BASE_VALUE_AGENT_WRITE
    assert trace["value"] > 0.5, "不得回退到旧的硬编码 0.5"


@pytest.mark.parametrize("importance,expected", [
    ("high", 0.8),
    ("normal", BASE_VALUE_AGENT_WRITE),
    ("low", 0.4),
    (None, BASE_VALUE_AGENT_WRITE),      # 不传 = normal，与历史行为一致
])
@mock.patch.object(memos_daemon, "auth_state", return_value={})
@mock.patch.object(memos_daemon, "engine_request")
@mock.patch("agentmemhub.memos.push_bundle")
def test_memory_save_importance_tiers(push_bundle, engine_request, _auth,
                                      importance, expected):
    """importance 档位 → 初始价值；不传等价 normal（向后兼容）。"""
    push_bundle.return_value = {"imported": 1, "skipped": 0}
    engine_request.return_value = {}
    h, _ = _handler()
    args: dict = {"content": f"档位测试 {importance}"}
    if importance is not None:
        args["importance"] = importance
    _call(h, _req("tools/call", {"name": "memory_save", "arguments": args}))

    trace = push_bundle.call_args[0][0]["traces"][0]
    assert trace["value"] == expected
    assert trace["priority"] == expected


def test_importance_tiers_are_ordered_and_disjoint():
    """三档必须递增且互不相同，且 normal 恰为 Agent 写入基线。"""
    vals = AGENT_IMPORTANCE_VALUES
    assert set(vals) == {"high", "normal", "low"}
    assert vals["low"] < vals["normal"] < vals["high"]
    assert vals["normal"] == BASE_VALUE_AGENT_WRITE


@mock.patch.object(memos_daemon, "auth_state", return_value={})
def test_memory_save_rejects_unknown_importance(_auth):
    """非法档位报参数错误，不静默落到默认值。"""
    h, _ = _handler()
    r = _call(h, _req("tools/call", {"name": "memory_save",
                                     "arguments": {"content": "x",
                                                   "importance": "urgent"}}))
    assert r["result"]["isError"] is True
    assert "importance" in r["result"]["content"][0]["text"]


@mock.patch.object(memos_daemon, "auth_state", return_value={})
@mock.patch.object(memos_daemon, "engine_request")
@mock.patch("agentmemhub.memos.push_bundle")
def test_memory_save_passes_tags(push_bundle, engine_request, _auth):
    """tags 透传进 bundle：去空白、去空串；非序列一律忽略（不写脏数据）。"""
    push_bundle.return_value = {"imported": 1, "skipped": 0}
    engine_request.return_value = {}
    h, _ = _handler()
    _call(h, _req("tools/call", {"name": "memory_save",
                                 "arguments": {"content": "带标签",
                                               "tags": ["dsh", " mcp ", "", "  "]}}))
    assert push_bundle.call_args[0][0]["traces"][0]["tags"] == ["dsh", "mcp"]

    _call(h, _req("tools/call", {"name": "memory_save",
                                 "arguments": {"content": "标签类型非法",
                                               "tags": "dsh"}}))
    assert push_bundle.call_args[0][0]["traces"][0]["tags"] == []


@mock.patch.object(memos_daemon, "auth_state", return_value={})
def test_memory_save_requires_content(_auth):
    """参数校验优先：引擎在线时缺 content 报参数错误（离线时统一报引擎未运行）。"""
    h, _ = _handler()
    r = _call(h, _req("tools/call", {"name": "memory_save",
                                     "arguments": {"content": "  "}}))
    assert r["result"]["isError"] is True
    assert "content 参数" in r["result"]["content"][0]["text"]


def test_unknown_tool_is_protocol_error():
    h, _ = _handler()
    r = _call(h, _req("tools/call", {"name": "nope", "arguments": {}}))
    assert r is not None and r["error"]["code"] == -32601


# ---------------------------------------------------------------------------
# stdio 编码（Windows 兼容性回归）
# ---------------------------------------------------------------------------

def test_run_stdio_forces_utf8_on_locale_streams(monkeypatch):
    """复现 Windows 中文损坏：run_stdio 必须把 stdio 切到 UTF-8。

    Windows 上 ``sys.stdin``/``sys.stdout`` 默认走 locale 编码（cp936），
    而 MCP 协议规定 UTF-8。不强制时：客户端发来的 UTF-8 中文被按 cp936
    解码（读进来就已损坏），服务端写出的中文被编成 cp936（对端按 UTF-8
    解成乱码）。这里用 cp936 包装的字节流模拟该平台的默认 stdio。
    """
    fake_in = io.TextIOWrapper(io.BytesIO(), encoding="cp936")
    fake_out = io.TextIOWrapper(io.BytesIO(), encoding="cp936", write_through=True)
    monkeypatch.setattr(sys, "stdin", fake_in)
    monkeypatch.setattr(sys, "stdout", fake_out)

    run_stdio()          # 空输入 → 读完即返回

    assert fake_in.encoding.lower().replace("-", "") == "utf8"
    assert fake_out.encoding.lower().replace("-", "") == "utf8"


def test_run_stdio_roundtrips_chinese_over_locale_streams(monkeypatch):
    """cp936 平台默认流下，中文仍须能按 UTF-8 正确往返（端到端）。"""
    req = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                     ensure_ascii=False)
    stdin_raw = io.BytesIO((req + "\n").encode("utf-8"))
    stdout_raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdin",
                        io.TextIOWrapper(stdin_raw, encoding="cp936"))
    monkeypatch.setattr(sys, "stdout",
                        io.TextIOWrapper(stdout_raw, encoding="cp936",
                                         write_through=True))

    run_stdio()

    # 响应里的中文工具描述必须能按 UTF-8 解出（cp936 编码的字节会在这里暴露）
    text = stdout_raw.getvalue().decode("utf-8")
    payload = json.loads(text)
    assert payload["result"]["tools"]
    assert "语义检索" in text