from __future__ import annotations

from typing import Any

from plugins import HubPlugin, local_http_json, plugin_settings, require_local_url, write_plugin_settings

DEFAULT_URL = "http://127.0.0.1:8420"


class TencentAgentMemoryPlugin(HubPlugin):
    id = "tencent-agent-memory"
    title = "腾讯 Agent Memory"
    summary = "可选连接本机 TencentDB Agent Memory 网关（默认 8420）。中心不启动、不内置他们的服务。"
    docs_url = "https://github.com/TencentCloud/TencentDB-Agent-Memory"
    capabilities = ("search",)
    secret_fields = ("api_key",)

    def settings(self) -> dict[str, Any]:
        stored = plugin_settings(self.id)
        return {
            "enabled": bool(stored.get("enabled")),
            "base_url": str(stored.get("base_url") or DEFAULT_URL).strip() or DEFAULT_URL,
            "api_key": str(stored.get("api_key") or ""),
            "session_key": str(stored.get("session_key") or "conversation-hub"),
        }

    def save_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        current = self.settings()
        url = str(payload.get("base_url") or current["base_url"]).strip() or DEFAULT_URL
        require_local_url(url)
        key = str(payload.get("api_key") or "").strip()
        if payload.get("keep_api_key") and not key:
            key = current.get("api_key") or ""
        saved = write_plugin_settings(
            self.id,
            {
                "enabled": bool(payload.get("enabled")),
                "base_url": url.rstrip("/"),
                "api_key": key,
                "session_key": str(payload.get("session_key") or current["session_key"]).strip()
                or "conversation-hub",
            },
        )
        return {**self.settings(), "api_key": ""} | {"has_api_key": bool(saved.get("api_key"))}

    def health(self) -> dict[str, Any]:
        settings = self.settings()
        try:
            payload = local_http_json("GET", f"{settings['base_url']}/health", api_key=settings.get("api_key") or "")
            status = str(payload.get("status") or payload.get("ok") or "ok")
            if settings["enabled"]:
                return {"ok": True, "status": "connected", "detail": f"网关 {status}"}
            return {
                "ok": True,
                "status": "reachable",
                "detail": f"网关在线（{status}），插件仍关闭，保存启用后才会检索。",
            }
        except Exception as exc:  # noqa: BLE001
            if not settings["enabled"]:
                return {
                    "ok": False,
                    "status": "disabled",
                    "detail": f"默认关闭。本机网关未响应：{exc}",
                }
            return {
                "ok": False,
                "status": "unreachable",
                "detail": f"连不上 {settings['base_url']}：{exc}",
            }

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        settings = self.settings()
        if not settings["enabled"] or not str(query or "").strip():
            return []
        body = {
            "query": str(query).strip(),
            "session_key": settings["session_key"],
            "limit": max(1, min(20, int(limit))),
        }
        last_error = ""
        for path in ("/recall", "/search", "/memory/search"):
            try:
                payload = local_http_json(
                    "POST",
                    f"{settings['base_url']}{path}",
                    api_key=settings.get("api_key") or "",
                    payload=body,
                )
                return _normalize_hits(payload, limit)[: max(1, min(20, int(limit)))]
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                continue
        raise RuntimeError(last_error or "网关没有可用的检索接口")


def _normalize_hits(payload: Any, limit: int) -> list[dict[str, Any]]:
    rows: list[Any]
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        for key in ("results", "items", "memories", "data", "hits"):
            if isinstance(payload.get(key), list):
                rows = payload[key]
                break
        else:
            rows = [payload]
    else:
        rows = []
    hits: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, str):
            text = row.strip()
            if text:
                hits.append({"title": "记忆", "text": text, "layer": ""})
            continue
        if not isinstance(row, dict):
            continue
        text = str(row.get("text") or row.get("content") or row.get("memory") or row.get("snippet") or "").strip()
        if not text:
            continue
        hits.append(
            {
                "title": str(row.get("title") or row.get("scene") or row.get("persona") or "记忆")[:80],
                "text": text[:500],
                "layer": str(row.get("layer") or row.get("type") or row.get("kind") or ""),
            }
        )
        if len(hits) >= limit:
            break
    return hits
