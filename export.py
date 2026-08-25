"""AgentMemHub 导出器。

把 store 中的统一会话导出为：
- JSONL：每行一个事件（跨 Agent 统一结构，机器可读/可回流）
- Markdown：每个会话一个 .md（人类可读，含 tool/reasoning/patch 渲染）
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from event_model import Event, events_to_markdown
from store import Store


def export_jsonl(store: Store, out_dir: Path, source: Optional[str] = None) -> int:
    """导出 JSONL：每个会话一个 <source>__<id>.jsonl，每行一个事件。

    返回导出的会话数。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    convs = store.list_conversations(source)
    count = 0
    for c in convs:
        events = store.get_events(c["source"], c["id"])
        if not events:
            continue
        safe_id = _safe(c["id"])
        path = out_dir / f"{c['source']}__{safe_id}.jsonl"
        lines = [json.dumps(e.to_dict(), ensure_ascii=False, default=str) for e in events]
        path.write_text("\n".join(lines), encoding="utf-8")
        count += 1
    return count


def export_markdown(store: Store, out_dir: Path, source: Optional[str] = None) -> int:
    """导出 Markdown：每个会话一个 <source>__<id>.md，含 tool/reasoning/patch 渲染。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    convs = store.list_conversations(source)
    count = 0
    for c in convs:
        events = store.get_events(c["source"], c["id"])
        if not events:
            continue
        md = _conversation_markdown(c, events)
        safe_id = _safe(c["id"])
        path = out_dir / f"{c['source']}__{safe_id}.md"
        path.write_text(md, encoding="utf-8")
        count += 1
    return count


def _conversation_markdown(conv, events: list[Event]) -> str:
    header = f"# [{conv['source']}] {conv['title'] or conv['id']}"
    parts = [header, ""]
    parts.append(f"- 来源: {conv['source']} | 会话ID: {conv['id']}")
    if conv["cwd"]:
        parts.append(f"- 工作目录: `{conv['cwd']}`")
    if conv["model"]:
        parts.append(f"- 模型: {conv['model']}")
    parts.append(f"- 事件数: {len(events)}")
    parts.append("")
    parts.extend(events_to_markdown(events).splitlines(keepends=False))
    return "\n".join(parts)

def _safe(name: str) -> str:
    import re
    return re.sub(r'[\\/:*?"<>|]', "_", name)[:120]
