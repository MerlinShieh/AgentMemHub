"""QoderCN Adapter。

数据位置：~/.qoder-cn/cache/projects/<project>/conversation-history/<hash>/<hash>.jsonl
每行: {role: "user"|"assistant", message: {content: [{type:"text", text:"..."}, ...]}}
内容以纯文本为主（也可能含 tool block），统一走 message_text 可读化。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from event_model import Event, normalize_role, message_text, _to_epoch, renumber
from .base import AgentAdapter


class QoderCnAdapter(AgentAdapter):
    source = "qodercn"
    label = "QoderCN"

    def candidate_paths(self) -> list[Path]:
        return [Path.home() / ".qoder-cn"]

    def load(self, path: Path) -> list[dict[str, Any]]:
        jsonl_files = list(path.rglob("*.jsonl"))
        sessions_map: dict[str, dict] = {}

        for fp in jsonl_files:
            msgs: list[Event] = []
            mtime = _to_epoch(fp.stat().st_mtime)
            try:
                with open(fp, encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            o = json.loads(line)
                        except Exception:
                            continue
                        if not isinstance(o, dict):
                            continue
                        role = normalize_role(o.get("role"), default="assistant")
                        msg_body = o.get("message") or {}
                        content = message_text(msg_body.get("content") if isinstance(msg_body, dict) else msg_body)
                        if not content:
                            continue
                        msgs.append(Event(role=role, time=mtime, content=content,
                                          raw_json=json.dumps(o, ensure_ascii=False)))
            except Exception:
                continue

            if not msgs:
                continue
            # 以上下层目录作为会话 id（对话目录名）
            sid = fp.parent.name
            if sid not in sessions_map:
                sessions_map[sid] = {
                    "source": self.source, "id": sid,
                    "title": msgs[0].content[:40] if msgs[0].role == "user" else "",
                    "cwd": "",
                    "created_at": mtime, "updated_at": mtime,
                    "model": "", "meta": {"file": str(fp)}, "events": renumber(msgs),
                }
            else:
                sessions_map[sid]["events"] = renumber(sessions_map[sid]["events"] + msgs)

        return list(sessions_map.values())
