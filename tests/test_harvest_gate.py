# -*- coding: utf-8 -*-
"""安全闸的测试：**队列非空 ⇒ 不提交实时更新**。

这条闸是"实时与离线互不打架"的全部机制，两个方向都要守住：

- 队列**非空**（离线在飞）⇒ 只收割，**绝不**提交实时更新 —— 否则正在被离线编译的
  那批域会被实时路径**重新编译一遍**（结果一致，但白花一次实时的钱）；
- 队列**空**（实时模式恒如此）⇒ 实时判定与执行**一如既往** —— 收割是零成本 no-op。
"""
import pytest

from agentmemhub import wiki_triggers as wt


@pytest.fixture()
def _isolated(monkeypatch, tmp_path):
    """把目标目录、锁、状态文件都隔离掉（不碰真实 wiki 产物）。"""
    monkeypatch.setattr(wt, "_targets", lambda: (tmp_path, tmp_path, ":memory:"))
    monkeypatch.setattr(wt, "_take_lock", lambda: True)
    monkeypatch.setattr(wt, "_single_flight", lambda: _null_cm())
    return tmp_path


class _null_cm:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_align(*a, **k):
    return {"needs_recompile": True, "stages": {"l1": {}}}


def test_队列非空时不提交实时更新(_isolated, monkeypatch):
    """**重复付费的闸门**：离线批次在飞时，实时路径会把同一批域重新编译一遍。"""
    called = []
    monkeypatch.setattr(wt, "_harvest_gate",
                        lambda **k: {"action": "harvested", "pending": 2, "busy": True})
    monkeypatch.setattr("agentmemhub.wiki.align", _fake_align)
    monkeypatch.setattr("agentmemhub.wiki.update",
                        lambda **k: called.append(k) or {"l1": {}, "l2": {}})

    r = wt._run_locked(hook="write", force=True)

    assert r["should_run"] is False
    assert "离线批次在飞" in r["reasons"][0]
    assert r["harvest"]["pending"] == 2
    assert called == [], "队列非空时**绝不能**提交实时更新"


def test_队列空时照常走实时判定(_isolated, monkeypatch):
    """**实时通道不受影响的证明**：闸门放行后，实时判定与执行一如既往。"""
    called = []
    monkeypatch.setattr(wt, "_harvest_gate",
                        lambda **k: {"action": "empty", "pending": 0, "busy": False})
    monkeypatch.setattr("agentmemhub.wiki.align", _fake_align)
    monkeypatch.setattr("agentmemhub.wiki.update",
                        lambda **k: called.append(k) or {"l1": {}, "l2": {}})

    r = wt._run_locked(hook="write", force=True)

    assert r["should_run"] is True and r["reasons"] == ["手动强制"]
    assert len(called) == 1, "队列空 ⇒ 实时更新照常执行"


def test_抢不到收割锁时保守不提交实时(_isolated, monkeypatch):
    """`locked`（别的进程正在收割）时 pending 未知 ⇒ 保守视为"忙"。"""
    called = []
    monkeypatch.setattr(wt, "_harvest_gate",
                        lambda **k: {"action": "locked", "pending": 0, "busy": True})
    monkeypatch.setattr("agentmemhub.wiki.align", _fake_align)
    monkeypatch.setattr("agentmemhub.wiki.update",
                        lambda **k: called.append(k) or {})

    r = wt._run_locked(hook="write", force=True)

    assert r["should_run"] is False and called == []


def test_闸门异常的判定(_isolated, monkeypatch):
    """闸门自己抛错 ⇒ `busy=False`（**绝不能因为闸门坏了就永久挡住实时**）。"""
    import agentmemhub.task_queue as tq
    from agentmemhub import wiki_batch

    def boom(*a, **k):
        raise RuntimeError("闸门炸了")

    monkeypatch.setattr(tq, "harvest", boom)

    gate = wt._harvest_gate(l1=_isolated, l2=_isolated, db=":memory:")

    assert gate["busy"] is False and gate["action"] == "error"


def test_没配凭据时闸门放行(_isolated, monkeypatch):
    """没配 `llm_batch.api_key` ⇒ 通道不可用 ⇒ `no_channel`、放行实时（busy=False）。"""
    from agentmemhub import wiki_batch

    monkeypatch.setattr(wiki_batch, "make_harvester", lambda **k: None)

    gate = wt._harvest_gate(l1=_isolated, l2=_isolated, db=":memory:")

    assert gate["action"] == "no_channel" and gate["busy"] is False


# ---------------------------------------------------------------------------
# 接线：MCP 启动 / CLI 入口
# ---------------------------------------------------------------------------

def test_harvest_now_在测试沙箱里被熔断():
    """`_targets()` 自带**测试沙箱熔断** ⇒ 测试里绝不会真去查厂商（更不会计费）。"""
    r = wt.harvest_now()

    assert r["action"] == "error"
    assert "测试环境" in (r.get("error") or ""), \
        "应被 `AGENTMEMHUB_TEST_SANDBOX` 熔断拦住（不是真去请求）"


def test_MCP启动钩子吞掉一切异常(monkeypatch):
    """收割是旁路：它炸了也**绝不能**影响 MCP 启动。"""
    from agentmemhub import mcp_server

    def boom():
        raise RuntimeError("收割炸了")

    monkeypatch.setattr("agentmemhub.wiki_triggers.harvest_now", boom)

    mcp_server._harvest_probe()             # 不该抛


