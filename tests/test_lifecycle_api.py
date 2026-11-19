"""生命周期 HTTP 接口（M3.2）：概览 / 候选 / 归档 / 恢复 / 冲突队列。

隔离：索引库指向临时文件（monkeypatch `load_settings`），采集库用临时库，
不触碰生产数据。CSRF 中间件对**不带 Origin 的请求放行**（curl / 脚本语义），
所以 TestClient 直接 POST 即可 —— 跨站拦截另由 `tests/test_guardrails.py` 守。
"""
from __future__ import annotations

import sqlite3
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agentmemhub import conflicts
from agentmemhub.rag.ingest import open_index
from agentmemhub.rag.memstore import ensure_memstore_schema

DAY = 86400


def _mk_db(tmp_path):
    db = tmp_path / "idx_lifecycle_api.db"
    conn = open_index(db)
    ensure_memstore_schema(conn)
    from agentmemhub.distill import ensure_distill_schema
    ensure_distill_schema(conn)
    conflicts.ensure_conflicts_schema(conn)
    conn.commit()
    conn.close()
    return db


def _add(conn, *, src_id, age_days=100, manual=None, text="一条很老的记忆"):
    ts = int(time.time()) - age_days * DAY
    cur = conn.execute(
        "INSERT INTO units(source, conversation_id, seq, role, turn_key,"
        " src_id, time, title, text, chars)"
        " VALUES('zcode','conv',?,'distilled','tk',?,?,'标题',?,?)",
        (abs(hash(src_id)) % 100000, src_id, ts, text, len(text)))
    uid = cur.lastrowid
    if manual is not None:
        conn.execute("INSERT OR REPLACE INTO unit_values(unit_id, value, r_human,"
                     " priority, updated_at, manual_value) VALUES(?,?,0,0,?,?)",
                     (uid, 0.3, ts, manual))
    conn.commit()
    return uid


def _dual(conn):
    """一对双投影 + 蒸馏行（归档要能验证"两侧一起锁"）。"""
    ts = int(time.time()) - 120 * DAY
    ids = {}
    for prefix in ("dst", "mcp"):
        cur = conn.execute(
            "INSERT INTO units(source, conversation_id, seq, role, turn_key,"
            " src_id, time, title, text, chars)"
            " VALUES('zcode','conv',?,'distilled','tk',?,?,'标题','同一内容',4)",
            (abs(hash(prefix)) % 100000, f"{prefix}_cafe01", ts))
        ids[prefix] = cur.lastrowid
    conn.execute(
        "INSERT INTO distilled_memories(source, conversation_id, type, content,"
        " confidence, status, content_hash, prompt_ver, created_at)"
        " VALUES('zcode','conv','fact','同一内容','high','new','cafe01','v1',?)",
        (ts,))
    conn.commit()
    return ids


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    monkeypatch.setattr("agentmemhub.rag.config.load_settings",
                        lambda: SimpleNamespace(index_db=str(db)))
    from agentmemhub.web.app import create_app
    client = TestClient(create_app(tmp_path / "admin.db"))
    return client, db


# ── 读 ──────────────────────────────────────────────────────────────────

def test_summary_returns_counts(env):
    client, db = env
    conn = sqlite3.connect(db)
    try:
        _add(conn, src_id="dst_a")
        _add(conn, src_id="dst_b", manual=0.0)
        _add(conn, src_id="dst_c", manual=0.9)
    finally:
        conn.close()
    r = client.get("/api/lifecycle/summary")
    assert r.status_code == 200
    body = r.json()
    assert body["candidates"] == 1
    assert body["archived"] == 1
    assert body["pinned"] == 1
    assert body["conflicts_total"] == 0


def test_stale_excludes_archived_and_has_tier(env):
    client, db = env
    conn = sqlite3.connect(db)
    try:
        alive = _add(conn, src_id="dst_alive")
        _add(conn, src_id="dst_arch", manual=0.0)
    finally:
        conn.close()
    r = client.get("/api/lifecycle/stale")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    item = body["items"][0]
    assert item["id"] == alive
    assert item["tier"] == "never_hit"


def test_stale_days_param_is_honoured(env):
    client, db = env
    conn = sqlite3.connect(db)
    try:
        _add(conn, src_id="dst_young", age_days=10)
        old = _add(conn, src_id="dst_old", age_days=100)
    finally:
        conn.close()
    assert client.get("/api/lifecycle/stale?days=90").json()["count"] == 1
    assert client.get("/api/lifecycle/stale?days=5").json()["items"][0]["id"] == old


