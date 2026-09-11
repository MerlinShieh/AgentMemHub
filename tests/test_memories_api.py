"""记忆报表 API 测试：UNION 查询、筛选、分页、会话联动、sessionUid 反查。

隔离策略：采集库与索引库都指向临时文件；rag_bridge.settings 用 configure
注入（数据路径指向临时目录），不触碰生产数据。
"""
from __future__ import annotations

import dataclasses
import sqlite3

import pytest
from fastapi.testclient import TestClient

from agentmemhub.models import Event, renumber
from agentmemhub import rag_bridge
from agentmemhub.store import Store


@pytest.fixture()
def memories_env(tmp_path, monkeypatch):
    """临时采集库 + 临时索引库，注入 rag_bridge.settings；返回 (client, paths)。"""
    from pathlib import Path

    from agentmemhub.rag.config import load_settings
    from agentmemhub.rag.ingest import open_index
    from agentmemhub.distill import ensure_distill_schema

    src_path = tmp_path / "agentmemhub.db"
    store = Store(src_path)
    evs = renumber([Event(role="user", content="会话一", time=1)])
    store.replace_source("zcode", [{"source": "zcode", "id": "c1", "title": "会话甲",
                                    "cwd": "w", "created_at": 1, "updated_at": 1,
                                    "model": "m", "meta": {}, "events": evs}],
                         signature="t")
    store.close()

    idx_path = tmp_path / "session_rag.db"
    conn = open_index(idx_path)
    conn.row_factory = sqlite3.Row
    ensure_distill_schema(conn)
    from agentmemhub.rag.memstore import ensure_memstore_schema
    ensure_memstore_schema(conn)
    # 两条蒸馏记忆（不同会话/类型）+ 一条手动记忆
    for i, (cid, mtype, content) in enumerate(
            (("c1", "decision", "会话一的蒸馏结论"),
             ("c1", "fact", "会话一的事实记录"),
             ("c2", "lesson", "会话二的踩坑教训"))):
        conn.execute(
            "INSERT INTO distilled_memories"
            "(source, conversation_id, slice_key, turn_key, type, topic, content,"
            " confidence, status, content_hash, prompt_ver, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("zcode", cid, "s0", f"tk{i}", mtype, f"主题{i}", content,
             "high", "new", f"h{i}", 1, 1000 + i))
    # units 投影 + 分值：h0/h1 已投影（dst_<hash>），h2 故意不投影
    for i, h in enumerate(("h0", "h1")):
        cur = conn.execute(
            "INSERT INTO units(source, conversation_id, seq, role, turn_key,"
            " src_id, time, text, chars) VALUES(?,?,?,?,?,?,?,?,?)",
            ("zcode", "c1", -1 - i, "distilled", f"tk{i}", f"dst_{h}",
             1000 + i, f"投影条目{i}", 5))
        conn.execute(
            "INSERT INTO unit_values(unit_id, value, updated_at) VALUES(?,?,?)",
            (cur.lastrowid, 0.3, 1000))
    cur = conn.execute(
        "INSERT INTO units(source, conversation_id, seq, role, turn_key,"
        " src_id, time, text, chars) VALUES('memory','mcp',1,'user','mcp',"
        "'mcp_manual1',500,'Agent 手动写入的记忆',9)")
    conn.execute(
        "INSERT INTO unit_values(unit_id, value, updated_at) VALUES(?,?,?)",
        (cur.lastrowid, 0.6, 500))
    conn.commit()
    conn.close()

    settings = dataclasses.replace(load_settings(), source_db=src_path,
                                   index_db=idx_path)
    rag_bridge.configure(settings)

    from agentmemhub.web.app import create_app as _ca
    client = TestClient(_ca(src_path))
    yield client, {"src": src_path, "idx": idx_path}
    rag_bridge.reset_settings()
    from agentmemhub.rag.runtime import reset_embedders
    reset_embedders()


