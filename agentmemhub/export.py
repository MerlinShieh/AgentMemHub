"""AgentMemHub 导出器。

把 store 中的统一会话导出为：
- JSONL：每行一个事件（跨 Agent 统一结构，机器可读/可回流）
- Markdown：每个会话一个 .md（人类可读，含 tool/reasoning/patch 渲染）
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from agentmemhub.models import Event, events_to_markdown
from agentmemhub.store import Store


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
        _atomic_write(path, "\n".join(lines))
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
        _atomic_write(path, md)
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

def _safe(name: str, *, limit: int = 120) -> str:
    """把会话 id 变成可用的文件名。

    要挡住的东西比"替换几个符号"多：
    - **Windows 保留设备名**（`CON` / `PRN` / `COM1`…）：带扩展名也不行，
      `CON.jsonl` 依然是设备名，写入表现为静默失败或打到控制台。
    - **尾部点与空格**：资源管理器能建，多数 API 会吃掉，于是两个不同 id
      落成同一个文件。
    - **超长截断**：必须留指纹，否则前 120 字符相同的两个 id 互相覆盖，
      后写的赢且没有任何提示。
    """
    import hashlib
    import re
    raw = str(name or "")
    clean = re.sub(r'[\\/:*?"<>|]', "_", raw).strip(" .")
    if not clean:
        clean = "unnamed"
    if clean.split(".")[0].upper() in _WIN_RESERVED:
        clean = f"_{clean}"
    if len(clean) > limit:
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
        clean = f"{clean[:limit - 9]}_{digest}"
    return clean


#: Windows 保留设备名 —— 带扩展名同样不可用
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL",
                 *(f"COM{i}" for i in range(1, 10)),
                 *(f"LPT{i}" for i in range(1, 10))}


def _atomic_write(path: Path, text: str) -> None:
    """先写临时文件再替换：导出中途被打断，不该留下半个 .jsonl。"""
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
