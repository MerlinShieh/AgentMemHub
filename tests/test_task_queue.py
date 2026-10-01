# -*- coding: utf-8 -*-
"""通用收割基础设施（`agentmemhub/task_queue.py`）的测试。

**最关键的是 `test_队列空时零副作用`** —— 它是"实时更新不受影响"的全部根据：
实时通道是同步的，队列恒空，收割对它必须是"立即返回且根本不碰通道"。
"""
import os
import time

import pytest

from agentmemhub import task_queue as tq


class _FakeHarvester:
    """假通道：记录被调用次数，用来验证"该调才调"。"""

    def __init__(self, *, pend=(), result=None, raises=None):
        self._pend = list(pend)
        self._result = result or {"step": "harvested"}
        self._raises = raises
        self.harvested = 0

    def pending(self):
        return list(self._pend)

    def harvest(self, *, now):
        self.harvested += 1
        if self._raises:
            raise self._raises
        return dict(self._result)


def test_队列空时零副作用(tmp_path):
    """**实时通道不受影响的根据**：队列空 ⇒ 立即返回，且**根本不碰通道**。"""
    h = _FakeHarvester(pend=[])

    r = tq.harvest(tmp_path, lambda: h)

    assert r["action"] == "empty" and r["pending"] == 0
    assert h.harvested == 0, "队列空时绝不能调用通道的 harvest（实时模式恒走这条）"
    assert tq.load_state(tmp_path)["last_action"] == "empty"


def test_没配通道时连队列都不读(tmp_path):
    r = tq.harvest(tmp_path, lambda: None)
    assert r["action"] == "no_channel"
    assert not (tmp_path / tq.STATE_NAME).exists()


def test_有在飞任务时会推进(tmp_path):
    h = _FakeHarvester(pend=["t1"], result={"step": "landed", "pages": 3})

    r = tq.harvest(tmp_path, lambda: h, interval_s=0)

    assert r["action"] == "harvested" and h.harvested == 1
    assert r["result"]["step"] == "landed"
    st = tq.load_state(tmp_path)
    assert st["last_action"] == "harvested" and st["last_result"]["pages"] == 3


def test_节流_间隔内不重复查(tmp_path):
    h = _FakeHarvester(pend=["t1"])
    now = 1_000_000.0

    r1 = tq.harvest(tmp_path, lambda: h, now=now, interval_s=300)
    r2 = tq.harvest(tmp_path, lambda: h, now=now + 10, interval_s=300)

    assert r1["action"] == "harvested" and h.harvested == 1
    assert r2["action"] == "throttled" and r2["reason"] == "interval"
    assert h.harvested == 1, "节流期内不该再查厂商"
    assert r2["wait_s"] == pytest.approx(290.0, abs=1.0)


def test_超过间隔后会再查(tmp_path):
    h = _FakeHarvester(pend=["t1"])
    now = 1_000_000.0

    tq.harvest(tmp_path, lambda: h, now=now, interval_s=300)
    r = tq.harvest(tmp_path, lambda: h, now=now + 301, interval_s=300)

    assert r["action"] == "harvested" and h.harvested == 2


def test_force_绕过节流(tmp_path):
    h = _FakeHarvester(pend=["t1"])
    now = 1_000_000.0

    tq.harvest(tmp_path, lambda: h, now=now, interval_s=300)
    r = tq.harvest(tmp_path, lambda: h, now=now + 1, interval_s=300, force=True)

    assert r["action"] == "harvested" and h.harvested == 2


def test_抢不到锁时不算错误(tmp_path):
    """别的进程正在收割 ⇒ `locked`（**不是错误**，也不该被记成失败）。"""
    lock = tq._try_lock(tmp_path)
    assert lock is not None
    try:
        h = _FakeHarvester(pend=["t1"])
        r = tq.harvest(tmp_path, lambda: h, interval_s=0)
        assert r["action"] == "locked" and h.harvested == 0
    finally:
        tq._unlock(lock)