def test_CLI入口会顺手收割(monkeypatch):
    """挂 CLI 入口的意义：**任何一条命令**都会顺带检查一次在飞任务。"""
    from agentmemhub import cli

    seen = []
    monkeypatch.setattr("agentmemhub.wiki_triggers.harvest_now",
                        lambda: seen.append(1) or {"action": "empty"})

    cli._harvest_quietly()

    assert seen == [1]


def test_CLI收割失败不影响命令(monkeypatch):
    from agentmemhub import cli

    def boom():
        raise RuntimeError("炸")

    monkeypatch.setattr("agentmemhub.wiki_triggers.harvest_now", boom)

    cli._harvest_quietly()                  # 不该抛


# ---------------------------------------------------------------------------
# 离线触发器：**离线优先、实时兜底**
# ---------------------------------------------------------------------------

def _fake_align(*a, **k):
    """脏数据 5 条（≥ 任意阈值）且需要重编译。"""
    return {"needs_recompile": True, "stages": {"l1": {"added_total": 5}}}


def _batch_cfg(**kw):
    base = {"enabled": True, "schedule": [], "dirty_memories": 1,
            "first_write_daily": False}
    base.update(kw)
    return base


def test_离线启用且命中时走离线(_isolated, monkeypatch):
    """**离线优先**：两个通道都开时，离线命中就不再走实时。"""
    monkeypatch.setattr(wt, "get_effective_batch_config", lambda: _batch_cfg())
    seen = {"offline": 0, "realtime": 0}
    monkeypatch.setattr(
        wt, "_submit_offline",
        lambda **k: seen.__setitem__("offline", seen["offline"] + 1)
        or {"channel": "offline", "should_run": True})
    monkeypatch.setattr("agentmemhub.wiki.align", _fake_align)
    monkeypatch.setattr(
        "agentmemhub.wiki.update",
        lambda **k: seen.__setitem__("realtime", seen["realtime"] + 1) or {})

    r = wt._run_locked(hook="write", force=False)

    assert r["channel"] == "offline"
    assert seen["offline"] == 1 and seen["realtime"] == 0, \
        "离线命中后**不得**再走实时（否则同一批数据被两条通道各处理一次）"


def test_离线不可用时回落到实时(_isolated, monkeypatch):
    """**实时兜底**：离线开着、但凭据/目录没配好 ⇒ 回落实时，而不是什么都不做。"""
    monkeypatch.setattr(wt, "get_effective_batch_config", lambda: _batch_cfg())
    monkeypatch.setattr(wt, "_submit_offline", lambda **k: None)   # 通道不可用
    monkeypatch.setattr("agentmemhub.wiki.align", _fake_align)
    seen = []
    monkeypatch.setattr("agentmemhub.wiki.update",
                        lambda **k: seen.append(k) or {"l1": {}, "l2": {}})

    r = wt._run_locked(hook="write", force=False)

    assert len(seen) == 1, "离线不可用时必须回落到实时"
    assert r.get("channel") != "offline"


def test_离线未启用时走实时(_isolated, monkeypatch):
    monkeypatch.setattr(wt, "get_effective_batch_config",
                        lambda: _batch_cfg(enabled=False))
    called = []
    monkeypatch.setattr(wt, "_submit_offline",
                        lambda **k: called.append("offline") or {})
    monkeypatch.setattr("agentmemhub.wiki.align", _fake_align)
    monkeypatch.setattr("agentmemhub.wiki.update", lambda **k: {"l1": {}, "l2": {}})

    wt._run_locked(hook="write", force=True)

    assert called == [], "离线关着就不该走离线"


def test_force不会打开关着的离线通道(_isolated, monkeypatch):
    """`--force` 只在**已启用的通道内**绕过规则 —— 不该把关着的离线通道打开，
    否则一次手动强制就可能意外花掉离线批量的钱。"""
    monkeypatch.setattr(wt, "get_effective_batch_config",
                        lambda: _batch_cfg(enabled=False))
    called = []
    monkeypatch.setattr(wt, "_submit_offline",
                        lambda **k: called.append("offline") or {})
    monkeypatch.setattr("agentmemhub.wiki.align", _fake_align)
    monkeypatch.setattr("agentmemhub.wiki.update", lambda **k: {"l1": {}, "l2": {}})

    wt._run_locked(hook="write", force=True)

    assert called == []


def test_提交离线成功后打标记(_isolated, monkeypatch):
    """提交成功要**消耗额度**（与实时共用同一套状态 = 一天一轮 wiki 更新），否则
    每次写入都会重新提交同一个批次 —— 那是真花钱的。"""
    import json
    from datetime import datetime

    from agentmemhub import batch_ops

    monkeypatch.setattr(batch_ops, "run", lambda **k: {
        "l2": {"offline_submitted": {"batch_id": "b1", "n_requests": 3,
                                     "stage": "l2compile"}}})
    sp = _isolated / "state.json"
    monkeypatch.setattr(wt, "state_path", lambda: sp)

    r = wt._submit_offline(hook="write", dirty=5, now=datetime.now(),
                           state={}, reasons=["攒够脏记忆"], due_slots=["02:00"])

    assert r["channel"] == "offline"
    assert r["offline_submitted"]["batch_id"] == "b1"
    st = json.loads(sp.read_text(encoding="utf-8"))
    assert st.get("last_update_at"), "提交成功要打标记（否则会重复提交）"
    assert st.get("fired"), "时点档也要记上（fired_today）"
    assert "update_started_at" not in st, "收工要清掉'进行中'"
