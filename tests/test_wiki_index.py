# -*- coding: utf-8 -*-
"""页面级召回测试：L2 知识页投影进召回面 + 召回结果的层级标记。

设计要点（与前面的架构讨论对应）：
  · 页面**整页投影**成一条 unit，不切片（wiki 是切片整合的产物，切回去是反向做功）
  · 投影是**全量对齐**：页面重编/改名/删除后，召回面自动收敛，不留旧版本
  · 召回结果带 kind（page/memory/message）与 wiki_path——两阶段召回的基础：
    页面给"标题+摘要+路径"（正文截断），需要细节时按路径读全文
"""
from __future__ import annotations

import os
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
    # 两阶段第二阶段入口：必须是**能直接打开的绝对路径** ——
    # 此前存的是根内相对路径，而消费者不知道根在哪 ⇒ 拿到也没法读（2026-10-12 改）
    assert os.path.isabs(row[5]) and row[5].endswith("01-域A" + os.sep + "001-页A.md")
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
    # 内容没变 → 不 UPDATE、也不重算向量（2026-10-20 起；此前口径是 updated=1）
    assert r2["inserted"] == 0 and r2["updated"] == 0
    assert r2["unchanged"] == 1
    assert conn.execute("SELECT COUNT(*) FROM units WHERE source='wiki'"
                        ).fetchone()[0] == 1


# ---------------------------------------------------------------------------
# 增量友好：只重算「变化页」的向量
#
# 2026-10-20 优化背景：此前每轮把**整库页面**重新 encode —— 实测 626 页 180+ 秒，
# 占一轮增量总耗时 44%，而一轮通常只改 1~2 页。判据三条：新页 / 正文变 /
# **某模型向量表里缺该行**（换模型、新建表）。
# ---------------------------------------------------------------------------

def _patch_vectors_counting(monkeypatch):
    """与 `_patch_vectors` 相同，但统计送进 encode 的文本条数。"""
    import numpy as np

    calls = {"texts": 0}

    def _fake_encode(conn, st, texts, embedder=None):
        calls["texts"] += len(texts)
        return [(_FakeSpec(), [np.zeros(8, dtype=np.float32) for _ in texts])]

    monkeypatch.setattr(wiki_index, "_encode_pages", _fake_encode)
    return calls


def test_投影_未变页不重算向量(env, monkeypatch):
    conn, tmp_path = env
    d = tmp_path / "l2"
    _page(d, "01-域A", "001-页A.md", "页A", "摘要A", "正文A")
    _page(d, "01-域A", "002-页B.md", "页B", "摘要B", "正文B")
    calls = _patch_vectors_counting(monkeypatch)

    _project(conn, d)
    assert calls["texts"] == 2, "首次投影：两页都要编码"

    calls["texts"] = 0
    r = _project(conn, d)
    assert calls["texts"] == 0, "内容没变 → 一页都不该重算向量"
    assert r["unchanged"] == 2 and r["updated"] == 0 and r["inserted"] == 0


def test_投影_内容变化才重算该页(env, monkeypatch):
    conn, tmp_path = env
    d = tmp_path / "l2"
    _page(d, "01-域A", "001-页A.md", "页A", "摘要A", "正文A")
    _page(d, "01-域A", "002-页B.md", "页B", "摘要B", "正文B")
    calls = _patch_vectors_counting(monkeypatch)
    _project(conn, d)

    _page(d, "01-域A", "001-页A.md", "页A", "摘要A", "正文A 改过了")
    calls["texts"] = 0
    r = _project(conn, d)
    assert calls["texts"] == 1, "只有变化那一页要重算"
    assert r["updated"] == 1 and r["unchanged"] == 1


