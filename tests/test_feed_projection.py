# -*- coding: utf-8 -*-
"""M2：多根投影 + 并列隔离（投喂交付物与自有 wiki **互不影响** 的测试锁）。

设计见 `docs/feed-adapter-plan.md` §5.1 的四层并列语义。这里锁住其中三条：
存储并列（根互不嵌套）、身份并列（同名相对路径不撞）、清扫安全（不误删别的根）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agentmemhub import wiki_index


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
    import numpy as np

    def _fake_encode(conn, st, texts, embedder=None):
        return [(_FakeSpec(), [np.zeros(8, dtype=np.float32) for _ in texts])]

    monkeypatch.setattr(wiki_index, "_encode_pages", _fake_encode)


@pytest.fixture()
def env(tmp_path):
    from agentmemhub.distill import ensure_distill_schema
    from agentmemhub.rag.ingest import ensure_bridge_schema, open_index

    conn = open_index(tmp_path / "session_rag.db")
    ensure_distill_schema(conn)
    ensure_bridge_schema(conn)
    conn.execute("CREATE TABLE IF NOT EXISTS vec_test(rowid INTEGER PRIMARY KEY,"
                 " embedding BLOB)")
    conn.commit()
    return conn, tmp_path


def _page(root: Path, domain: str, name: str, title: str, body: str = "正文") -> Path:
    d = root / domain
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text("---\ntitle: %s\ndomain: %s\n---\n\n# %s\n\n**摘要**：s\n\n%s\n"
                 % (title, domain, title, body), encoding="utf-8")
    return p


def _project(conn, roots):
    return wiki_index.project_pages(conn, roots, settings=_FakeSettings(),
                                    log=lambda *_: None)


# ---------------------------------------------------------------------------
# 身份：不同根下同名相对路径**必须不撞**
# ---------------------------------------------------------------------------

def test_多根_同名相对路径不撞(env, monkeypatch):
    """两个根下如果都有 `01-域A/001-页.md`，不加根标签就会互相覆盖 ——
    `src_id`/`seq` 都由路径派生，撞了就只剩一页。"""
    conn, tmp_path = env
    l2, feed = tmp_path / "out_l2_mimo", tmp_path / "out_feed"
    _page(l2, "01-域A", "001-页.md", "自有页")
    _page(feed, "01-域A", "001-页.md", "投喂页")
    _patch_vectors(monkeypatch)

    r = _project(conn, [l2, feed])
    assert r["pages"] == 2 and r["inserted"] == 2

    rows = conn.execute("SELECT title, src_id, seq, wiki_path, origin"
                        " FROM units WHERE source='wiki'").fetchall()
    assert {r[0] for r in rows} == {"自有页", "投喂页"}
    assert len({r[1] for r in rows}) == 2, "src_id 必须不同（根标签参与派生）"
    assert len({r[2] for r in rows}) == 2, "seq 也必须不同（否则撞 UNIQUE 约束）"
    # 同一个域（conversation_id）下两页并存 —— 根不同但域名可能相同
    assert all(os_is_abs(r[3]) for r in rows), "wiki_path 必须是可直接打开的绝对路径"


def os_is_abs(p: str) -> bool:
    import os
    return bool(p) and os.path.isabs(p)


def test_主根不加标签_既有src_id不变(env, monkeypatch):
    """多根改造**绝不能**让自有 wiki 的既有页面整体重键 ——
    重键 = 旧行被清 + 新行插入，挂在那些 unit 上的价值分与 👍👎 反馈会变孤儿
    （实测库里有 396 个页面 unit）。所以主根的身份必须与单根时代**逐字节一致**。
    """
    import zlib

    from agentmemhub.wiki_index import WIKI_SRC_PREFIX

    conn, tmp_path = env
    l2, feed = tmp_path / "out_l2_mimo", tmp_path / "out_feed"
    _page(l2, "01-域A", "001-页.md", "自有页")
    _page(feed, "01-域A", "001-页.md", "投喂页")
    _patch_vectors(monkeypatch)
    _project(conn, [l2, feed])

    got = dict(conn.execute(
        "SELECT title, src_id FROM units WHERE source='wiki'").fetchall())
    old = WIKI_SRC_PREFIX + "%08x" % (
        zlib.crc32("01-域A/001-页.md".encode("utf-8")) & 0xFFFFFFFF)
    assert got["自有页"] == old, "主根不带标签 ⇒ 身份与单根时代一致（不重键）"
    assert got["投喂页"] != old, "非主根必须带标签，否则与主根同名路径撞车"


def test_标签按配置位置定_不随根是否存在漂移(env, monkeypatch):
    """标签若按"已存在的根"分配，某个根临时缺席会让另一个根的标签漂移
    ⇒ `src_id` 跟着变 ⇒ 旧行被判为"磁盘上已不存在"而被清、新行插入（投影错乱）。"""
    conn, tmp_path = env
    l2, feed = tmp_path / "out_l2_mimo", tmp_path / "out_feed"
    _page(l2, "01-域A", "001-页.md", "自有页")
    _page(feed, "01-域A", "001-页.md", "投喂页")
    _patch_vectors(monkeypatch)
    _project(conn, [l2, feed])
    before = dict(conn.execute(
        "SELECT title, src_id FROM units WHERE source='wiki'").fetchall())

    # 主根临时缺席（L2 被移走）→ 只剩 feed 根。feed 的标签不能因此变成"主根"
    moved = tmp_path / "l2_moved"
    l2.rename(moved)
    r = _project(conn, [l2, feed])
    after = dict(conn.execute(
        "SELECT title, src_id FROM units WHERE source='wiki'").fetchall())
    assert r["inserted"] == 0 and r["removed"] == 0, "缺席的根不参与清扫，也不该重键"
    assert after == before, "投喂页的身份不能因为另一个根缺席而改变"
    l2_back = tmp_path / "out_l2_mimo"
    moved.rename(l2_back)


def test_多根_单根调用仍兼容(env, monkeypatch):
    """老调用方（传单个 Path 字符串）不该因为多根改造而失败。"""
    conn, tmp_path = env
    _page(tmp_path / "l2", "01-域A", "001-页.md", "页A")
    _patch_vectors(monkeypatch)
    assert _project(conn, str(tmp_path / "l2"))["pages"] == 1
    assert _project(conn, tmp_path / "l2")["pages"] == 1      # Path 形态同样可用


# ---------------------------------------------------------------------------
# 清扫安全：**只清本轮真扫过的根**
# ---------------------------------------------------------------------------

def test_多根_某个根不存在时其投影保留(env, monkeypatch):
    """⚠️ 最容易造成数据损失的一条：`out_feed` 暂时被移走（或还没建）时，
    若不区分"本轮扫了哪些根"，清扫会把投喂页**全部删光**。"""
    conn, tmp_path = env
    l2, feed = tmp_path / "out_l2_mimo", tmp_path / "out_feed"
    _page(l2, "01-域A", "001-页.md", "自有页")
    _page(feed, "01-域A", "001-页.md", "投喂页")
    _patch_vectors(monkeypatch)
    _project(conn, [l2, feed])
    assert conn.execute("SELECT COUNT(*) FROM units WHERE source='wiki'"
                        ).fetchone()[0] == 2

    # 投喂根整体移走 → 再投影一次（只扫得到 l2）
    moved = tmp_path / "out_feed_moved"
    feed.rename(moved)
    r = _project(conn, [l2, feed])
    assert r["removed"] == 0, "不能因为根不在就把它的投影当'磁盘上已不存在'清掉"
    left = {x[0] for x in conn.execute("SELECT title FROM units WHERE source='wiki'")}
    assert left == {"自有页", "投喂页"}, "投喂页必须留着，等根回来继续对齐"

    # 根回来 → 继续正常对齐，且不重复。
    # 两页内容都没变 ⇒ 现在既不重插也不 UPDATE（2026-10-20 起只重算变化页；
    # 此前口径是 updated=2，因为旧实现每轮无条件 UPDATE 全部行）
    moved.rename(feed)
    r2 = _project(conn, [l2, feed])
    assert r2["inserted"] == 0 and r2["updated"] == 0
    assert r2["unchanged"] == 2
    assert conn.execute("SELECT COUNT(*) FROM units WHERE source='wiki'"
                        ).fetchone()[0] == 2


def test_多根_根内页面删除仍会被清掉(env, monkeypatch):
    """安全清扫**不能过头**：根在扫、页面确实被删了，就该收敛掉。"""
    conn, tmp_path = env
    l2, feed = tmp_path / "out_l2_mimo", tmp_path / "out_feed"
    _page(l2, "01-域A", "001-页.md", "自有页")
    p = _page(feed, "01-域A", "001-投喂.md", "投喂页")
    _patch_vectors(monkeypatch)
    _project(conn, [l2, feed])

    p.unlink()
    r = _project(conn, [l2, feed])
    assert r["removed"] == 1
    left = {x[0] for x in conn.execute("SELECT title FROM units WHERE source='wiki'")}
    assert left == {"自有页"}


def test_多根_所有根都不存在则明确报错(env):
    """一个根都不存在时**不能静默成功**（否则会被误读成"投影过了、就是这样"）。"""
    conn, tmp_path = env
    with pytest.raises(FileNotFoundError):
        _project(conn, [tmp_path / "没有1", tmp_path / "没有2"])


# ---------------------------------------------------------------------------
# 根集合是**单点定义**的
# ---------------------------------------------------------------------------

def test_根集合由page_roots单点定义(tmp_path):
    """`page_roots()` 是"页面层有哪些根"的**唯一**定义处 ——
    CLI / wiki 管道 / 面板各写一份必然漂移（本项目在"两处流程各写一份"上踩过多次）。"""
    class _C:
        def feed_paths(self):
            return {"kb": tmp_path / "out_feed"}

    l2 = tmp_path / "out_l2_mimo"
    roots = wiki_index.page_roots(l2, config=_C())
    assert [p.name for p in roots] == ["out_l2_mimo", "out_feed"], \
        "两个根都要在（L2 自有 + 投喂交付物）"
    # **互不嵌套**：任何一方嵌在另一方里面，整域替换/rmtree 就会毁掉另一方
    a, b = (str(roots[0].resolve()), str(roots[1].resolve()))
    assert a not in b and b not in a, "两个页面根不能互相嵌套"
    # 去重：同一目录配成两个根不该扫两遍
    assert len(wiki_index.page_roots(l2, config=_C())) == 2


def test_根集合_配置读不到时只投L2(tmp_path):
    """配置坏了不该让整个投影失败（页面层至少还能投自有 wiki）。"""
    class _Boom:
        def feed_paths(self):
            raise RuntimeError("配置读不到")

    roots = wiki_index.page_roots(tmp_path / "l2", config=_Boom())
    assert [p.name for p in roots] == ["l2"]


# ---------------------------------------------------------------------------
# R1 锁：全量 wiki 聚合**绝不碰**投喂交付物
# ---------------------------------------------------------------------------

def test_R1_全量聚合清理不碰投喂根(tmp_path):
    """wiki 全量聚合会 `rmtree` 掉"不属于本轮"的域目录（整域替换）。
    投喂交付物在**自己的根**里，所以结构上不可能被它扫到 ——
    这里用真实的清理函数验证这条隔离（而不是只靠"文档说不会"）。"""
    import sys
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import wiki_aggregate as wa

    l2 = tmp_path / "out_l2_mimo"
    (l2 / "01-旧域").mkdir(parents=True)
    (l2 / "01-旧域" / "001-旧页.md").write_text("旧", encoding="utf-8")
    feed = tmp_path / "out_feed"
    feed.mkdir()
    (feed / "abc123.md").write_text("投喂交付物", encoding="utf-8")

    removed = wa._prune_stale_domain_dirs(l2, {"02-本轮域"}, partial=False)

    assert removed >= 1, "L2 里不属于本轮的域目录该被清"
    assert (feed / "abc123.md").exists(), "投喂交付物必须原封不动（它在另一个根里）"
