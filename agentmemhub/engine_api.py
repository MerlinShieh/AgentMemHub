"""内置记忆引擎（`agentmemhub.rag`）的运行时门面。

**历史**：本模块原名「MemOS 记忆引擎 daemon 生命周期管理」，负责拉起 / 停止 /
巡检 vendored MemOS Local Plugin 的 bridge daemon。2026-10-24 起 MemOS 上游被
整体移除（`memOS/` 物理目录、`agentmemhub.memos` 模块、`/api/v1/import` 端点、
`memos` 子命令、`memory_backend` 配置键全部删除），本模块只保留**进程内引擎**
所需的最小门面：

- `engine_request`：统一出口。MemOS 时代的 `/api/v1/*` 端点语义在这里由
  `_rag_dispatch` 派发到 `agentmemhub.rag_bridge`，调用方（MCP / Web / CLI）
  一行不用改。
- `auth_state`：在线探测。内置引擎没有鉴权面，在线即视为已鉴权；离线返回 None
  （与旧 HTTP 语义保持一致，调用方据此报 503）。
- `daemon_status`：面板 / 控制台消费的引擎状态摘要。

**没有「守护进程」这回事**：内置引擎是进程内直调，不存在可启停的 daemon，也没有
端口、PID 文件、插件目录。模块名保留是为了不惊动几十处调用方。
"""
from __future__ import annotations

import urllib.parse
from pathlib import Path
from typing import Any, Optional


def _backend_is_rag() -> bool:
    """恒 `True` —— MemOS 回退后端已移除。

    保留函数只为不动调用方的分支结构；凡是 `if _backend_is_rag(): ... else: ...`
    的 else 分支都是死代码，可择机清理。
    """
    return True


def _data_dir() -> Path:
    from agentmemhub import config
    return config.config().data_dir


def _log_file() -> Path:
    """统一日志目录：`<程序根>/logs/engine.log`（引擎侧输出）。"""
    from agentmemhub import logs
    return logs.log_dir() / "engine.log"


def base_url() -> str:
    """回退后端（MemOS）地址 —— 该后端已移除，恒返回空串。

    保留函数只为不动调用方签名；`engine_request` 在内置引擎下忽略 `base`。
    """
    return ""


class EngineAuthError(Exception):
    """引擎在线但鉴权失败（保留异常类型：旧调用方按它区分 401 语义）。"""


def _rag_dispatch(method: str, path: str, body: Optional[dict]) -> dict:
    """把 /api/v1 端点语义派发到进程内 rag_bridge（唯一接缝）。

    返回与旧 HTTP 端点同形状的 dict；调用方（mcp/web/cli）零改动。
    未知路径抛异常（配置/版本漂移早暴露），不回退 HTTP。
    """
    from agentmemhub import rag_bridge
    p = path.split("?", 1)
    route, qs = p[0], (p[1] if len(p) > 1 else "")
    body = body or {}
    if route == "/api/v1/memory/search" and method == "POST":
        return rag_bridge.search(str(body.get("agent", "")),
                                 str(body.get("query", "")),
                                 origin=str(body.get("origin", "")))
    if route == "/api/v1/overview" and method == "GET":
        return rag_bridge.overview()
    if route == "/api/v1/traces" and method == "GET":
        q = urllib.parse.parse_qs(qs)
        return rag_bridge.traces_list(
            limit=int((q.get("limit") or ["20"])[0]),
            offset=int((q.get("offset") or ["0"])[0]))
    if route == "/api/v1/feedback" and method == "POST":
        return rag_bridge.feedback(
            str(body.get("traceId", "")), str(body.get("polarity", "")),
            magnitude=float(body.get("magnitude", 1.0) or 1.0),
            channel=str(body.get("channel", "explicit")),
            state=bool(body.get("state", False)),
            revoke=bool(body.get("revoke", False)))
    if route == "/api/v1/embeddings/rebuild" and method == "POST":
        return rag_bridge.rebuild_embeddings(
            mode=str(body.get("mode", "repair")))
    if route == "/api/v1/health" and method == "GET":
        return rag_bridge.health()
    if route == "/api/v1/health/reclaim" and method == "POST":
        return rag_bridge.health_reclaim()
    if route == "/api/v1/auth/status" and method == "GET":
        # rag 无鉴权面：恒就绪（在线性由 probe 承载）
        return {"enabled": False, "needsSetup": False, "authenticated": True,
                "backend": "rag"}
    raise RuntimeError(f"rag 后端未实现该引擎端点：{method} {route}")


def engine_request(method: str, path: str, body: Optional[dict] = None,
                   timeout: float = 30, retries: int = 1,
                   base: Optional[str] = None) -> dict:
    """引擎请求统一出口：进程内派发到 `rag_bridge`，不发 HTTP。

    `timeout` / `retries` / `base` 三个参数只为兼容旧调用方签名而保留，内置
    引擎下均无意义。
    """
    return _rag_dispatch(method, path, body)


def auth_state() -> Optional[dict]:
    """引擎在线状态：在线返回鉴权摘要，离线返回 None。

    内置引擎无鉴权面，probe 在线即视为已鉴权（与旧 HTTP 语义对齐：
    调用方用 `auth_state() is None` 判断是否要报 503）。
    """
    from agentmemhub import rag_bridge
    p = rag_bridge.probe()
    return ({"enabled": False, "authenticated": True}
            if p.get("online") else None)


def daemon_status() -> dict[str, Any]:
    """引擎状态摘要（面板 / 控制台消费）。

    字段名沿用旧 HTTP 引擎时代（`pid` / `engine_home` / `lightweight` 等返回中性值），
    消费端据此降级展示，不必改前端契约。
    """
    from agentmemhub import rag_bridge
    p = rag_bridge.probe()
    online = bool(p.get("online"))
    ov = p.get("summary") or {}
    result: dict[str, Any] = {
        "online": online, "backend": "rag",
        "auth": {"enabled": False, "authenticated": online} if online else None,
        "auth_required": False, "pid": None, "managed": False,
        "base_url": "in-process://rag", "plugin_dir": None, "repo_dir": None,
        "engine_home": None, "lightweight": None,
    }
    if online:
        emb = ov.get("embedder") or {}
        result["summary"] = {
            "episodes": ov.get("episodes"),
            "traces": ov.get("traces"),
            "embedding_ready": emb.get("available"),
            "embedding_model": emb.get("model"),
            "llm_available": (ov.get("llm") or {}).get("available"),
            "coverage": (ov.get("rag") or {}).get("coverage"),
            "memory_units": ov.get("memory_units"),
        }
    return result