def test_投影_向量缺失时补算(env, monkeypatch):
    """换模型 / 新建向量表后旧页在表里没有行 —— 必须补算，不能被"内容没变"跳过。

    这是三条判据里最容易被漏掉的一条：只看正文会以为"什么都没变"，
    结果新表的向量永远是空的，召回静默失效。
    """
    conn, tmp_path = env
    d = tmp_path / "l2"
    _page(d, "01-域A", "001-页A.md", "页A", "摘要A", "正文A")
    calls = _patch_vectors_counting(monkeypatch)
    _project(conn, d)

    conn.execute("DELETE FROM vec_test")      # 模拟"新表还是空的"
    conn.commit()
    calls["texts"] = 0
    r = _project(conn, d)
    assert calls["texts"] == 1, "向量缺失必须补算"
    assert r["unchanged"] == 0


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


# ---------------------------------------------------------------------------
# 来源维度：自有记忆沉淀 vs 外部投喂
# ---------------------------------------------------------------------------

def test_来源_页面默认自有_声明external则为投喂(env):
    conn, tmp_path = env
    d = tmp_path / "l2"
    _page(d, "01-域A", "001-自有.md", "自有页", "摘要", "正文")
    # 投喂编译的页面：frontmatter 标 origin: external（可带 doc id）
    ext = d / "01-域A" / "002-投喂.md"
    ext.write_text(
        "---\ntitle: 投喂页\ndomain: 01-域A\norigin: external\n"
        "doc: web_abc123\n---\n\n# 投喂页\n\n**摘要**：外部素材\n\n正文\n",
        encoding="utf-8")
    p1 = wiki_index.parse_page(d / "01-域A" / "001-自有.md")
    p2 = wiki_index.parse_page(ext)
    assert p1["origin"] == "native"
    assert p2["origin"] == "external"


def test_来源_投影写入units且召回可依据它区分(env, monkeypatch):
    conn, tmp_path = env
    d = tmp_path / "l2"
    _page(d, "01-域A", "001-自有.md", "自有页", "摘要", "正文")
    ext = d / "01-域A" / "002-投喂.md"
    ext.write_text(
        "---\ntitle: 投喂页\ndomain: 01-域A\norigin: external\n---\n\n"
        "# 投喂页\n\n**摘要**：外部素材\n\n正文\n", encoding="utf-8")
    _patch_vectors(monkeypatch)
    _project(conn, d)
    origins = dict(conn.execute(
        "SELECT title, origin FROM units WHERE source='wiki'").fetchall())
    assert origins == {"自有页": "native", "投喂页": "external"}


def test_来源_字段与层级正交(env):
    """origin（来源维度）与 kind（层级维度）互不干扰：页面可以是自有的，
    记忆也可以是投喂来的（source='feed' 的切片投影时标 external）。"""
    from agentmemhub.rag.search import Hit, kind_of
    h = Hit(unit_id=1, source="wiki", conversation_id="01-域A", seq=-1,
            role="wiki", turn_key=None, time=1, title="外部资料页",
            text="x", score=1.0, origin="external")
    assert kind_of("wiki_abc") == "page" and h.origin == "external"
    assert Hit(unit_id=2, source="zcode", conversation_id="c1", seq=1,
               role="user", turn_key="t", time=1, title=None, text="y",
               score=1.0).origin == "native"        # 默认自有沉淀


# ---------------------------------------------------------------------------
# 溯源：投喂页面的 doc 锚（M0 地基）
#
# `doc:` 是投喂页面**回指原件**的锚。2026-10-10 实测：`_DOC_RE` 解析了它、也用它
# 判定了 `is_external`，但**返回的字典里没有 doc 字段**，投影写 units 的列清单里
# 也没有它 ⇒ 召回命中一个投喂页后**无法反查是哪份外部素材**（只能靠 wiki_path
# 去读文件再抠 frontmatter，页面一重编或删除溯源就断）。
# ---------------------------------------------------------------------------

def _ext_page(tmp_path, name="002-投喂.md", *, doc="web_abc123"):
    d = tmp_path / "l2" / "01-域A"
    d.mkdir(parents=True, exist_ok=True)
    doc_line = f"doc: {doc}\n" if doc else ""
    (d / name).write_text(
        "---\ntitle: 投喂页\ndomain: 01-域A\norigin: external\n"
        f"{doc_line}---\n\n# 投喂页\n\n**摘要**：外部素材\n\n正文\n",
        encoding="utf-8")
    return d / name


