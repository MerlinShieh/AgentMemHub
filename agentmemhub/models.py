"""AgentMemHub 统一事件模型。

标准会话事件流：user / assistant / tool / reasoning / patch / shell / meta。

设计原则：
1. 全量保留 — 工具链、思维链、Shell 执行、代码补丁都不丢弃（原项目只留 user/assistant）。
2. 最大兼容 — 不同 Agent 原始结构不同，统一映射后允许空 key，原始 JSON 存 raw_json 无损保底。
3. 机器可读 — 纯 JSON，无嵌套对话对象，流式处理友好。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

EVENT_VERSION = 1

# 系统注入消息识别（伪装成 role=user 的 harness 自动注入）。
# 与前端 index.html isSystemMessage 同规则，另补 task-notification / system-reminder。
# adapter 层打 is_system 标记 → MemOS 导出跳过这些轮，Web 渲染归 system。
_SYSTEM_INJECT_RE = re.compile(
    r"^The TodoWrite tool"
    r"|^Current runtime context"
    r"|Current DSH file policy\s*:"
    r"|Any available operation enforced by the DSH file sandbox"
    r"|^System\s*:|^\[System\s*:|^<system>"
    r"|^You are an AI assistant|^Available tools|^The following tools"
    r"|^<\|.*\|>|^System\b"
    r'|^\{\s*"type"\s*:\s*"session/'
    r"|^<antml:"
    r"|^<task-notification\b|^<system-reminder\b"
    r"|environment_context|runtime\.context|file\.policy|approval\.policy",
    re.IGNORECASE,
)


def is_system_inject(text: Any) -> bool:
    """检测 user 消息文本是否为 harness 系统注入（TodoWrite 提醒/上下文快照等）。"""
    if not text:
        return False
    return bool(_SYSTEM_INJECT_RE.search(str(text).strip()))


# ---------------------------------------------------------------------------
# 标准事件
# ---------------------------------------------------------------------------

@dataclass
class Event:
    """一条标准对话事件（每行一个，构成完整事件流）。"""

    role: str                      # user | assistant | tool | reasoning | patch | shell | meta
    seq: int = 0                   # 会话内排列序号（renumber 会重排，默认 0）
    time: Optional[float] = None   # Unix 时间戳（秒）
    content: Optional[str] = None  # user/assistant/reasoning 的正文
    parent_id: Optional[str] = None

    # 记忆桥接锚（MemOS 导出幂等与轮次分组用）
    src_id: Optional[str] = None   # 事件在源数据里的稳定定位（re-ingest 不变）
    turn_key: Optional[str] = None # 所属轮次的锚（= 该轮根 user 消息的 src_id）
    is_system: Optional[bool] = None  # 源级可识别的系统注入消息（DSH plugin / Qwen system）

    # tool
    tool_name: Optional[str] = None
    tool_input: Optional[dict] = None
    tool_output: Optional[str] = None
    tool_status: Optional[str] = None   # completed | error | cancelled
    tool_call_id: Optional[str] = None

    # reasoning（思维链）
    reasoning: Optional[str] = None

    # patch（代码变更）
    patch_file: Optional[str] = None
    patch_diff: Optional[str] = None

    # shell（Shell 执行）
    shell_cmd: Optional[str] = None
    shell_output: Optional[str] = None
    shell_cwd: Optional[str] = None

    # 元数据
    model: Optional[str] = None
    tokens: Optional[dict] = None

    # 无损保底：原始事件 JSON
    raw_json: Optional[str] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        return {k: v for k, v in d.items() if v is not None}

    def to_json(self) -> str:
        return json.dumps(
            {"v": EVENT_VERSION, **self.to_dict()},
            ensure_ascii=False,
        )


def conversation(session: dict[str, Any]) -> dict[str, Any]:
    """标准会话元数据模型（Adapter 统一输出）。

    session: {source, id, title, cwd, created_at, updated_at, model, events: [Event]}
    """
    events = session.get("events") or []
    created = session.get("created_at") or (events[0].time if events else 0)
    updated = session.get("updated_at") or (
        max(e.time for e in events if e.time) if events else created
    )
    return {
        "source": session.get("source", ""),
        "id": str(session.get("id", "")),
        "title": session.get("title", "") or "",
        "cwd": session.get("cwd", "") or "",
        "created_at": created or 0,
        "updated_at": updated or created or 0,
        "model": session.get("model", "") or "",
        "event_count": len(events),
        "roles": sorted({e.role for e in events}),
        "session_meta": session.get("meta") or {},
    }


# ---------------------------------------------------------------------------
# 消息 → 事件 转换辅助
# ---------------------------------------------------------------------------

def normalize_role(role: Any, default: str = "assistant") -> str:
    """把各种 Agent 原始 role 归一化为标准 role。"""
    r = str(role or "").strip().lower()
    mapping = {
        "user": "user", "human": "user", "用户": "user",
        "assistant": "assistant", "ai": "assistant", "model": "assistant",
        "助手": "assistant", "gpt": "assistant",
        "tool": "tool", "function": "tool", "tool_call": "tool",
        "toolcall": "tool", "tool_calls": "tool",
        "reasoning": "reasoning", "thinking": "reasoning", "thought": "reasoning",
        "思维链": "reasoning", "reflect": "reasoning",
        "patch": "patch", "code_patch": "patch", "修改": "patch",
        "shell": "shell", "bash": "shell", "command": "shell",
        "system": "meta", "developer": "meta", "meta": "meta",
    }
    return mapping.get(r, default)


def message_text(content: Any) -> str:
    """把 content（可能是 str / list[dict] / dict）归一为纯文本。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") in ("tool_use", "tool_call"):
                    parts.append(f"[Tool: {block.get('name', '?')}]")
                elif block.get("type") in ("tool_result",):
                    parts.append("[Tool Result]")
                elif block.get("text"):
                    parts.append(str(block.get("text")))
            else:
                parts.append(str(block))
    elif isinstance(content, dict):
        if content.get("text"):
            parts.append(str(content.get("text")))
        elif content.get("content"):
            return message_text(content.get("content"))
    return "\n".join(parts).strip()


