"""Grok Build recovery: summary.json index + updates.jsonl user/assistant text."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .common import (
    epoch,
    evidence_for_line,
    redact_preview,
    redact_secrets,
)

HOME = Path.home()
GROK_MAP_SCHEMA = "find-agent-data/grok-map-v1"
MAX_UPDATES_BYTES = 100 * 1024 * 1024


def default_home() -> Path:
    return Path(os.environ.get("GROK_HOME") or HOME / ".grok")


def sessions_root(home: Path | None = None) -> Path:
    root = Path(home) if home is not None else default_home()
    sessions = root / "sessions"
    return sessions if sessions.is_dir() else root


def discovery_locations(home: Path | None = None) -> list[dict[str, Any]]:
    root = Path(home) if home is not None else default_home()
    return [
        {
            "role": "transcript_root",
            "path": root / "sessions",
            "storage": "jsonl",
            "evidence": True,
        },
        {
            "role": "session_index",
            "path": root / "sessions" / "session_search.sqlite",
            "storage": "sqlite",
            "evidence": False,
        },
        {
            "role": "runtime_root",
            "path": root,
            "storage": "mixed",
            "evidence": False,
        },
    ]


def summary_files(home: Path | None = None) -> list[Path]:
    root = sessions_root(home)
    if not root.is_dir():
        return []
    result: list[Path] = []
    for summary in root.rglob("summary.json"):
        if any(part.casefold() == "subagents" for part in summary.parts):
            continue
        result.append(summary)
    return result


def _chunk_text(content: Any) -> str:
    if isinstance(content, dict) and isinstance(content.get("text"), str):
        return content["text"]
    if isinstance(content, str):
        return content
    return ""


def parse_updates(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {
            "status": "missing",
            "message_count": 0,
            "user_messages": 0,
            "assistant_messages": 0,
            "invalid_lines": 0,
            "messages": [],
            "first_evidence": None,
            "last_evidence": None,
        }
    try:
        size = path.stat().st_size
    except OSError as exc:
        return {
            "status": "unreadable",
            "error": f"{type(exc).__name__}: {exc}",
            "message_count": 0,
            "user_messages": 0,
            "assistant_messages": 0,
            "invalid_lines": 0,
            "messages": [],
            "first_evidence": None,
            "last_evidence": None,
        }
    if size > MAX_UPDATES_BYTES:
        return {
            "status": "unreadable",
            "error": f"updates.jsonl exceeds {MAX_UPDATES_BYTES} bytes",
            "message_count": 0,
            "user_messages": 0,
            "assistant_messages": 0,
            "invalid_lines": 0,
            "messages": [],
            "first_evidence": None,
            "last_evidence": None,
        }

    messages: list[dict[str, Any]] = []
    invalid_lines = 0
    current_role = ""
    current_parts: list[str] = []
    current_ts = 0.0
    current_raw = ""
    current_line = 0
    current_payload: dict[str, Any] = {}

    def flush() -> None:
        nonlocal current_role, current_parts, current_ts, current_raw, current_line, current_payload
        text = redact_secrets("".join(current_parts)).strip()
        if current_role and text:
            messages.append(
                {
                    "role": current_role,
                    "text": text,
                    "timestamp": current_ts,
                    "evidence": evidence_for_line(current_raw, current_line, current_payload),
                }
            )
        current_role = ""
        current_parts = []
        current_ts = 0.0
        current_raw = ""
        current_line = 0
        current_payload = {}

    try:
        with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
            for line_number, raw in enumerate(handle, start=1):
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    payload = json.loads(stripped)
                except json.JSONDecodeError:
                    invalid_lines += 1
                    continue
                if not isinstance(payload, dict):
                    continue
                params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
                update = params.get("update") if isinstance(params.get("update"), dict) else {}
                kind = str(update.get("sessionUpdate") or "").casefold()
                meta = update.get("_meta") if isinstance(update.get("_meta"), dict) else {}
                outer_meta = payload.get("_meta") if isinstance(payload.get("_meta"), dict) else {}
                stamp = epoch(
                    payload.get("timestamp")
                    or meta.get("agentTimestampMs")
                    or outer_meta.get("agentTimestampMs")
                )
                text = _chunk_text(update.get("content"))
                evidence_payload = {
                    "uuid": meta.get("eventId") or outer_meta.get("eventId"),
                }
                if kind == "user_message_chunk" and text:
                    if current_role == "assistant":
                        flush()
                    if current_role != "user":
                        current_role = "user"
                        current_ts = stamp
                        current_raw = stripped
                        current_line = line_number
                        current_payload = evidence_payload
                    current_parts.append(text)
                    continue
                if kind == "agent_message_chunk" and text:
                    if current_role == "user":
                        flush()
                    if current_role != "assistant":
                        current_role = "assistant"
                        current_ts = stamp
                        current_raw = stripped
                        current_line = line_number
                        current_payload = evidence_payload
                    current_parts.append(text)
                    continue
                if kind == "turn_completed" and current_role == "assistant":
                    flush()
    except OSError as exc:
        return {
            "status": "unreadable",
            "error": f"{type(exc).__name__}: {exc}",
            "message_count": len(messages),
            "user_messages": sum(item["role"] == "user" for item in messages),
            "assistant_messages": sum(item["role"] == "assistant" for item in messages),
            "invalid_lines": invalid_lines,
            "messages": messages,
            "first_evidence": messages[0]["evidence"] if messages else None,
            "last_evidence": messages[-1]["evidence"] if messages else None,
        }
    flush()
    return {
        "status": "readable",
        "message_count": len(messages),
        "user_messages": sum(item["role"] == "user" for item in messages),
        "assistant_messages": sum(item["role"] == "assistant" for item in messages),
        "invalid_lines": invalid_lines,
        "messages": messages,
        "first_evidence": messages[0]["evidence"] if messages else None,
        "last_evidence": messages[-1]["evidence"] if messages else None,
    }


def _read_summary(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
    if payload.get("parent_session_id") or info.get("parent_session_id"):
        return None
    session_id = str(info.get("id") or path.parent.name).strip()
    if not session_id:
        return None
    return {
        "session_id": session_id,
        "session_title": str(payload.get("generated_title") or payload.get("session_summary") or ""),
        "cwd": str(info.get("cwd") or ""),
        "created": epoch(payload.get("created_at")),
        "updated": epoch(payload.get("last_active_at") or payload.get("updated_at")),
        "model": str(payload.get("current_model_id") or ""),
        "agent_name": str(payload.get("agent_name") or ""),
        "summary_path": str(path.resolve(strict=False)),
        "updates_path": str(path.with_name("updates.jsonl").resolve(strict=False)),
    }


@dataclass
class RecoveredGrokSession:
    session_id: str
    title: str = ""
    cwd: str = ""
    created: float = 0.0
    updated: float = 0.0
    model: str = ""
    source_kind: str = "metadata_only"
    selected_path: Path | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    parsed: dict[str, Any] = field(default_factory=dict)


def recover_all(
    home: Path | None = None,
    *,
    include_messages: bool = True,
    include_preview: bool = False,
    preview_chars: int = 500,
) -> list[RecoveredGrokSession]:
    recovered: list[RecoveredGrokSession] = []
    for summary_path in summary_files(home):
        metadata = _read_summary(summary_path)
        if not metadata:
            continue
        parsed = parse_updates(summary_path.with_name("updates.jsonl"))
        messages = list(parsed.get("messages") or []) if include_messages or include_preview else []
        if include_preview and messages:
            first_user = next((row for row in messages if row["role"] == "user"), None)
            latest_user_index = max(
                (index for index, row in enumerate(messages) if row["role"] == "user"),
                default=-1,
            )
            latest_user = messages[latest_user_index] if latest_user_index >= 0 else None
            latest_assistant = next(
                (
                    row
                    for row in reversed(messages[latest_user_index + 1 :])
                    if row["role"] == "assistant"
                ),
                None,
            )

            def preview(row: dict[str, Any] | None) -> dict[str, Any] | None:
                if not row:
                    return None
                return {
                    "text": redact_preview(row["text"], preview_chars),
                    "evidence": row["evidence"],
                }

            parsed["preview"] = {
                "original_goal": preview(first_user),
                "latest_user": preview(latest_user),
                "latest_assistant_after_user": preview(latest_assistant),
            }
        updates = summary_path.with_name("updates.jsonl")
        source_kind = "transcript" if parsed.get("message_count") else "metadata_only"
        recovered.append(
            RecoveredGrokSession(
                session_id=metadata["session_id"],
                title=metadata["session_title"],
                cwd=metadata["cwd"],
                created=metadata["created"],
                updated=metadata["updated"],
                model=metadata["model"],
                source_kind=source_kind,
                selected_path=updates if updates.is_file() else summary_path,
                messages=messages if include_messages else [],
                metadata=metadata,
                parsed=parsed,
            )
        )
    recovered.sort(key=lambda item: item.updated, reverse=True)
    return recovered


def recover_query(
    *,
    query: str | None = None,
    session_id: str | None = None,
    home: Path | None = None,
    include_preview: bool = False,
    preview_chars: int = 500,
    limit: int = 20,
) -> dict[str, Any]:
    sessions = recover_all(
        home,
        include_messages=include_preview,
        include_preview=include_preview,
        preview_chars=max(50, preview_chars),
    )
    selected = sessions
    if session_id:
        key = session_id.casefold()
        selected = [
            item
            for item in sessions
            if item.session_id.casefold() == key or Path(item.metadata.get("summary_path") or "").parent.name.casefold() == key
        ]
    elif query:
        key = query.casefold()
        selected = [
            item
            for item in sessions
            if key in item.title.casefold()
            or key in item.session_id.casefold()
            or key in item.cwd.casefold()
        ]
    selected = selected[: max(1, limit)]
    payload_sessions = []
    for item in selected:
        chosen = None
        if item.selected_path:
            parsed = dict(item.parsed)
            parsed.pop("messages", None)
            chosen = {
                "path": str(item.selected_path),
                "source_kind": "updates_jsonl" if item.source_kind == "transcript" else "summary_only",
                "match": "exact",
                "selected": True,
                **parsed,
            }
        payload_sessions.append(
            {
                "metadata": item.metadata,
                "source_kind": item.source_kind,
                "candidate_count": 1 if chosen else 0,
                "selected_transcript": chosen,
                "candidates": [chosen] if chosen else [],
            }
        )
    root = Path(home) if home is not None else default_home()
    return {
        "schema": GROK_MAP_SCHEMA,
        "product": "grok",
        "read_only": True,
        "selection_rule": "summary.json index; updates.jsonl user/assistant coverage",
        "paths": {
            "home": str(root.resolve(strict=False)),
            "sessions": str(sessions_root(root).resolve(strict=False)),
        },
        "matched_sessions": len(payload_sessions),
        "sessions": payload_sessions,
        "warnings": [
            "Never read auth.json. session_search.sqlite is derived search text, not a transcript candidate.",
        ],
    }
