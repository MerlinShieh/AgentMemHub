"""Hub-wide transcript readability. Vendor stores stay untouched."""

from __future__ import annotations

import json
import re
from typing import Any

SKIP_PARTS = {
    "tool_use",
    "tool_result",
    "function",
    "function_call",
    "computer_initialize_state",
    "server_tool_use",
    "permission-result",
    "permission_result",
    "tool-todowrite",
    "tool-bash",
    "tool-read",
    "tool-edit",
    "tool-write",
    "tool-grep",
    "tool-glob",
    "tool-task",
}
THINKING_PARTS = {"thinking", "reasoning", "tool-thinking", "redacted_thinking"}
TEXT_PARTS = {"text", "output_text", "input_text", "markdown"}
WRAPPER_RE = re.compile(
    r"<(?:system-reminder|environment_context|recommended_plugins|local-command-caveat|"
    r"task-notification|teammate-message)\b[^>]*>.*?</(?:system-reminder|environment_context|"
    r"recommended_plugins|local-command-caveat|task-notification|teammate-message)>",
    re.DOTALL | re.IGNORECASE,
)


def _as_payload(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        return value
    text = str(value or "").strip()
    if not text or text[:1] not in "[{":
        return None
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, (list, dict)) else None


def _clean(value: Any) -> str:
    return WRAPPER_RE.sub("", str(value or "").replace("\x00", "")).strip()


def _part_kind(part: dict[str, Any]) -> str:
    return str(part.get("type") or part.get("kind") or "").casefold()


def _thinking_text(part: dict[str, Any]) -> str:
    for key in ("thinking", "text", "content"):
        value = part.get(key)
        if isinstance(value, str) and value.strip():
            return _clean(value)
    incoming = part.get("input")
    if isinstance(incoming, dict):
        return _clean(incoming.get("text") or incoming.get("thinking") or "")
    return ""


def _collect(value: Any, texts: list[str], thinkings: list[str]) -> None:
    if isinstance(value, str):
        cleaned = _clean(value)
        if cleaned:
            texts.append(cleaned)
        return
    if isinstance(value, list):
        for item in value:
            _collect(item, texts, thinkings)
        return
    if not isinstance(value, dict):
        return
    kind = _part_kind(value)
    if kind in THINKING_PARTS or kind.endswith("-thinking"):
        thought = _thinking_text(value)
        if thought:
            thinkings.append(thought)
        return
    if kind in SKIP_PARTS or kind.startswith("tool-"):
        return
    if kind in TEXT_PARTS or (not kind and isinstance(value.get("text"), str)):
        cleaned = _clean(value.get("text") or "")
        if cleaned:
            texts.append(cleaned)
        return
    nested = value.get("content")
    if isinstance(nested, (list, dict, str)):
        _collect(nested, texts, thinkings)


def readable_turn_text(role: str, value: Any) -> str:
    """Normalize one turn for the hub reader. Thinking is tagged for 完全体."""
    payload = _as_payload(value)
    texts: list[str] = []
    thinkings: list[str] = []
    if payload is not None:
        _collect(payload, texts, thinkings)
    else:
        cleaned = _clean(value)
        if cleaned:
            texts.append(cleaned)
    if role == "user":
        texts = [item for item in texts if item and not item.casefold().startswith("<system-reminder")]
        thinkings = []
    pieces: list[str] = []
    if texts:
        pieces.append("\n\n".join(texts))
    if thinkings:
        pieces.append("<thinking>\n" + "\n\n".join(thinkings) + "\n</thinking>")
    return "\n\n".join(pieces).strip()
