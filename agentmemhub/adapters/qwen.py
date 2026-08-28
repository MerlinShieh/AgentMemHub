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

from agentmemhub.models import Event, _to_epoch, renumber, is_system_inject, message_text
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
        # 统一配置 agents.qwen 可覆盖（默认官方路径 ~/.qwen）
        try:
            from agentmemhub import config
            override = config.config().agent_path(self.source)
        except Exception:
            override = None
        if override is not None and override.exists():
            return override
        p = Path.home() / ".qwen"
        return p if p.exists() else None

    def load(self, path: Path) -> list[dict[str, Any]]:
        # 收集所有 chats/*.jsonl（按大小排，跳过 usage_record/系统文件）
        # 注意：projects/ 下的文件会被 path.rglob 和 (path/"projects").rglob 双重复收集
        # ——必须按 resolve() 去重，否则同一会话事件双倍入库
        seen: set[Path] = set()
        for root in (path / "projects", path):
            if root.is_dir():
                for fp in root.rglob("*.jsonl"):
                    seen.add(fp.resolve())
        # 过滤明显的非对话文件
        jsonl_files = [Path(p) for p in sorted(seen)
                       if p.name not in ("usage_record.jsonl",) and "usage" not in p.name]

        sessions_map: dict[str, dict] = {}
        for fp in jsonl_files:
            try:
                turn_by_uuid: dict[str, str] = {}
                with open(fp, encoding="utf-8", errors="ignore") as f:
                    for ln, line in enumerate(f, 1):
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
                        # 稳定锚 + 轮次归属：沿 parentUuid 追到根 user 行
                        uuid = str(o.get("uuid") or f"idx:{ln}")
                        parent = str(o["parentUuid"]) if o.get("parentUuid") else None
                        msg_obj = o.get("message") if isinstance(o.get("message"), dict) else {}
                        parts = msg_obj.get("parts")
                        if typ == "user":
                            # 系统注入（task-notification / TodoWrite 提醒等）不算轮起点
                            if not is_system_inject(_parts_text(parts)):
                                turn_key = uuid
                            else:
                                turn_key = turn_by_uuid.get(parent) or parent
                        else:
                            turn_key = turn_by_uuid.get(parent) or parent
                        turn_by_uuid[uuid] = turn_key or uuid
                        ev = self._line_to_event(
                            o, fp, src_id=f"line:{uuid}", turn_key=turn_key,
                            parent_id=parent,
                        )
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

    def _line_to_event(self, o: dict, fp: Path, *,
                       src_id: str | None = None, turn_key: str | None = None,
                       parent_id: str | None = None):
        typ = o.get("type")
        ts = _to_epoch(o.get("timestamp"))
        msg = o.get("message") or {}
        parts = msg.get("parts") if isinstance(msg, dict) else None
        raw = json.dumps(o, ensure_ascii=False)
        kw = {"src_id": src_id, "turn_key": turn_key, "parent_id": parent_id,
              "raw_json": raw}

        if typ == "system":
            if o.get("subtype") in _SKIP_SYSTEM_SUBTYPES:
                return None
            return Event(role="meta", time=ts, is_system=True, **kw)
        if typ == "user":
            text = _parts_text(parts) or (fp.stem if not parts else "")
            if not text:
                return None
            return Event(role="user", time=ts, content=text,
                         is_system=True if is_system_inject(text) else None, **kw)
        if typ == "assistant":
            text = _parts_text(parts)
            if not text:
                return None
            tool_call_id = _first_function_call_id(parts)
            return Event(role="assistant", time=ts, content=text,
                         model=o.get("model") or None,
                         tool_call_id=tool_call_id, **kw)
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
                         tool_status="completed", **kw)
        # 其他类型：保底 meta
        return Event(role="meta", time=ts, **kw)


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
            elif "functionCall" in p:
                texts.append(f"[Tool: {p['functionCall'].get('name', '?')}]")
            elif "functionResponse" in p:
                texts.append("[Tool Result]")
        elif isinstance(p, str):
            texts.append(p)
    return "\n".join(texts).strip()


def _first_function_call_id(parts: Any) -> str | None:
    """assistant parts 里第一个 functionCall 的 id（工具调用关联锚）。"""
    if not isinstance(parts, list):
        return None
    for p in parts:
        if isinstance(p, dict):
            fc = p.get("functionCall")
            if isinstance(fc, dict) and fc.get("id"):
                return str(fc["id"])
    return None
