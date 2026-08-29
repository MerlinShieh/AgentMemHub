"""看板后台任务管理器（ingest / memos push 等耗时操作）。

web 端管理操作多为分钟级（提取全量会话、embedding 重建），不能同步阻塞
uvicorn 事件循环 → 提交到后台线程执行，前端轮询状态。同一时刻只允许
一个任务（看板单用户 + 避免并发写库）；线程内不共享 web 的 Store 连接
（任务函数自建连接，WAL 下与 web 主连接并发安全）。

实时输出：fn(emit) 收到 emit(line) 回调，把运行中的输出逐行写入
job["output"]（前端 loading 期间即可看到进展）；fn 返回终态文本追加。
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Callable, Optional

_LOCK = threading.Lock()
_CURRENT: Optional[dict] = None     # {id, name, status, output, error, ...}
_OUTPUT_CAP = 200_000               # 输出上限（字符），防长期任务撑爆内存


def _append_output(job: dict, text: str) -> None:
    job["output"] = (job.get("output") or "") + text
    if len(job["output"]) > _OUTPUT_CAP:
        job["output"] = job["output"][-_OUTPUT_CAP:]


def submit(name: str, fn: Callable[[Callable[[str], None]], str]) -> Optional[dict]:
    """提交一个后台任务（幂等串行：已有 running 任务则拒绝）。

    fn(emit) -> str：emit(line) 逐行实时输出；返回终态文本（追加到 output）。
    异常 → status=error + error 字段。返回任务快照；被拒绝时返回 None。
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
        def emit(line: str) -> None:
            with _LOCK:
                _append_output(job, line.rstrip("\n") + "\n")
        try:
            fn(emit)            # 输出全部经 emit 实时流入 job（fn 返回值仅供日志/终态复核）
            with _LOCK:
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