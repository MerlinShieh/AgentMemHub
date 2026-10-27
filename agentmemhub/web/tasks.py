"""看板后台任务管理器（ingest / 向量化 / wiki 编译等耗时操作）。

web 端管理操作多为分钟级（提取全量会话、embedding 重建），不能同步阻塞
uvicorn 事件循环 → 提交到后台线程执行，前端轮询状态。同一时刻只允许
一个任务（看板单用户 + 避免并发写库）；线程内不共享 web 的 Store 连接
（任务函数自建连接，WAL 下与 web 主连接并发安全）。

实时输出：fn(emit, meta) 收到 emit(line) 回调，把运行中的输出逐行写入
job["output"]（前端 loading 期间即可看到进展）；fn 返回终态文本追加。

## 为什么"取消"是协作式的（而不是杀线程）

Python 没有办法安全地终止一个线程：`thread.raise()` 之类只会在**阻塞点**炸，
把它抛在 `sqlite3` 写事务中间就是库损坏。所以这里的 `cancel()` 只做一件事
—— 置旗标，任务在**自己选定的检查点**（循环每次迭代）调用
`meta["should_stop"]()`，发现为真就抛 `TaskCanceled` 正常退出。任务函数必须
主动配合；不配合的任务（没检查点的短任务）取消请求会一直无效，前端会显示
"已请求取消，等待任务退出"。

**单槽只在任务真正结束后才释放**（`_run` 的 finally 改状态），否则"取消一个
还在写的任务 + 立刻提交新任务"就会变成两个线程同时写同一份产物 —— 那比卡住
更糟。

## 超时同理

`submit(..., timeout_s=N)` 到点只是置同一个旗标并标 `timedOut=True`，终态因此
是 `timeout` 而不是 `canceled`。它**不会**让卡在阻塞调用里的线程消失，只保证
"任务一旦回到自己的循环就能退出"，并且前端能明确区分"还在跑"与"超时了"。
"""
from __future__ import annotations

import threading
import time
import traceback
import uuid
from typing import Any, Callable, Optional

_LOCK = threading.Lock()
_CURRENT: Optional[dict] = None     # {id, name, status, output, error, ...}
_THREAD: Optional[threading.Thread] = None   # 当前任务线程（不放进 job，避免 JSON 序列化炸）
# 线程属于哪个 job：`_CURRENT` 与 `_THREAD` 是**两次**持锁赋值（先建 job、后建线程），
# 中间窗口里 `status()` 会看到"新 job + 旧线程"。没有这个 id，僵尸检测就会把
# 刚提交、线程还没 start 的新任务判成"线程已退出"并永久标 error。
_THREAD_JOB_ID: Optional[str] = None
_OUTPUT_CAP = 200_000               # 输出上限（字符），防长期任务撑爆内存


class TaskCanceled(Exception):
    """任务在检查点发现「已请求取消 / 已超时」时抛出。

    由 `submit` 捕获并落成终态（`canceled` / `timeout`），不算错误 —— 所以
    **任务函数不需要自己 try 它**，抛出来即可。
    """


def _append_output(job: dict, text: str) -> None:
    job["output"] = (job.get("output") or "") + text
    if len(job["output"]) > _OUTPUT_CAP:
        job["output"] = job["output"][-_OUTPUT_CAP:]


