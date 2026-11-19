"""记忆生命周期 service（M3 数据层）：候选分层 / 归档 / 恢复 / 概览 / 审计。

设计见 `docs/lifecycle-panel-plan.md`。这里守四件事：

1. **候选清单要把用户表过态的排除掉**（2026-11-19 修的真 bug：6 条已归档混进来）；
2. **有证据的排最前**（superseded 旧方），其余按 stale_score；
3. **归档必须双层退场 + 双投影一起**（只锁一层等于没归档，2026-11-07 实测）；
4. **恢复是归档的镜像**，且批量有上限。
"""
from __future__ import annotations

import sqlite3
import time

import pytest

from agentmemhub import conflicts, lifecycle
from agentmemhub.rag.ingest import open_index
from agentmemhub.rag.memstore import ensure_memstore_schema

DAY = 86400


def _mk_db(tmp_path):
    """最小但**真实**的索引库：用生产 schema 建表，只插需要的数据。"""
    db = tmp_path / "idx_lifecycle.db"
    conn = open_index(db)
    ensure_memstore_schema(conn)
    from agentmemhub.distill import ensure_distill_schema
    ensure_distill_schema(conn)
    conflicts.ensure_conflicts_schema(conn)
    conn.commit()
    return db, conn


def _add(conn, *, src_id, text="一条很老的记忆", age_days=100, role="distilled",
         source="zcode", manual=None):
    ts = int(time.time()) - age_days * DAY
    with conn:
        cur = conn.execute(
            "INSERT INTO units(source, conversation_id, seq, role, turn_key,"
            " src_id, time, title, text, chars)"
            " VALUES(?,?,?,?,?,?,?,'标题',?,?)",
            (source, "conv", abs(hash(src_id)) % 100000, role, "tk",
             src_id, ts, text, len(text)))
        uid = cur.lastrowid
        if manual is not None:
            conn.execute("INSERT OR REPLACE INTO unit_values(unit_id, value,"
                         " r_human, priority, updated_at, manual_value)"
                         " VALUES(?,?,0,0,?,?)", (uid, 0.3, ts, manual))
    return uid


# ── 候选清单 ────────────────────────────────────────────────────────────

def test_候选排除已归档(tmp_path):
    """`manual_value=0`（已归档）不该再出现在候选里 —— 修的就是这个 bug。

    修复前实测有 6 条已归档条目混在 1076 条候选里：点归档也没反应
    （本来就是 0），等于让人重复确认一件做过的事。
    """
    db, conn = _mk_db(tmp_path)
    try:
        arch = _add(conn, src_id="dst_arch", manual=0.0)
        alive = _add(conn, src_id="dst_alive")
    finally:
        conn.close()
    ids = [r["id"] for r in lifecycle.stale_candidates(db)]
    assert alive in ids
    assert arch not in ids, "已归档的记忆不该出现在候选清单"


def test_候选排除用户加权(tmp_path):
    """⭐ 加权过的记忆是用户意志，不该被建议清理。"""
    db, conn = _mk_db(tmp_path)
    try:
        pinned = _add(conn, src_id="dst_pin", manual=0.8)
        alive = _add(conn, src_id="dst_alive")
    finally:
        conn.close()
    ids = [r["id"] for r in lifecycle.stale_candidates(db)]
    assert alive in ids
    assert pinned not in ids


def test_候选够老的才进(tmp_path):
    """没满窗口的记忆不进候选。"""
    db, conn = _mk_db(tmp_path)
    try:
        _add(conn, src_id="dst_new", age_days=10)
        old = _add(conn, src_id="dst_old", age_days=100)
    finally:
        conn.close()
    ids = [r["id"] for r in lifecycle.stale_candidates(db, days=90)]
    assert ids == [old]


