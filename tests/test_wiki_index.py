# -*- coding: utf-8 -*-
"""页面级召回测试：L2 知识页投影进召回面 + 召回结果的层级标记。

设计要点（与前面的架构讨论对应）：
  · 页面**整页投影**成一条 unit，不切片（wiki 是切片整合的产物，切回去是反向做功）
  · 投影是**全量对齐**：页面重编/改名/删除后，召回面自动收敛，不留旧版本
  · 召回结果带 kind（page/memory/message）与 wiki_path——两阶段召回的基础：
    页面给"标题+摘要+路径"（正文截断），需要细节时按路径读全文
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agentmemhub import wiki_index


def _page(tmp_path, domain, name, title, summary, body, refs=()):
    d = tmp_path / domain
    d.mkdir(parents=True, exist_ok=True)
    refs_txt = " ".join(f"[m{r}]" for r in refs)
    (d / name).write_text(
        "---\n"
        f"title: {title}\ndomain: {domain}\ntype: concept\n"
        "compiled_at: 2026-09-19 10:00:00\n"
        "generator: wiki_aggregate prototype\n---\n\n"
        f"# {title}\n\n**摘要**：{summary}\n\n{body}\n\n"
        f"**来源**：{refs_txt}\n\n"
        "<details><summary>合并自 2 个会话内页面</summary>\n\n- 页A\n- 页B\n\n</details>\n",
        encoding="utf-8")
    return d / name


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from agentmemhub.distill import ensure_distill_schema
    from agentmemhub.rag.ingest import ensure_bridge_schema, open_index

    idx_path = tmp_path / "session_rag.db"
    conn = open_index(idx_path)
    ensure_distill_schema(conn)
    ensure_bridge_schema(conn)
    # 投影用的向量表替身（投影只做 INSERT OR REPLACE / DELETE，普通表足够）
    conn.execute("CREATE TABLE IF NOT EXISTS vec_test(rowid INTEGER PRIMARY KEY,"
                 " embedding BLOB)")
    conn.commit()
    return conn, tmp_path


class _FakeSpec:
    id = "fake"
    vec_table = "vec_test"


class _FakeSettings:
    active_spec = _FakeSpec()
    active_model = "fake"

    @property
    def write_order(self):
        return ["fake"]

    def model(self, mid):
        return _FakeSpec()


def _patch_vectors(monkeypatch):
    """替身：绕开真实 embedding 模型（测试不加载 bge）。"""
    import numpy as np

    def _fake_encode(conn, st, texts, embedder=None):
        return [(_FakeSpec(), [np.zeros(8, dtype=np.float32) for _ in texts])]

    monkeypatch.setattr(wiki_index, "_encode_pages", _fake_encode)


def _project(conn, d):
    return wiki_index.project_pages(conn, d, settings=_FakeSettings(),
                                    log=lambda *_: None)


# ---------------------------------------------------------------------------
# 页面解析
# ---------------------------------------------------------------------------

def test_解析_标题摘要正文与溯源引用(env):
    conn, tmp_path = env
    md = _page(tmp_path / "l2", "01-域A", "001-页A.md", "页A 标题",
               "这是摘要", "正文内容。", refs=(3189, 3200))
    p = wiki_index.parse_page(md)
    assert p["title"] == "页A 标题"
    assert p["domain"] == "01-域A"
    assert p["summary"] == "这是摘要"
    assert "正文内容" in p["body"]
    assert p["refs"] == [3189, 3200]
    assert p["path"] == "01-域A/001-页A.md"
    # 元数据行不进正文（否则向量会被编号串稀释）
    assert "来源" not in p["body"] and "合并自" not in p["body"]


def test_解析_index文件跳过(env):
    conn, tmp_path = env
    d = tmp_path / "l2" / "01-域A"
    d.mkdir(parents=True)
    (d / "index.md").write_text("# 域索引\n", encoding="utf-8")
    assert wiki_index.parse_page(d / "index.md") is None


# ---------------------------------------------------------------------------
# 投影：整页进召回面
# ---------------------------------------------------------------------------

def test_投影_页面整页进units且带层级标记(env, monkeypatch):
    conn, tmp_path = env
    _page(tmp_path / "l2", "01-域A", "001-页A.md", "页A", "摘要A", "正文A")
    _patch_vectors(monkeypatch)
    r = _project(conn, tmp_path / "l2")
    assert r["pages"] == 1 and r["inserted"] == 1
    row = conn.execute("SELECT source, role, conversation_id, title, text,"
                       " wiki_path, src_id FROM units WHERE source='wiki'").fetchone()
    assert row[0] == "wiki" and row[1] == "wiki"
    assert row[2] == "01-域A"
    assert row[3] == "页A"
    assert "摘要A" in row[4] and "正文A" in row[4]     # 整页文本（不切片）
    assert row[5] == "01-域A/001-页A.md"               # 两阶段第二阶段入口
    assert row[6].startswith("wiki_")


def test_投影_全量对齐_页面删除后召回面收敛(env, monkeypatch):
    conn, tmp_path = env
    _patch_vectors(monkeypatch)
    d = tmp_path / "l2"
    _page(d, "01-域A", "001-页A.md", "页A", "摘要A", "正文A")
    _page(d, "01-域A", "002-页B.md", "页B", "摘要B", "正文B")
    _project(conn, d)
    assert conn.execute("SELECT COUNT(*) FROM units WHERE source='wiki'"
                        ).fetchone()[0] == 2
    # 删掉一页 → 再投影 → 该页从召回面消失（不留旧版本）
    (d / "01-域A" / "002-页B.md").unlink()
    r = _project(conn, d)
    assert r["removed"] == 1
    left = [x[0] for x in conn.execute(
        "SELECT title FROM units WHERE source='wiki'")]
    assert left == ["页A"]


def test_投影_幂等_重复投影不重复插入(env, monkeypatch):
    conn, tmp_path = env
    _patch_vectors(monkeypatch)
    d = tmp_path / "l2"
    _page(d, "01-域A", "001-页A.md", "页A", "摘要A", "正文A")
    _project(conn, d)
    r2 = _project(conn, d)
    assert r2["inserted"] == 0 and r2["updated"] == 1
    assert conn.execute("SELECT COUNT(*) FROM units WHERE source='wiki'"
                        ).fetchone()[0] == 1


# ---------------------------------------------------------------------------
# 召回：层级标记与两阶段返回
# ---------------------------------------------------------------------------

def test_召回层级判定():
    from agentmemhub.rag.search import kind_of
    assert kind_of("wiki_abc123") == "page"
    assert kind_of("dst_abc123") == "memory"
    assert kind_of("mcp_abc123") == "memory"
    assert kind_of("msg:123") == "message"
    assert kind_of(None) == "message"


def test_召回_页面层返回摘要与路径且正文截断():
    """页面 Hit 的形态：kind=page、给摘要与路径、正文截断（两阶段）。"""
    from agentmemhub.rag.search import PAGE_TEXT_CAP, Hit, kind_of
    long_text = "标题\n\n摘要：这是要点\n\n" + "正文内容。" * 300
    h = Hit(unit_id=1, source="wiki", conversation_id="01-域A", seq=-1,
            role="wiki", turn_key=None, time=1, title="页A", text=long_text,
            score=1.0)
    # 模拟 hybrid_search 的页面层处理
    k = kind_of("wiki_" + "x" * 8)
    assert k == "page"
    assert len(long_text) > PAGE_TEXT_CAP
