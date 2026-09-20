# -*- coding: utf-8 -*-
"""快照与回滚测试：create/list/restore 闭环 + 保留策略。

关键验证：
  · 备份随 data_dir 沙箱自动隔离（绝不写真实 backups/）
  · 索引库整库备份（含蒸馏表数据）
  · restore 能把"被改坏的数据"恢复为快照时状态——这正是它存在的意义
  · restore 前自动做保护快照（防误恢复不可逆）
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from agentmemhub import snapshot


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """沙箱：备份目录 + 索引库 + wiki 产物目录全部指向临时路径。"""
    data = tmp_path / "data"
    monkeypatch.setattr(snapshot, "backups_dir", lambda: data / "backups")
    idx = tmp_path / "session_rag.db"
    monkeypatch.setattr(snapshot, "_index_db", lambda: idx)
    l1 = tmp_path / "out_l1"
    l2 = tmp_path / "out_l2"
    l1.mkdir()
    l2.mkdir()
    monkeypatch.setattr(snapshot, "_wiki_dirs", lambda: (l1, l2))
    # 建最小索引库
    conn = sqlite3.connect(idx)
    conn.execute("CREATE TABLE distilled_memories (id INTEGER PRIMARY KEY,"
                 " content TEXT, content_hash TEXT)")
    conn.execute("INSERT INTO distilled_memories VALUES (1,'原始内容','h1')")
    conn.commit()
    conn.close()
    return tmp_path, idx, l1, l2


def _pin_keep(monkeypatch, n):
    """把生效的保留份数钉死为 n —— 配置层 `snapshot.keep` 的替身。

    保留份数现在来自配置（config().snapshot.keep），不再是模块常量，
    所以测试要替换配置视图而不是 monkeypatch KEEP。
    """
    class _Cfg:
        snapshot = {"keep": n}
    monkeypatch.setattr(snapshot.hub_config, "config", lambda: _Cfg())


def test_create_备份索引库与wiki目录(env, monkeypatch):
    _, idx, l1, l2 = env
    (l1 / "a__s1.md").write_text("页面内容", encoding="utf-8")
    r = snapshot.create(reason="测试")
    assert r["parts"] == ["session_rag.db", "wiki_l1", "wiki_l2"]
    assert r["keep"] == snapshot.keep_count()      # 返回值带上生效的保留上限
    conn = sqlite3.connect(r["path"] + "/session_rag.db")
    assert conn.execute("SELECT content FROM distilled_memories").fetchone()[0] == "原始内容"
    conn.close()
    from pathlib import Path as _P
    assert (_P(r["path"]) / "wiki_l1" / "a__s1.md").exists()


def test_restore_把改坏的数据恢复为快照状态(env):
    _, idx, l1, l2 = env
    (l1 / "a__s1.md").write_text("页面内容", encoding="utf-8")
    r = snapshot.create(reason="回滚锚")
    # 模拟"改坏"：内容被覆盖 + wiki 页面被删
    conn = sqlite3.connect(idx)
    conn.execute("UPDATE distilled_memories SET content='被污染的内容'")
    conn.commit()
    conn.close()
    (l1 / "a__s1.md").write_text("被污染的页面", encoding="utf-8")
    # 回滚
    res = snapshot.restore(r["id"])
    assert set(res["restored"]) == {"session_rag.db", "wiki_l1", "wiki_l2"}
    conn = sqlite3.connect(idx)
    assert conn.execute("SELECT content FROM distilled_memories").fetchone()[0] == "原始内容"
    conn.close()
    assert (l1 / "a__s1.md").read_text(encoding="utf-8") == "页面内容"
    # 防误恢复保护快照存在
    assert snapshot.list_snapshots()[0]["reason"].startswith("restore")


def test_保留策略_由配置决定保留份数(env, monkeypatch):
    _pin_keep(monkeypatch, 2)
    assert snapshot.keep_count() == 2
    for i in range(3):
        snapshot.create(reason=f"第{i}份")
    ids = [s["id"] for s in snapshot.list_snapshots()]
    assert len(ids) == 2                      # 只剩最近 2 份
    assert snapshot.list_snapshots()[0]["reason"] == "第2份"


def test_保留策略_调大时全部留存(env, monkeypatch):
    _pin_keep(monkeypatch, 9)
    for i in range(3):
        snapshot.create(reason=f"第{i}份")
    assert len(snapshot.list_snapshots()) == 3
    assert snapshot.keep_count() == 9


@pytest.mark.parametrize("bad", [0, -1, "abc", None, "  "])
def test_保留策略_非法配置回退内置默认(env, monkeypatch, bad):
    """配置写错不能让历史快照被清空——非法值一律回退默认。"""
    _pin_keep(monkeypatch, bad)
    assert snapshot.keep_count() == snapshot.KEEP
    for i in range(snapshot.KEEP + 2):
        snapshot.create(reason=f"第{i}份")
    assert len(snapshot.list_snapshots()) == snapshot.KEEP


def test_保留策略_默认配置为5份(env):
    """内置默认值（用户可见的契约）；本地 yaml 若另配则按配置走。"""
    assert snapshot.KEEP == 5
    assert snapshot.keep_count() >= 1


def test_restore_不存在的快照明确报错(env):
    with pytest.raises(ValueError):
        snapshot.restore("nope_12345678")
