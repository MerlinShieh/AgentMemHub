# -*- coding: utf-8 -*-
"""待更新记忆的查看与删除（软删除 / 硬删除）。

边界：本模块只处理**尚未进 wiki** 的记忆。已进 wiki 的记忆硬删会让页面里的
`[m<id>]` 变成死引用，那属于 docs/memory-deletion.md 的范畴（墓碑 + 引用重映射），
所以 `drop()` 会拒绝这类 id —— 这是本文件最该守住的约束。
"""
from __future__ import annotations

import sqlite3 as _sqlite3

from agentmemhub import wiki, wiki_manifest as wm, wiki_pending


def _mk(tmp_path, n=3):
    """最小场景：库 n 条 new / 一页引用 m1+m2 / manifest 只含前 n-1 条。

    最后一条因此成为"待更新"（在库里、但不在编译基线里）。

    用**真实 schema** 建库（open_index + ensure_distill_schema）而不是手写精简
    表：`open_index` 会建 units_fts 及其触发器，精简表与之不兼容。
    """
    from agentmemhub.distill import ensure_distill_schema
    from agentmemhub.rag.ingest import open_index

    db = tmp_path / "session_rag.db"
    conn = open_index(db)
    ensure_distill_schema(conn)
    for i in range(1, n + 1):
        # 真实 schema 里 prompt_ver 是 NOT NULL，且 (source, cid, content_hash)
        # 有唯一约束 —— 夹具必须照它来，否则要么插不进去、要么互相冲突
        conn.execute(
            "INSERT INTO distilled_memories"
            " (id, source, conversation_id, slice_key, type, confidence, status,"
            "  content, content_hash, prompt_ver, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (i, "a", "s1", "s0", "concept", "high", "new",
             "内容%d 这一条用来测试删除" % i, "h%d" % i, 1, 1000 + i))
    conn.commit()
    conn.close()

    l1 = tmp_path / "out_l1"
    l1.mkdir()
    (l1 / "a__s1.md").write_text(
        "---\ntitle: a / s1\nsource: a\nconversation_id: s1\n---\n\n# 页\n\n[m1] [m2]\n",
        encoding="utf-8")
    # manifest 只含前 n-1 条 → 第 n 条成为"待更新"
    wm.write_manifest(wm.manifest_path(l1, "l1"),
                      wm.build_manifest("l1", db, drop_ids={n}))
    return db, l1


def _count(db, mid):
    conn = _sqlite3.connect(db)
    try:
        return conn.execute("SELECT count(*) FROM distilled_memories WHERE id=?",
                            (mid,)).fetchone()[0]
    finally:
        conn.close()


def test_列出待更新的记忆明细(tmp_path):
    db, l1 = _mk(tmp_path, 3)
    r = wiki_pending.pending(l1_dir=l1, db=str(db))
    assert r["count"] == 1 and r["added_total"] == 1
    assert [it["id"] for it in r["items"]] == [3]
    it = r["items"][0]
    assert it["summary"].startswith("内容3")     # 带摘要（光有 id 没法判断该不该删）
    assert it["chars"] > 0 and it["type"] == "concept"


def test_软删除_从待更新消失_不触发重编_记忆仍在(tmp_path):
    db, l1 = _mk(tmp_path, 3)
    r = wiki_pending.drop(ids=[3], mode="soft", l1_dir=l1, db=str(db))
    assert r["dropped"] == 1 and r["mode"] == "soft"

    assert wiki_pending.pending(l1_dir=l1, db=str(db))["count"] == 0

    # 关键：软删一条**待更新**的记忆不触发重编 ——
    # 它本来就不在 manifest 基线里，编译输入去掉它，diff 依然是零
    assert wiki.align(l1, "l1", db=str(db))["needs_recompile"] is False

    assert _count(db, 3) == 1                    # 记忆本体仍在（仍可召回）
    conn = _sqlite3.connect(db)
    ignored_at = conn.execute(
        "SELECT wiki_ignore_at FROM distilled_memories WHERE id=3").fetchone()[0]
    conn.close()
    assert ignored_at


def test_取消忽略_重新变回待更新(tmp_path):
    db, l1 = _mk(tmp_path, 3)
    wiki_pending.drop(ids=[3], mode="soft", l1_dir=l1, db=str(db))
    assert wiki_pending.ignored(l1_dir=l1, db=str(db))["count"] == 1

    r = wiki_pending.restore(ids=[3], l1_dir=l1, db=str(db))
    assert r["restored"] == 1
    assert wiki_pending.ignored(l1_dir=l1, db=str(db))["count"] == 0
    assert wiki_pending.pending(l1_dir=l1, db=str(db))["count"] == 1


def test_硬删除_删行并重建manifest_不报失去输入(tmp_path):
    db, l1 = _mk(tmp_path, 3)
    r = wiki_pending.drop(ids=[3], mode="hard", confirm=True,
                          l1_dir=l1, db=str(db))
    assert r["dropped"] == 1
    assert _count(db, 3) == 0

    # manifest 已重建：库里行没了而基线还留着 → align 会报"失去输入" →
    # 明明页面里引用不到，却触发一次无意义的重编。必须避免。
    a = wiki.align(l1, "l1", db=str(db))
    assert a["stages"]["l1"]["removed_total"] == 0
    assert a["needs_recompile"] is False


def test_硬删除_缺confirm被拒且不删(tmp_path):
    db, l1 = _mk(tmp_path, 3)
    r = wiki_pending.drop(ids=[3], mode="hard", confirm=False,
                          l1_dir=l1, db=str(db))
    assert "confirm" in r["error"]
    assert _count(db, 3) == 1                    # 没删


def test_只允许删待更新的_id(tmp_path):
    """已进 wiki 的记忆（在基线里且未变更）不在待更新列表 → 拒绝。

    它们可能已被页面引用，硬删会产生死引用 —— 那是 memory-deletion.md 的
    范畴（墓碑 + 引用重映射/占位），本入口刻意不承担。
    """
    db, l1 = _mk(tmp_path, 3)
    r = wiki_pending.drop(ids=[1], mode="hard", confirm=True,
                          l1_dir=l1, db=str(db))
    assert r["dropped"] == 0
    assert [x["id"] for x in r["rejected"]] == [1]
    assert "不在待更新列表" in r["rejected"][0]["reason"]
    assert _count(db, 1) == 1                    # 确实没动


def test_混合请求_只删待更新的_其余如实回报(tmp_path):
    db, l1 = _mk(tmp_path, 3)
    r = wiki_pending.drop(ids=[1, 3], mode="soft", l1_dir=l1, db=str(db))
    assert r["dropped"] == 1
    assert [x["id"] for x in r["rejected"]] == [1]     # 被拒的不静默丢弃


def test_空ids与非法mode被拒(tmp_path):
    db, l1 = _mk(tmp_path, 3)
    assert "error" in wiki_pending.drop(ids=[], l1_dir=l1, db=str(db))
    r = wiki_pending.drop(ids=[3], mode="purge", l1_dir=l1, db=str(db))
    assert "mode" in r["error"]


def test_缺少manifest时给出明确错误(tmp_path):
    db, l1 = _mk(tmp_path, 3)
    wm.manifest_path(l1, "l1").unlink()
    r = wiki_pending.pending(l1_dir=l1, db=str(db))
    assert "manifest" in r["error"]
