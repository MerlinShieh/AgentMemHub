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


# ---------------------------------------------------------------------------
# 推送通道：Pushplus（微信）
# ---------------------------------------------------------------------------

class _Resp:
    """假的 urlopen 响应。"""

    def __init__(self, body: str):
        self._body = body

    def read(self) -> bytes:
        return self._body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_open(body: str):
    def _open(req, timeout=None):
        _fake_open.sent = json.loads(req.data.decode("utf-8"))
        return _Resp(body)
    return _open


def test_推送默认关_不发任何请求(monkeypatch):
    """**默认 `enabled: false`**：没配就不该往外发消息。

    ⚠️ `push()` 返回的是 `{通道名: 结果}` —— 多通道之后契约变了（原先只有
    Pushplus 时是单个 `{"ok", ...}`）。
    """
    monkeypatch.setattr(N, "_notify_cfg", lambda: {"pushplus": {"enabled": False}})
    assert N.push("info", "batch", "标题", "正文")["pushplus"] == {
        "ok": False, "skipped": "disabled"}


def test_推送开了但没token也不发(monkeypatch):
    monkeypatch.setattr(N, "_notify_cfg",
                        lambda: {"pushplus": {"enabled": True, "token": "  "}})
    assert N.push("info", "batch", "标题")["pushplus"]["skipped"] == "no_token"


def test_推送成功与业务失败(monkeypatch):
    """成功判定是响应体的 `code == 200`；业务失败返回 ok=False 但**不抛**。"""
    monkeypatch.setattr(N, "_notify_cfg",
                        lambda: {"pushplus": {"enabled": True, "token": "tk"}})
    # 本测试要走到 `pushplus_send()` **本体**（报文拼装 + 响应解析），故开逃生门；
    # 网络出口已在下面换成替身 ⇒ 仍然不出网。
    monkeypatch.setenv(N.ALLOW_REAL_PUSH_ENV, "1")

    monkeypatch.setattr(N.urllib.request, "urlopen",
                        _fake_open('{"code":200,"msg":"请求成功"}'))
    r = N.push("warning", "batch", "离线批次失败", "厂商返回 401")["pushplus"]

    assert r["ok"] is True
    assert _fake_open.sent["token"] == "tk"
    assert "离线批次失败" in _fake_open.sent["title"]
    assert "厂商返回 401" in _fake_open.sent["content"]

    monkeypatch.setattr(N.urllib.request, "urlopen",
                        _fake_open('{"code":500,"msg":"token 无效"}'))
    r2 = N.push("info", "batch", "x")["pushplus"]
    assert r2["ok"] is False and r2["code"] == 500


def test_推送网络异常永不抛(monkeypatch):
    """推送是旁路：超时/连不上只如实返回，绝不影响调用方。"""
    def _boom(req, timeout=None):
        raise TimeoutError("连接超时")

    monkeypatch.setattr(N, "_notify_cfg",
                        lambda: {"pushplus": {"enabled": True, "token": "tk"}})
    monkeypatch.setenv(N.ALLOW_REAL_PUSH_ENV, "1")
    monkeypatch.setattr(N.urllib.request, "urlopen", _boom)

    r = N.push("info", "batch", "x")["pushplus"]

    assert r["ok"] is False and "TimeoutError" in r["error"]


# ---------------------------------------------------------------------------
# **测试绝不出网**（2026-10-12 事故：测试真的往微信发了消息）
#
# `notify()` 默认 `push_too=True`，而 `_notify_cfg()` 刻意绕过 config 单例
# **直读真实 yaml** ⇒ 任何"只测落库"的 `notify("warning"/"error", …)` 都会
# 用真实 token 真发微信。实测本文件有 7 处这样的调用，**跑一次测试刷一串微信**。
# 现在唯一网络出口 `pushplus_send()` 见沙箱变量即短路 —— 下面三条把它钉死。
# ---------------------------------------------------------------------------