def test_溯源_doc被解析且落库(env, monkeypatch):
    conn, tmp_path = env
    p = wiki_index.parse_page(_ext_page(tmp_path))
    assert p["doc"] == "web_abc123", "parse_page 必须带回 doc id"

    _patch_vectors(monkeypatch)
    _project(conn, tmp_path / "l2")
    assert conn.execute("SELECT doc_id FROM units WHERE title='投喂页'"
                        ).fetchone()[0] == "web_abc123"


def test_溯源_自有页面doc_id为NULL(env, monkeypatch):
    """自有 wiki 页面没有 doc 锚 ⇒ 落 NULL（而非空字符串，便于查询区分）。"""
    conn, tmp_path = env
    _page(tmp_path / "l2", "01-域A", "001-自有.md", "自有页", "摘要", "正文")
    _patch_vectors(monkeypatch)
    _project(conn, tmp_path / "l2")
    assert conn.execute("SELECT doc_id FROM units WHERE title='自有页'"
                        ).fetchone()[0] is None


def test_溯源_doc变化时更新既有行(env, monkeypatch):
    """doc 是 **UPDATE 路径也要写**的列——只测 INSERT 会漏掉幂等更新分支。"""
    conn, tmp_path = env
    _ext_page(tmp_path, doc="web_old")
    _patch_vectors(monkeypatch)
    _project(conn, tmp_path / "l2")
    assert conn.execute("SELECT COUNT(*) FROM units WHERE source='wiki'"
                        ).fetchone()[0] == 1

    _ext_page(tmp_path, doc="web_new")      # 同一路径换了 doc（重投喂同一文件的新版）
    r = _project(conn, tmp_path / "l2")
    assert r["inserted"] == 0 and r["updated"] == 1, "同路径应更新而非新增"
    assert conn.execute("SELECT doc_id FROM units WHERE title='投喂页'"
                        ).fetchone()[0] == "web_new"


def test_溯源_带引号的frontmatter值也能解析(env):
    """frontmatter 的值**允许带引号**（投喂链路用 `json.dumps` 写，因为标题里可能有
    `:` `#` 引号）。不 strip 外层引号会把引号当成 id 的一部分 ⇒ `units.doc_id`
    与 `materials.db` 对不上、**溯源链静默断掉**（2026-10-12 实测修）。
    """
    conn, tmp_path = env
    d = tmp_path / "l2" / "01-域A"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "001-投喂.md"
    p.write_text('---\ntitle: "被引号包住的标题"\norigin: "external"\n'
                 'doc: "abc123"\n---\n\n# 页\n\n**摘要**：s\n\n正文\n',
                 encoding="utf-8")
    parsed = wiki_index.parse_page(p)
    assert parsed["doc"] == "abc123", "引号必须被剥掉，否则反查不到原件"
    assert parsed["origin"] == "external", "带引号的 origin 也要认"
    assert parsed["title"] == "被引号包住的标题"


# ---------------------------------------------------------------------------
# 页面 FTS 回路（多回路召回）
# ---------------------------------------------------------------------------

def test_页面子集全文通道_只返回页面且在子集内():
    from agentmemhub.rag.search import subset_fts_search
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE VIRTUAL TABLE units_fts USING fts5("
                 "text, title, tokenize='trigram')")
    conn.execute("INSERT INTO units_fts(rowid, text, title)"
                 " VALUES (1, 'ADBController 容错语义设计', '')")
    conn.execute("INSERT INTO units_fts(rowid, text, title)"
                 " VALUES (2, '无关内容', '')")
    # 子集只含 2：1 虽匹配但不在子集 → 不返回（子集隔离生效）
    assert subset_fts_search(conn, "ADBController 容错语义设计", 5, {2}) == []
    # 子集含 1 → 命中
    hits = subset_fts_search(conn, "ADBController 容错语义设计", 5, {1, 2})
    assert hits and hits[0][0] == 1
    # 空子集直接短路
    assert subset_fts_search(conn, "ADBController", 5, set()) == []
    conn.close()


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


