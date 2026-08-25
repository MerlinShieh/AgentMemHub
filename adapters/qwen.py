"""Qwen（通义千问 CLI）Adapter。

数据位置：~/.qwen/projects/<slug>/chats/<session>.jsonl
每行: {type, timestamp, sessionId, cwd, version, model?, message:{role, parts:[...]}}
type ∈ user / assistant / tool_result / system（system 多为 ui_telemetry 噪音，忽略）
parts: [{text: "..."} | {functionResponse:{id, name, response:{output}}}]
也兼容项目根目录的直接会话 jsonl。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from event_model import Event, _to_epoch, renumber
from .base import AgentAdapter

# system 类型中无信息量的（遥测/快照）——忽略
_SKIP_SYSTEM_SUBTYPES = {"ui_telemetry", "attribution_snapshot", "file_history_snapshot",
                         "custom_title", "rewind", "at_command", "slash_command"}


class QwenAdapter(AgentAdapter):
    source = "qwen"
    label = "Qwen (千问)"

    def candidate_paths(self) -> list[Path]:
        return [Path.home() / ".qwen"]

    def locate(self) -> Path:
        # chats 目录是主来源；取 .qwen 本身或其 chats 子目录
        p = Path.home() / ".qwen"
        return p if p.exists() else None

    def load(self, path: Path) -> list[dict[str, Any]]:
        # 收集所有 chats/*.jsonl（按大小排，跳过 usage_record/系统文件）
        jsonl_files: list[Path] = []
        for root in (path / "projects", path):
            if root.is_dir():
                jsonl_files.extend(root.rglob("*.jsonl"))
        # 过滤明显的非对话文件
        jsonl_files = [p for p in jsonl_files
                       if p.name not in ("usage_record.jsonl",) and "usage" not in p.name]

        sessions_map: dict[str, dict] = {}
        for fp in jsonl_files:
            try:
                with open(fp, encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            o = json.loads(line)
                            if not isinstance(o, dict):
                                continue
                        except Exception:
                            continue
                        typ = o.get("type")
                        sid = o.get("sessionId") or fp.stem
                        ev = self._line_to_event(o, fp)
                        if ev is None:
                            continue
                        if sid not in sessions_map:
                            sessions_map[sid] = {
                                "source": self.source, "id": sid,
                                "title": "", "cwd": o.get("cwd") or "",
                                "created_at": _to_epoch(o.get("timestamp")),
                                "updated_at": _to_epoch(o.get("timestamp")),
                                "model": o.get("model") or "",
                                "meta": {"file": str(fp)}, "events": [],
                            }
                        sessions_map[sid]["events"].append(ev)
                        if o.get("cwd"):
                            sessions_map[sid]["cwd"] = o["cwd"]
                        if o.get("timestamp"):
                            sessions_map[sid]["updated_at"] = _to_epoch(o["timestamp"])
            except Exception:
                continue

        # 重编号 + 填充 title
        sessions = []
        for s in sessions_map.values():
            s["events"] = renumber(s["events"])
            if not s["title"]:
                for e in s["events"]:
                    if e.role == "user" and e.content:
                        s["title"] = e.content[:40]
                        break
            sessions.append(s)
        return sessions

    def _line_to_event(self, o: dict, fp: Path):
        typ = o.get("type")
        ts = _to_epoch(o.get("timestamp"))
        msg = o.get("message") or {}
        parts = msg.get("parts") if isinstance(msg, dict) else None
        raw = json.dumps(o, ensure_ascii=False)

        if typ == "system":
            if o.get("subtype") in _SKIP_SYSTEM_SUBTYPES:
                return None
            return Event(role="meta", time=ts, content=json.dumps(o, ensure_ascii=False)[:500],
                         raw_json=raw)
        if typ == "user":
            text = _parts_text(parts) or (fp.stem if not parts else "")
            return Event(role="user", time=ts, content=text, raw_json=raw) if text else None
        if typ == "assistant":
            text = _parts_text(parts)
            return Event(role="assistant", time=ts, content=text, model=o.get("model") or None,
                         raw_json=raw) if text else None
        if typ == "tool_result":
            # functionResponse 提取
            name, output = "", ""
            for part in (parts or []):
                if isinstance(part, dict) and "functionResponse" in part:
                    fr = part["functionResponse"]
                    name = fr.get("name", "")
                    resp = fr.get("response") or {}
                    output = resp.get("output") if isinstance(resp, dict) else str(resp) if resp else ""
                    if isinstance(output, (dict, list)):
                        output = json.dumps(output, ensure_ascii=False)
            return Event(role="tool", time=ts, tool_name=name or None,
                         tool_output=str(output) if output else None,
                         tool_status="completed", raw_json=raw)
        # 其他类型：保底 meta
        return Event(role="meta", time=ts, content=json.dumps(o, ensure_ascii=False)[:500],
                     raw_json=raw)


def _parts_text(parts: Any) -> str:
    if not parts:
        return ""
    if not isinstance(parts, list):
        return str(parts)
    texts = []
    for p in parts:
        if isinstance(p, dict):
            if p.get("text"):
                texts.append(str(p["text"]))
            elif "functionResponse" in p:
                texts.append("[Tool Result]")
        elif isinstance(p, str):
            texts.append(p)
    return "\n".join(texts).strip()