def test_沙箱下pushplus_send一个字节都不出网(monkeypatch):
    """判据最硬：把 `urlopen` 换成"一调用就炸"，若仍返回 skipped ⇒ 根本没出网。"""
    def _must_not_call(*a, **k):
        raise AssertionError("沙箱下不该有任何网络调用")

    monkeypatch.delenv(N.ALLOW_REAL_PUSH_ENV, raising=False)
    monkeypatch.setattr(N.urllib.request, "urlopen", _must_not_call)

    assert N.pushplus_send(token="tk", title="t", content="c") == {
        "ok": False, "skipped": "test_sandbox"}


def test_逃生门与替换网络出口缺一不可(monkeypatch):
    """只置逃生门、不换出口 = **真的发消息**，所以本测试两道都做。"""
    monkeypatch.setenv(N.ALLOW_REAL_PUSH_ENV, "1")
    monkeypatch.setattr(N.urllib.request, "urlopen",
                        _fake_open('{"code":200,"msg":"ok"}'))

    r = N.pushplus_send(token="tk", title="t", content="c")

    assert r["ok"] is True and _fake_open.sent["token"] == "tk"


def test_配置真开着时warning通知也只落库不出网(db, monkeypatch):
    """端到端：即使 pushplus **真的 enabled 且 token 非空**，测试里也不出网。

    同时验证**落库不受影响** —— 推送是旁路，短路不该影响主流程。
    """
    monkeypatch.setattr(N, "_notify_cfg", lambda: {
        "pushplus": {"enabled": True, "token": "real-token-shape"},
        "push_when": {"warning": True, "error": True}})
    monkeypatch.delenv(N.ALLOW_REAL_PUSH_ENV, raising=False)

    def _must_not_call(*a, **k):
        raise AssertionError("不该出网")

    monkeypatch.setattr(N.urllib.request, "urlopen", _must_not_call)

    assert N.notify("warning", "batch", "标题", "正文", db=db) is True
    assert [n["title"] for n in N.list_notifications(db=db)] == ["标题"]
    # 审计里能看到"没发出去"的原因，而不是静默
    assert N.push("warning", "batch", "标题")["pushplus"]["skipped"] == "test_sandbox"


# ---------------------------------------------------------------------------
# 通道可插拔（"后续手动添加其他渠道"的入口）
# ---------------------------------------------------------------------------

def test_可以注册新通道(monkeypatch):
    """**这就是加渠道的全部成本**：写一个函数 + `register_channel()` —— 落库、
    推送策略、去重、审计全都自动复用，核心逻辑一行都不用改。"""
    got: list = []

    def _fake_channel(*, level, category, title, body, meta, cfg):
        got.append((level, category, title, cfg))
        return {"ok": True, "via": "fake"}

    monkeypatch.setattr(N, "_notify_cfg", lambda: {
        "fake": {"enabled": True, "whatever": 1}})
    N.register_channel("fake", _fake_channel)
    try:
        assert "fake" in N.channels()
        out = N.push("warning", "batch", "标题", "正文")
        assert out["fake"] == {"ok": True, "via": "fake"}
        assert "pushplus" in out, "**多通道**：已有通道不受影响、照旧各发各的"
        # 通道拿到的是**它自己那段配置**（渠道自定义配置键，核心不必认识）
        assert got and got[0][0] == "warning" and got[0][3] == {"enabled": True,
                                                                "whatever": 1}
    finally:
        N._CHANNELS.pop("fake", None)


def test_指定通道只推那一个(monkeypatch):
    """联调单个渠道时用 `channel=`。"""
    calls: list = []

    def _ch(name):
        def _fn(*, level, category, title, body, meta, cfg):
            calls.append(name)
            return {"ok": True}
        return _fn

    monkeypatch.setattr(N, "_notify_cfg", lambda: {})
    N.register_channel("c1", _ch("c1"))
    N.register_channel("c2", _ch("c2"))
    try:
        out = N.push("info", "batch", "标题", channel="c2")
        assert list(out) == ["c2"] and calls == ["c2"]
    finally:
        N._CHANNELS.pop("c1", None)
        N._CHANNELS.pop("c2", None)


