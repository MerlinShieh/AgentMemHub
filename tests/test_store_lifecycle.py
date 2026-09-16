"""Store 生命周期测试：用户修改在 ingest 循环中不被覆盖（P1 核心保证）。

覆盖三类修改 × 三条写入路径（replace_source 整源重写 / upsert_sessions
会话级增量）的组合，以及全局会话 uid 的稳定性与只增不减。
"""
from __future__ import annotations

import pytest

from agentmemhub.models import Event, renumber
from agentmemhub.store import Store


def _sess(sid: str = "a", title: str = "原标题", *, updated: int = 1,
          n_events: int = 1, content: str = "正文"):
    evs = renumber([Event(role="user", content=f"{content} {i}", time=updated + i)
                    for i in range(n_events)])
    return {"source": "zcode", "id": sid, "title": title, "cwd": "w",
            "created_at": 1, "updated_at": updated, "model": "m",
            "meta": {}, "events": evs}


def _row(store: Store, sid: str = "a", source: str = "zcode"):
    store.conn.row_factory = __import__("sqlite3").Row
    r = store.conn.execute(
        "SELECT session_uid, title, title_custom FROM conversations"
        " WHERE source=? AND id=?", (source, sid)).fetchone()
    return r


def _tomb(store: Store, sid: str = "a", source: str = "zcode"):
    store.conn.row_factory = __import__("sqlite3").Row
    return store.conn.execute(
        "SELECT session_uid, title FROM deleted_conversations"
        " WHERE source=? AND id=?", (source, sid)).fetchone()


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def test_new_sessions_get_incrementing_uid(store):
    """新会话分配全局递增 uid。"""
    store.replace_source("zcode", [_sess("a"), _sess("b")])
    assert _row(store, "a")["session_uid"] == 1
    assert _row(store, "b")["session_uid"] == 2


def test_uid_stable_across_rewrite(store):
    """会话内容变化被重写后 uid 保持不变（同一会话的身份不漂移）。"""
    store.upsert_sessions("zcode", [_sess("a", updated=1)])
    uid1 = _row(store, "a")["session_uid"]
    store.upsert_sessions("zcode", [_sess("a", updated=2, n_events=2)])
    assert _row(store, "a")["session_uid"] == uid1


def test_renamed_title_survives_upsert(store):
    """改标题 → 会话级增量重写 → 用户标题保留（不被源端标题覆盖）。"""
    store.upsert_sessions("zcode", [_sess("a", updated=1)])
    assert store.update_title("zcode", "a", "用户起的名字")
    store.upsert_sessions("zcode", [_sess("a", title="源端新标题", updated=2,
                                          n_events=2)])
    row = _row(store, "a")
    assert row["title"] == "用户起的名字"
    assert row["title_custom"] == 1


def test_renamed_title_survives_replace_source(store):
    """改标题 → 整源重写 → 用户标题保留。"""
    store.upsert_sessions("zcode", [_sess("a", updated=1)])
    store.update_title("zcode", "a", "用户起的名字")
    store.replace_source("zcode", [_sess("a", title="源端标题", updated=3,
                                         n_events=3)])
    row = _row(store, "a")
    assert row["title"] == "用户起的名字"
    assert row["title_custom"] == 1


def test_untouched_title_follows_source(store):
    """未改过标题的会话：重写时正常采用源端标题（title_custom=0）。"""
    store.upsert_sessions("zcode", [_sess("a", title="旧标题", updated=1)])
    store.replace_source("zcode", [_sess("a", title="源端新标题", updated=2,
                                         n_events=2)])
    row = _row(store, "a")
    assert row["title"] == "源端新标题"
    assert row["title_custom"] == 0


def test_deleted_session_not_revived_by_upsert(store):
    """删除会话 → 源端仍有该会话 → upsert 不复活（墓碑语义）。"""
    store.upsert_sessions("zcode", [_sess("a", updated=1)])
    uid = _row(store, "a")["session_uid"]
    store.delete_conversation("zcode", "a")

    r = store.upsert_sessions("zcode", [_sess("a", updated=2, n_events=2)])
    assert r["tombstoned"] == 1
    assert _row(store, "a") is None                        # 未复活
    tomb = _tomb(store, "a")
    assert tomb["session_uid"] == uid                      # 墓碑带 uid


def test_deleted_session_not_revived_by_replace_source(store):
    """删除会话 → 整源重写 → 不复活（replace_source 同受墓碑约束）。"""
    store.upsert_sessions("zcode", [_sess("a", updated=1)])
    store.delete_conversation("zcode", "a")
    store.replace_source("zcode", [_sess("a", updated=2, n_events=2)])
    assert _row(store, "a") is None


def test_deleted_uid_never_reused(store):
    """uid 只增不减：删除 uid=1 的会话后，新会话从 2 起分配（不复用）。"""
    store.upsert_sessions("zcode", [_sess("a", updated=1)])
    uid1 = _row(store, "a")["session_uid"]
    store.delete_conversation("zcode", "a")
    store.upsert_sessions("zcode", [_sess("b", updated=1)])
    uid_b = _row(store, "b")["session_uid"]
    assert uid_b > uid1                                    # 不复用被删 uid


def test_tombstone_persists_across_backfill(store):
    """uid 回填基准含墓碑：已有数据 + 墓碑场景下新分配仍只增不减。"""
    store.upsert_sessions("zcode", [_sess("a", updated=1), _sess("b", updated=1)])
    store.delete_conversation("zcode", "b")                # 墓碑带 uid=2
    store.upsert_sessions("zcode", [_sess("c", updated=1)])
    assert _row(store, "c")["session_uid"] == 3            # 不复用 uid=2
