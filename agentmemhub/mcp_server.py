"""AgentMemHub MCP 记忆网关（stdio / Streamable HTTP 双传输）。

把本地记忆引擎（MemOS）的语义检索/读写包装成 MCP server，挂在
ZCode / OpenCode / Claude Code 等支持 MCP 的 Agent harness 上。

设计原则：
- 引擎生命周期完全由用户控制（看板 / `agentmemhub memos-daemon`）；
  本网关**只转发请求、从不拉起或停止引擎**，引擎离线时返回明确错误。
- 业务复用 memos_daemon.engine_request（含自动登录），不写本地库：
  检索/写入全部实时走引擎 HTTP API。
- 协议层（JSON-RPC 处理）与传输层分离，两种传输共用同一份逻辑。

传输方式：
- **stdio**（默认）：`python -m agentmemhub mcp`，由 Agent host 拉起子进程
  使用；本地个人场景。可用 console script `agentmemhub-mcp`（PATH 内）。
- **Streamable HTTP**（--http）：`python -m agentmemhub mcp --http
  [--bind 0.0.0.0] [--port 9100]`，单 endpoint `POST /mcp`（JSON-RPC，
  非流式响应；GET 405、DELETE 结束会话）；一台机器常驻，局域网/团队
  多个客户端共享同一个记忆引擎。默认只绑 127.0.0.1，团队共享需显式
  `--bind 0.0.0.0`（按需自设访问控制）。

Tools:
- memory_search(query, topK)  语义检索历史记忆（转发 /api/v1/memory/search）
- memory_recent(limit)        最近记忆时间线（/api/v1/traces）
- memory_stats()              引擎在线状态与记忆概要
- memory_save(content, tags)  写入一条记忆（构造 bundle → /api/v1/import）
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from typing import Any, Callable, Optional

from agentmemhub import memos_daemon

# ---- Streamable HTTP 软依赖 ----------------------------------------------
# fastapi 仅 --http 模式需要；stdio 模式不强制安装。顶部 import 是为了让
# FastAPI 能从模块全局命名空间解析 build_http_app 里的注解（函数内 import
# 不会注册到模块全局，否则注解会被当成查询参数）。
try:
    from fastapi import FastAPI  # noqa: F401
    from fastapi import Request  # noqa: F401
    _HAS_FASTAPI = True
except ImportError:
    _HAS_FASTAPI = False

SERVER_INFO = {"name": "agentmemhub-mcp", "version": "0.1.0"}

#: 引擎记忆空间的 agent 标识（与 web 网关 /api/memos/* 口径一致）
_AGENT = "hermes"

_ENGINE_HINT = ("记忆引擎未运行——先启动：`agentmemhub memos-daemon start`，"
                "或看板「记忆引擎 → 启动」，或手动 `npm run bridge:daemon` 后重试")
_AUTH_HINT = ("引擎已设密码且网关未登录：先运行 `agentmemhub memos-daemon "
              "--set-password <密码>` 保存密码后重试")


class _ToolError(Exception):
    """工具级错误（以 isError 结果返回给模型，而不是协议错误）。"""


def _clamp(v: Any, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def _trace_id(content: str, ts: int) -> str:
    digest = hashlib.sha256(f"mcp|{ts}|{content}".encode("utf-8")).hexdigest()
    return "mcp_" + digest[:16]


# ---------------------------------------------------------------------------
# Tools（模块级函数：返回展示文本；错误抛 _ToolError）
# ---------------------------------------------------------------------------

def _search(args: dict) -> str:
    q = str(args.get("query", "")).strip()
    if not q:
        raise _ToolError("memory_search 需要 query 参数")
    top = _clamp(args.get("topK"), 1, 30, 8)
    try:
        res = memos_daemon.engine_request("POST", "/api/v1/memory/search",
                                          body={"agent": _AGENT, "query": q},
                                          timeout=30)
        ov: dict = {}
        try:
            ov = memos_daemon.engine_request("GET", "/api/v1/overview", timeout=8)
        except Exception:
            pass
    except memos_daemon.EngineAuthError:
        raise _ToolError(_AUTH_HINT)
    except Exception as e:
        raise _ToolError(f"引擎检索失败：{e}")

    hits = res.get("hits") or []
    lines = [f"记忆检索「{q}」：{len(hits)} 条命中"
             f"（引擎在线，episodes={ov.get('episodes')}, traces={ov.get('traces')}）", ""]
    for h in hits[:top]:
        lines.append(f"- score={h.get('score')} tier={h.get('tier')} "
                     f"ref={h.get('refKind')}/{h.get('refId')}")
        lines.append(f"  {h.get('snippet') or ''}")
    ctx = (res.get("injectedContext") or "").strip()
    if ctx:
        lines += ["", f"引擎建议注入上下文：{ctx[:400]}" + ("…" if len(ctx) > 400 else "")]
    return "\n".join(lines)


def _recent(args: dict) -> str:
    limit = _clamp(args.get("limit"), 1, 30, 8)
    try:
        res = memos_daemon.engine_request(
            "GET", f"/api/v1/traces?limit={limit}&offset=0&groupByTurn=1", timeout=15)
    except memos_daemon.EngineAuthError:
        raise _ToolError(_AUTH_HINT)
    except Exception as e:
        raise _ToolError(f"引擎读取失败：{e}")

    traces = res.get("traces") or []
    if not traces:
        return "记忆库为空——尚无已写入的记忆。"
    lines = [f"最近记忆（{len(traces)} 条）:", ""]
    for t in traces:
        ts = t.get("ts") or 0
        if ts > 1e12:      # 毫秒 → 秒
            ts = ts / 1000
        try:
            when = time.strftime("%m-%d %H:%M", time.localtime(ts))
        except Exception:
            when = ""
        user = (t.get("userText") or "").strip().replace("\n", " ")
        agent = (t.get("agentText") or "").strip().replace("\n", " ")
        lines.append(f"- [{when}] value={t.get('value')} {user[:80]}")
        if agent:
            lines.append(f"    ↳ {agent[:120]}")
    return "\n".join(lines)


def _stats(args: dict) -> str:
    st = memos_daemon.daemon_status()
    if not st["online"]:
        raise _ToolError(_ENGINE_HINT)
    lines = [f"记忆引擎：在线（{st['base_url']}）"]
    s = st.get("summary") or {}
    lines.append(f"- 记忆总量：episodes={s.get('episodes')}，traces={s.get('traces')}")
    lines.append(f"- 语义检索：{'可用（模型 ' + str(s.get('embedding_model')) + '）' if s.get('embedding_ready') else '未就绪'}")
    lines.append(f"- LLM 评分：{'可用' if s.get('llm_available') else '不可用'}")
    lw = st.get("lightweight")
    mode = "轻量" if lw is True else ("完整进化" if lw is False else "引擎自管")
    lines.append(f"- 记忆模式：{mode}")
    lines.append(f"- 鉴权：{'已通过' if (st.get('auth') or {}).get('authenticated') else '未登录'}")
    return "\n".join(lines)


def _save(args: dict) -> str:
    content = str(args.get("content", "")).strip()
    if not content:
        raise _ToolError("memory_save 需要 content 参数")
    ts = int(time.time() * 1000)
    tid = _trace_id(content, ts)
    bundle = {
        "version": 1,
        "traces": [{
            "id": tid, "episodeId": "mcp", "sessionId": "mcp",
            "ts": ts, "turnId": ts,
            "userText": content, "agentText": "",
            "summary": content[:200],
            "value": 0.5, "alpha": 0.3, "priority": 0,
            "toolCalls": [], "agentThinking": [],
        }],
        "policies": [], "worldModels": [], "skills": [],
    }
    try:
        from agentmemhub.memos import push_bundle
        resp = push_bundle(bundle, memos_daemon.base_url())
    except memos_daemon.EngineAuthError:
        raise _ToolError(_AUTH_HINT)
    except Exception as e:
        raise _ToolError(f"记忆写入失败：{e}")
    # 单条入库后补一次增量向量（repair：只补缺失），失败不阻塞
    try:
        memos_daemon.engine_request(
            "POST", "/api/v1/embeddings/rebuild",
            body={"mode": "repair"}, timeout=300)
    except Exception:
        pass
    return (f"记忆已写入（imported={resp.get('imported')}, skipped={resp.get('skipped')}）\n"
            f"id={tid}\n内容：{content[:120]}" + ("…" if len(content) > 120 else ""))


_TOOL_HANDLERS: dict[str, Callable[[dict], str]] = {
    "memory_search": _search,
    "memory_recent": _recent,
    "memory_stats": _stats,
    "memory_save": _save,
}

_TOOLS: list[dict] = [
    {
        "name": "memory_search",
        "description": "从本地记忆引擎语义检索历史记忆。开始新任务、或用户问题涉及之前做过/讨论过的内容时调用。返回相关记忆条目（命中分/层级/摘要）与引擎建议注入上下文。引擎离线时返回明确错误。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索查询，自然语言描述想找的记忆主题"},
                "topK": {"type": "integer", "description": "返回条数（默认 8，最大 30）"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "memory_recent",
        "description": "查看最近写入记忆引擎的若干条记忆（时间线）。用于快速了解近期积累了什么、或确认一次写入是否生效。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "条数（默认 8，最大 30）"},
            },
        },
    },
    {
        "name": "memory_stats",
        "description": "查询记忆引擎在线状态、记忆总量（episodes/traces）、语义检索与 LLM 评分可用性、记忆模式与鉴权状态。任何会话开始时先调用它以确认记忆功能可用。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "memory_save",
        "description": "把一条值得长期保留的事实/结论写入记忆引擎（独立于会话采集链路，即时入库并补向量）。适合在用户明确要求记住、或发现重要且可复用的结论时调用。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "要保存的记忆内容（一句话结论或事实，可含少量上下文）"},
            },
            "required": ["content"],
        },
    },
]


# ---------------------------------------------------------------------------
# JSON-RPC / MCP 处理
# ---------------------------------------------------------------------------

def _err(mid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": code, "message": message}}


class MCPHandler:
    """一次 stdio 会话：逐行读 JSON-RPC 请求，逐行回响应（可注入流做测试）。"""

    def __init__(self, stdin=None, stdout=None):
        self._in = stdin or sys.stdin
        self._out = stdout or sys.stdout

    def run(self) -> None:
        for line in self._in:
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            reply = self._dispatch(line)
            if reply is not None:
                self._out.write(json.dumps(reply, ensure_ascii=False) + "\n")
                self._out.flush()

    def _dispatch(self, line: str) -> Optional[dict]:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return _err(None, -32700, "Parse error")
        return self._dispatch_msg(msg)

    def _dispatch_msg(self, msg: Any) -> Optional[dict]:
        """JSON-RPC 消息 → 响应（stdio 与 Streamable HTTP 共用）。"""
        if not isinstance(msg, dict) or "method" not in msg:
            return _err(msg.get("id") if isinstance(msg, dict) else None,
                        -32600, "Invalid Request")
        mid = msg.get("id")
        method = msg["method"]
        params = msg.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        if mid is None:                       # 通知：不回复
            self._notify(method, params)
            return None
        try:
            return {"jsonrpc": "2.0", "id": mid,
                    "result": self._handle(method, params)}
        except _ToolError as e:
            return {"jsonrpc": "2.0", "id": mid,
                    "result": {"content": [{"type": "text", "text": str(e)}],
                               "isError": True}}
        except LookupError as e:
            return _err(mid, -32601, str(e))
        except Exception:
            return _err(mid, -32603, "Internal error")

    def _notify(self, method: str, params: dict) -> None:
        pass    # notifications/initialized 等通知无需动作

    def _handle(self, method: str, params: dict) -> Any:
        if method == "initialize":
            ver = params.get("protocolVersion") or "2024-11-05"
            return {"protocolVersion": ver,
                    "capabilities": {"tools": {}},
                    "serverInfo": SERVER_INFO}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": _TOOLS}
        if method == "tools/call":
            return self._tools_call(params)
        raise LookupError(f"unknown method: {method}")

    def _tools_call(self, params: dict) -> dict:
        name = params.get("name", "")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            args = {}
        fn = _TOOL_HANDLERS.get(name)
        if fn is None:
            raise LookupError(f"unknown tool: {name}")
        # 引擎未运行时所有工具统一返回明确指引（网关不代管引擎生命周期）
        if memos_daemon.auth_state() is None:
            raise _ToolError(_ENGINE_HINT)
        return {"content": [{"type": "text", "text": fn(args)}]}


def run_stdio() -> None:
    """stdio 传输：逐行读 stdin，逐行写响应（Agent host 拉起子进程）。"""
    MCPHandler().run()


# ---------------------------------------------------------------------------
# Streamable HTTP 传输：单 endpoint POST /mcp（非流式 JSON 响应）
# 依赖 fastapi/uvicorn（属于 [web] extra）；stdio 模式不需要它们
# ---------------------------------------------------------------------------

def _http_err(mid: Any, code: int, message: str, status: int):
    from fastapi.responses import JSONResponse
    return JSONResponse(_err(mid, code, message), status_code=status)


def build_http_app():
    """构造 Streamable HTTP MCP server（POST /mcp；GET 405；DELETE 结束会话）。"""
    if not _HAS_FASTAPI:
        raise RuntimeError("--http 需要 web 依赖：uv pip install -e '.[web]'")
    import uuid

    from fastapi import FastAPI, Request
    from fastapi.concurrency import run_in_threadpool
    from fastapi.responses import JSONResponse, Response

    app = FastAPI(title="AgentMemHub MCP (Streamable HTTP)",
                  version=SERVER_INFO["version"],
                  docs_url=None, openapi_url=None)
    handler = MCPHandler()
    _sessions: set[str] = set()      # 宽松会话簿：记录但不强制校验

    @app.post("/mcp")
    async def mcp_post(request: Request):
        ctype = request.headers.get("content-type") or ""
        if "application/json" not in ctype:
            return _http_err(None, -32600,
                             "Content-Type must be application/json", 415)
        try:
            msg = json.loads((await request.body()).decode("utf-8"))
        except Exception:
            return _http_err(None, -32700, "Parse error", 400)
        if not isinstance(msg, dict) or "method" not in msg or "jsonrpc" not in msg:
            return _http_err(msg.get("id") if isinstance(msg, dict) else None,
                             -32600, "Invalid Request", 400)
        sid = request.headers.get("mcp-session-id")
        if msg.get("method") == "initialize":
            sid = sid or uuid.uuid4().hex
            _sessions.add(sid)
        # 工具调用/引擎请求是阻塞 IO，放线程池避免卡事件循环
        reply = await run_in_threadpool(handler._dispatch_msg, msg)
        if reply is None:                 # 通知（如 notifications/initialized）
            return Response(status_code=202)
        headers = {"Mcp-Session-Id": sid} if sid else {}
        return JSONResponse(reply, headers=headers)

    @app.get("/mcp")
    def mcp_get():
        # 无服务器主动推送：GET SSE 流不支持，明确 405
        return Response(status_code=405, headers={"Allow": "POST, DELETE"})

    @app.delete("/mcp")
    def mcp_delete(request: Request):
        sid = request.headers.get("mcp-session-id")
        if sid:
            _sessions.discard(sid)
        return Response(status_code=204)

    return app


def run_http(host: str = "127.0.0.1", port: int = 9100) -> None:
    """常驻 Streamable HTTP 服务：一台机器共享记忆引擎给多个客户端。"""
    try:
        import uvicorn
    except ImportError:      # pragma: no cover
        raise SystemExit("--http 需要 web 依赖：uv pip install -e '.[web]'")
    uvicorn.run(build_http_app(), host=host, port=port, log_level="info")


def main() -> None:
    """stdio 入口（python -m agentmemhub mcp_server / console script）。"""
    run_stdio()


if __name__ == "__main__":
    main()