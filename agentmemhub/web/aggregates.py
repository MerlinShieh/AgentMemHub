"""AgentMemHub Web — 统计聚合与缓存。

把 /api/stats、/api/facets 需要的聚合全部下推为 SQL GROUP BY，
结果在进程内做 TTL 缓存；数据变更（删除/改标题）时调用 invalidate() 失效。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional

from agentmemhub.store import Store

_TTL_SECONDS = 60.0


def _workspace_of(cwd: Optional[str]) -> str:
    """cwd 路径的最后一段（文件夹名），用于分组统计。"""
    if not cwd:
        return "(unknown)"
    return cwd.rstrip("\\/").rsplit("\\", 1)[-1].rsplit("/", 1)[-1] or "(unknown)"


class Aggregates:
    """围绕一个 Store 的只读聚合（带 TTL 缓存）。"""

    def __init__(self, store: Store, ttl: float = _TTL_SECONDS):
        self.store = store
        self._ttl = ttl
        self._cache: dict[str, tuple[float, Any]] = {}

    # ------------------------------------------------------------------
    # 缓存
    # ------------------------------------------------------------------

    def invalidate(self) -> None:
        self._cache.clear()

    def _cached(self, key: str):
        hit = self._cache.get(key)
        if hit and (time.time() - hit[0]) < self._ttl:
            return hit[1]
        return None

    def _store(self, key: str, value: Any) -> Any:
        self._cache[key] = (time.time(), value)
        return value

    # ------------------------------------------------------------------
    # 公开聚合
    # ------------------------------------------------------------------

    def meta(self) -> dict[str, Any]:
        hit = self._cached("meta")
        if hit is not None:
            return hit
        conn = self.store.conn
        total_conv = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        total_ev = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        tool_calls = conn.execute("SELECT COUNT(*) FROM events WHERE role='tool'").fetchone()[0]
        models_count = conn.execute(
            "SELECT COUNT(DISTINCT model) FROM conversations WHERE model != ''"
        ).fetchone()[0]
        workspaces_count = len(self.workspaces_raw())
        row = conn.execute(
            "SELECT MIN(created_at), MAX(updated_at) FROM conversations"
        ).fetchone()
        avg = round(total_ev / total_conv, 1) if total_conv else 0
        max_ev = conn.execute(
            "SELECT MAX(cnt) FROM (SELECT COUNT(*) cnt FROM events GROUP BY source, conversation_id)"
        ).fetchone()[0]
        value = {
            "totalConversations": total_conv,
            "totalEvents": total_ev,
            "toolCalls": tool_calls,
            "modelsCount": models_count,
            "workspacesCount": workspaces_count,
            "dateRange": [row[0] or 0, row[1] or 0],
            "avgEventsPerConv": avg,
            "maxEventsInConv": max_ev or 0,
        }
        return self._store("meta", value)

    def sources(self) -> list[dict[str, Any]]:
        hit = self._cached("sources")
        if hit is not None:
            return hit
        rows = self.store.conn.execute(
            "SELECT source, COUNT(*) AS c FROM conversations GROUP BY source ORDER BY c DESC"
        ).fetchall()
        palette = {
            "zcode": "#4F46E5", "opencode": "#059669", "qwen": "#D97706",
            "hermes": "#7C3AED", "workbuddy": "#DB2777", "dsh": "#0891B2",
            "qodercn": "#DC2626",
        }
        value = [
            {"source": r["source"], "count": r["c"], "color": palette.get(r["source"], "#6B7280")}
            for r in rows
        ]
        return self._store("sources", value)

    def roles(self) -> list[dict[str, Any]]:
        hit = self._cached("roles")
        if hit is not None:
            return hit
        rows = self.store.conn.execute(
            "SELECT role, COUNT(*) AS c FROM events GROUP BY role ORDER BY c DESC"
        ).fetchall()
        palette = {
            "user": "#4F46E5", "assistant": "#059669", "tool": "#0891B2",
            "reasoning": "#D97706", "patch": "#DB2777", "shell": "#DC2626",
            "meta": "#6B7280",
        }
        value = [
            {"role": r["role"], "count": r["c"], "color": palette.get(r["role"], "#9CA3AF")}
            for r in rows
        ]
        return self._store("roles", value)

    def models(self) -> list[dict[str, Any]]:
        hit = self._cached("models")
        if hit is not None:
            return hit
        rows = self.store.conn.execute(
            "SELECT model, COUNT(*) AS c FROM conversations WHERE model != '' "
            "GROUP BY model ORDER BY c DESC LIMIT 20"
        ).fetchall()
        return self._store("models", [{"model": r["model"], "count": r["c"]} for r in rows])

    def daily_trend(self) -> list[dict[str, Any]]:
        """按天活跃会话数（以事件时间为准）。"""
        hit = self._cached("trend")
        if hit is not None:
            return hit
        rows = self.store.conn.execute(
            "SELECT date(time,'unixepoch','localtime') AS d, COUNT(DISTINCT conversation_id) AS c "
            "FROM events WHERE time IS NOT NULL GROUP BY d ORDER BY d"
        ).fetchall()
        return self._store("trend", [{"date": r["d"], "count": r["c"]} for r in rows])

    def source_role(self) -> dict[str, dict[str, int]]:
        hit = self._cached("source_role")
        if hit is not None:
            return hit
        rows = self.store.conn.execute(
            "SELECT e.source AS s, e.role AS r, COUNT(*) AS c "
            "FROM events e JOIN conversations c ON c.source=e.source AND c.id=e.conversation_id "
            "GROUP BY s, r"
        ).fetchall()
        out: dict[str, dict[str, int]] = {}
        for r in rows:
            out.setdefault(r["s"], {})[r["r"]] = r["c"]
        return self._store("source_role", out)

    def workspaces_raw(self) -> list[str]:
        """去重后的工作空间名（cwd 最后一段）——与 conversations.workspace 口径一致。"""
        rows = self.store.conn.execute("SELECT DISTINCT cwd FROM conversations").fetchall()
        return sorted({_workspace_of(r["cwd"]) for r in rows if r["cwd"]})

    def stats_bundle(self, db_path: Path | None = None) -> dict[str, Any]:
        """一次性返回 meta+stats 全量（前端首个请求）。"""
        workspaces = self.workspaces_raw()
        return {
            "meta": {**self.meta(), "generatedAt": int(time.time())},
            "stats": {
                "sources": self.sources(),
                "roles": self.roles(),
                "cwds": workspaces,
                "models": self.models(),
                "dailyTrend": self.daily_trend(),
                "sourceRole": self.source_role(),
            },
        }

    def facets(self) -> dict[str, Any]:
        return {
            "sources": [s["source"] for s in self.sources()],
            "workspaces": self.workspaces_raw(),
            "models": [m["model"] for m in self.models()],
            "dateRange": self.meta()["dateRange"],
        }

    def folders(self, source: str | None = None) -> list[dict[str, Any]]:
        groups: dict[str, dict[str, int]] = {}
        for c in self.store.list_conversations(source):
            ws = _workspace_of(c["cwd"])
            groups.setdefault(ws, {})
            groups[ws][c["source"]] = groups[ws].get(c["source"], 0) + 1
        result = [
            {"workspace": ws, "total": sum(v.values()), "bySource": dict(sorted(v.items()))}
            for ws, v in groups.items()
        ]
        result.sort(key=lambda x: -x["total"])
        return result
