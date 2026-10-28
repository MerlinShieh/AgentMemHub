# -*- coding: utf-8 -*-
"""编译清单（manifest）测试：快照口径、round-trip、diff 的四类变更。

manifest 是 wiki 与 RAG 库对齐的锚 —— 它错了，增量检测就全错。所以重点测：

  · 口径：只收 `status IN ('new','similar')`（与两个编译脚本一致）
  · diff：added / changed / removed 三类一个都不能漏、不能错
  · removed 的细分：真删除 vs 状态迁移（处理方式不同，混了会误导）
  · 脏会话映射：变更必须落到正确的会话上
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from agentmemhub import wiki_manifest as wm


def _mkdb(tmp_path, rows):
    """rows: (id, source, cid, status, content)"""
    db = tmp_path / "session_rag.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE distilled_memories ("
        " id INTEGER PRIMARY KEY, source TEXT, conversation_id TEXT,"
        " status TEXT, content TEXT, content_hash TEXT, created_at INTEGER)")
    for i, (mid, src, cid, st, h) in enumerate(rows):
        conn.execute("INSERT INTO distilled_memories VALUES (?,?,?,?,?,?,?)",
                     (mid, src, cid, st, "内容%d" % mid, h, 1000 + i))
    conn.commit()
    conn.close()
    return db


ROWS = [
    (1, "a", "s1", "new", "h1"),
    (2, "a", "s1", "similar", "h2"),
    (3, "a", "s2", "new", "h3"),
    (4, "b", "s3", "duplicate", "h4"),   # 非输入口径
    (5, "b", "s3", "merged", "h5"),      # 非输入口径
]


# ---------------------------------------------------------------------------
# 快照口径
# ---------------------------------------------------------------------------

def test_快照只收_new_and_similar(tmp_path):
    db = _mkdb(tmp_path, ROWS)
    conn = sqlite3.connect(db)
    inputs, sessions = wm.snapshot_inputs(conn)
    conn.close()
    assert set(inputs) == {1, 2, 3}
    assert sessions == {"a/s1": [1, 2], "a/s2": [3]}


def test_快照_内容为NULL时指纹等于空串指纹(tmp_path):
    db = tmp_path / "s.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE distilled_memories ("
        " id INTEGER PRIMARY KEY, source TEXT, conversation_id TEXT,"
        " status TEXT, content TEXT, content_hash TEXT, created_at INTEGER)")
    conn.execute("INSERT INTO distilled_memories VALUES (1,'a','s1','new',NULL,'x',1)")
    conn.commit()
    inputs, _ = wm.snapshot_inputs(conn)
    conn.close()
    assert inputs[1] == wm._fingerprint(None)


# ---------------------------------------------------------------------------
# build / write / load
# ---------------------------------------------------------------------------

def test_build_manifest_结构完整且键为字符串(tmp_path):
    db = _mkdb(tmp_path, ROWS)
    m = wm.build_manifest("l1", db)
    assert m["stage"] == "l1" and m["n_inputs"] == 3
    assert set(m["inputs"]) == {"1", "2", "3"}          # json key 必须是 str
    assert m["inputs"]["1"] == wm._fingerprint("内容1")  # 指纹现算自 content
    assert m["sessions"]["a/s1"] == [1, 2]
    assert m["src_db"] == str(db)


def test_write_load_roundtrip(tmp_path):
    db = _mkdb(tmp_path, ROWS)
    p = tmp_path / "manifest_l1.json"
    wm.write_manifest(p, wm.build_manifest("l1", db))
    m2 = wm.load_manifest(p)
    assert m2 is not None and m2["n_inputs"] == 3
    json.dumps(m2, ensure_ascii=False)                   # 可序列化


def test_load_不存在与损坏都返回None(tmp_path):
    assert wm.load_manifest(tmp_path / "nope.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{oops", encoding="utf-8")
    assert wm.load_manifest(bad) is None


# ---------------------------------------------------------------------------
# diff：四类变更
# ---------------------------------------------------------------------------

def test_diff_无变化时全零(tmp_path):
    db = _mkdb(tmp_path, ROWS)
    m = wm.build_manifest("l1", db)
    d = wm.diff_manifest(m, db)
    assert d["added_total"] == d["changed_total"] == d["removed_total"] == 0
    assert d["current_inputs"] == 3


def test_diff_检测新增输入(tmp_path):
    db = _mkdb(tmp_path, ROWS)
    m = wm.build_manifest("l1", db)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO distilled_memories VALUES (6,'a','s1','new','内容6','h6',2000)")
    conn.commit(); conn.close()
    d = wm.diff_manifest(m, db)
    assert d["added_total"] == 1 and 6 in d["added"]
    assert d["dirty_sessions"]["a/s1"] == {"added": 1, "changed": 0, "removed": 0}


def test_diff_检测内容变更_绕过hash字段的直接改库也能抓到(tmp_path):
    """关键场景：修复脚本直接 UPDATE content 而不重算 content_hash ——
    靠库字段会漏报（实测踩过），自算指纹必须抓到。"""
    db = _mkdb(tmp_path, ROWS)
    m = wm.build_manifest("l1", db)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE distilled_memories SET content='改过的内容' WHERE id=2")
    conn.commit(); conn.close()
    d = wm.diff_manifest(m, db)
    assert d["changed_total"] == 1 and 2 in d["changed"]


def test_diff_removed细分_真删除与状态迁移(tmp_path):
    db = _mkdb(tmp_path, ROWS)
    m = wm.build_manifest("l1", db)
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM distilled_memories WHERE id=1")     # 真删除
    conn.execute("UPDATE distilled_memories SET status='merged' WHERE id=3")  # 迁移
    conn.commit(); conn.close()
    d = wm.diff_manifest(m, db)
    assert d["removed_total"] == 2
    assert d["removed_missing"] == [1]
    assert d["removed_reclassified"] == [3]


def test_diff_脏会话_变更落到正确会话(tmp_path):
    db = _mkdb(tmp_path, ROWS)
    m = wm.build_manifest("l1", db)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO distilled_memories VALUES (6,'a','s2','new','内容6','h6',2000)")
    conn.execute("UPDATE distilled_memories SET content='改过的内容' WHERE id=1")  # a/s1
    conn.commit(); conn.close()
    d = wm.diff_manifest(m, db)
    assert d["dirty_sessions"]["a/s2"]["added"] == 1
    assert d["dirty_sessions"]["a/s1"]["changed"] == 1
    assert d["dirty_session_total"] == 2


def test_diff_removed记忆的会话归属来自旧清单(tmp_path):
    """被删的记忆查不到当前归属 —— 必须用 manifest 里的旧归属定位脏会话。"""
    db = _mkdb(tmp_path, ROWS)
    m = wm.build_manifest("l1", db)
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM distilled_memories WHERE id=2")     # a/s1
    conn.commit(); conn.close()
    d = wm.diff_manifest(m, db)
    assert d["dirty_sessions"]["a/s1"]["removed"] == 1