def test_memories_union_distill_and_manual(memories_env):
    """记忆报表 = 蒸馏终稿 ∪ Agent 手动记忆（origin 区分）。"""
    client, _ = memories_env
    d = client.get("/api/memories", params={"status": "all"}).json()
    assert d["total"] == 4
    origins = {i["origin"] for i in d["items"]}
    assert origins == {"distilled", "manual"}


def test_memories_active_default_excludes_merged(memories_env):
    """默认只看有效终稿；归档（merged/duplicate）用 status=all 才出现。"""
    client, paths = memories_env
    c = sqlite3.connect(str(paths["idx"]))
    c.execute("UPDATE distilled_memories SET status='merged'"
              " WHERE content_hash='h0'")
    c.commit(); c.close()
    assert client.get("/api/memories").json()["total"] == 3
    assert client.get("/api/memories", params={"status": "all"}).json()["total"] == 4


def test_memories_filter_by_type(memories_env):
    d = client = memories_env[0].get(
        "/api/memories", params={"type": "lesson"}).json()
    assert d["total"] == 1 and d["items"][0]["type"] == "lesson"


def test_memories_filter_by_conversation(memories_env):
    """会话 → 记忆联动：按 conversationId 筛选。"""
    client, _ = memories_env
    d = client.get("/api/memories", params={"conversationId": "c1"}).json()
    assert d["total"] == 2
    assert {i["content"] for i in d["items"]} == {"会话一的蒸馏结论", "会话一的事实记录"}


def test_memories_filter_by_session_uid(memories_env):
    """全局会话 uid → 记忆（会话报表跳转记忆报表的绑定链路）。"""
    client, paths = memories_env
    c = sqlite3.connect(str(paths["src"]))
    uid = c.execute("SELECT session_uid FROM conversations WHERE id='c1'").fetchone()[0]
    c.close()
    d = client.get("/api/memories", params={"sessionUid": uid}).json()
    assert d["total"] == 2
    assert client.get("/api/memories", params={"sessionUid": 99999}).json()["total"] == 0


def test_memories_keyword_and_pagination(memories_env):
    client, _ = memories_env
    d = client.get("/api/memories", params={"q": "踩坑"}).json()
    assert d["total"] == 1
    p1 = client.get("/api/memories", params={"status": "all", "page_size": 2,
                                             "page": 1}).json()
    p2 = client.get("/api/memories", params={"status": "all", "page_size": 2,
                                             "page": 2}).json()
    assert len(p1["items"]) == 2 and len(p2["items"]) == 2
    ids1 = {(i["origin"], i["id"]) for i in p1["items"]}
    ids2 = {(i["origin"], i["id"]) for i in p2["items"]}
    assert not (ids1 & ids2), "分页不得重叠"


def test_memories_stats_shape(memories_env):
    d = memories_env[0].get("/api/memories").json()
    assert set(d["stats"].keys()) == {"byType", "byStatus", "bySource"}


def test_conversations_carry_session_uid(memories_env):
    """会话列表带全局 uid（会话报表 → 记忆报表的导航键）。"""
    client, paths = memories_env
    r = client.get("/api/conversations").json()
    rows = [x for x in r["items"] if x["id"] == "c1"]
    assert rows and isinstance(rows[0]["sessionUid"], int)


def test_memories_carry_unit_ref_and_value(memories_env):
    """报表带 unit_id / value / manual_value：面板 ⭐ 加权与 👍👎 的定位锚。

    未投影的归档条目 unit_id 为 null（该条不在召回面上，面板禁用加权）。
    """
    client, _ = memories_env
    d = client.get("/api/memories", params={"status": "all"}).json()
    by_content = {i["content"]: i for i in d["items"]}
    m = by_content["会话一的蒸馏结论"]                 # 已投影 dst_h0
    assert m["unit_id"] is not None and m["value"] == 0.3
    assert m["manual_value"] is None
    man = by_content["Agent 手动写入的记忆"]          # units.id 即 unit_id
    assert man["unit_id"] is not None and man["value"] == 0.6
    assert by_content["会话二的踩坑教训"]["unit_id"] is None


