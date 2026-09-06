"""QoderCN Adapter。

数据位置：~/.qoder-cn/cache/projects/<project>/conversation-history/<hash>/<hash>.jsonl
每行: {role: "user"|"assistant", message: {content: [{type:"text", text:"..."}, ...]}}
内容以纯文本为主（也可能含 tool block），统一走 message_text 可读化。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from agentmemhub.models import Event, normalize_role, message_text, _to_epoch, renumber
from .base import AgentAdapter


class QoderCnAdapter(AgentAdapter):
    source = "qodercn"
    label = "QoderCN"

    def candidate_paths(self) -> list[Path]:
        return [Path.home() / ".qoder-cn"]

    def list_sessions(self, path: Path) -> Optional[list[dict[str, Any]]]:
        """轻量清单：rglob + stat，不读文件内容（增量对比用）。"""
        out: dict[str, float] = {}
        try:
            for fp in path.rglob("*.jsonl"):
                mtime = _to_epoch(fp.stat().st_mtime) or 0
                sid = fp.parent.name
                out[sid] = max(out.get(sid, 0.0), mtime)
        except OSError:
            return None
        return [{"id": k, "updated_at": v} for k, v in out.items()]

    def load(self, path: Path, only_ids: Optional[set[str]] = None) -> list[dict[str, Any]]:
        jsonl_files = list(path.rglob("*.jsonl"))
        sessions_map: dict[str, dict] = {}
        # 会话级 updated_at = 所有贡献文件的 mtime 最大值（增量对比锚：
        # 只用首文件 mtime 时，同会话其他文件更新会永远判"已变化"）
        sid_mtime: dict[str, float] = {}

        for fp in jsonl_files:
            sid = fp.parent.name
            if only_ids is not None and sid not in only_ids:
                continue  # 增量：未变化会话不读文件
            msgs: list[Event] = []
            mtime = _to_epoch(fp.stat().st_mtime) or 0
            sid_mtime[sid] = max(sid_mtime.get(sid, 0.0), mtime)
            # 当前轮锚：最近一条 user 行的 src_id（源无父链，只能按行序分桶）
            turn_key: str | None = None
            try:
                with open(fp, encoding="utf-8", errors="ignore") as f:
                    for ln, line in enumerate(f, 1):
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
                        if role == "user":
                            turn_key = f"line:{sid}#{ln}"
                        # 行号作为稳定锚（文件重写会漂移，属数据本身重建）
                        msgs.append(Event(role=role, time=mtime, content=content,
                                          src_id=f"line:{sid}#{ln}", turn_key=turn_key,
                                          raw_json=json.dumps(o, ensure_ascii=False)))
            except Exception:
                continue

            if not msgs:
                continue
            # 以上下层目录作为会话 id（对话目录名）
            if sid not in sessions_map:
                sessions_map[sid] = {
                    "source": self.source, "id": sid,
                    "title": msgs[0].content[:40] if msgs[0].role == "user" else "",
                    "cwd": "",
                    "created_at": mtime, "updated_at": sid_mtime[sid],
                    "model": "", "meta": {"file": str(fp)}, "events": renumber(msgs),
                }
            else:
                sessions_map[sid]["events"] = renumber(sessions_map[sid]["events"] + msgs)
                sessions_map[sid]["updated_at"] = sid_mtime[sid]

        return list(sessions_map.values())
