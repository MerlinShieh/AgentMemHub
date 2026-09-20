# -*- coding: utf-8 -*-
"""快照 HTTP 接口测试：列表 / 创建 / 回滚（后台任务）端到端。"""
from __future__ import annotations

import sqlite3

import pytest


@pytest.fixture()
def snap_env(tmp_path, monkeypatch):
    """沙箱：快照目标全部指向临时路径；返回 TestClient。"""
    from fastapi.testclient import TestClient

    from agentmemhub import snapshot
    from agentmemhub.web.app import create_app

    idx = tmp_path / "session_rag.db"
    conn = sqlite3.connect(idx)
    conn.execute("CREATE TABLE distilled_memories (id INTEGER PRIMARY KEY,"
                 " content TEXT, content_hash TEXT)")
    conn.execute("INSERT INTO distilled_memories VALUES (1,'原始内容','h1')")
    conn.commit()
    conn.close()
    l1 = tmp_path / "out_l1"
    l2 = tmp_path / "out_l2"
    l1.mkdir()
    l2.mkdir()
    (l1 / "a__s1.md").write_text("页面内容", encoding="utf-8")
    monkeypatch.setattr(snapshot, "_index_db", lambda: idx)
    monkeypatch.setattr(snapshot, "_wiki_dirs", lambda: (l1, l2))

    app = create_app()
    client = TestClient(app)
    return client, idx, l1


def test_接口_列表为空时明确标记(snap_env):
    client, idx, l1 = snap_env
    r = client.get("/api/snapshots")
    assert r.status_code == 200
    assert r.json()["snapshots"] == []
    # 保留上限一并下发，面板/调用方可据此提示"还能留几份"
    assert r.json()["keep"] >= 1


def test_接口_创建与列表(snap_env):
    client, idx, l1 = snap_env
    r = client.post("/api/snapshots/create?reason=测试快照")
    assert r.status_code == 200
    assert r.json()["reason"] == "测试快照"
    lst = client.get("/api/snapshots").json()["snapshots"]
    assert len(lst) == 1 and lst[0]["reason"] == "测试快照"


def test_接口_回滚闭环_改坏后恢复(snap_env):
    client, idx, l1 = snap_env
    snap_id = client.post("/api/snapshots/create?reason=锚").json()["id"]
    # 改坏：内容覆盖 + 页面覆盖
    conn = sqlite3.connect(idx)
    conn.execute("UPDATE distilled_memories SET content='被污染'")
    conn.commit()
    conn.close()
    (l1 / "a__s1.md").write_text("被污染", encoding="utf-8")
    # 回滚（后台任务——TestClient 下同步等待其完成不现实，轮询任务状态）
    r = client.post(f"/api/snapshots/restore?snapshot_id={snap_id}")
    assert r.status_code == 200
    job = r.json()["job"]
    # 轮询任务直到完成（tasks.submit 的线程很快结束）
    import time
    from agentmemhub.web import tasks
    for _ in range(50):
        st = tasks.status()
        if st and st["status"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert st["status"] == "done", st.get("error")
    assert "被污染" not in (l1 / "a__s1.md").read_text(encoding="utf-8")
    assert "页面内容" in (l1 / "a__s1.md").read_text(encoding="utf-8")
    conn = sqlite3.connect(idx)
    assert conn.execute("SELECT content FROM distilled_memories").fetchone()[0] == "原始内容"


def test_接口_回滚不存在的快照返回409或错误(snap_env):
    client, idx, l1 = snap_env
    r = client.post("/api/snapshots/restore?snapshot_id=nope_123")
    # 后台任务执行时才失败——任务状态应为 error
    assert r.status_code == 200
    from agentmemhub.web import tasks
    import time
    for _ in range(50):
        st = tasks.status()
        if st and st["status"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert st["status"] == "error"