def test_memories_sort_by_value(memories_env):
    """sort=value：手动权重优先于自动值；无分条目沉底。"""
    client, paths = memories_env
    c = sqlite3.connect(str(paths["idx"]))
    c.execute("UPDATE unit_values SET manual_value=1.0 WHERE unit_id="
              "(SELECT id FROM units WHERE src_id='dst_h1')")
    c.commit(); c.close()
    d = client.get("/api/memories",
                   params={"status": "all", "sort": "value"}).json()
    vals = [i["manual_value"] if i["manual_value"] is not None else i["value"]
            for i in d["items"]]
    nums = [v for v in vals if v is not None]
    assert nums == sorted(nums, reverse=True)
    assert d["items"][0]["content"] == "会话一的事实记录"   # manual 1.0 居首
    assert d["items"][-1]["unit_id"] is None               # 无分沉底


def test_memories_sort_fields_and_order(memories_env):
    """表头排序：type/confidence/status/source/conversation + asc/desc。

    conversation 依赖 ATTACH 采集库按会话标题排序（标题在采集库）。
    """
    client, _ = memories_env
    d = client.get("/api/memories", params={"status": "all", "sort": "type",
                                            "order": "asc"}).json()
    types = [i["type"] for i in d["items"]]
    assert types == sorted(types)
    d2 = client.get("/api/memories", params={"status": "all", "sort": "conversation",
                                             "order": "asc"}).json()
    keys = [i["conversation_title"] or i["conversation_id"] for i in d2["items"]]
    assert keys == sorted(keys)
    d3 = client.get("/api/memories", params={"status": "all", "sort": "confidence",
                                             "order": "asc"}).json()
    rank = {None: 3, "high": 0, "medium": 1, "low": 2}
    ranks = [rank[i["confidence"]] for i in d3["items"]]
    assert ranks == sorted(ranks)
    # 未知字段回退 time（不报错）
    d4 = client.get("/api/memories", params={"status": "all", "sort": "bogus"}).json()
    assert d4["total"] == 4


def test_memories_conversation_flag_and_feedback_state(memories_env, monkeypatch):
    """has_conversation：MCP 原子记忆（占位会话 mcp）不得可点为会话链接（404 根因）；
    fb_polarity + 状态式反馈：点赞可高亮、再点同极性=取消、干净回退初始分。

    feedback 端点走 engine_request → 本用例显式 opt-in rag 后端（进程内直调）。
    """
    monkeypatch.setenv("AGENTMEMHUB_BACKEND", "rag")
    client, _ = memories_env
    d = client.get("/api/memories", params={"status": "all"}).json()
    by = {i["content"]: i for i in d["items"]}
    assert by["会话一的蒸馏结论"]["has_conversation"] is True
    assert by["Agent 手动写入的记忆"]["has_conversation"] is False
    assert all(i["fb_polarity"] is None for i in d["items"])

    uid = by["会话一的蒸馏结论"]["unit_id"]
    r = client.post("/api/memos/feedback",
                    params={"traceId": f"unit:{uid}", "polarity": "positive"})
    assert r.status_code == 200, r.text
    d2 = client.get("/api/memories", params={"status": "all"}).json()
    m2 = {i["content"]: i for i in d2["items"]}["会话一的蒸馏结论"]
    assert m2["fb_polarity"] == "positive" and m2["value"] == 1.0

    r2 = client.post("/api/memos/feedback",
                     params={"traceId": f"unit:{uid}", "revoke": True})
    assert r2.status_code == 200, r2.text
    d3 = client.get("/api/memories", params={"status": "all"}).json()
    m3 = {i["content"]: i for i in d3["items"]}["会话一的蒸馏结论"]
    assert m3["fb_polarity"] is None
    assert m3["value"] == 0.3, "取消后回蒸馏来源初始分"