def test_conflicts_list(env):
    client, db = env
    conn = sqlite3.connect(db)
    try:
        a = _add(conn, src_id="dst_a")
        b = _add(conn, src_id="dst_b")
        conn.execute(
            "INSERT INTO memory_conflicts(unit_new, unit_old, kind, rule_hit,"
            " similarity, status, detail_json, detected_at)"
            " VALUES(?,?,'evolution','model',0.95,'open','{}',?)",
            (b, a, int(time.time())))
        conn.commit()
    finally:
        conn.close()
    body = client.get("/api/lifecycle/conflicts").json()
    assert body["count"] == 1
    assert body["items"][0]["unit_new"] == b


# ── 归档 / 恢复 ─────────────────────────────────────────────────────────

def test_archive_two_layers_and_dual_projection(env):
    client, db = env
    conn = sqlite3.connect(db)
    try:
        ids = _dual(conn)
    finally:
        conn.close()
    r = client.post("/api/lifecycle/archive",
                    json={"ids": [ids["dst"]], "reason": "测试"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and len(body["changed"]) == 1

    conn = sqlite3.connect(db)
    try:
        mvs = dict(conn.execute("SELECT unit_id, manual_value FROM unit_values"))
        assert mvs[ids["dst"]] == 0 and mvs[ids["mcp"]] == 0, "双投影要一起锁"
        tagged = conn.execute("SELECT wiki_ignore_at FROM distilled_memories"
                              " WHERE content_hash='cafe01'").fetchone()[0]
        assert tagged, "必须同时退 wiki 编译层"
    finally:
        conn.close()


def test_archive_rejects_over_batch_limit(env):
    client, _ = env
    r = client.post("/api/lifecycle/archive",
                    json={"ids": list(range(1, 102))})
    assert r.status_code == 400
    assert "最多" in r.json()["detail"]


def test_archive_rejects_bad_ids(env):
    client, _ = env
    r = client.post("/api/lifecycle/archive", json={"ids": "not-a-list"})
    assert r.status_code == 400


def test_restore_returns_item_to_candidates(env):
    client, db = env
    conn = sqlite3.connect(db)
    try:
        ids = _dual(conn)
    finally:
        conn.close()
    client.post("/api/lifecycle/archive", json={"ids": [ids["dst"]]})
    assert client.get("/api/lifecycle/stale").json()["count"] == 0

    r = client.post("/api/lifecycle/restore", json={"ids": [ids["dst"]]})
    assert r.status_code == 200 and len(r.json()["changed"]) == 1
    # 双投影去重：`dst_` / `mcp_` 是同一内容的两行，候选里只能出现一条
    # （去重前这里是 2 —— M3.2 的接口测试撞出来的）
    assert client.get("/api/lifecycle/stale").json()["count"] == 1


def test_archived_list_after_archive(env):
    """归档后能从"已归档"里查到（面板恢复入口的数据源）。"""
    client, db = env
    conn = sqlite3.connect(db)
    try:
        ids = _dual(conn)
    finally:
        conn.close()
    assert client.get("/api/lifecycle/archived").json()["count"] == 0
    client.post("/api/lifecycle/archive", json={"ids": [ids["dst"]]})
    body = client.get("/api/lifecycle/archived").json()
    assert body["count"] == 1, "双投影去重后只该一条"
    assert body["items"][0]["src_id"].startswith("dst_")


# ── 冲突处置 ────────────────────────────────────────────────────────────

def _conflict(conn):
    old = _add(conn, src_id="dst_old", age_days=120)
    new = _add(conn, src_id="dst_new", age_days=100)
    cur = conn.execute(
        "INSERT INTO memory_conflicts(unit_new, unit_old, kind, rule_hit,"
        " similarity, status, detail_json, detected_at)"
        " VALUES(?,?,'evolution','model',0.95,'open','{}',?)",
        (new, old, int(time.time())))
    conn.commit()
    return cur.lastrowid, old, new


def test_resolve_superseded_archives_old(env):
    client, db = env
    conn = sqlite3.connect(db)
    try:
        cid, old, _ = _conflict(conn)
    finally:
        conn.close()
    r = client.post(f"/api/lifecycle/conflicts/{cid}/resolve",
                    json={"action": "superseded", "reason": "新结论取代"})
    assert r.status_code == 200
    assert r.json()["old_archived"] is True
    conn = sqlite3.connect(db)
    try:
        mv = conn.execute("SELECT manual_value FROM unit_values WHERE unit_id=?",
                          (old,)).fetchone()
        assert mv is not None and mv[0] == 0
    finally:
        conn.close()


def test_resolve_rejects_bad_action(env):
    client, db = env
    conn = sqlite3.connect(db)
    try:
        cid, _, _ = _conflict(conn)
    finally:
        conn.close()
    r = client.post(f"/api/lifecycle/conflicts/{cid}/resolve",
                    json={"action": "nuke"})
    assert r.status_code == 400
    assert "action" in r.json()["detail"]


def test_resolve_missing_conflict_404(env):
    client, _ = env
    r = client.post("/api/lifecycle/conflicts/999999/resolve",
                    json={"action": "kept_both"})
    assert r.status_code == 404