def test_一个通道炸了不牵连别的(monkeypatch):
    """渠道是**互不牵连**的：一个抛异常，其它照发。"""
    def _boom(*, level, category, title, body, meta, cfg):
        raise RuntimeError("这个渠道坏了")

    good: list = []
    monkeypatch.setattr(N, "_notify_cfg", lambda: {})
    N.register_channel("bad", _boom)
    N.register_channel("good", lambda **k: good.append(k) or {"ok": True})
    try:
        out = N.push("info", "batch", "标题")
        assert out["bad"]["ok"] is False and "这个渠道坏了" in out["bad"]["error"]
        assert out["good"]["ok"] is True and len(good) == 1
    finally:
        N._CHANNELS.pop("bad", None)
        N._CHANNELS.pop("good", None)


def test_未知通道如实报(monkeypatch):
    monkeypatch.setattr(N, "_notify_cfg", lambda: {})
    assert N.push("info", "batch", "x", channel="不存在")["不存在"]["skipped"] \
        == "unknown_channel"


# ---------------------------------------------------------------------------
# 语义化快捷函数
# ---------------------------------------------------------------------------

def test_快捷函数写进库并带上meta(tmp_path, monkeypatch):
    """`**meta` 直接变成结构化 meta —— 面板/排查时能按字段查。"""
    db = str(tmp_path / "n.db")
    monkeypatch.setattr(N, "push", lambda *a, **k: {})

    # 快捷函数固定用默认库；为隔离，这里用它的等价 `notify(..., db=...)`
    N.notify("warning", "batch", "快过期", "还有 2 小时", db=db,
             meta={"batch_id": "b1", "hours": 2})

    row = N.list_notifications(db=db, limit=1)[0]
    assert row["level"] == "warning" and row["meta"]["batch_id"] == "b1"


def test_快捷函数签名一致(monkeypatch):
    """四个快捷函数都能被同样地调用，且**两种 meta 写法都支持**：
    关键字直接展开（`batch_id="b1"`）与显式 `meta={...}`（便于从 `notify(...)` 迁移）。"""
    seen: list = []
    monkeypatch.setattr(N, "notify",
                        lambda lv, cat, ttl, body="", **k:
                        seen.append((lv, cat, ttl, k.get("meta"))) or True)

    N.info("c", "t", "b", k1=1)
    N.warn("c", "t", "b", meta={"k2": 2})
    N.error("c", "t", "b", meta={"k3": 3}, extra="x")
    N.debug("c", "t", "b")

    assert [s[0] for s in seen] == ["info", "warning", "error", "debug"]
    assert seen[0][3] == {"k1": 1}
    assert seen[1][3] == {"k2": 2}
    assert seen[2][3] == {"k3": 3, "extra": "x"}, "显式 meta 与散装关键字要合并"
    assert seen[3][3] is None, "都没有 meta 时传 None（不写空 dict）"


# ---------------------------------------------------------------------------
# 去重窗口
# ---------------------------------------------------------------------------

def test_去重窗口内同名只留一条(db, monkeypatch):
    """失败重试与重复触发会刷屏，而 Pushplus 是**推微信** —— 去重是必须的。"""
    monkeypatch.setattr(N, "dedup_window_s", lambda: 300.0)

    assert N.notify("info", "batch", "同一标题", db=db, ts=1000.0) is True
    assert N.notify("info", "batch", "同一标题", db=db, ts=1100.0) is True, \
        "去重命中也要返回 True（'这条已被代表'），不能当成失败"
    assert len(N.list_notifications(db=db, limit=10)) == 1

    assert N.notify("info", "batch", "同一标题", db=db, ts=2000.0) is True
    assert len(N.list_notifications(db=db, limit=10)) == 2, "出窗口后可再落一条"

    # 标题不同不受影响
    assert N.notify("info", "batch", "另一个标题", db=db, ts=2010.0) is True
    assert len(N.list_notifications(db=db, limit=10)) == 3


