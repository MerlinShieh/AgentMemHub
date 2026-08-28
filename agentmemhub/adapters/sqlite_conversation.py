"""通用 SQLite 会话 Adapter。

ZCode 和 OpenCode 都用同一套基于 opencode 的数据库结构：
    session(id, project_id, directory, path, title, time_created, time_updated, ...)
    message(id, session_id, time_created, data JSON, sequence)
    part   (id, message_id, session_id, time_created, data JSON, sequence)

message.data: {"role": "user|assistant", "time": {...}, "modelID": "...", "content": ...}
part.data:    {"type": "text|reasoning|tool|patch|step-start|step-finish", ...} 或 {"timelineType":"model_change"}

本 Adapter 把二者统一归一化为全量事件流（含 tool / reasoning / patch）。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

from agentmemhub.models import (Event, normalize_role, _to_epoch, message_text,
                                renumber, is_system_inject)
from .base import AgentAdapter

# part 中无信息量的类型（跳过）
_SKIP_PART_TYPES = {"step-start", "step-finish", "snapshot", "compaction", "attachment",
                    "web-search", "web_search"}
_META_TIMELINE_TYPES = {"model_change", "model-switch", "title", "summary"}


class SqliteConversationAdapter(AgentAdapter):
    """基于 session/message/part 表的通用 adapter（子类只需给 source/label/candidate_paths）。"""

    def load(self, path: Path) -> list[dict[str, Any]]:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.execute("PRAGMA query_only=ON")
        conn.row_factory = sqlite3.Row

        sessions: list[dict[str, Any]] = []
        try:
            for s in conn.execute("SELECT * FROM session").fetchall():
                sid = s["id"]
                meta = {"project_id": s["project_id"], "parent_id": s["parent_id"],
                        "permission": s["permission"], "slug": s["slug"]}
                session_dict = {
                    "source": self.source,
                    "id": sid,
                    "title": s["title"] or "",
                    "cwd": s["directory"] or "",
                    "created_at": _to_epoch(s["time_created"]),
                    "updated_at": _to_epoch(s["time_updated"]),
                    "model": "",
                    "meta": {k: v for k, v in meta.items() if v is not None},
                    "events": [],
                }
                session_dict["events"] = renumber(self._load_events(conn, sid))
                sessions.append(session_dict)
        finally:
            conn.close()
        return sessions

    def _load_events(self, conn: sqlite3.Connection, session_id: str) -> list[Event]:
        events: list[Event] = []
        # 兼容 schema 差异：有的 message/part 有 sequence 列，有的没有
        msg_order = ("sequence, time_created" if self._has_col(conn, "message", "sequence")
                     else "time_created, id")
        part_order = ("sequence, time_created" if self._has_col(conn, "part", "sequence")
                      else "time_created, id")
        # 当前轮锚：最近一条真实 user 消息的 id（系统注入的 user 消息不算轮起点）
        turn_key: Optional[str] = None
        for m in conn.execute(
            f"SELECT * FROM message WHERE session_id = ? ORDER BY {msg_order}",
            (session_id,),
        ).fetchall():
            md = self._safe_json(m["data"])
            msg_role = str(md.get("role", "")).lower()
            msg_id = str(m["id"])
            msg_time = _to_epoch(md.get("time", {}).get("created") if isinstance(md.get("time"), dict) else md.get("time")) or _to_epoch(m["time_created"])
            model = md.get("modelID") or (md.get("model") or {}).get("modelID")

            # 该消息下的 parts
            parts = conn.execute(
                f"SELECT * FROM part WHERE message_id = ? ORDER BY {part_order}",
                (m["id"],),
            ).fetchall()
            # 系统注入检测：user 消息的正文（text part 优先，其次消息级 content）
            is_sys = False
            if msg_role in ("user", "human"):
                probe_parts = []
                for p in parts:
                    pd0 = self._safe_json(p["data"])
                    if str(pd0.get("type") or "").lower() in ("text", "text_stream", "input_text", "output_text"):
                        probe_parts.append(str(pd0.get("text") or ""))
                probe = "\n".join(x for x in probe_parts if x) or \
                    (message_text(md.get("content")) if md.get("content") else "")
                is_sys = is_system_inject(probe)
                if not is_sys:
                    turn_key = msg_id

            for p in parts:
                pd = self._safe_json(p["data"])
                ev = self._part_to_event(pd, msg_role, msg_time, model,
                                         part_id=str(p["id"]), parent_id=msg_id,
                                         turn_key=turn_key,
                                         is_system=True if is_sys else None,
                                         raw=p["data"])
                if ev is not None:
                    events.append(ev)
            # 若消息没有有效 part，仍保留消息本身正文（避免丢内容）
            if not parts and md.get("content"):
                ev = Event(role=normalize_role(msg_role, default="assistant"),
                           seq=0, time=msg_time, content=message_text(md["content"]),
                           parent_id=msg_id, src_id=f"m:{msg_id}", turn_key=turn_key,
                           is_system=True if is_sys else None,
                           model=model or None, raw_json=m["data"])
                events.append(ev)
        return events

    def _part_to_event(self, pd: dict, msg_role: str, msg_time: float, model: Any,
                       part_id: str | None = None, parent_id: str | None = None,
                       turn_key: str | None = None, is_system: bool | None = None,
                       raw: Any = None) -> Optional[Event]:
        typ = str(pd.get("type") or pd.get("timelineType") or "").lower()
        raw_str = raw if isinstance(raw, str) else json.dumps(pd, ensure_ascii=False)

        if typ in _SKIP_PART_TYPES:
            return None

        def _mk(role, **kw):
            return Event(role=role, seq=0, time=msg_time,
                         parent_id=parent_id,
                         src_id=(f"p:{part_id}" if part_id else None),
                         turn_key=turn_key,
                         is_system=is_system if is_system is not None else None,
                         model=model or None, raw_json=raw_str, **kw)

        # timeline 元信息
        if typ in _META_TIMELINE_TYPES or "timelineType" in pd:
            return _mk("meta", content=str(pd.get("display") or ""))

        # text：归属 message role
        if typ in ("text", "text_stream", "input_text", "output_text"):
            role = "user" if msg_role in ("user", "human") else "assistant"
            return _mk(role, content=pd.get("text") or "")

        # reasoning / 思维链
        if typ in ("reasoning", "thinking", "thought", "internal"):
            return _mk("reasoning", content=pd.get("text") or "",
                       reasoning=pd.get("text") or "")

        # tool 调用 / 结果
        if typ in ("tool", "tool_call", "tool_use", "tool_result", "function_call"):
            state = pd.get("state") if isinstance(pd.get("state"), dict) else {}
            tool_name = pd.get("tool") or pd.get("name") or pd.get("toolCallId") or ""
            tool_input = state.get("input") if isinstance(state.get("input"), (dict, str)) else \
                (pd.get("input") if isinstance(pd.get("input"), (dict, str)) else None)
            tool_status = state.get("status") or pd.get("status") or "completed"
            # output 可能在 state / 顶层 / result
            tool_output = state.get("output") or state.get("result") or pd.get("output") or pd.get("result")
            if isinstance(tool_output, (dict, list)):
                tool_output = json.dumps(tool_output, ensure_ascii=False)
            return _mk("tool",
                       tool_name=str(tool_name) if tool_name else None,
                       tool_input=tool_input,
                       tool_output=str(tool_output) if tool_output else None,
                       tool_status=str(tool_status),
                       tool_call_id=pd.get("callID") or pd.get("toolCallId") or None)

        # patch / 代码变更
        if typ in ("patch", "edit", "diff", "file_edit", "textedit"):
            return _mk("patch", patch_file=pd.get("file") or pd.get("path") or pd.get("filename"),
                       patch_diff=pd.get("diff") or pd.get("text") or "")

        # 其余未知类型：保底为 meta，raw_json 无损
        return _mk("meta", content=message_text(pd) or None)

    @staticmethod
    def _has_col(conn: sqlite3.Connection, table: str, col: str) -> bool:
        try:
            return any(r[1] == col for r in conn.execute(f"PRAGMA table_info({table})"))
        except Exception:
            return False

    @staticmethod
    def _safe_json(text: Any) -> dict:
        if isinstance(text, dict):
            return text
        if isinstance(text, str):
            try:
                o = json.loads(text)
                return o if isinstance(o, dict) else {}
            except Exception:
                return {}
        return {}
