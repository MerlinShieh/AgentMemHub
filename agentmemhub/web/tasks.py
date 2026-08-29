"""看板后台任务管理器（ingest / memos push 等耗时操作）。

web 端管理操作多为分钟级（提取全量会话、embedding 重建），不能同步阻塞
uvicorn 事件循环 → 提交到后台线程执行，前端轮询状态。同一时刻只允许
一个任务（看板单用户 + 避免并发写库）；线程内不共享 web 的 Store 连接
（任务函数自建连接，WAL 下与 web 主连接并发安全）。
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Callable, Optional

_LOCK = threading.Lock()
_CURRENT: Optional[dict] = None     # {id, name, status, output, error, ...}


def submit(name: str, fn: Callable[[], str]) -> Optional[dict]:
    """提交一个后台任务（幂等串行：已有 running 任务则拒绝）。

    fn 返回任务结果文本（成功路径）。异常 → status=error + error 字段。
    返回任务快照；被拒绝时返回 None。
    """
    global _CURRENT
    with _LOCK:
        if _CURRENT and _CURRENT["status"] == "running":
            return None
        job: dict[str, Any] = {
            "id": uuid.uuid4().hex[:12],
            "name": name,
            "status": "running",
            "startedAt": time.time(),
            "finishedAt": None,
            "output": "",
            "error": None,
        }
        _CURRENT = job

    def _run() -> None:
        try:
            text = fn()
            with _LOCK:
                job["output"] = text
                job["status"] = "done"
                job["finishedAt"] = time.time()
        except Exception as e:          # noqa: BLE001 —— 任务错误如实上报
            with _LOCK:
                job["error"] = str(e)
                job["status"] = "error"
                job["finishedAt"] = time.time()

    threading.Thread(target=_run, daemon=True).start()
    return dict(job)


def status() -> Optional[dict]:
    """当前任务快照（无任务返回 None）。"""
    with _LOCK:
        return dict(_CURRENT) if _CURRENT else None


def reset() -> None:
    """清空任务状态（测试用）。"""
    global _CURRENT
    with _LOCK:
        _CURRENT = None