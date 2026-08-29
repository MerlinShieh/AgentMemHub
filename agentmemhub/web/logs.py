"""看板统一操作日志：内存环形缓冲 + JSONL 持久化（数据目录 web.log）。

记录面板控制/点击触发的后端动作（引擎启停、提取/推送任务提交与结果）、
任务实时输出等；前端「操作日志」面板轮询读取。文件留痕便于跨会话追溯。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from agentmemhub import config

_MAX_RING = 500
_LOCK = threading.Lock()
_RING: list[dict] = []


def _path() -> Path:
    return config.config().data_dir / "web.log"


def record(msg: str, level: str = "info", actor: str = "web") -> dict:
    """写一条日志（内存 + JSONL 文件）。返回条目。"""
    entry = {"ts": time.time(), "level": level, "actor": actor, "msg": msg}
    with _LOCK:
        _RING.append(entry)
        if len(_RING) > _MAX_RING:
            del _RING[:-_MAX_RING]
    try:
        with open(_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return entry


def recent(limit: int = 100) -> list[dict]:
    """最近 N 条（新 -> 旧）。"""
    with _LOCK:
        return list(_RING[-limit:])[::-1]


# ---------------------------------------------------------------------------
# 任务完整输出落盘：<data_dir>/tasks/<job_id>.log（页面关掉/进程中断也可追溯）
# ---------------------------------------------------------------------------

def task_log_dir() -> Path:
    return config.config().data_dir / "tasks"


def task_log_path(job_id: str) -> Path:
    return task_log_dir() / f"{job_id}.log"


def append_task_line(job_id: str, line: str) -> Path:
    """把任务输出的一行追加到该 job 的完整日志文件（含时间戳前缀）。"""
    p = task_log_path(job_id)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {line}\n")
    except Exception:
        pass
    return p


def task_log_tail(job_id: str, limit: int = 40) -> str:
    """任务完整日志尾部（查看/面板展示用）。"""
    p = task_log_path(job_id)
    if not p.exists():
        return ""
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-limit:])
    except Exception:
        return ""


def reset() -> None:
    """清空内存缓冲（测试用；不清文件）。"""
    with _LOCK:
        _RING.clear()