"""DeepSeek Harness (DSH) Adapter。

数据位置：~/.dsh/sessions/<workspace>/<session-id>/session.jsonl.zstd（zstd 压缩 JSONL）
事件类型（解压后逐行 JSON，含 seq/time/data）：
    session           会话头（元数据）
    user/message      用户输入：data.content=[{type:"text",text}]，data.role=user
    assistant/message AI 回复：data.message.content=[{type:reasoning|text|tool_use|tool-result,...}]
    tool/call         工具调用：data.{callId,name,arguments(JSON)}
    tool/result       工具结果：data.message.content=[{type:"tool-result",content:[{type:"text",text}]}]
    session/title     会话标题：data.title
    (流式分块/边界/请求元数据 → 跳过，避免与完整消息重复)
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from agentmemhub.models import Event, _to_epoch, message_text, renumber, normalize_role
from .base import AgentAdapter

try:
    import zstandard
except ImportError:  # pragma: no cover
    zstandard = None

# 无对话价值的类型 → 跳过
_SKIP_TYPES = {
    "session",                      # 会话头，只取元数据
    "permission/preset", "sandbox/mode", "approval/policy", "config", "init",
    "system", "startup", "heartbeat",
    "agent/inbox/spliced",          # 注入的上下文
    "request/header", "request/context", "request/message",
    "llm/retry", "llm/retry-started",
    "session/title-llm-request",
    "turn/start", "turn/end", "step/start", "step/end",   # 边界
    "assistant/chunk", "reasoning-chunks", "text-chunks", # 流式分块（assistant/message 里有完整版）
    "tool-call-chunks",
}


class DshAdapter(AgentAdapter):
    source = "dsh"
    label = "DeepSeek Harness"

    def candidate_paths(self) -> list[Path]:
        return [Path.home() / ".dsh"]

    def load(self, path: Path) -> list[dict[str, Any]]:
        if zstandard is None:
            raise RuntimeError("zstandard 未安装：pip install zstandard")
        sessions_dir = path / "sessions"
        if not sessions_dir.is_dir():
            return []

        sessions_map: dict[str, dict] = {}
        for zf in sessions_dir.rglob("*.jsonl.zstd"):
            try:
                self._read_zstd(zf, sessions_map)
            except Exception:
                continue
        return list(sessions_map.values())

    def _read_zstd(self, zf: Path, sessions_map: dict[str, dict]) -> None:
        dctx = zstandard.ZstdDecompressor()
        with open(zf, "rb") as f:
            with dctx.stream_reader(f) as reader:
                text = reader.read().decode("utf-8", errors="ignore")
        sid = zf.parent.name
        events: list[Event] = []
        sess_meta: dict[str, Any] = {"file": str(zf)}
        # 当前轮锚：最近一条真实 user/message 的 data.id（plugin 注入不算轮起点）
        turn_key: Optional[str] = None
        for ln, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            if not isinstance(o, dict):
                continue
            if o.get("type") == "session":
                sid = o.get("id") or sid
                sess_meta["createdAt"] = o.get("createdAt")
                sess_meta["cwd"] = o.get("cwd")
            elif o.get("type") == "session/title":
                title = (o.get("data") or {}).get("title")
                if title:
                    sess_meta["title"] = title
            else:
                data = o.get("data") or {}
                # 真实 user/message 开启新一轮（plugin 注入的 user/message 不更新轮锚）
                src_kind = (data.get("source") or {}).get("kind") if isinstance(data.get("source"), dict) else None
                if o.get("type") == "user/message" and src_kind in (None, "user"):
                    turn_key = str(data.get("id")) or None
                for ev in self._line_to_events(o, turn_key=turn_key, line_no=ln):
                    if ev is not None:
                        events.append(ev)
        if not events:
            return
        s = sessions_map.get(sid) or {
            "source": self.source, "id": sid, "title": sess_meta.get("title", ""),
            "cwd": sess_meta.get("cwd") or "",
            "created_at": _to_epoch(sess_meta.get("createdAt")),
            "updated_at": None, "model": "", "meta": sess_meta, "events": [],
        }
        if (sess_meta.get("title") or "") and not s["title"]:
            s["title"] = sess_meta["title"]
        s["events"] = renumber(s["events"] + events)
        if not s["title"]:
            for e in s["events"]:
                if e.role == "user" and e.content:
                    s["title"] = e.content[:40]
                    break
        times = [e.time for e in s["events"] if e.time]
        s["updated_at"] = max(times) if times else s["created_at"]
        sessions_map[sid] = s

    def _line_to_events(self, o: dict, turn_key: str | None = None,
                        line_no: int = 1) -> list[Event]:
        typ = o.get("type")
        if typ in _SKIP_TYPES:
            return []
        ts = _to_epoch(o.get("time"))
        raw = json.dumps(o, ensure_ascii=False)
        data = o.get("data") or {}
        seq = str(o.get("seq") or line_no)

        if typ == "user/message":
            role = data.get("role") or "user"
            text = message_text(data.get("content"))
            if not text:
                return []
            # 源级系统注入（DSH system-prompt 快照等 source.kind != user）
            src = data.get("source") if isinstance(data.get("source"), dict) else {}
            src_kind = src.get("kind")
            return [Event(
                role=normalize_role(role, default="user"), time=ts, content=text,
                src_id=f"uc:{data.get('id') or seq}", turn_key=turn_key,
                is_system=bool(src_kind not in (None, "user")),
                raw_json=raw)]

        if typ == "assistant/message":
            msg = data.get("message") or {}
            return self._assistant_content_to_events(
                ts, msg, raw, src_id=f"ac:{data.get('message', {}).get('id') or seq}",
                turn_key=turn_key)

        if typ in ("tool/call", "tool_call"):
            arguments = data.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except Exception:
                    pass
            call_id = data.get("callId") or data.get("id")
            return [Event(role="tool", time=ts,
                          tool_name=data.get("name") or "",
                          tool_input=arguments if isinstance(arguments, dict) else None,
                          tool_status="initiated",
                          tool_call_id=call_id,
                          src_id=f"call:{call_id or seq}", turn_key=turn_key,
                          raw_json=raw)]

        if typ in ("tool/result", "tool_result"):
            msg = data.get("message") or {}
            output = _tool_result_text(msg)
            tool_call_id = (msg.get("source") or {}).get("callId") if isinstance(msg.get("source"), dict) else None
            if not tool_call_id and isinstance(data.get("message"), dict):
                src = data["message"].get("source") or {}
                tool_call_id = src.get("callId")
            return [Event(role="tool", time=ts, tool_output=output or None,
                          tool_status="completed", tool_call_id=tool_call_id,
                          src_id=f"res:{tool_call_id or seq}", turn_key=turn_key,
                          raw_json=raw)] if output else []

        # 其余：meta 保底
        return [Event(role="meta", time=ts, src_id=f"meta:{typ}:{seq}",
                      turn_key=turn_key, raw_json=raw)]

    @staticmethod
    def _assistant_content_to_events(ts, msg, raw, src_id=None, turn_key=None) -> list[Event]:
        """把 assistant message 的 content（可能含 reasoning/text/tool_use）拆为多个事件。"""
        kw = {"src_id": src_id, "turn_key": turn_key, "raw_json": raw}
        content = msg.get("content")
        if not isinstance(content, list):
            text = message_text(content) if content else ""
            return [Event(role="assistant", time=ts, content=text or None, **kw)] if text else []
        events = []
        text_buf: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                text_buf.append(str(block))
                continue
            btype = block.get("type")
            if btype == "reasoning":
                if text_buf:
                    events.append(Event(role="assistant", time=ts,
                                        content="\n".join(text_buf), **kw))
                    text_buf = []
                events.append(Event(role="reasoning", time=ts,
                                    content=block.get("text") or "", **kw))
            elif btype == "text":
                text_buf.append(block.get("text") or "")
            elif btype in ("tool_use", "tool_call"):
                if text_buf:
                    events.append(Event(role="assistant", time=ts,
                                        content="\n".join(text_buf), **kw))
                    text_buf = []
                events.append(Event(role="tool", time=ts,
                                    tool_name=block.get("name") or "",
                                    tool_input=block.get("input"),
                                    tool_call_id=block.get("id") or block.get("callId"),
                                    tool_status="initiated", **kw))
            elif btype == "tool-result":
                events.append(Event(role="tool", time=ts,
                                    tool_output=_tool_result_text(block),
                                    tool_call_id=block.get("toolCallId"),
                                    tool_status="completed", **kw))
        if text_buf:
            events.append(Event(role="assistant", time=ts,
                                content="\n".join(text_buf), **kw))
        return events


def _tool_result_text(msg: Any) -> str:
    """从 tool-result 消息/块中提取文本输出。"""
    content = msg.get("content") if isinstance(msg, dict) else None
    if not isinstance(content, list):
        return str(content) if content else ""
    texts = []
    for block in content:
        if isinstance(block, dict):
            if block.get("type") == "text":
                texts.append(str(block.get("text", "")))
            elif block.get("type") == "tool-result":
                inner = _tool_result_text(block)
                if inner:
                    texts.append(inner)
            else:
                t = message_text(block)
                if t:
                    texts.append(t)
        elif isinstance(block, str):
            texts.append(block)
    return "\n".join(t for t in texts if t).strip()
