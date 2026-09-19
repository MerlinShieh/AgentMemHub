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
    """状态文件指向临时目录（默认 logs/ 下，测试绝不碰真实状态）。"""
    f = tmp_path / "trigger_state.json"
    monkeypatch.setattr(wiki_triggers, "STATE_FILE", f)
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
