"""AgentMemHub → MemOS 桥接器。

把统一事件流（store 中的会话）转换为 MemOS Local Plugin 的导入 bundle
（TraceDTO 格式），可直接 POST 到 MemOS 的 /api/v1/import 生成历史记忆。

粒度设计：
- 每个会话 → 1 个 episode
- 每个"turn"（一条 user 消息 + 其后的 assistant/tool/reasoning）→ 1 条 trace
- trace: userText=用户输入, agentText=最终回复, toolCalls=期间工具, agentThinking=思维链

导入路径（MemOS 侧）：
- importBundle：直接落 L1 trace 库（不触发 L2/L3/Skill 进化）——适合历史数据批量导入
- 后续真实对话仍会触发完整进化链，检索时能命中这些历史 trace

用法（见 agentmemhub.py memos 子命令）。
"""
from __future__ import annotations

import hashlib
import json
import urllib.request
from pathlib import Path
from typing import Any, Optional

from store import Store


def _id(parts: list[str], prefix: str, length: int = 16) -> str:
    """确定性生成合法 id（规避 MemOS 的 FK 约束）。"""
    raw = "|".join(parts)
    return f"{prefix}_{hashlib.sha256(raw.encode()).hexdigest()[:length]}"


def _session_events_to_traces(source: str, conv: Any,
                              events: list[Any]) -> list[dict[str, Any]]:
    """把统一事件流分割为 MemOS traces（按 user 消息为界）。"""
    traces: list[dict[str, Any]] = []
    session_id = _id([source, str(conv["id"])], "semem_session")
    episode_id = _id([source, str(conv["id"])], "semem_ep")

    # 按 user 消息切 turn
    turn_user = None
    turn_asst: list[str] = []
    turn_tools: list[dict] = []
    turn_thinking: list[str] = []
    pending_reasoning: list[str] = []

    def flush(idx: int, ts: Optional[float]) -> None:
        nonlocal turn_user, turn_asst, turn_tools, turn_thinking, pending_reasoning
        if turn_user is None:
            return
        agent_text = "\n".join(a for a in turn_asst if a)
        # 组装 assistant 正文前的 thinking 归入 agentThinking
        traces.append({
            "id": _id([source, str(conv["id"]), str(idx)], "trac"),
            "episodeId": episode_id,
            "sessionId": session_id,
            "ts": int(ts * 1000) if ts else 0,
            "userText": turn_user,
            "agentText": agent_text,
            "summary": (agent_text or turn_user)[:200],
            "toolCalls": turn_tools,
            "agentThinking": "\n".join(turn_thinking) if turn_thinking else None,
        })
        turn_user, turn_asst, turn_tools, turn_thinking, pending_reasoning = None, [], [], [], []

    for ev in events:
        role = ev.role
        if role == "user":
            flush(len(traces), ev.time)
            turn_user = ev.content or ""
        elif role == "assistant":
            turn_asst.append(ev.content or "")
        elif role == "reasoning":
            turn_thinking.append(ev.content or "")
        elif role == "tool":
            tc = {
                "name": ev.tool_name or "",
                "input": ev.tool_input,
                "output": ev.tool_output,
                "errorCode": None if ev.tool_status != "error" else "error",
            }
            if ev.tool_call_id:
                tc["toolCallId"] = ev.tool_call_id
            if ev.tool_output:
                tc["output"] = ev.tool_output
            turn_tools.append(tc)
        # meta / patch 在此粒度暂不单独成 trace（patch 可后续作为 evidence）

    flush(len(traces), events[-1].time if events else None)
    return traces


def build_bundle(store: Store, source: Optional[str] = None) -> dict[str, Any]:
    """从 store 构建 MemOS bundle。"""
    convs = store.list_conversations(source)
    traces: list[dict] = []
    for c in convs:
        events = store.get_events(c["source"], c["id"])
        if not events:
            continue
        try:
            traces.extend(_session_events_to_traces(c["source"], c, events))
        except Exception:
            continue
    return {
        "version": 1,
        "exportedAt": _now_ms(),
        "traces": traces,
        "policies": [],
        "worldModels": [],
        "skills": [],
    }


def write_bundle(bundle: dict[str, Any], out_path: Path) -> None:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")


def push_bundle(bundle: dict[str, Any], base_url: str = "http://127.0.0.1:18800") -> dict:
    """POST bundle 到 MemOS /api/v1/import。返回响应 JSON。"""
    url = base_url.rstrip("/") + "/api/v1/import"
    data = json.dumps(bundle, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _now_ms() -> int:
    import time
    return int(time.time() * 1000)
