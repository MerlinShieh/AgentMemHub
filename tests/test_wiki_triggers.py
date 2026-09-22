# -*- coding: utf-8 -*-
"""wiki 增量更新触发器测试。

设计要点（用户定下的）：没有定时任务 —— 检测挂在写入记忆时，满足规则才触发
并打标记。所以测的重点是：

  · 规则判定（纯函数）：三条规则各自命中/不命中、组合、无脏数据一票否决
  · 标记语义：update **成功之后**才记（失败不记，下次写入重试）
  · 编辑接口：overrides 覆盖生效、非法输入拒绝
  · 写入钩子端到端：mock update，验证蒸馏钩子 → 触发 → 标记 的完整链路
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from agentmemhub import wiki, wiki_triggers


@pytest.fixture(autouse=True)
def _tmp_state(tmp_path, monkeypatch):
    """状态文件与**锁文件**都指向临时目录（测试绝不碰真实状态）。

    锁文件必须一起隔离：`_single_flight()` 用跨进程锁，若用默认的
    `logs/wiki_update.lock`，测试就会与**真实正在跑的 update** 抢锁 ——
    实测真实 update 运行时本文件 7 个钩子测试全挂（拿不到锁 → 判定为
    "已有更新在执行"）。这与之前的 `log_dir` / `_targets` 是同一类隔离缺陷。
    """
    f = tmp_path / "trigger_state.json"
    monkeypatch.setattr(wiki_triggers, "STATE_FILE", f)
    monkeypatch.setattr(wiki_triggers, "_LOCK_FILE", tmp_path / "update.lock")
    yield f


def _now(hhmm="10:00"):
    return datetime.strptime("2026-09-20 " + hhmm, "%Y-%m-%d %H:%M")


def _cfg(**kw):
    base = {"enabled": True, "schedule": ["09:00", "12:00", "18:00"],
            "dirty_memories": 10, "first_write_daily": True}
    base.update(kw)
    return base


def _slots_fired(today="2026-09-20"):
    """档位全部标记已触发 —— 专测其它规则时排除档位干扰（否则真实系统时间
    一旦越过任何档位，用例就会被档位规则意外命中）。"""
    return {"fired": {today: ["09:00", "12:00", "18:00"]}}


# ---------------------------------------------------------------------------
# 规则判定
# ---------------------------------------------------------------------------

def test_规则_时点档已过且未触发则命中():
    r = wiki_triggers.evaluate(now=_now("09:30"), dirty=1, has_dirty=True,
                               hook="write", cfg=_cfg(), state={})
    assert r["should_run"] and r["due_slots"] == ["09:00"]


def test_规则_时点未到且无其它规则命中则不触发():
    # 08:59：档位未到；manual 钩子不消耗"每日首次"；脏数不足阈值
    r = wiki_triggers.evaluate(now=_now("08:59"), dirty=1, has_dirty=True,
                               hook="manual", cfg=_cfg(), state={})
    assert not r["should_run"]


def test_规则_该档位今日已触发过不重复():
    # 09:00 档已触发 + 首写已记 + 脏数不足 → 只剩"已过档位"这一条路也走不通
    state = {"fired": {"2026-09-20": ["09:00"]},
             "first_write_date": "2026-09-20"}
    r = wiki_triggers.evaluate(now=_now("09:30"), dirty=1, has_dirty=True,
                               hook="write", cfg=_cfg(), state=state)
    assert not r["should_run"]
    # 但下一个档位（12:00）到了仍然触发
    r2 = wiki_triggers.evaluate(now=_now("12:05"), dirty=1, has_dirty=True,
                                hook="write", cfg=_cfg(), state=state)
    assert r2["should_run"] and r2["due_slots"] == ["12:00"]


def test_规则_脏记忆达到阈值命中_不足不命中():
    r = wiki_triggers.evaluate(now=_now("10:00"), dirty=10, has_dirty=True,
                               hook="write", cfg=_cfg(), state=_slots_fired())
    assert r["should_run"]
    r2 = wiki_triggers.evaluate(now=_now("10:00"), dirty=9, has_dirty=True,
                                hook="write", cfg=_cfg(),
                                state={**_slots_fired(),
                                       "first_write_date": "2026-09-20"})
    assert not r2["should_run"]     # 档位已耗、不足阈值、首写已记


def test_规则_每日首次写入命中_手动触发不消耗额度():
    r = wiki_triggers.evaluate(now=_now("10:00"), dirty=1, has_dirty=True,
                               hook="write", cfg=_cfg(), state=_slots_fired())
    assert r["should_run"]
    r2 = wiki_triggers.evaluate(now=_now("10:00"), dirty=1, has_dirty=True,
                                hook="manual", cfg=_cfg(), state=_slots_fired())
    assert not r2["should_run"]     # manual 不算首次写入


def test_规则_无脏数据一票否决():
    r = wiki_triggers.evaluate(now=_now("09:30"), dirty=0, has_dirty=False,
                               hook="write", cfg=_cfg(), state={})
    assert not r["should_run"]


def test_规则_总开关关闭全部不触发():
    r = wiki_triggers.evaluate(now=_now("09:30"), dirty=99, has_dirty=True,
                               hook="write", cfg=_cfg(enabled=False), state={})
    assert not r["should_run"]


# ---------------------------------------------------------------------------
# 编辑接口（overrides）
# ---------------------------------------------------------------------------

def test_overrides_编辑生效且持久化(_tmp_state):
    cfg = wiki_triggers.set_overrides({"schedule": ["08:00"],
                                       "dirty_memories": 3})
    assert cfg["schedule"] == ["08:00"] and cfg["dirty_memories"] == 3
    # 持久化：重新加载仍在
    assert wiki_triggers.get_effective_config()["dirty_memories"] == 3


def test_overrides_时点格式校验(_tmp_state):
    with pytest.raises(ValueError):
        wiki_triggers.set_overrides({"schedule": ["9点"]})


def test_overrides_未知字段拒绝(_tmp_state):
    with pytest.raises(ValueError):
        wiki_triggers.set_overrides({"whatever": 1})


# ---------------------------------------------------------------------------
# 写入钩子端到端（mock update —— 测编排与标记，不跑真编译）
# ---------------------------------------------------------------------------

def _mk_trigger_env(tmp_path, monkeypatch):
    """造最小 manifest 场景 + 1 条未编译的新记忆（造脏）+ 指向它的配置。

    禁用档位（schedule=[]）隔离真实系统时间 —— 档位规则由纯函数用例覆盖。
    mock update 记录调用（不真刷新 manifest，防重复由 update 真实行为保证，
    已有 update 集成测试验证）。
    """
    from test_wiki_service import _mk_wiki
    db, l1, l2 = _mk_wiki(tmp_path)
    import sqlite3 as _sq
    conn = _sq.connect(db)
    conn.execute("INSERT INTO distilled_memories VALUES (4,'a','s1','concept','high','t','new','新内容','h4',2000)")
    conn.commit(); conn.close()
    monkeypatch.setattr(wiki_triggers, "_targets", lambda: (l1, l2, str(db)))
    wiki_triggers.set_overrides({"schedule": []})
    calls = []

    def fake_update(**kw):
        calls.append(kw)
        return {"updated": True}

    monkeypatch.setattr(wiki, "update", fake_update)
    return db, l1, l2, calls


def test_钩子_每日首次写入触发并打标记(_tmp_state, tmp_path, monkeypatch):
    db, l1, l2, calls = _mk_trigger_env(tmp_path, monkeypatch)
    r = wiki_triggers.on_memories_written()
    assert r["should_run"] is True and len(calls) == 1
    st = json.loads(_tmp_state.read_text(encoding="utf-8"))
    assert st["first_write_date"] == datetime.now().strftime("%Y-%m-%d")
    # 同日第二次写入：首次已消耗、无时点档、脏数不足 → 不再触发
    r2 = wiki_triggers.on_memories_written()
    assert r2["should_run"] is False and len(calls) == 1


def test_钩子_update失败不打标记_下次写入重试(_tmp_state, tmp_path, monkeypatch):
    db, l1, l2, calls = _mk_trigger_env(tmp_path, monkeypatch)
    monkeypatch.setattr(wiki, "update",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("编译炸了")))
    r = wiki_triggers.on_memories_written()
    assert r["should_run"] is True and "error" in r["update"]
    st = json.loads(_tmp_state.read_text(encoding="utf-8"))
    assert "first_write_date" not in st           # 失败不记 → 下次写入重试
    monkeypatch.setattr(wiki, "update", lambda **kw: {"updated": True})
    r2 = wiki_triggers.on_memories_written()
    assert r2["should_run"] is True               # 重试成功
    assert json.loads(_tmp_state.read_text(encoding="utf-8"))["first_write_date"]


def test_钩子_定量阈值命中触发(_tmp_state, tmp_path, monkeypatch):
    db, l1, l2, calls = _mk_trigger_env(tmp_path, monkeypatch)
    wiki_triggers.set_overrides({"first_write_daily": False, "dirty_memories": 1})
    # _mk_trigger_env 已造 1 条新记忆 → 脏数 1 ≥ 阈值 1（首写规则已关）
    r = wiki_triggers.on_memories_written()
    assert r["should_run"] is True and "阈值" in (r["reasons"] or [""])[0]
    assert len(calls) == 1


def test_手动force_绕过规则直接更新(_tmp_state, tmp_path, monkeypatch):
    db, l1, l2, calls = _mk_trigger_env(tmp_path, monkeypatch)
    # 库未变（align 归零），规则判定不会触发；force 仍然执行
    r = wiki_triggers.run_manual(force=True)
    assert r["should_run"] is True and len(calls) == 1
    assert r["reasons"] == ["手动强制"]


# ---------------------------------------------------------------------------
# 判定留痕（logs/wiki.log 的 trigger_check 事件）
# ---------------------------------------------------------------------------

def _capture_wiki_audit(monkeypatch) -> list[dict]:
    entries: list[dict] = []
    monkeypatch.setattr("agentmemhub.logs.audit_wiki", lambda e: entries.append(e))
    return entries


def test_判定结果落wiki日志_触发时(_tmp_state, tmp_path, monkeypatch):
    """触发器此前**没有**这条日志，"某次为什么触发"只能靠 fired 记录 + 代码
    反推（实测踩过，回查历史触发原因时无从下手）。判定信息比执行记录更该留痕。
    """
    _mk_trigger_env(tmp_path, monkeypatch)
    entries = _capture_wiki_audit(monkeypatch)
    wiki_triggers.on_memories_written()
    checks = [e for e in entries if e.get("event") == "trigger_check"]
    assert len(checks) == 1
    e = checks[0]
    assert e["hook"] == "write" and e["should_run"] is True
    assert e["reasons"], "必须记下**为什么**触发"
    assert e["dirty"] >= 1


def test_判定结果落wiki日志_未触发时也记(_tmp_state, tmp_path, monkeypatch):
    """没触发也要留痕 —— 否则无法区分"根本没检测"和"检测了但规则不满足"。"""
    _mk_trigger_env(tmp_path, monkeypatch)
    entries = _capture_wiki_audit(monkeypatch)
    wiki_triggers.on_memories_written()          # 第一次：每日首写 → 触发
    wiki_triggers.on_memories_written()          # 第二次：规则不再满足
    checks = [e for e in entries if e.get("event") == "trigger_check"]
    assert len(checks) == 2
    assert checks[0]["should_run"] is True
    assert checks[1]["should_run"] is False


def test_钩子_L1部分失败不打标记(_tmp_state, tmp_path, monkeypatch):
    """update **正常返回**但 L1 有会话失败（`failed > 0`）—— 此前被当成成功：
    当日档位与 last_update_at 一起被消耗，失败会话（已剔除出新基线）要等到
    **明天**首次写入才有机会重试。实测 2026-09-21 的一次分组超时正是这么从
    "可重试的失败"变成"静默丢失"的：update 只 catch 异常、不看返回值。
    """
    db, l1, l2, calls = _mk_trigger_env(tmp_path, monkeypatch)
    monkeypatch.setattr(wiki, "update", lambda **kw: {
        "updated": True,
        "l1": {"recompiled": 0, "failed": 1, "removed_files": []}})
    entries = _capture_wiki_audit(monkeypatch)

    r = wiki_triggers.on_memories_written()
    assert r["should_run"] is True and r["fired"] is False
    assert "1 个会话编译失败" in r["fired_note"]
    st = json.loads(_tmp_state.read_text(encoding="utf-8"))
    assert "first_write_date" not in st and "last_update_at" not in st
    # 留痕，便于回查"为什么没打标记"
    assert any(e.get("event") == "trigger_fired_skipped" for e in entries)

    # 修好之后（failed=0）下一次写入即可正常打标记
    monkeypatch.setattr(wiki, "update", lambda **kw: {
        "updated": True, "l1": {"recompiled": 1, "failed": 0}})
    r2 = wiki_triggers.on_memories_written()
    assert r2["fired"] is True
    assert json.loads(_tmp_state.read_text(encoding="utf-8"))["first_write_date"]


# ---------------------------------------------------------------------------
# 单飞行锁必须**跨进程**
#   2026-09-21 实测：MCP 的写入钩子（daemon 线程）与人工恢复脚本并发跑同一批
#   产物与 manifest，日志里出现两份"两段式：先分组"（101 / 102 两个版本）。
#   `threading.Lock` 只在单进程内有效，挡不住这种并发。
# ---------------------------------------------------------------------------

def test_单飞行_同进程内不重复取得(_tmp_state, tmp_path, monkeypatch):
    monkeypatch.setattr(wiki_triggers, "_LOCK_FILE", tmp_path / "u.lock")
    with wiki_triggers._single_flight() as a:
        assert a is True
        with wiki_triggers._single_flight() as b:
            assert b is False                 # 同进程第二次拿不到
    with wiki_triggers._single_flight() as c:
        assert c is True                      # 释放后可再取


def test_单飞行_锁文件打不开时退化为不锁(_tmp_state, tmp_path, monkeypatch):
    """锁文件异常不能炸掉调用方的写入流程，也不能让 wiki 更新整体失效。"""
    monkeypatch.setattr(wiki_triggers, "_LOCK_FILE",
                        tmp_path / "不存在的目录" / "x" / "u.lock")
    monkeypatch.setattr(wiki_triggers.Path, "mkdir",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("磁盘只读")))
    with wiki_triggers._file_lock() as got:
        assert got is True


def test_单飞行_其它进程持锁时取不到(_tmp_state, tmp_path, monkeypatch):
    """真·跨进程验证：子进程持锁期间，本进程必须拿不到。"""
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    lock = tmp_path / "u.lock"
    monkeypatch.setattr(wiki_triggers, "_LOCK_FILE", lock)   # 两边必须同一个锁文件
    code = (
        "import sys, time\n"
        "sys.path.insert(0, r'%s')\n"
        "from pathlib import Path\n"
        "from agentmemhub import wiki_triggers as t\n"
        "t._LOCK_FILE = Path(r'%s')\n"
        "ctx = t._file_lock()\n"
        "assert ctx.__enter__() is True, 'child failed to lock'\n"
        "print('LOCKED', flush=True)\n"
        "time.sleep(20)\n" % (root, lock)
    )
    proc = subprocess.Popen([sys.executable, "-c", code], cwd=str(root),
                            stdout=subprocess.PIPE, text=True)
    try:
        assert "LOCKED" in (proc.stdout.readline() or ""), "子进程未取得锁"
        with wiki_triggers._file_lock() as got:
            assert got is False, "另一个进程持锁，本进程却拿到了 —— 不是跨进程锁"
    finally:
        proc.kill()
        proc.wait(timeout=10)
    # 子进程退出后锁由内核释放
    with wiki_triggers._file_lock() as got:
        assert got is True


def test_configured_总开关关闭则视为未配置(monkeypatch):
    """`wiki.enabled` 此前是**假开关** —— 它没有任何消费点，关掉照样触发
    （2026-09-22 配置审计发现）。现在它是真正的总开关。"""
    from agentmemhub import config as _cfg

    class _C:
        wiki = {"enabled": True, "out_l1": "a", "out_l2": "b"}

    monkeypatch.setattr(_cfg, "config", lambda: _C())
    assert wiki_triggers._configured() is True

    # 总开关关闭 → 即便目录配齐也视为未就绪
    _C.wiki = {"enabled": False, "out_l1": "a", "out_l2": "b"}
    assert wiki_triggers._configured() is False

    # 目录没配齐同样未就绪（原有语义不变）
    _C.wiki = {"enabled": True, "out_l1": "a", "out_l2": ""}
    assert wiki_triggers._configured() is False

    # 键缺失时按"开"处理（与 DEFAULT_WIKI 的 true 一致）
    _C.wiki = {"out_l1": "a", "out_l2": "b"}
    assert wiki_triggers._configured() is True
