# -*- coding: utf-8 -*-
"""全局通知（`agentmemhub/notify.py`）的单元测试。

全部注入临时库路径（`db=...`），**不碰真实的 `database/notify.db`**。
"""
from __future__ import annotations

import json
import sqlite3
import time

import pytest

from agentmemhub import notify as N


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "notify.db")


# ---------------------------------------------------------------------------
# 基本读写
# ---------------------------------------------------------------------------

def test_写入后能查回并带未读标记(db):
    assert N.notify("warning", "batch", "提交失败", "余额不足", db=db) is True

    rows = N.list_notifications(db=db)

    assert len(rows) == 1
    r = rows[0]
    assert r["level"] == "warning" and r["category"] == "batch"
    assert r["title"] == "提交失败" and r["body"] == "余额不足"
    assert r["unread"] is True and r["read_at"] is None
    assert N.count_unread(db=db) == 1


def test_非法等级回落info而不是丢通知(db):
    """**通知丢一条比分类不标准更糟** —— 所以校验只回落、不拒绝。"""
    N.notify("catastrophe", "batch", "x", db=db)
    assert N.list_notifications(db=db)[0]["level"] == "info"


def test_meta以JSON落库并能读回(db):
    N.notify("info", "batch", "t", meta={"batch_id": "batch_1", "域": "域A"}, db=db)
    assert N.list_notifications(db=db)[0]["meta"] == {"batch_id": "batch_1", "域": "域A"}


def test_按等级与类别筛选(db):
    N.notify("error", "wiki", "e1", db=db)
    N.notify("warning", "batch", "w1", db=db)
    N.notify("info", "batch", "i1", db=db)

    assert [r["title"] for r in N.list_notifications(level="error", db=db)] == ["e1"]
    # 三条几乎同一毫秒写入 → ts 相同，靠 `id DESC` 兜底保证"最新在前"
    assert [r["title"] for r in N.list_notifications(category="batch", db=db)] == ["i1", "w1"]
    assert [r["title"] for r in N.list_notifications(category="batch", level="info",
                                                     db=db)] == ["i1"]


def test_最新在前(db):
    now = time.time()
    N.notify("info", "batch", "旧", ts=now - 100, db=db)
    N.notify("info", "batch", "新", ts=now, db=db)
    assert [r["title"] for r in N.list_notifications(db=db)] == ["新", "旧"]


def test_标记已读与未读过滤(db):
    N.notify("info", "batch", "a", db=db)
    N.notify("info", "batch", "b", db=db)
    ids = {r["title"]: r["id"] for r in N.list_notifications(db=db)}

    assert N.mark_read([ids["a"]], db=db) == 1, "按标题取 id，别依赖返回顺序"
    assert N.count_unread(db=db) == 1
    assert [r["title"] for r in N.list_notifications(unread_only=True, db=db)] == ["b"]

    assert N.mark_read(all=True, db=db) == 1
    assert N.count_unread(db=db) == 0
    assert N.mark_read(all=True, db=db) == 0, "重复标记不该重复计数"
    assert N.mark_read([], db=db) == 0, "空 ids 且不 all → 什么都不做"


def test_清理超期通知(db):
    now = time.time()
    N.notify("info", "batch", "旧", ts=now - 40 * 86400, db=db)
    N.notify("info", "batch", "新", ts=now, db=db)

    assert N.prune(30, db=db) == 1
    assert [r["title"] for r in N.list_notifications(db=db)] == ["新"]
    assert N.prune(0, db=db) == 0, "keep_days <= 0 表示不限，不该动手"


# ---------------------------------------------------------------------------
# 旁路：降级与"绝不抛"
# ---------------------------------------------------------------------------

def test_落库失败降级到文件且不抛(db, monkeypatch, tmp_path):
    logs_dir = tmp_path / "logs"
    monkeypatch.setattr("agentmemhub.logs.log_dir", lambda: logs_dir)

    def boom(path=None):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(N, "_connect", boom)

    assert N.notify("error", "batch", "写不进去", "详情", db=db) is False, \
        "落库失败要如实返回 False，而不是假装成功"

    fb = logs_dir / "notify_fallback.jsonl"
    assert fb.exists(), "降级文件必须留下痕迹"
    rec = json.loads(fb.read_text(encoding="utf-8").strip())
    assert rec["title"] == "写不进去" and rec["level"] == "error"
    assert "disk I/O" in rec["fallback_error"]


def test_查询类接口在库不可用时返回空值而不抛(monkeypatch, db):
    def boom(path=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(N, "_connect", boom)

    assert N.list_notifications(db=db) == []
    assert N.count_unread(db=db) == 0
    assert N.mark_read(all=True, db=db) == 0
    assert N.prune(30, db=db) == 0


def test_写通知会落通知事实流(db, monkeypatch):
    seen = []
    import agentmemhub.logs as logs
    monkeypatch.setattr(logs, "audit_notify", lambda e: seen.append(e))

    N.notify("warning", "batch", "标题", db=db)

    assert seen and seen[0]["event"] == "notify_write"
    assert seen[0]["ok"] is True
    assert seen[0]["level"] == "warning", "事实流保留原始等级，映射交给 logs"


# ---------------------------------------------------------------------------
# 通知事实流本身（logs/notify.log）
# ---------------------------------------------------------------------------

def test_通知事件的warning在日志流里写成warn(tmp_path, monkeypatch):
    """两条流各用各的等级写法，但**在入口映射一次**，免得 grep 时漏。"""
    import agentmemhub.logs as logs
    monkeypatch.setattr(logs, "log_dir", lambda: tmp_path)

    logs.audit_notify({"event": "notify_write", "level": "warning",
                       "category": "batch", "title": "余额不足"})

    rec = logs.read_notify_audit(tmp_path / "notify.log")[0]
    assert rec["level"] == "warn"
    assert rec["event"] == "notify_write"
    assert "余额不足" in rec["msg"], "标准头里的 msg 要能直接读"
