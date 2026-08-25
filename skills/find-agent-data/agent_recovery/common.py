"""Shared read-only helpers for evidence-backed transcript recovery."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


SYSTEM_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL | re.IGNORECASE)
USER_QUERY_RE = re.compile(r"<user_query>(.*?)</user_query>", re.DOTALL | re.IGNORECASE)
ATTACHED_FILES_RE = re.compile(r"<attached_files>.*?</attached_files>", re.DOTALL | re.IGNORECASE)
SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)(api[_-]?key\s*[:=]\s*)[^\s,;]+"),
)
SKIP_CONTENT_TYPES = {
    "thinking",
    "reasoning",
    "tool_use",
    "tool_result",
    "function",
    "function_call",
    "function_result",
    "computer_initialize_state",
    "server_tool_use",
}


def sqlite_ro(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def sqlite_tables(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    try:
        conn = sqlite_ro(path)
        try:
            return {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
        finally:
            conn.close()
    except (OSError, sqlite3.DatabaseError):
        return set()


def epoch(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    if isinstance(value, (int, float)):
        number = float(value)
        while number > 10_000_000_000:
            number /= 1000
        return number
    text = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        try:
            return epoch(float(text))
        except ValueError:
            return 0.0


def content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        pieces: list[str] = []
        for item in value:
            if isinstance(item, str):
                pieces.append(item)
                continue
            if not isinstance(item, dict):
                continue
            if str(item.get("type") or "").casefold() in SKIP_CONTENT_TYPES:
                continue
            text = item.get("text")
            if isinstance(text, str):
                pieces.append(text)
        return "\n".join(piece for piece in pieces if piece)
    if isinstance(value, dict):
        for key in ("text", "content", "value"):
            if key in value:
                return content_text(value[key])
    return ""


def extract_message(payload: dict[str, Any]) -> tuple[str, str]:
    containers: list[dict[str, Any]] = []
    for key in ("message", "data"):
        value = payload.get(key)
        if isinstance(value, dict):
            containers.append(value)
    containers.append(payload)

    role = ""
    text = ""
    for item in containers:
        candidate_role = str(item.get("role") or item.get("type") or "").casefold()
        if candidate_role in {"human", "user"}:
            role = "user"
        elif candidate_role in {"assistant", "ai"}:
            role = "assistant"
        if not text:
            text = content_text(item.get("content"))
        if role and text:
            break
    return role, text


def clean_user_text(text: str) -> str:
    text = SYSTEM_REMINDER_RE.sub("", text)
    queries = USER_QUERY_RE.findall(text)
    if queries:
        text = queries[-1]
    else:
        text = ATTACHED_FILES_RE.sub("", text)
    return text.strip()


def redact_secrets(text: str) -> str:
    value = text
    for pattern in SECRET_PATTERNS:
        if "api" in pattern.pattern.casefold():
            value = pattern.sub(r"\1[REDACTED]", value)
        else:
            value = pattern.sub("[REDACTED]", value)
    return value


def redact_preview(text: str, max_chars: int) -> str:
    value = redact_secrets(text).strip()
    if len(value) > max_chars:
        return value[:max_chars].rstrip() + "…"
    return value


def evidence_for_line(raw: str, line_number: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "line": line_number,
        "event_id": payload.get("uuid") or payload.get("id") or payload.get("event_id") or None,
        "sha256_16": hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16],
    }


def parse_transcript(path: Path) -> dict[str, Any]:
    """Parse a JSONL transcript into safe user/assistant messages plus evidence."""
    messages: list[dict[str, Any]] = []
    invalid_lines = 0
    cwd = ""
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
                if not cwd and payload.get("cwd"):
                    cwd = str(payload["cwd"])
                role, text = extract_message(payload)
                if role == "user":
                    text = clean_user_text(text)
                if role not in {"user", "assistant"} or not text.strip():
                    continue
                messages.append(
                    {
                        "role": role,
                        "text": text.strip(),
                        "timestamp": epoch(payload.get("timestamp")),
                        "evidence": evidence_for_line(stripped, line_number, payload),
                    }
                )
    except OSError as exc:
        return {
            "status": "unreadable",
            "error": f"{type(exc).__name__}: {exc}",
            "message_count": 0,
            "user_messages": 0,
            "assistant_messages": 0,
            "invalid_lines": invalid_lines,
            "messages": [],
            "cwd": cwd,
            "first_evidence": None,
            "last_evidence": None,
        }

    return {
        "status": "readable",
        "message_count": len(messages),
        "user_messages": sum(item["role"] == "user" for item in messages),
        "assistant_messages": sum(item["role"] == "assistant" for item in messages),
        "invalid_lines": invalid_lines,
        "messages": messages,
        "cwd": cwd,
        "first_evidence": messages[0]["evidence"] if messages else None,
        "last_evidence": messages[-1]["evidence"] if messages else None,
    }


def rank_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ranked = sorted(
        candidates,
        key=lambda item: (
            int(item.get("message_count", 0)),
            int((item.get("last_evidence") or {}).get("line", 0)),
            int(item.get("modified_ns", 0)),
            str(item.get("path") or ""),
        ),
        reverse=True,
    )
    for index, item in enumerate(ranked):
        item["selected"] = index == 0
    return ranked
