"""增量水位与变更集（delta）状态：<data_dir>/watermarks.json。

把一次 ingest 产出的「变更会话集」保存下来，供下游阶段（clean / push /
score）按需单独处理，避免每个环节都全量扫描。

version 1 结构：
{
  "version": 1,
  "updated_at": 1786000000.0,
  "last_ingest": {
      "ts": 1786000000.0,
      "sources": {"zcode": {"ts": ..., "counts": {"added": 1, "updated": 2,
                                                  "unchanged": 40}}}
  },
  "delta": {
      "ingest_ts": 1786000000.0,
      "oversized": false,
      "cap": 5000,
      "conversations": [{"source": "zcode", "id": "abc", "status": "updated"}]
  },
  "pending_score": ["trac_..."],
  "consumed": {"clean": 1786000000.0, "push": 1786000000.0}
}

扩展点：新的流水线阶段（导出 / 通知 / 多库同步…）读 delta、在自己的
consumed 键登记时间戳即可接入，无需改动 ingest。文件缺失/损坏 = 无水位，
下一次 ingest 仍能跑（对比基准是库本身），下游回退全量扫描。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

VERSION = 1
DEFAULT_CAP = 5000


def state_path(data_dir: Path) -> Path:
    return Path(data_dir) / "watermarks.json"


def load_state(data_dir: Path) -> dict[str, Any]:
    """读水位状态；文件缺失/损坏时返回全默认（下游回退全量，不致命）。"""
    st: dict[str, Any] = {
        "version": VERSION,
        "updated_at": 0.0,
        "last_ingest": {"ts": 0.0, "sources": {}},
        "delta": {"ingest_ts": 0.0, "oversized": False,
                  "cap": DEFAULT_CAP, "conversations": []},
        "pending_score": [],
        "consumed": {},
    }
    try:
        raw = json.loads(state_path(data_dir).read_text(encoding="utf-8"))
    except Exception:
        return st
    if not isinstance(raw, dict):
        return st
    for key in ("updated_at", "last_ingest", "delta", "pending_score", "consumed"):
        v = raw.get(key)
        if v is not None:
            st[key] = v
    st["version"] = VERSION
    li = st.setdefault("last_ingest", {})
    li.setdefault("ts", 0.0)
    li.setdefault("sources", {})
    dl = st.setdefault("delta", {})
    dl.setdefault("ingest_ts", 0.0)
    dl.setdefault("oversized", False)
    dl.setdefault("cap", DEFAULT_CAP)
    dl.setdefault("conversations", [])
    st.setdefault("pending_score", [])
    st.setdefault("consumed", {})
    return st


def save_state(data_dir: Path, st: dict[str, Any]) -> None:
    """原子写回（tmp + replace，防写一半损坏）。"""
    p = state_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def record_ingest(st: dict[str, Any], *, ts: Optional[float] = None,
                  source_counts: Optional[dict[str, dict]] = None,
                  changed: Optional[list[dict[str, str]]] = None,
                  cap: int = DEFAULT_CAP) -> dict[str, Any]:
    """ingest 成功后落水位与 delta（变更集超 cap → oversized，下游回退全量）。

    合并规则：上一轮 delta 若尚未被任何下游阶段消费（clean/push 都没跑），
    其条目并入本轮（同 (source,id) 以新状态为准）——防止「连续 ingest 两次
    才 sync」时丢变更；一旦 clean 或 push 消费过上一轮，残留不再保留
    （score 走独立的 pending_score 队列，与此无关）。
    """
    ts = ts if ts is not None else time.time()
    changed = list(changed or [])
    prev = st.get("delta") or {}
    prev_ts = float(prev.get("ingest_ts") or 0)
    consumed = st.get("consumed") or {}
    prev_consumed = any(float(v or 0) >= prev_ts for v in consumed.values())
    if prev_ts and not prev.get("oversized") and not prev_consumed:
        seen = {(c.get("source"), c.get("id")) for c in changed}
        for c in prev.get("conversations") or []:
            if (c.get("source"), c.get("id")) not in seen:
                changed.append(dict(c))
    oversized = len(changed) > cap
    st["version"] = VERSION
    st["updated_at"] = ts
    st["last_ingest"] = {"ts": ts, "sources": source_counts or {}}
    st["delta"] = {
        "ingest_ts": ts,
        "oversized": oversized,
        "cap": cap,
        "conversations": [] if oversized else changed,
    }
    return st


def pending_for(st: dict[str, Any], stage: str) -> Optional[list[dict[str, str]]]:
    """某阶段的待处理会话列表。

    返回 None = 该阶段应回退全量（无 delta / oversized / 状态异常）；
    返回 []  = 当前 delta 已被该阶段消费过，本轮无事可做；
    返回列表 = 需处理的 [{source, id, status}]。
    """
    delta = st.get("delta") or {}
    if not delta or delta.get("oversized"):
        return None
    ing_ts = float(delta.get("ingest_ts") or 0)
    if ing_ts <= 0:
        return None
    consumed = float((st.get("consumed") or {}).get(stage) or 0)
    if consumed >= ing_ts:
        return []
    return [dict(c) for c in (delta.get("conversations") or [])
            if isinstance(c, dict) and c.get("source") and c.get("id")]


def mark_consumed(st: dict[str, Any], stage: str, ts: Optional[float] = None) -> None:
    """阶段处理完成：登记消费时间戳。

    默认取当前 delta 的 ingest_ts（与 pending_for 的对比口径一致）。
    注意：阶段处理与 mark 之间若又发生了一次 ingest，本次登记会把新 delta
    一起标记为已消费——CLI 串行流程无此问题；看板并发任务请避免与 ingest
    同时跑同一阶段。
    """
    delta_ts = float((st.get("delta") or {}).get("ingest_ts") or 0)
    st.setdefault("consumed", {})[stage] = float(ts if ts is not None else delta_ts)


def add_pending_score(st: dict[str, Any], trace_ids: list[str],
                      cap: int = 20000) -> int:
    """push 后追加待评分 trace id（去重、超 cap 丢最旧）。返回当前队列长度。"""
    if not trace_ids:
        return len(st.get("pending_score") or [])
    merged = list(dict.fromkeys((st.get("pending_score") or []) + list(trace_ids)))
    if len(merged) > cap:
        merged = merged[-cap:]
    st["pending_score"] = merged
    return len(merged)


def take_pending_score(st: dict[str, Any]) -> list[str]:
    """取出并清空待评分队列（score --pending 消费）。"""
    ids = list(st.get("pending_score") or [])
    st["pending_score"] = []
    return ids
