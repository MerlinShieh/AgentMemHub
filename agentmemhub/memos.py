"""AgentMemHub → MemOS 桥接器。

把统一事件流（store 中的会话）转换为 MemOS Local Plugin 的导入 bundle
（TraceDTO 格式），可直接 POST 到 MemOS 的 /api/v1/import 生成历史记忆。

粒度设计：
- 每个会话 → 1 个 episode
- 每个"turn"（一条 user 消息 + 其后的 assistant/tool/reasoning）→ 1 条 trace
- trace: userText=用户输入, agentText=最终回复, toolCalls=期间工具, agentThinking=思维链

稳定幂等（v2）：
- trace id 由 src_id 派生（sha256），同一条源事件重复导出 id 不变 → MemOS 按 id 去重
- turn 按事件流顺序以 user 消息为界切分（与 events 表 turn_key 语义一致）；
  系统注入的 user 消息（is_system）直接跳过，不进 bundle
- value 启发式初值：让导入 trace 有正向价值信号（参与检索排序），
  error 轮给负值；后续 MemOS 真实 reward 会覆盖这些静态值

导入路径（MemOS 侧）：
- importBundle：直接落 L1 trace 库（不触发 L2/L3/Skill 进化）——适合历史数据批量导入
- 导入的 embedding 不会自动生成，需 POST /api/v1/embeddings/rebuild 补向量后才可语义检索

用法（见 agentmemhub.py memos 子命令）。
"""
from __future__ import annotations

import hashlib
import json
import urllib.request
from pathlib import Path
from typing import Any, Optional

from agentmemhub.store import Store


def _id(parts: list[str], prefix: str, length: int = 16) -> str:
    """确定性生成合法 id（规避 MemOS 的 FK 约束）。"""
    raw = "|".join(parts)
    return f"{prefix}_{hashlib.sha256(raw.encode()).hexdigest()[:length]}"


def _heuristic_value(tools: list[dict], agent_text: str) -> float:
    """导入 trace 的价值启发式初值（无 reward 可依时给检索排序用的粗糙信号）。

    - 任意 tool error → -0.3（教训类）
    - 有工具 + 较长回复 → 0.6（实干型任务）
    - 有工具 → 0.4
    - 长回复 → 0.35；中等回复 → 0.2；纯寒暄 → 0
    """
    if any(tc.get("errorCode") for tc in tools):
        return -0.3
    a_len = len(agent_text or "")
    if tools:
        return 0.6 if a_len >= 80 else 0.4
    if a_len >= 200:
        return 0.35
    if a_len >= 30:
        return 0.2
    return 0.0


def _session_events_to_traces(source: str, conv: Any,
                              events: list[Any]) -> list[dict[str, Any]]:
    """把统一事件流分割为 MemOS traces（按 user 消息为界，系统注入跳过）。"""
    traces: list[dict[str, Any]] = []
    session_id = _id([source, str(conv["id"])], "semem_session")
    episode_id = _id([source, str(conv["id"])], "semem_ep")

    # 当前轮状态
    user_ev: Optional[Any] = None      # 轮起点 user 事件（is_system 的不开轮）
    bucket: list[Any] = []             # 轮内非 user 事件

    def flush() -> None:
        nonlocal user_ev, bucket
        if user_ev is None and not bucket:
            return
        asst_texts = [b.content or "" for b in bucket if b.role == "assistant"]
        agent_text = "\n".join(t for t in asst_texts if t)
        tools: list[dict] = []
        thinking: list[str] = []
        for b in bucket:
            if b.role == "tool":
                tc: dict[str, Any] = {
                    "name": b.tool_name or "",
                    "input": b.tool_input,
                    "output": b.tool_output,
                    "errorCode": None if b.tool_status != "error" else "error",
                }
                if b.tool_call_id:
                    tc["toolCallId"] = b.tool_call_id
                if b.tool_output:
                    tc["output"] = b.tool_output
                tools.append(tc)
            elif b.role == "reasoning":
                if b.content:
                    thinking.append(b.content)

        # 时间轴：user 事件时间优先，否则桶内首个事件
        ts_ev = user_ev if user_ev is not None else (bucket[0] if bucket else None)
        ts = int((ts_ev.time or 0) * 1000) if ts_ev and ts_ev.time else 0
        # 稳定锚：user src_id 优先，旧数据回退 seq
        anchor = user_ev.src_id if user_ev is not None and user_ev.src_id \
            else f"seq:{user_ev.seq}" if user_ev is not None else "seq:0"
        value = _heuristic_value(tools, agent_text)

        traces.append({
            "id": _id([source, str(conv["id"]), anchor], "trac"),
            "episodeId": episode_id,
            "sessionId": session_id,
            "ts": ts,
            "turnId": ts,
            "userText": (user_ev.content or "") if user_ev is not None else "",
            "agentText": agent_text,
            "summary": (agent_text or (user_ev.content if user_ev else "") or "")[:200],
            "toolCalls": tools,
            "agentThinking": "\n".join(thinking) if thinking else None,
            "value": value,
            "alpha": 0.3,
            "priority": max(value, 0.0),
        })
        user_ev, bucket = None, []

    for ev in events:
        role = ev.role
        if role == "user" and not ev.is_system:
            flush()
            user_ev = ev
            bucket = []
        elif role == "user":  # 系统注入消息（TodoWrite / DSH runtime context 等）不进 bundle
            continue
        else:
            bucket.append(ev)

    flush()
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
    """POST bundle 到 MemOS /api/v1/import（走网关统一出口，自动登录带 cookie）。

    引擎 viewer 设了密码时 urllib 直连会 401——统一走 engine_request
    （登录缓存 cookie、401 自动重登），CLI/看板/MCP memory_save 全部受益。
    """
    from agentmemhub import memos_daemon
    return memos_daemon.engine_request("POST", "/api/v1/import",
                                       body=bundle, timeout=60, base=base_url)


def rebuild_embeddings(base_url: str = "http://127.0.0.1:18800",
                       mode: str = "repair",
                       limit: int = 500,
                       max_rounds: int = 200) -> dict:
    """POST /api/v1/embeddings/rebuild 补齐缺失向量（导入的 trace 无 embedding，无法语义检索）。

    服务端每批只处理 limit 条并返回 done/nextOffset——此处分页循环直到全部补齐。
    mode: repair=只补 null 向量（默认，快）；rebuild=全部重算。
    返回汇总：{rounds, processed, updated, failed, done, statsAfter}。
    """
    url = base_url.rstrip("/") + "/api/v1/embeddings/rebuild"
    total = {"rounds": 0, "processed": 0, "updated": 0, "failed": 0,
             "done": False, "statsAfter": None}
    for _ in range(max_rounds):
        body = json.dumps({"mode": mode, "limit": limit}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=600) as resp:
            d = json.loads(resp.read().decode("utf-8"))
        total["rounds"] += 1
        for k in ("processed", "updated", "failed"):
            total[k] += d.get(k, 0)
        total["done"] = bool(d.get("done"))
        total["statsAfter"] = d.get("statsAfter")
        if total["done"]:
            break
    return total


def _now_ms() -> int:
    import time
    return int(time.time() * 1000)
