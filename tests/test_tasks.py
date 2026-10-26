"""长任务状态机：协作式取消 / 超时旗标 / 僵尸检测 / 单槽互斥。

为什么值得单独测：任务一旦卡住，前端会永远转圈，而且单槽被别人占着之后
**新任务提交不进来** —— 用户看到的现象是"按钮点了没反应"。这几条守的就是
"任何情况下状态都会走到终态"。

取消刻意不是"立刻停"（硬杀写 sqlite 的线程就是库损坏），所以断言都围绕
"旗标置上 → 任务回到检查点 → 落 canceled/timeout"这条链路。
"""
from __future__ import annotations

import threading
import time

import pytest

from agentmemhub.web import tasks


@pytest.fixture(autouse=True)
def _isolate():
    tasks.reset()
    yield
    # 收尾：还活着就先请它停，再清状态（否则下一个用例会被上一个的线程干扰）
    if (tasks.status() or {}).get("status") == "running":
        tasks.cancel()
        time.sleep(0.3)
    tasks.reset()


def _wait_terminal(timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = tasks.status()
        if s and s["status"] != "running":
            return s
        time.sleep(0.02)
    raise AssertionError(f"任务没有在 {timeout}s 内到达终态：{tasks.status()}")


def test_取消在检查点生效并落_canceled():
    started = threading.Event()

    def fn(emit, meta):
        started.set()
        for _ in range(500):
            time.sleep(0.01)
            if meta["should_stop"]():
                raise tasks.TaskCanceled()
        return "跑完了"

    assert tasks.submit("长任务", fn)["status"] == "running"
    assert started.wait(2)

    assert tasks.cancel()["ok"] is True

    # 单槽**不提前释放**：线程还在跑，状态就得还是 running。
    # 否则"取消 + 立刻提交新任务"= 两个线程写同一份产物，比卡住更糟。
    assert tasks.status()["status"] == "running"

    s = _wait_terminal()
    assert s["status"] == "canceled"
    assert "取消" in (s["error"] or "")


def test_任务自己提前返回也按旗标落终态():
    """`wiki.update` 这类长任务选择"在阶段边界干净收工"而不是抛异常。

    它返回后如果不管旗标就会显示"完成"，用户会以为整轮跑完了 —— 所以 `_run`
    在 fn 正常返回后也要看一次旗标。
    """
    started = threading.Event()
    hold = threading.Event()

    def fn(emit, meta):
        started.set()
        hold.wait(3)                    # 等测试把旗标置上
        return "阶段边界收工"

    tasks.submit("长任务", fn)
    assert started.wait(2)
    tasks.cancel()
    hold.set()

    s = _wait_terminal()
    assert s["status"] == "canceled"
    assert "阶段边界" in (s["error"] or "")


def test_不可取消的任务拒绝取消请求():
    hold = threading.Event()

    def fn(emit, meta):
        hold.wait(3)
        return "ok"

    tasks.submit("回滚快照", fn, cancelable=False)
    out = tasks.cancel()
    assert out["ok"] is False
    assert "不允许中断" in out["reason"]

    hold.set()
    assert _wait_terminal()["status"] == "done"


def test_超时到点置旗标且终态是_timeout():
    started = threading.Event()

    def fn(emit, meta):
        started.set()
        for _ in range(500):
            time.sleep(0.01)
            if meta["should_stop"]():
                raise tasks.TaskCanceled()
        return "跑完了"

    tasks.submit("长任务", fn, timeout_s=0.2)
    assert started.wait(2)

    s = _wait_terminal()
    assert s["status"] == "timeout"
    assert s["timedOut"] is True


def test_僵尸检测_线程已退出则改判错误():
    """异常在 `_run` 之外逃逸（或进程残留）会让状态永远停在 running。"""
    with tasks._LOCK:
        tasks._CURRENT = {
            "id": "dead", "name": "僵尸", "status": "running",
            "startedAt": time.time(), "finishedAt": None, "output": "",
            "error": None, "progress": None, "cancelable": True,
            "cancelRequested": False, "timedOut": False, "timeoutAt": None,
        }
        tasks._THREAD = threading.Thread(target=lambda: None)
    tasks._THREAD.start()
    tasks._THREAD.join()                # 线程已结束，但状态还是 running

    s = tasks.status()
    assert s["status"] == "error"
    assert "未留下终态" in s["error"]
    # 关键：僵尸清掉后新任务提交得进来（不然单槽被死人永久占住）
    assert tasks.submit("新任务", lambda emit, meta: "ok") is not None


def test_单槽互斥_已有任务时拒绝新任务():
    hold = threading.Event()

    def fn(emit, meta):
        hold.wait(3)
        return "ok"

    assert tasks.submit("占着", fn) is not None
    assert tasks.submit("抢槽", lambda emit, meta: "ok") is None

    hold.set()
    assert _wait_terminal()["status"] == "done"
