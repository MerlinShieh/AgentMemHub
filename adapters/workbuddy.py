"""WorkBuddy Adapter。

数据位置：~/.workbuddy/workbuddy.db（会话元数据）+ ~/.workbuddy/audit-log/*.jsonl（Shell 命令审计）

说明：WorkBuddy 的完整对话消息散落在客户端本地存储中不易直接读取；
本 Adapter 采用"最小可用"策略：
  - 会话元数据来自 workbuddy.db 的 sessions 表（标题/工作目录/时间/模型）
  - 会话内事件来自 audit-log（命令安全审计日志，含 Shell 命令执行）
  这样至少能保留"会话清单 + Shell 执行"（符合全量事件流中 shell 的需求）。
后续可扩展从客户端 blob/local_storage 提取完整 user/assistant 文本。
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from event_model import Event, _to_epoch, renumber
from .base import AgentAdapter


class WorkBuddyAdapter(AgentAdapter):
    source = "workbuddy"
    label = "WorkBuddy"

    def candidate_paths(self) -> list[Path]:
        home = Path.home()
        env = os.environ.get("WORKBUDDY_HOME", "").strip()
        paths: list[Path] = []
        if env:
            paths.append(Path(env) / "workbuddy.db")
        paths.append(home / ".workbuddy" / "workbuddy.db")
        return paths

    def load(self, path: Path) -> list[dict[str, Any]]:
        db_dir = path.parent if path.name == "workbuddy.db" else path
        conn = None
        sessions_list: list[dict[str, Any]] = []
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            conn.execute("PRAGMA query_only=ON")
            conn.row_factory = sqlite3.Row
            has_sessions = any(
                r[0] == "sessions" for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            )
            if has_sessions:
                rows = conn.execute("SELECT * FROM sessions").fetchall()
                for r in rows:
                    sessions_list.append({
                        "source": self.source,
                        "id": r["id"],
                        "title": r["title"] or r["custom_title"] or "",
                        "cwd": r["cwd"] or "",
                        "created_at": _to_epoch(r["created_at"]),
                        "updated_at": _to_epoch(r["updated_at"] or r["last_activity_at"]),
                        "model": r["model"] or "",
                        "meta": {"status": r["status"], "mode": r["mode"],
                                 "project_id": r["project_id"]},
                        "events": [],
                    })
        finally:
            if conn:
                conn.close()

        # 收集 audit-log，关联到会话
        self._attach_shell_events(db_dir, sessions_list)
        return sessions_list

    def _attach_shell_events(self, db_dir: Path, sessions_list: list[dict]) -> None:
        audit_dir = Path(os.environ.get("WORKBUDDY_AUDIT_DIR", str(db_dir / "audit-log")))
        if not audit_dir.is_dir():
            return
        # sessionId → session index
        idx = {s["id"]: s for s in sessions_list}
        # 兜底容器：审计中有但 sessions 表里没有的会话
        for audit in audit_dir.glob("*.jsonl"):
            try:
                with open(audit, encoding="utf-8", errors="ignore") as f:
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
                        sid = o.get("sessionId")
                        if not sid:
                            continue
                        ev = self._audit_to_event(o)
                        if ev is None:
                            continue
                        if sid not in idx:
                            idx[sid] = {
                                "source": self.source, "id": sid, "title": "",
                                "cwd": "", "created_at": self._min_time(o),
                                "updated_at": _to_epoch(o.get("timestamp")), "model": "",
                                "meta": {"from": "audit-log"}, "events": [],
                            }
                            sessions_list.append(idx[sid])
                        idx[sid]["events"].append(ev)
                        # 更新更新时间
                        t = _to_epoch(o.get("timestamp"))
                        if t and (not idx[sid]["updated_at"] or t > idx[sid]["updated_at"]):
                            idx[sid]["updated_at"] = t
            except Exception:
                continue

        for s in sessions_list:
            if s["events"]:
                s["events"] = renumber(s["events"])

    @staticmethod
    def _audit_to_event(o: dict) -> Event | None:
        event_type = o.get("eventType") or ""
        command = o.get("commandPreview") or o.get("messageKey") or ""
        ts = _to_epoch(o.get("timestamp"))
        raw = json.dumps(o, ensure_ascii=False)
        # Shell / 工具执行类
        if "command" in str(event_type).lower() or "shell" in str(event_type).lower() or command:
            if command:
                return Event(role="tool", time=ts,
                             tool_name="shell",
                             tool_input={"command": command},
                             tool_status=str(o.get("decision") or "executed"),
                             raw_json=raw)
        # 其他审计事件：meta 保底
        if event_type:
            return Event(role="meta", time=ts,
                         content=f"[{event_type}] {command}".strip(),
                         raw_json=raw)
        return None

    @staticmethod
    def _min_time(o: dict) -> float | None:
        return _to_epoch(o.get("timestamp"))
