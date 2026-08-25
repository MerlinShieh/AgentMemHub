"""Hermes Agent Adapter。

数据位置（Windows）：%LOCALAPPDATA%\hermes\state.db
表结构：
    sessions(id, title, started_at, message_count, model, cwd, system_prompt, ...)
    messages(id, session_id, role, content, tool_calls, tool_name, reasoning_content,
             timestamp, token_count, ...)
role ∈ {user, assistant, tool}；assistant 可带 tool_calls(JSON) 与 reasoning_content。
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from event_model import Event, normalize_role, _to_epoch, renumber
from .base import AgentAdapter


class HermesAdapter(AgentAdapter):
    source = "hermes"
    label = "Hermes Agent"

    def candidate_paths(self) -> list[Path]:
        home = Path.home()
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        paths = [
            local / "hermes" / "state.db",
            home / ".hermes" / "state.db",
            home / ".hermes" / "state.sqlite",
        ]
        return paths

    def load(self, path: Path) -> list[dict[str, Any]]:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.execute("PRAGMA query_only=ON")
        conn.row_factory = sqlite3.Row

        sessions: list[dict[str, Any]] = []
        try:
            for s in conn.execute("SELECT * FROM sessions").fetchall():
                sid = s["id"]
                events = self._load_events(conn, sid, s)
                sessions.append({
                    "source": self.source,
                    "id": sid,
                    "title": s["title"] or "",
                    "cwd": s["cwd"] or "",
                    "created_at": _to_epoch(s["started_at"]),
                    "updated_at": _to_epoch(s["last_activity_at"]) or _to_epoch(s["ended_at"]),
                    "model": s["model"] or "",
                    "meta": {"chat_type": s["chat_type"], "profile": s["profile_name"],
                             "system_prompt_hash": s["system_prompt_hash"]},
                    "events": events,
                })
        finally:
            conn.close()
        return sessions

    def _load_events(self, conn: sqlite3.Connection, session_id: str,
                     sess: sqlite3.Row) -> list[Event]:
        events: list[Event] = []
        order = "timestamp, id"
        if any(r[1] == "sequence" for r in conn.execute("PRAGMA table_info(messages)")):
            order = "sequence, timestamp"
        msgs = conn.execute(
            f"SELECT * FROM messages WHERE session_id = ? ORDER BY {order}",
            (session_id,),
        ).fetchall()
        for m in msgs:
            role = normalize_role(m["role"], default="assistant")
            ts = _to_epoch(m["timestamp"])
            raw = json.dumps(dict(m), ensure_ascii=False, default=str)

            if role == "user":
                events.append(Event(role="user", time=ts, content=m["content"] or "", raw_json=raw))
            elif role == "tool":
                events.append(Event(role="tool", time=ts,
                                    tool_name=m["tool_name"] or "",
                                    tool_output=m["content"] or "",
                                    tool_status="completed", raw_json=raw))
            else:  # assistant
                events.append(Event(role="assistant", time=ts, content=m["content"] or "",
                                    model=sess["model"] or None, raw_json=raw))
                # 思维链
                if m["reasoning_content"]:
                    events.append(Event(role="reasoning", time=ts,
                                        content=m["reasoning_content"],
                                        reasoning=m["reasoning_content"], raw_json=raw))
                # 工具调用（assistant 发起）
                for tc in self._tool_calls(m["tool_calls"]):
                    events.append(Event(role="tool", time=ts,
                                        tool_name=tc.get("name", ""),
                                        tool_input=tc.get("arguments"),
                                        tool_status="initiated",
                                        tool_call_id=tc.get("call_id") or tc.get("id"),
                                        raw_json=raw))
        return events

    @staticmethod
    def _tool_calls(tc: Any) -> list[dict]:
        if not tc:
            return []
        if isinstance(tc, str):
            try:
                tc = json.loads(tc)
            except Exception:
                return []
        if not isinstance(tc, list):
            tc = [tc]
        result = []
        for item in tc:
            if not isinstance(item, dict):
                continue
            fn = item.get("function") or {}
            if isinstance(fn, dict):
                args = fn.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = args
                result.append({
                    "name": fn.get("name", ""),
                    "arguments": args if isinstance(args, dict) else (str(args) if args else None),
                    "call_id": item.get("call_id") or item.get("id"),
                })
            else:
                result.append(item)
        return result