def test_未处置冲突的旧方排在前面且带标记(tmp_path):
    """有 `open` 冲突的旧方是唯一"有证据"的候选，必须排最前并带 tier。

    ⚠️ 不能拿 `superseded` 旧方当例子：`resolve('superseded')` 会**立刻归档**
    旧方（`manual_value=0`），它同时满足候选排除条件 —— 实测那个状态恒为空
    （2026-11-19 在真实库验证，`superseded` 31 条、旧方在候选里 0 条）。
    """
    db, conn = _mk_db(tmp_path)
    try:
        old = _add(conn, src_id="dst_old", age_days=120)
        plain = _add(conn, src_id="dst_plain", age_days=100)
        with conn:
            conn.execute(
                "INSERT INTO memory_conflicts(unit_new, unit_old, kind,"
                " rule_hit, similarity, status, detail_json, detected_at)"
                " VALUES(?,?,'evolution','model',0.95,'open','{}',?)",
                (plain, old, int(time.time())))
    finally:
        conn.close()
    rows = lifecycle.stale_candidates(db)
    assert rows[0]["id"] == old, "旧方必须排第一"
    assert rows[0]["tier"] == lifecycle.TIER_CONFLICT_OLD
    assert all(r["tier"] == lifecycle.TIER_NEVER_HIT for r in rows[1:])


def test_已处置冲突的旧方不算证据(tmp_path):
    """`dismissed` / `kept_both` 是用户已经表过态的，不能继续打"冲突"标记。"""
    db, conn = _mk_db(tmp_path)
    try:
        old = _add(conn, src_id="dst_old", age_days=120)
        plain = _add(conn, src_id="dst_plain", age_days=100)
        with conn:
            conn.execute(
                "INSERT INTO memory_conflicts(unit_new, unit_old, kind,"
                " rule_hit, similarity, status, detail_json, detected_at)"
                " VALUES(?,?,'evolution','model',0.95,'dismissed','{}',?)",
                (plain, old, int(time.time())))
    finally:
        conn.close()
    rows = lifecycle.stale_candidates(db)
    assert all(r["tier"] == lifecycle.TIER_NEVER_HIT for r in rows)


# ── 归档 ────────────────────────────────────────────────────────────────

def _dual(conn, *, manual=None):
    """建一对双投影（`dst_h` / `mcp_h` 共享 content_hash）+ 蒸馏行。"""
    ts = int(time.time()) - 120 * DAY
    ids = {}
    for prefix in ("dst", "mcp"):
        cur = conn.execute(
            "INSERT INTO units(source, conversation_id, seq, role, turn_key,"
            " src_id, time, title, text, chars)"
            " VALUES('zcode','conv',?,'distilled','tk',?,?,'标题','同一内容',4)",
            (abs(hash(prefix)) % 100000, f"{prefix}_cafe01", ts))
        ids[prefix] = cur.lastrowid
        if manual is not None:
            conn.execute("INSERT OR REPLACE INTO unit_values(unit_id, value,"
                         " r_human, priority, updated_at, manual_value)"
                         " VALUES(?,?,0,0,?,?)",
                         (ids[prefix], 0.3, ts, manual))
    conn.execute(
        "INSERT INTO distilled_memories(source, conversation_id, type, content,"
        " confidence, status, content_hash, prompt_ver, created_at)"
        " VALUES('zcode','conv','fact','同一内容','high','new','cafe01','v1',?)",
        (ts,))
    conn.commit()
    return ids


def test_归档必须双层退场且双投影一起(tmp_path):
    db, conn = _mk_db(tmp_path)
    try:
        ids = _dual(conn)
    finally:
        conn.close()

    r = lifecycle.archive_units(db, [ids["dst"]], by="panel", reason="测试")
    assert len(r["changed"]) == 1 and not r["errors"]

    conn = sqlite3.connect(db)
    try:
        locked = {u: mv for u, mv in conn.execute(
            "SELECT unit_id, manual_value FROM unit_values")}
        assert locked[ids["dst"]] == 0, "被归档的那行必须锁 0"
        assert locked[ids["mcp"]] == 0, "双投影另一行也必须锁（否则等于没归档）"
        tagged = conn.execute(
            "SELECT wiki_ignore_at FROM distilled_memories"
            " WHERE content_hash='cafe01'").fetchone()[0]
        assert tagged, "必须同时写 wiki_ignore_at，否则 wiki 还在教旧结论"
    finally:
        conn.close()


def test_归档幂等_已归档进_skipped(tmp_path):
    db, conn = _mk_db(tmp_path)
    try:
        ids = _dual(conn)
    finally:
        conn.close()
    lifecycle.archive_units(db, [ids["dst"]])
    r = lifecycle.archive_units(db, [ids["dst"]])
    assert not r["changed"]
    assert r["skipped"][0]["why"] == "已归档"


