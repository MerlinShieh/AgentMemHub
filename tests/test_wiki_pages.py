# -*- coding: utf-8 -*-
"""知识库页面清单/读取（面板「wiki知识库」的数据源 + 两个 HTTP 接口）。

守四件事：
① **域 = 相对根的第一段目录**（L2 按 `NN-域名/页面.md` 组织；投喂交付物平铺无域）；
② **origin 由"第几个根"决定**（第 0 根 = 自有 L2 → native，其余 = 投喂 → external），
   与投影层的 origin 规则同源；
③ **标题优先取 frontmatter**，没有就回落文件名（不解析正文 H1 —— 清单阶段不读全文）；
④ **路径穿越必须被拒** —— 面板是本地服务，但"能读任意文件"的口子不该存在。

隔离：全部在 tmp 下造两个根，**不碰真实 out_l2 / out_feed**。
"""
from pathlib import Path

import pytest

from agentmemhub.web import wiki_pages


class _Cfg:
    """最小配置：两个页面根（第 0 个 native、第 1 个 external）。"""

    def __init__(self, l2: Path, kb: Path):
        self._l2, self._kb = l2, kb

    @property
    def wiki(self) -> dict:
        return {"out_l2": str(self._l2)}

    def feed_paths(self) -> dict:
        return {"kb": self._kb}


@pytest.fixture()
def roots(tmp_path):
    l2 = tmp_path / "out_l2"
    kb = tmp_path / "out_feed"
    (l2 / "01-域A").mkdir(parents=True)
    (l2 / "02-域B").mkdir()
    kb.mkdir()
    (l2 / "01-域A" / "page-a.md").write_text(
        "---\ntitle: 甲页\ntype: analysis\ncompiled_at: 2026-10-01 10:00:00\n"
        "sources: 5\n---\n\n# 甲\n\n正文内容。\n", encoding="utf-8")
    (l2 / "02-域B" / "page-b.md").write_text(
        "# 乙\n\n这个页面没有 frontmatter。\n", encoding="utf-8")
    # 根下的散页（没有域）—— 投喂交付物就是这种平铺形态
    (kb / "doc123.md").write_text(
        "---\ntitle: 投喂素材\n---\n\n外部内容。\n", encoding="utf-8")
    return _Cfg(l2, kb)


@pytest.fixture()
def client(tmp_path):
    from starlette.testclient import TestClient

    from agentmemhub.web.app import create_app
    return TestClient(create_app(tmp_path / "admin.db"))


@pytest.fixture()
def patched(roots, monkeypatch):
    """让接口层用 tmp 的两个根（app.py 本身不调 config()，所以只影响 wiki_pages）。"""
    monkeypatch.setattr("agentmemhub.config.config", lambda: roots)
    return roots


# ---------------------------------------------------------------------------
# 单元：清单与读取
# ---------------------------------------------------------------------------

def test_列页面_域与来源都正确(roots):
    rep = wiki_pages.list_pages(config=roots)
    assert rep["total"] == 3
    assert rep["originCounts"] == {"native": 2, "external": 1}
    by_title = {i["title"]: i for i in rep["items"]}
    assert by_title["甲页"]["domain"] == "01-域A"
    assert by_title["甲页"]["origin"] == "native"
    assert by_title["甲页"]["type"] == "analysis"
    # 无 frontmatter ⇒ 标题回落文件名（清单阶段不读全文找 H1）
    assert by_title["page-b"]["domain"] == "02-域B"
    # 根下散页：没有域，来源是 external
    assert by_title["投喂素材"]["domain"] == ""
    assert by_title["投喂素材"]["origin"] == "external"


def test_按来源过滤(roots):
    native = wiki_pages.list_pages(origin="native", config=roots)
    assert native["total"] == 2
    ext = wiki_pages.list_pages(origin="external", config=roots)
    assert ext["total"] == 1 and ext["items"][0]["title"] == "投喂素材"


def test_按域过滤与标题搜索(roots):
    dom = wiki_pages.list_pages(domain="01-域A", config=roots)
    assert dom["total"] == 1 and dom["items"][0]["title"] == "甲页"
    hit = wiki_pages.list_pages(q="甲", config=roots)
    assert hit["total"] == 1
    assert wiki_pages.list_pages(q="不存在的词", config=roots)["total"] == 0


def test_分页(roots):
    p1 = wiki_pages.list_pages(page=1, page_size=2, config=roots)
    assert len(p1["items"]) == 2 and p1["pageCount"] == 2 and p1["total"] == 3
    p2 = wiki_pages.list_pages(page=2, page_size=2, config=roots)
    assert len(p2["items"]) == 1


def test_读页面_带frontmatter与渲染(roots):
    rep = wiki_pages.list_pages(config=roots)
    path = next(i["path"] for i in rep["items"] if i["title"] == "甲页")
    page = wiki_pages.read_page(path, config=roots)
    assert page["title"] == "甲页" and page["domain"] == "01-域A"
    assert page["frontmatter"]["type"] == "analysis"
    assert "<h1>甲</h1>" in page["html"]
    assert page["chars"] > 0


def test_拒绝根外路径(roots, tmp_path):
    outside = tmp_path / "secret.md"
    outside.write_text("机密", encoding="utf-8")
    with pytest.raises(wiki_pages.WikiPageError):
        wiki_pages.read_page(str(outside), config=roots)


def test_拒绝穿越(roots):
    rep = wiki_pages.list_pages(config=roots)
    bad = Path(rep["items"][0]["path"]).parent / ".." / ".." / "escape.md"
    with pytest.raises(wiki_pages.WikiPageError):
        wiki_pages.read_page(str(bad), config=roots)


def test_拒绝非md(roots):
    txt = Path(roots.feed_paths()["kb"]) / "notmd.txt"
    txt.write_text("x", encoding="utf-8")
    with pytest.raises(wiki_pages.WikiPageError):
        wiki_pages.read_page(str(txt), config=roots)


def test_origin取值非法要报错(roots):
    with pytest.raises(wiki_pages.WikiPageError):
        wiki_pages.list_pages(origin="乱填", config=roots)


# ---------------------------------------------------------------------------
# HTTP 接口
# ---------------------------------------------------------------------------

def test_接口_页面清单(client, patched):
    r = client.get("/api/wiki/pages")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["total"] == 3
    assert d["originCounts"] == {"native": 2, "external": 1}
    assert len(d["domains"]) >= 2


def test_接口_单页正文(client, patched):
    items = client.get("/api/wiki/pages").json()["items"]
    path = next(i["path"] for i in items if i["title"] == "甲页")
    r = client.get("/api/wiki/page", params={"path": path})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["title"] == "甲页"
    assert "<h1>甲</h1>" in body["html"]
    assert "frontmatter" in body


def test_接口_越界路径返回400(client, patched, tmp_path):
    outside = tmp_path / "secret.md"
    outside.write_text("x", encoding="utf-8")
    r = client.get("/api/wiki/page", params={"path": str(outside)})
    assert r.status_code == 400
    assert "根内" in r.json()["detail"]


def test_接口_非法origin返回400(client, patched):
    r = client.get("/api/wiki/pages", params={"origin": "乱"})
    assert r.status_code == 400
