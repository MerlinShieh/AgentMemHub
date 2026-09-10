"""R6 记忆排除 API 端点测试：标记/取消/查询 + 事件流带出排除态 + 批量任务。

用临时库 + TestClient；索引侧以 monkeypatch 桩替换（不触真实 session_rag.db）。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from agentmemhub.models import Event, renumber
from agentmemhub.store import Store


@pytest.fixture()
def store():
    tmp = Path(tempfile.mkdtemp()) / "excl_web.db"
    s = Store(tmp)
    events = renumber([
        Event(role="user", content="第一轮问题：批处理报错", time=1750000001,
              src_id="p:1", turn_key="tk1"),
        Event(role="assistant", content="第一轮回答：括号要转义", time=1750000002,
              src_id="p:2", turn_key="tk1"),
        Event(role="user", content="第二轮问题：向量检索评测怎么扩", time=1750000003,
              src_id="p:3", turn_key="tk2"),
        Event(role="assistant", content="第二轮回答：用改写式查询防偷题", time=1750000004,
              src_id="p:4", turn_key="tk2"),
    ])
    s.replace_source("zcode", [{
        "source": "zcode", "id": "sess_x", "title": "排除测试",
        "cwd": "D:/proj/X", "created_at": 1750000001, "updated_at": 1750000004,
        "model": "m", "meta": {}, "events": events,
    }], signature="t")
    yield s
    s.close()


@pytest.fixture()
def client(store, monkeypatch):
    from fastapi.testclient import TestClient

    from agentmemhub.web import app as webapp

    # 索引侧桩：记录清理调用，不触真实库
    purges: list[tuple] = []
    monkeypatch.setattr(webapp, "_purge_index",
                        lambda src, cid, turn_key=None:
                        purges.append((src, cid, turn_key)) or 3)
    monkeypatch.setattr(webapp, "_index_counts",
                        lambda src, cid, tks=None, whole=False: (10, 3))
    c = TestClient(webapp.create_app(store.db_path))
    c.purges = purges          # type: ignore[attr-defined]
    return c


def test_exclusion_endpoints_roundtrip(client, store):
    base = "/api/conversations/zcode/sess_x/memory-exclusion"
    # 初始：无排除
    r = client.get(base)
    assert r.status_code == 200
    body = r.json()
    assert body["whole"] is False and body["turns"] == []
    assert body["indexedUnits"] == 10 and body["removedUnits"] == 3
    assert body["peerTurns"] == {}
    # 整会话排除
    r = client.post(base, json={"turn_key": "", "note": "含敏感内容"})
    assert r.status_code == 200
    body = r.json()
    assert body["excluded"] is True and body["unitsRemoved"] == 3
    assert ("zcode", "sess_x", None) in client.purges, "整会话 → 清整会话索引"
    assert client.get(base).json()["whole"] is True
    # 取消
    r = client.delete(base)
    assert r.status_code == 200 and r.json()["excluded"] is False
    assert client.get(base).json()["whole"] is False


def test_turn_level_exclusion_passes_turn_key(client, store):
    base = "/api/conversations/zcode/sess_x/memory-exclusion"
    r = client.post(base, json={"turn_key": "tk2"})
    assert r.status_code == 200
    assert ("zcode", "sess_x", "tk2") in client.purges, "轮次级 → 只清该轮"
    assert client.get(base).json()["turns"] == ["tk2"]
    r = client.delete(base + "?turn_key=tk2")
    assert r.status_code == 200 and r.json()["removed"] is True


def test_events_carry_exclusion_state(client, store):
    store.add_exclusion("zcode", "sess_x", "tk2")
    r = client.get("/api/conversations/zcode/sess_x/events?offset=0&limit=10")
    assert r.status_code == 200
    body = r.json()
    assert body["exclusion"] == {"whole": False, "turns": ["tk2"]}
    # tk2 的两条带 ex 标记，tk1 的不带
    marked = [e for e in body["events"] if e.get("ex")]
    assert len(marked) == 2
    assert all(e["tk"] == "tk2" for e in marked)


def test_whole_exclusion_marks_all_events(client, store):
    store.add_exclusion("zcode", "sess_x")
    r = client.get("/api/conversations/zcode/sess_x/events?offset=0&limit=10")
    assert all(e.get("ex") for e in r.json()["events"])


def test_exclusion_404_on_unknown_conversation(client):
    r = client.post("/api/conversations/zcode/nope/memory-exclusion", json={})
    assert r.status_code == 404
    assert client.get("/api/conversations/zcode/nope/memory-exclusion"
                      ).status_code == 404


def test_delete_conversation_purges_index(client, store):
    """R6 修复验证：删除会话必须同步清索引（否则已删会话仍可被召回）。"""
    r = client.delete("/api/conversations/zcode/sess_x")
    assert r.status_code == 200
    assert ("zcode", "sess_x", None) in client.purges


def test_turn_exclusion_sync_peers(client, store):
    """跨源副本一并排除：同 turn_key 在别的 source 下也落标记 + 清索引。"""
    # 造一份"另一 Agent 采集的同一轮"（同 turn_key，不同 source）
    s2 = Store(store.db_path)
    try:
        s2.replace_source("zcode-import", [{
            "source": "zcode-import", "id": "imported_copy", "title": "导入副本",
            "cwd": "/x", "created_at": 1750000001, "updated_at": 1750000003,
            "model": "m", "meta": {},
            "events": renumber([
                Event(role="user", content="第二轮问题：向量检索评测怎么扩",
                      time=1750000003, src_id="q:3", turn_key="tk2"),
            ]),
        }], signature="t2")
    finally:
        s2.close()

    base = "/api/conversations/zcode/sess_x/memory-exclusion"
    # 副本提示：排除 tk2 前，GET 应能看到 peers
    r = client.post(base, json={"turn_key": "tk2", "sync_peers": True})
    assert r.status_code == 200
    body = r.json()
    assert ("zcode-import", "imported_copy", "tk2") in client.purges
    assert any(pe["id"] == "imported_copy" for pe in body["peers"])
    # 副本的标记也落了
    whole, turns = store.exclusion_state("zcode-import", "imported_copy")
    assert "tk2" in turns


def test_peer_hint_when_not_synced(client, store):
    """不勾选同步时，GET 应提示存在副本。"""
    s2 = Store(store.db_path)
    try:
        s2.replace_source("zcode-import", [{
            "source": "zcode-import", "id": "copy2", "title": "副本2",
            "cwd": "/x", "created_at": 1750000001, "updated_at": 1750000003,
            "model": "m", "meta": {},
            "events": renumber([Event(role="user", content="第二轮问题",
                                      time=1750000003, src_id="r:3",
                                      turn_key="tk2")]),
        }], signature="t3")
    finally:
        s2.close()
    base = "/api/conversations/zcode/sess_x/memory-exclusion"
    client.post(base, json={"turn_key": "tk2"})          # 不同步副本
    body = client.get(base).json()
    assert any(p["id"] == "copy2" for p in body["peerTurns"].get("tk2", []))