def test_归档不存在的id进_skipped(tmp_path):
    db, conn = _mk_db(tmp_path)
    conn.close()
    r = lifecycle.archive_units(db, [999999])
    assert not r["changed"]
    assert r["skipped"][0]["why"] == "不存在"


def test_批量上限(tmp_path):
    db, conn = _mk_db(tmp_path)
    conn.close()
    with pytest.raises(ValueError, match="最多"):
        lifecycle.archive_units(db, list(range(1, lifecycle.MAX_BATCH + 2)))


# ── 恢复 ────────────────────────────────────────────────────────────────

def test_恢复是归档的镜像(tmp_path):
    db, conn = _mk_db(tmp_path)
    try:
        ids = _dual(conn)
    finally:
        conn.close()
    lifecycle.archive_units(db, [ids["dst"]])
    r = lifecycle.restore_units(db, [ids["dst"]])
    assert len(r["changed"]) == 1

    conn = sqlite3.connect(db)
    try:
        mvs = {u: mv for u, mv in conn.execute(
            "SELECT unit_id, manual_value FROM unit_values")}
        assert mvs[ids["dst"]] is None and mvs[ids["mcp"]] is None, \
            "双投影两侧都要恢复"
        tagged = conn.execute(
            "SELECT wiki_ignore_at FROM distilled_memories"
            " WHERE content_hash='cafe01'").fetchone()[0]
        assert not tagged, "恢复必须清 wiki_ignore_at，否则不进 wiki"
    finally:
        conn.close()

    # 恢复后重新出现在候选里（闭环）
    assert ids["dst"] in [x["id"] for x in lifecycle.stale_candidates(db)]


def test_候选对双投影只出现一条(tmp_path):
    """`dst_` / `mcp_` 是同一内容的两行投影，候选里不能出现两条。

    不去重的话，面板上归档掉一条、另一条还在，用户会以为没生效
    （底层其实会把两侧一起锁，但视图先错了）。2026-11-19 由 M3.2 的
    接口测试撞出来：同一内容恢复后候选从 0 变 2。
    """
    db, conn = _mk_db(tmp_path)
    try:
        ids = _dual(conn)
    finally:
        conn.close()
    rows = lifecycle.stale_candidates(db)
    assert len(rows) == 1, "同一内容只该有一条候选"
    # 保留的是蒸馏正本 `dst_`
    assert rows[0]["src_id"].startswith("dst_")


def test_已归档列表可恢复且双投影去重(tmp_path):
    """归档后要能列出来（面板的"恢复"抽屉靠它），且同一内容只出现一条。"""
    db, conn = _mk_db(tmp_path)
    try:
        ids = _dual(conn)
    finally:
        conn.close()
    lifecycle.archive_units(db, [ids["dst"]])
    rows = lifecycle.archived_units(db)
    assert len(rows) == 1, "双投影归档后仍应只列一条"
    assert rows[0]["src_id"].startswith("dst_")


# ── 概览 ────────────────────────────────────────────────────────────────

def test_概览数字(tmp_path):
    db, conn = _mk_db(tmp_path)
    try:
        _add(conn, src_id="dst_a")
        _add(conn, src_id="dst_b", manual=0.0)
        _add(conn, src_id="dst_c", manual=0.9)
    finally:
        conn.close()
    s = lifecycle.summary(db)
    assert s["candidates"] == 1
    assert s["archived"] == 1
    assert s["pinned"] == 1
    assert s["conflicts_total"] == 0


# ── 审计 ────────────────────────────────────────────────────────────────

def test_归档与恢复都落审计(tmp_path):
    """`logs/memory.log` 必须能回答『谁在什么时候归档了什么』。"""
    from agentmemhub import logs

    db, conn = _mk_db(tmp_path)
    try:
        ids = _dual(conn)
    finally:
        conn.close()
    lifecycle.archive_units(db, [ids["dst"]], by="panel", reason="复核")
    lifecycle.restore_units(db, [ids["dst"]], by="panel")

    events = [e.get("event") for e in logs.read_memory_audit()]
    assert "lifecycle_archive" in events
    assert "lifecycle_restore" in events