def submit(
    name: str,
    fn: Callable[[Callable[[str], None], dict], str],
    *,
    timeout_s: float = 0.0,
    cancelable: bool = True,
) -> Optional[dict]:
    """提交一个后台任务（幂等串行：已有 running 任务则拒绝）。

    fn(emit, meta) -> str：emit(line) 逐行实时输出；meta 里有 `id` / `name` /
    `should_stop()`（取消与超时的唯一检查点）；返回终态文本。
    异常 → status=error + error 字段。返回任务快照；被拒绝时返回 None。

    `timeout_s > 0` 时到点自动请求取消（终态 `timeout`）；`cancelable=False`
    用于"绝不能被打断"的任务（如已经开始的快照回滚 —— 半途停下比跑完更危险）。
    """
    global _CURRENT, _THREAD, _THREAD_JOB_ID
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
            "progress": None,           # 结构化进度 {done,total,pct,label}（任务可选回填）
            "cancelable": bool(cancelable),
            "cancelRequested": False,
            "timedOut": False,
            "timeoutAt": (time.time() + timeout_s) if timeout_s and timeout_s > 0 else None,
        }
        _CURRENT = job

    def should_stop() -> bool:
        with _LOCK:
            return bool(job["cancelRequested"])

    def emit(line: str) -> None:
        with _LOCK:
            _append_output(job, line.rstrip("\n") + "\n")

    def _run() -> None:
        meta = {"id": job["id"], "name": name, "should_stop": should_stop}
        try:
            fn(emit, meta)              # 输出经 emit 实时流入 task["output"]
            with _LOCK:
                # 任务**自己提前返回**也算取消生效：`wiki.update` 这类长任务
                # 在阶段边界选择"干净地收工"（而不是抛异常），返回后这里按
                # 旗标落终态，免得显示"完成"让用户以为整轮跑完了。
                if job["cancelRequested"]:
                    job["status"] = "timeout" if job["timedOut"] else "canceled"
                    job["error"] = (
                        "超过时长上限，已在阶段边界收工" if job["timedOut"]
                        else "已按请求在阶段边界收工（未跑完）"
                    )
                else:
                    job["status"] = "done"
                job["finishedAt"] = time.time()
        except TaskCanceled:
            with _LOCK:
                job["status"] = "timeout" if job["timedOut"] else "canceled"
                job["error"] = (
                    "超过时长上限，已在检查点中止" if job["timedOut"]
                    else "已按请求取消"
                )
                job["finishedAt"] = time.time()
        except Exception as e:          # noqa: BLE001 —— 任务错误如实上报
            tb = traceback.format_exc()
            with _LOCK:
                job["error"] = str(e)
                # 只留尾部：报错现场通常在最后几帧，而完整 traceback 可能上千行、
                # 把 output 挤爆（output 有 200K 上限，超了从头部截断，反而把
                # 真正有用的上下文切掉）。
                _append_output(job, "\n[traceback]\n" + tb[-4000:] + "\n")
                job["status"] = "error"
                job["finishedAt"] = time.time()

    th = threading.Thread(target=_run, daemon=True, name=f"task-{name}")
    with _LOCK:
        _THREAD = th
        _THREAD_JOB_ID = job["id"]
    th.start()

    # 超时看门狗：只置旗标，不杀线程（理由见模块 docstring）
    if timeout_s and timeout_s > 0:
        timer = threading.Timer(timeout_s, lambda: _mark_timeout(job))
        timer.daemon = True
        timer.start()
    return _snapshot()


def _mark_timeout(job: dict) -> None:
    with _LOCK:
        if job is not _CURRENT or job["status"] != "running":
            return
        job["cancelRequested"] = True
        job["timedOut"] = True


def cancel(job_id: str = "") -> dict:
    """请求取消当前任务（协作式）。返回 {ok, id, reason}。

    **只置旗标**，真正结束要等任务走到检查点；前端据 `cancelRequested`
    显示"已请求取消"。没有检查点的任务会一直无效（这是刻意的：比起硬杀
    线程把库写坏，宁可让用户看到"请求已发出"）。
    """
    with _LOCK:
        if not _CURRENT or _CURRENT["status"] != "running":
            return {"ok": False, "reason": "没有正在运行的任务"}
        if job_id and _CURRENT["id"] != job_id:
            return {"ok": False, "reason": "任务 id 不匹配（可能已经换了一个任务）"}
        if not _CURRENT.get("cancelable"):
            return {"ok": False, "reason": "该任务不允许中断（半途停下会更危险）"}
        _CURRENT["cancelRequested"] = True
        return {"ok": True, "id": _CURRENT["id"], "reason": "已请求取消，等待任务退出"}


def _snapshot() -> Optional[dict]:
    """当前任务的**可序列化**快照（剔除线程对象；顺带做僵尸检测）。"""
    with _LOCK:
        if not _CURRENT:
            return None
        # 僵尸检测：状态还是 running，但**属于这个 job 的**线程已经没了 —— 说明异常
        # 在 `_run` 之外逃逸（或进程重启后残留）。不修的话前端会永远转圈、新的任务
        # 也永远提交不进来（单槽被一个死人占着）。
        # 三个前置条件是防误判：
        #  · `_THREAD_JOB_ID == id`：submit 里"建 job"与"建线程"是两次持锁赋值，
        #    中间窗口会看到「新 job + 旧线程」，不加这条会把新任务当场判死；
        #  · `ident is not None`：线程还没 start 过时 is_alive() 恒 False。
        if (_CURRENT["status"] == "running" and _THREAD is not None
                and _THREAD_JOB_ID == _CURRENT["id"]
                and _THREAD.ident is not None and not _THREAD.is_alive()):
            _CURRENT["status"] = "error"
            _CURRENT["error"] = "任务线程已退出但未留下终态（未捕获异常）"
            _CURRENT["finishedAt"] = time.time()
        return dict(_CURRENT)


def set_progress(job_id: str, progress: dict) -> None:
    """回填当前运行任务的进度（如 {done,total,pct,label}；前端渲染进度条）。"""
    with _LOCK:
        if _CURRENT and _CURRENT.get("id") == job_id and _CURRENT["status"] == "running":
            _CURRENT["progress"] = progress


def status() -> Optional[dict]:
    """当前任务快照（无任务返回 None）。"""
    return _snapshot()


def reset() -> None:
    """清空任务状态（测试用）。"""
    global _CURRENT, _THREAD, _THREAD_JOB_ID
    with _LOCK:
        _CURRENT = None
        _THREAD = None
        _THREAD_JOB_ID = None