def test_落库失败时不推送(tmp_path, monkeypatch):
    """落库降级（写 fallback）时不推 —— 否则同一条会既进 fallback 又发微信。"""
    sent = []
    monkeypatch.setattr(N, "push", lambda *a, **k: sent.append(a) or {"ok": True})

    def _boom(db=None):
        raise RuntimeError("库坏了")

    monkeypatch.setattr(N, "_db", _boom)

    N.notify("info", "batch", "标题", db=str(tmp_path / "x.db"))

    assert sent == [], "落库没成功就不该推"


def test_落库成功才推送(db, monkeypatch):
    sent = []
    monkeypatch.setattr(N, "push", lambda *a, **k: sent.append(a) or {"ok": True})

    N.notify("warning", "batch", "余额不足", "请充值", db=db, ts=1000.0)

    assert len(sent) == 1 and sent[0][0] == "warning"


def test_通知配置每次读盘_不依赖进程单例(monkeypatch):
    """配完 token 不该为了生效去重启 DSH —— 而 `config()` 是进程级单例
    （yaml 只在构造时读一次），长驻的 MCP 进程会看不到新值。所以这里破例直接读盘。"""
    import agentmemhub.config as C

    def _boom():
        raise RuntimeError("单例不可用")

    monkeypatch.setattr(C, "config", _boom)

    cfg = N._notify_cfg()

    assert "pushplus" in cfg, "`_notify_cfg()` 不该依赖 config() 单例"
    assert "dedup_window_s" in cfg


# ---------------------------------------------------------------------------
# 推送策略（push_when）
# ---------------------------------------------------------------------------

def _policy(monkeypatch, **kw):
    base = {"memory_write": False, "wiki_trigger": True,
            "info": False, "warning": True, "error": True}
    base.update(kw)
    monkeypatch.setattr(N, "_notify_cfg", lambda: {"push_when": base})


def test_推送策略_按类别与等级是OR(monkeypatch):
    """两类开关是 OR："只看警告以上"与"wiki 的动作我都要知道"能并存。"""
    _policy(monkeypatch)

    assert N.should_push("info", "batch") is True       # 类别命中（wiki_trigger）
    assert N.should_push("info", "trigger") is True
    assert N.should_push("info", "wiki") is True
    assert N.should_push("warning", "general") is True  # 等级命中
    assert N.should_push("error", "memory") is True     # 等级命中（哪怕 memory_write 关着）
    assert N.should_push("debug", "batch") is True      # 类别命中（debug 不是等级开关）
    assert N.should_push("info", "memory") is False     # 都不命中
    assert N.should_push("info", "general") is False


def test_推送策略_全关时一条都不推(monkeypatch):
    _policy(monkeypatch, memory_write=False, wiki_trigger=False,
            info=False, warning=False, error=False)

    for lv in ("info", "warning", "error"):
        assert N.should_push(lv, "batch") is False
    assert N.should_push("warning", "memory") is False


def test_只看警告以上(monkeypatch):
    """把 ② 关掉、只留 ④⑤ ⇒ 只有 warning/error 推，wiki 的 info 不推。"""
    _policy(monkeypatch, wiki_trigger=False, info=False,
            warning=True, error=True)

    assert N.should_push("info", "batch") is False
    assert N.should_push("warning", "batch") is True
    assert N.should_push("error", "wiki") is True


def test_落库不受策略影响(db, monkeypatch):
    """策略只决定"推不推微信" —— 落库始终全留（面板要查得到）。"""
    _policy(monkeypatch, memory_write=False, wiki_trigger=False,
            info=False, warning=False, error=False)
    monkeypatch.setattr(N, "push", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("策略全关时不该调用 push")))

    N.notify("error", "batch", "严重错误", "细节", db=db, ts=1000.0)

    assert len(N.list_notifications(db=db, limit=10)) == 1