def find_in_obj(obj: Any, *keys: str) -> Any:
    """在多级 dict 中查找第一个命中的 key（最大兼容性）。"""
    if isinstance(obj, dict):
        for k in keys:
            if k in obj and obj[k] is not None:
                return obj[k]
        for v in obj.values():
            r = find_in_obj(v, *keys)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_in_obj(v, *keys)
            if r is not None:
                return r
    return None


def to_event(
    role: Any,
    content: Any = None,
    *,
    time: Any = None,
    seq: int = 0,
    tool_name: Any = None,
    tool_input: Any = None,
    tool_output: Any = None,
    tool_status: Any = None,
    src_id: Any = None,
    turn_key: Any = None,
    is_system: Any = None,
    raw: Any = None,
    **extra: Any,
) -> Event:
    """从任意 Agent 原始消息构造标准 Event（空字段自动省略）。"""
    e = Event(
        role=normalize_role(role),
        seq=seq,
        time=_to_epoch(time),
        content=message_text(content) if content is not None else None,
        tool_name=str(tool_name) if tool_name else None,
        tool_input=tool_input if isinstance(tool_input, dict) else None,
        tool_output=str(tool_output) if tool_output is not None else None,
        tool_status=str(tool_status) if tool_status else None,
        src_id=str(src_id) if src_id is not None else None,
        turn_key=str(turn_key) if turn_key is not None else None,
        is_system=bool(is_system) if is_system is not None else None,
        raw_json=json.dumps(raw, ensure_ascii=False) if raw is not None else None,
        **{k: v for k, v in extra.items() if v is not None},
    )
    return e


def _to_epoch(value: Any) -> Optional[float]:
    """多格式时间 → Unix 秒（处理毫秒/纳秒/ISO 字符串）。"""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 10_000_000_000_000:   # 纳秒
            number /= 1_000_000_000
        elif number > 10_000_000_000:     # 毫秒
            number /= 1000
        return number
    from datetime import datetime
    text = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 事件流工具
# ---------------------------------------------------------------------------

def renumber(events: list[Event], start: int = 1) -> list[Event]:
    """重新编号（按时间戳排序后给定 seq）。"""
    sorted_events = sorted(
        (e for e in events if e is not None),
        key=lambda e: (e.time is None, e.time or 0),
    )
    for i, e in enumerate(sorted_events, start):
        e.seq = i
    return sorted_events


def events_to_markdown(events: list[Event], title: str | None = None) -> str:
    """事件流 → 人类可读 Markdown。title 为 None 时不渲染标题行。"""
    lines = [f"# {title}", ""] if title else []
    role_icons = {
        "user": "👤 用户", "assistant": "🤖 助手", "tool": "🔧 工具",
        "reasoning": "💭 思考", "patch": "📝 修改", "shell": "⚙️ Shell", "meta": "📋 元数据",
    }
    for e in events:
        label = role_icons.get(e.role, e.role)
        time_str = f" ({_fmt_time(e.time)})" if e.time else ""
        lines.append(f"## {label}{time_str}")
        lines.append("")
        if e.role == "tool":
            lines.append(f"**{e.tool_name or '?'}** [{e.tool_status or ''}]")
            if e.tool_input:
                lines.append("```json")
                lines.append(json.dumps(e.tool_input, ensure_ascii=False, indent=2))
                lines.append("```")
            if e.tool_output:
                lines.append("```")
                lines.append(e.tool_output[:2000])
                lines.append("```")
        elif e.role == "patch":
            lines.append(f"**{e.patch_file or '?'}**")
            if e.patch_diff:
                lines.append("```diff")
                lines.append(e.patch_diff[:2000])
                lines.append("```")
        elif e.role == "shell":
            if e.shell_cmd:
                lines.append("```bash")
                lines.append(e.shell_cmd)
                lines.append("```")
            if e.shell_output:
                lines.append("```")
                lines.append(e.shell_output[:2000])
                lines.append("```")
        else:
            if e.content:
                lines.append(e.content)
        lines.append("")
    return "\n".join(lines)


def _fmt_time(ts: float) -> str:
    from datetime import datetime
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")