# ---------------------------------------------------------------------------
# 空页面（编辑器在产物目录里自动新建的文件）
# ---------------------------------------------------------------------------

def test_空页面不入召回面(env, monkeypatch):
    """实测踩过（2026-09-25）：把产物目录当 Obsidian vault 打开时，点击**未解析**的
    `[[链接]]` 会让编辑器在 vault 根**自动新建 0 字节 md**。这类文件会被 parse_page
    照收 —— title 兜底成 path.stem（看着很正当）、domain 取父目录名（即 vault 根名，
    一个根本不存在的域）、origin 标成 native —— 于是 3 个空文件差点变成 3 条召回单元。
    """
    conn, tmp_path = env
    d = tmp_path / "l2"
    _page(d, "01-域A", "001-正常.md", "正常页", "摘要", "正文")
    # Obsidian 自动新建：0 字节、落在 vault 根（不在任何域目录内）
    (d / "ADBController 核心设计与功能清单.md").write_text("", encoding="utf-8")
    _patch_vectors(monkeypatch)

    st = _project(conn, d)

    titles = [r[0] for r in conn.execute(
        "SELECT title FROM units WHERE source='wiki'")]
    assert titles == ["正常页"], "空页面不该进召回面"
    assert st["skipped_empty"] == 1
    assert st["pages"] == 1
    conn.close()


def test_只有frontmatter没有正文的页面也被跳过(env, monkeypatch):
    """判据是「正文为空」而不是文件大小 —— 有 frontmatter 但无正文同样是空页面。"""
    conn, tmp_path = env
    d = tmp_path / "l2"
    _page(d, "01-域A", "001-正常.md", "正常页", "摘要", "正文")
    f = d / "01-域A" / "002-空壳.md"
    f.write_text("---\ntitle: 空壳\ndomain: 01-域A\n---\n\n", encoding="utf-8")
    _patch_vectors(monkeypatch)

    st = _project(conn, d)

    assert st["skipped_empty"] == 1
    assert [r[0] for r in conn.execute(
        "SELECT title FROM units WHERE source='wiki'")] == ["正常页"]
    conn.close()


def test_点开头目录下的页面不入召回面(env, monkeypatch):
    """`.obsidian/`（编辑器配置）与 `_swap_domain_dir()` 崩溃时残留的 `.tmp_*`
    都可能含 md；它们一旦被投影，会以**错误的 domain**（点目录名）混进召回面。
    """
    conn, tmp_path = env
    d = tmp_path / "l2"
    _page(d, "01-域A", "001-正常.md", "正常页", "摘要", "正文")
    _page(d, ".tmp_01-域A", "001-写盘中间态.md", "中间态页", "摘要", "正文")
    _page(d, ".obsidian", "001-配置.md", "配置页", "摘要", "正文")
    _patch_vectors(monkeypatch)

    _project(conn, d)

    titles = [r[0] for r in conn.execute(
        "SELECT title FROM units WHERE source='wiki'")]
    assert titles == ["正常页"], "点开头目录下的文件不该进召回面"
    conn.close()


def test_带aliases的页面仍能正常解析(tmp_path):
    """补 aliases 是为了 Obsidian；它不能影响 parse_page 的正则解析
    （title / domain / origin / doc 都是行锚正则，多一行不该被带偏）。"""
    from agentmemhub.wiki_index import parse_page

    f = tmp_path / "001-页A.md"
    f.write_text(
        '---\ntitle: 页A\naliases: ["页A"]\ndomain: 域A\norigin: external\n'
        'doc: web_abc123\n---\n\n# 页A\n\n**摘要**：要点\n\n正文内容\n',
        encoding="utf-8")

    p = parse_page(f)

    assert p["title"] == "页A"
    assert p["domain"] == "域A"
    assert p["origin"] == "external"
    assert "正文内容" in p["body"]
    assert "aliases" not in p["body"], "frontmatter 不该混进正文"
