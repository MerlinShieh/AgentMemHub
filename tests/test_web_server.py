"""AgentMemHub Web API 冒烟测试。

用临时 SQLite 库构造样例数据，经 FastAPI TestClient 验证各端点形状与状态码。
"""
import json
import tempfile
from pathlib import Path

import pytest

from agentmemhub.models import Event, renumber
from agentmemhub.store import Store


@pytest.fixture()
def store():
    tmp = Path(tempfile.mkdtemp()) / "test_web.db"
    s = Store(tmp)
    events = renumber([
        Event(role="user", content="帮我修复登录页面", time=1750000001),
        Event(role="reasoning", content="先看代码", time=1750000002),
        Event(role="tool", tool_name="Bash", tool_input={"command": "npm test"},
              tool_output="FAIL src/login", tool_status="completed", time=1750000003),
        Event(role="assistant", content="是事件监听器问题", model="test-model", time=1750000004),
        Event(role="patch", patch_file="src/login.ts", patch_diff="@@ -1 +1 @@", time=1750000005),
    ])
    s.replace_source("zcode", [{
        "source": "zcode", "id": "sess_1", "title": "修复登录",
        "cwd": "D:/proj/LoginApp", "created_at": 1750000001, "updated_at": 1750000005,
        "model": "test-model", "meta": {}, "events": events,
    }], signature="t")
    yield s
    s.close()


@pytest.fixture()
def client(store):
    from fastapi.testclient import TestClient
    from agentmemhub.web.app import create_app
    app = create_app(store.db_path)
    return TestClient(app)


def test_stats_shape(client):
    r = client.get("/api/stats")
    assert r.status_code == 200
    data = r.json()
    assert data["meta"]["totalConversations"] == 1
    assert data["meta"]["totalEvents"] == 5
    assert any(s["source"] == "zcode" for s in data["stats"]["sources"])
    assert {"user", "tool", "reasoning"} <= {r["role"] for r in data["stats"]["roles"]}
    assert len(data["stats"]["dailyTrend"]) >= 1


def test_facets(client):
    r = client.get("/api/facets")
    assert r.status_code == 200
    f = r.json()
    assert "zcode" in f["sources"]
    assert "LoginApp" in f["workspaces"]


def test_conversations_list_and_filters(client):
    r = client.get("/api/conversations?page=1&page_size=10")
    assert r.status_code == 200
    d = r.json()
    assert d["total"] == 1 and d["items"][0]["id"] == "sess_1"
    assert d["items"][0]["workspace"] == "LoginApp"          # camelCase 契约

    # source 过滤（无命中）
    r = client.get("/api/conversations?sources=opencode,qwen")
    assert r.json()["total"] == 0

    # workspace 过滤（命中）
    r = client.get("/api/conversations?workspace=LoginApp")
    assert r.json()["total"] == 1


def test_conversations_search(client):
    r = client.get("/api/conversations?q=监听器")       # reasoning 正文命中
    assert r.status_code == 200
    assert r.json()["total"] == 1


def test_events_short_keys(client):
    r = client.get("/api/conversations/zcode/sess_1/events")
    assert r.status_code == 200
    d = r.json()
    assert d["total"] == 5 and not d["capped"]
    evs = d["events"]
    tool = next(e for e in evs if e["r"] == "tool")
    assert tool["tn"] == "Bash" and "npm" in tool["ti"]
    patch = next(e for e in evs if e["r"] == "patch")
    assert patch["pf"] == "src/login.ts"


def test_events_404(client):
    assert client.get("/api/conversations/zcode/no_such/events").status_code == 404


def test_rename_and_delete_roundtrip(client, store):
    # 改标题
    r = client.patch("/api/conversations/zcode/sess_1/title",
                     json={"title": "新标题"})
    assert r.status_code == 200
    assert store.get_conversation("zcode", "sess_1")["title"] == "新标题"

    # 删除
    r = client.delete("/api/conversations/zcode/sess_1")
    assert r.status_code == 200
    assert r.json()["eventsRemoved"] == 5
    assert store.get_conversation("zcode", "sess_1") is None

    # 再删 → 404
    assert client.delete("/api/conversations/zcode/sess_1").status_code == 404


def test_folders_endpoint(client):
    r = client.get("/api/folders")
    assert r.status_code == 200
    folders = r.json()["folders"]
    entry = next(f for f in folders if f["workspace"] == "LoginApp")
    assert entry["bySource"] == {"zcode": 1}