def test_陈旧锁会被抢占(tmp_path):
    """锁超过 `LOCK_STALE_S` 没被更新 ⇒ 抢占（否则一次崩溃就永久卡死收割）。

    ⚠️ **只按时间戳判定、刻意不检查进程存活**：Windows 上 `os.kill(pid, 0)` 走的是
    `TerminateProcess`（那是"**杀掉对方**"而非探测），2026-10-01 实测把 pytest 自己
    杀了 —— 表现为整条命令无输出、退出码 1，排查成本极高。
    """
    p = tmp_path / tq.LOCK_NAME
    p.write_text('{"pid": 999999, "ts": %f}' % (time.time() - tq.LOCK_STALE_S - 1),
                 encoding="utf-8")

    lock = tq._try_lock(tmp_path)

    assert lock is not None, "过期锁必须能被抢占（否则一次崩溃就永久卡死收割）"
    tq._unlock(lock)


def test_自己持有的锁不会重入(tmp_path):
    """同进程重复抢锁 ⇒ None（**绝不能把自己的 PID 当成"死了"**）。"""
    lock = tq._try_lock(tmp_path)
    assert lock is not None
    try:
        assert tq._try_lock(tmp_path) is None
    finally:
        tq._unlock(lock)


def test_刚写下的锁不会被抢(tmp_path):
    """锁很新 ⇒ 不抢 —— 这正是"另一个进程正在收割"的判定（返回 locked 而非 error）。"""
    (tmp_path / tq.LOCK_NAME).write_text(
        '{"pid": %d, "ts": %f}' % (os.getpid() + 1, time.time()), encoding="utf-8")

    assert tq._try_lock(tmp_path) is None


def test_通道抛错时退避(tmp_path):
    h = _FakeHarvester(pend=["t1"], raises=RuntimeError("厂商 500"))
    now = 1_000_000.0

    r1 = tq.harvest(tmp_path, lambda: h, now=now, interval_s=300)
    assert r1["action"] == "error" and "厂商 500" in r1["error"]

    st = tq.load_state(tmp_path)
    assert st["consecutive_errors"] == 1 and st["next_check_at"] > now, "出错要退避"

    # 退避期内不再戳（哪怕已过普通节流间隔）
    r2 = tq.harvest(tmp_path, lambda: h, now=now + 301, interval_s=300)
    assert r2["action"] == "throttled" and r2["reason"] == "error_backoff"

    # 退避到点后重试；成功后清除退避
    h2 = _FakeHarvester(pend=["t1"])
    r3 = tq.harvest(tmp_path, lambda: h2,
                    now=now + tq.ERROR_BACKOFF_S + 1, interval_s=300)
    assert r3["action"] == "harvested"
    assert "next_check_at" not in tq.load_state(tmp_path)


def test_factory_抛错不影响调用方(tmp_path):
    def boom():
        raise RuntimeError("装不上")

    r = tq.harvest(tmp_path, boom)

    assert r["action"] == "error" and r["stage"] == "factory"


def test_检测流水只记有信息量的动作(tmp_path):
    """**队列空不写流水** —— CLI 每次调用都会走到它，写进去会刷成噪音；
    "上次查过、当时队列空"由 `harvest_state.json` 的 `last_action` 回答。"""
    h = _FakeHarvester(pend=[])
    tq.harvest(tmp_path, lambda: h)

    assert tq.read_checks(tmp_path, limit=10) == [], "empty 不该进流水"
    assert tq.load_state(tmp_path)["last_action"] == "empty", "但状态要记下来"


def test_检测流水按序追加(tmp_path):
    h = _FakeHarvester(pend=["t1"], result={"step": "landed"})
    now = 1_000_000.0

    tq.harvest(tmp_path, lambda: h, now=now, interval_s=0)
    tq.harvest(tmp_path, lambda: h, now=now + 1, interval_s=0)

    rows = tq.read_checks(tmp_path, limit=10)

    assert [r["action"] for r in rows] == ["harvested", "harvested"]
    assert all("ts" in r and "at" in r for r in rows)


def test_配置节流间隔():
    assert tq.check_interval_s({"check_interval_s": 60}) == 60
    assert tq.check_interval_s({}) == tq.DEFAULT_INTERVAL_S
    assert tq.check_interval_s({"check_interval_s": "abc"}) == tq.DEFAULT_INTERVAL_S
    assert tq.check_interval_s({"check_interval_s": -5}) == 0.0
