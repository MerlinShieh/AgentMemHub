# -*- coding: utf-8 -*-
"""外部投喂转译层 M1：三段物料 + 登记表 + T1（md/txt 直通）+ CLI 编排。

**隔离**：所有用例注入 tmp 配置（`_FakeCfg`）—— 绝不碰真实的
`database/materials.db` 与 `exports/feed_store`、`exports/wiki_work/out_feed`。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentmemhub.feed import get_converter
from agentmemhub.feed.base import QUALITY_WARN, FeedDocument, FeedError
from agentmemhub.feed.converters.text import TextConverter
from agentmemhub.feed.service import ingest, list_materials, render_kb_page
from agentmemhub.feed.store import (MaterialStore, MaterialsDB, doc_id_of,
                                    sha256_of)


class _FakeCfg:
    """测试用配置：三段路径全在 tmp 下。"""

    def __init__(self, root: Path):
        self.root = root
        self.paths = {"db": root / "materials.db",
                      "store": root / "store",
                      "kb": root / "kb"}

    def feed_paths(self):
        return dict(self.paths)

    def feed(self):
        # 空 = 全走内置默认（假开关 `convert.*` 已删，别再复刻）
        return {}


@pytest.fixture()
def cfg(tmp_path):
    return _FakeCfg(tmp_path)


@pytest.fixture()
def store(cfg):
    return MaterialStore(root=cfg.paths["store"], kb_dir=cfg.paths["kb"])


def _md(tmp_path, name="笔记.md", body="# 标题\n\n正文内容。\n", **fm):
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    if fm:
        head = "---\n" + "".join("%s: %s\n" % (k, v) for k, v in fm.items()) + "---\n\n"
        p.write_text(head + body, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# 契约：FeedDocument
# ---------------------------------------------------------------------------

def test_契约_空正文拒绝():
    """空正文绝不能进知识库（会变成"标题正当、内容为空"的召回单元）。"""
    with pytest.raises(FeedError) as ei:
        FeedDocument(doc_id="x", title="t", markdown="   \n ", kind="text",
                     source_ref="a.md", doc_time=0)
    assert ei.value.kind == "empty" and ei.value.retryable is False


def test_契约_质量夹取与低质判定():
    d = FeedDocument(doc_id="x", title="t", markdown="正文", kind="text",
                     source_ref="a.md", doc_time=0, quality=1.7)
    assert d.quality == 1.0 and not d.low_quality
    d2 = FeedDocument(doc_id="x", title="t", markdown="正文", kind="text",
                      source_ref="a.md", doc_time=0, quality=QUALITY_WARN - 0.1)
    assert d2.low_quality
    # 有告警也算"需要复核"，即使质量分不低
    d3 = FeedDocument(doc_id="x", title="t", markdown="正文", kind="text",
                      source_ref="a.md", doc_time=0, quality=1.0,
                      warnings=["编码无法确定"])
    assert d3.low_quality


# ---------------------------------------------------------------------------
# 三段存储
# ---------------------------------------------------------------------------

def test_存储_原件内容寻址且幂等(store):
    data = "内容是唯一的".encode("utf-8")
    sha, rel = store.put_raw(data, ext=".md")
    assert rel == str(Path("raw") / sha[:2] / (sha + ".md"))
    assert store.read_raw(rel) == data
    # 同内容再写：路径相同、不重复（内容寻址天然去重）
    sha2, rel2 = store.put_raw(data, ext=".md")
    assert (sha2, rel2) == (sha, rel)
    files = list((store.root / "raw").rglob("*"))
    assert len([f for f in files if f.is_file()]) == 1


def test_存储_kb路径只由doc_id派生(store):
    """路径必须**稳定**（投影的 src_id/seq 由它派生），所以不能拼可变的标题。"""
    assert store.kb_path_of("abc123") == "abc123.md"
    p = store.publish_kb("abc123", "内容")
    assert Path(p).read_text(encoding="utf-8") == "内容"
    assert store.kb_file("abc123").exists()


def test_存储_写原件不留半截文件(store):
    """先写 `.part` 再改名 —— 中途失败不留"看起来完整"的半截原件。"""
    store.put_raw(b"x" * 10, ext=".bin")
    assert not list(store.root.rglob("*.part"))


# ---------------------------------------------------------------------------
# 登记表
# ---------------------------------------------------------------------------

def test_登记表_增查与sha查重(cfg):
    db = MaterialsDB(cfg.paths["db"])
    try:
        db.upsert(doc_id="d1", sha256="s1", kind="text", source_ref="a.md",
                  title="甲", doc_time=100, raw_path="raw/x", status="published")
        assert db.get("d1")["title"] == "甲"
        assert db.find_by_sha("s1")["doc_id"] == "d1"
        assert db.find_by_sha("不存在") is None
        # upsert 幂等：同 doc_id 再来一次是更新，不是新行
        db.upsert(doc_id="d1", sha256="s1", kind="text", source_ref="a.md",
                  title="甲改", doc_time=100, raw_path="raw/x", status="failed",
                  error="boom", error_kind="crash")
        assert db.get("d1")["title"] == "甲改"
        assert len(db.list()) == 1
        assert db.get("d1")["status"] == "failed"
    finally:
        db.close()


def test_登记表_状态白名单(cfg):
    db = MaterialsDB(cfg.paths["db"])
    try:
        db.upsert(doc_id="d1", sha256="s1", kind="text", source_ref="a.md",
                  title="甲", doc_time=0, raw_path="raw/x")
        with pytest.raises(ValueError):
            db.set_status("d1", "瞎写的状态")
        db.set_status("d1", "retracted")
        assert db.get("d1")["status"] == "retracted"
    finally:
        db.close()


def test_登记表_低质量筛选(cfg):
    db = MaterialsDB(cfg.paths["db"])
    try:
        db.upsert(doc_id="good", sha256="s1", kind="text", source_ref="a",
                  title="好的", doc_time=0, raw_path="r", quality=1.0,
                  status="published")
        db.upsert(doc_id="bad", sha256="s2", kind="text", source_ref="b",
                  title="差的", doc_time=0, raw_path="r", quality=0.3,
                  warnings=["表格失真"], status="published")
        assert [r["doc_id"] for r in db.list(low_quality=True)] == ["bad"]
        assert len(db.list()) == 2
    finally:
        db.close()


# ---------------------------------------------------------------------------
# T1 转换器：md / txt 直通
# ---------------------------------------------------------------------------

def test_T1_认领md与txt():
    c = TextConverter()
    assert c.can_handle(ref="a.md") and c.can_handle(ref="a.txt")
    assert c.can_handle(ref="a.MD") and c.can_handle(ref="a.markdown")
    assert not c.can_handle(ref="a.pdf") and not c.can_handle(ref="a.html")
    assert not c.can_handle(ref="a.md", kind="pdf")   # 显式 kind 优先


def test_T1_md原样直通且质量满分():
    c = TextConverter()
    raw = "---\ntitle: 我的笔记\ndate: 2026-03-04\n---\n\n# 我的笔记\n\n正文\n"
    doc = c.convert(ref="笔记.md", data=raw.encode("utf-8"), meta={"doc_id": "d"})
    assert doc.markdown == raw                     # md 不做任何加工
    assert doc.title == "我的笔记"                  # frontmatter title 优先
    assert doc.quality == 1.0 and not doc.warnings
    assert doc.converter == "text@1"
    assert doc.doc_time > 0


def test_T1_txt包一层最小结构():
    c = TextConverter()
    doc = c.convert(ref="随手记.txt", data="一些零散内容".encode("utf-8"),
                    meta={"doc_id": "d"})
    assert doc.title == "随手记"                    # 无 H1 → 文件名兜底
    assert doc.markdown.startswith("# 随手记")
    assert "一些零散内容" in doc.markdown


def test_T1_GBK编码能解():
    """Windows 老 txt 大量是 GBK —— 解不出来就整份进不来，代价太大。"""
    c = TextConverter()
    doc = c.convert(ref="gbk.txt", data="中文内容测试".encode("gbk"),
                    meta={"doc_id": "d"})
    assert "中文内容测试" in doc.markdown
    assert doc.meta["encoding"] == "gb18030"


def test_T1_标题取值顺序():
    """H1 优先于文件名；frontmatter title 又优先于 H1。"""
    c = TextConverter()
    d1 = c.convert(ref="文件名.md", data="# 正文标题\n\nx".encode("utf-8"),
                   meta={"doc_id": "d"})
    assert d1.title == "正文标题"
    d2 = c.convert(ref="文件名.md", data="没有标题\n\nx".encode("utf-8"),
                   meta={"doc_id": "d"})
    assert d2.title == "文件名"


def test_T1_标题不外泄代码块里的注释():
    """代码块内的 `# xxx` 是**代码注释**（Python/shell），不是 markdown 标题。

    2026-10-12 实测：一份含 Python 片段的测试笔记被取成首个 `# 打开百度首页`
    注释当标题 —— 去掉围栏后该文件没有真 H1，正确回落到文件名。
    """
    c = TextConverter()
    body = ("一、Web测试\n\n示例：\n\n```python\n"
            "# 打开百度首页\n# 定位首页的设置按钮\ndriver.get(url)\n```\n\n正文结束\n")
    doc = c.convert(ref="测试笔记.md", data=body.encode("utf-8"),
                    meta={"doc_id": "d"})
    assert doc.title == "测试笔记", "不能被代码块里的注释带偏"


def test_T1_真有H1时仍用它当标题():
    c = TextConverter()
    body = "# 真的标题\n\n```python\n# 代码注释\nx=1\n```\n\n正文\n"
    doc = c.convert(ref="文件名.md", data=body.encode("utf-8"),
                    meta={"doc_id": "d"})
    assert doc.title == "真的标题"


def test_T1_文档时间用发布日而非投喂时刻():
    """时间语义硬约束：填投喂时刻会让时间衰减全错（与 _origin_time 同义）。"""
    c = TextConverter()
    doc = c.convert(ref="a.md",
                    data="---\ntitle: t\ndate: 2020-01-02\n---\n\n正文".encode("utf-8"),
                    meta={"doc_id": "d", "mtime": 9999999999})
    import time
    assert doc.doc_time == int(time.mktime(time.strptime("2020-01-02", "%Y-%m-%d")))


def test_注册表_认领与未知返回None():
    assert get_converter(ref="a.md").name == "text"
    # ⚠️ 别拿 `.pdf` 当"未知类型"样本了 —— M4 起它归 T3 转换器认领
    assert get_converter(ref="a.xyz") is None       # 未知类型必须显式无认领


# ---------------------------------------------------------------------------
# 端到端编排
# ---------------------------------------------------------------------------

def test_端到端_md投喂落三段并登记published(tmp_path, cfg):
    src = _md(tmp_path, "测试笔记.md", "# 测试笔记\n\n内容甲。\n")
    r = ingest(str(src), config=cfg, log=lambda *_: None)

    assert r["ok"] and not r["skipped"]
    assert r["converter"] == "text@1" and r["title"] == "测试笔记"
    assert r["quality"] == 1.0

    # 第 1 段：原件按内容寻址落盘，字节完全一致
    raw_file = cfg.paths["store"] / r["raw_path"]
    assert raw_file.read_bytes() == src.read_bytes()

    # 第 3 段：最终 md 带 frontmatter，且**溯源三项齐全**
    kb = Path(r["kb_path"]).read_text(encoding="utf-8")
    import yaml
    fm = yaml.safe_load(kb.split("---")[1])
    assert fm["origin"] == "external"
    assert fm["doc"] == r["doc_id"], "doc 锚必须能被 YAML 读回（供 wiki_index 反查）"
    assert "内容甲" in kb

    # 登记表：published + 可查
    row = [m for m in list_materials(config=cfg) if m["doc_id"] == r["doc_id"]][0]
    assert row["status"] == "published" and row["kb_path"]
    assert row["sha256"] == sha256_of(src.read_bytes())


def test_端到端_重复投喂被识别为幂等而非失败(tmp_path, cfg):
    src = _md(tmp_path)
    r1 = ingest(str(src), config=cfg, log=lambda *_: None)
    r2 = ingest(str(src), config=cfg, log=lambda *_: None)
    assert r1["ok"] and r2["ok"]
    assert r2["skipped"] == "duplicate" and r2["doc_id"] == r1["doc_id"]
    # 没有重复登记、没有重复原件
    assert len(list_materials(config=cfg)) == 1
    assert len(list((cfg.paths["store"] / "raw").rglob("*.*"))) == 1


def test_端到端_内容变了就是新素材(tmp_path, cfg):
    """doc_id 是**内容锚**：改一个字节 → 新 doc_id（旧版仍可溯源）。"""
    a = _md(tmp_path, "a.md", "# 甲\n\n版本一\n")
    r1 = ingest(str(a), config=cfg, log=lambda *_: None)
    a.write_text("# 甲\n\n版本二\n", encoding="utf-8")
    r2 = ingest(str(a), config=cfg, log=lambda *_: None)
    assert r1["doc_id"] != r2["doc_id"]
    assert len(list_materials(config=cfg)) == 2


def test_端到端_force可重跑(tmp_path, cfg):
    src = _md(tmp_path)
    r1 = ingest(str(src), config=cfg, log=lambda *_: None)
    r2 = ingest(str(src), force=True, config=cfg, log=lambda *_: None)
    assert r1["doc_id"] == r2["doc_id"] and not r2["skipped"]
    assert len(list_materials(config=cfg)) == 1


def test_端到端_dry_run不写盘(tmp_path, cfg):
    src = _md(tmp_path)
    r = ingest(str(src), dry_run=True, config=cfg, log=lambda *_: None)
    assert r["ok"] and r["skipped"] == "dry_run" and r["converter"] == "text@1"
    assert not (cfg.paths["store"]).exists()
    assert not (cfg.paths["db"]).exists()


# -- 失败路径：都要**显式报错**，不静默跳过 ---------------------------------

def test_失败_文件不存在(tmp_path, cfg):
    r = ingest(str(tmp_path / "没有这个.md"), config=cfg, log=lambda *_: None)
    assert not r["ok"] and r["error_kind"] == "not_found"


def test_URL投喂已实现但沙箱下不出网(cfg):
    """M3 起 URL 可投喂；而**测试沙箱下取回层熔断**（一个字节都不出网）。

    这是本项目"凡真实副作用（花钱/发消息/出网）的出口都要熔断"的同一条纪律
    —— 不能靠"测试记得 mock"。要测 URL 转译请 mock `feed.fetch.fetch` 并喂夹具。
    """
    r = ingest("https://example.com/a.html", config=cfg, log=lambda *_: None)
    assert not r["ok"] and r["error_kind"] == "fetch"
    assert "沙箱" in r["error"] and "mock" in r["error"]


def test_失败_未知类型不静默跳过(tmp_path, cfg):
    p = tmp_path / "x.xyz"
    p.write_bytes(b"unknown-format-bytes")
    r = ingest(str(p), config=cfg, log=lambda *_: None)
    assert not r["ok"] and r["error_kind"] == "unsupported"
    assert "没有转换器" in r["error"]


def test_失败_原件已落盘且登记failed(tmp_path, cfg):
    """转译失败**不删原件** —— 换更好的转换器版本后可以重转（三段保留的价值）。"""
    p = tmp_path / "x.xyz"
    p.write_bytes(b"unknown-format-bytes")
    r = ingest(str(p), config=cfg, log=lambda *_: None)
    assert not r["ok"]
    db = MaterialsDB(cfg.paths["db"])
    try:
        row = db.find_by_sha(sha256_of(p.read_bytes()))
        assert row and row["status"] == "failed" and row["error_kind"]
        assert (cfg.paths["store"] / row["raw_path"]).exists()
    finally:
        db.close()


def test_重转_来源被删也能从raw原件重跑(tmp_path, cfg):
    """**三段保留的全部意义**：原件不可再生、其余都可重建。

    若重转只能靠 `source_ref`（用户的下载目录），用户一清理文件就再也转不了
    —— 保留下来的原件等于白存（2026-10-12 核对投喂数据时发现的缺口）。
    """
    from agentmemhub.feed.service import reconvert, retry

    src = _md(tmp_path, "会消失.md", "# 原始标题\n\n原始内容\n")
    r1 = ingest(str(src), config=cfg, log=lambda *_: None)
    assert r1["ok"]

    src.unlink()                                   # 用户清空了下载目录
    r2 = retry(r1["doc_id"], config=cfg, log=lambda *_: None)
    assert r2["ok"] and r2["doc_id"] == r1["doc_id"]
    # 溯源不被改写：source_ref 仍是原来那个路径（不是 raw 路径）
    row = [m for m in list_materials(config=cfg) if m["doc_id"] == r1["doc_id"]][0]
    assert row["source_ref"].endswith("会消失.md")
    assert row["status"] == "published"

    # 显式 from-raw 也走同一条路
    r3 = reconvert(r1["doc_id"], config=cfg, log=lambda *_: None)
    assert r3["ok"] and r3["doc_id"] == r1["doc_id"]


def test_重转_原件也丢了就如实报错(tmp_path, cfg):
    from agentmemhub.feed.service import reconvert

    src = _md(tmp_path, "a.md")
    r1 = ingest(str(src), config=cfg, log=lambda *_: None)
    # 手工删掉 raw 里的原件（模拟磁盘故障/误删）
    (cfg.paths["store"] / r1["raw_path"]).unlink()
    r2 = reconvert(r1["doc_id"], config=cfg, log=lambda *_: None)
    assert not r2["ok"] and r2["error_kind"] == "not_found"
    assert "raw" in r2["error"]


def test_重转_不存在的doc_id如实报错(cfg):
    from agentmemhub.feed.service import reconvert, retry
    for fn in (retry, reconvert):
        r = fn("没有这个id", config=cfg, log=lambda *_: None)
        assert not r["ok"] and r["error_kind"] == "not_found"


# ---------------------------------------------------------------------------
# 日志：标准头五项（加新事件流必须守住）
# ---------------------------------------------------------------------------

def test_日志_事件行号不是恒等值(tmp_path, cfg):
    """**包装层必须 +1**：`audit_feed()` 默认 depth=3 假设"业务代码直接调它"，
    中间隔了 `_audit()` 包装时不 +1，`line` 就恒等于包装函数内部那一行、
    字段等于作废。2026-10-04 wiki 流踩过（commit 78a6e02），2026-10-12 feed 又犯一次。
    """
    from agentmemhub import logs as L

    ok = _md(tmp_path, "a.md", "# 甲\n\n正文\n")
    ingest(str(ok), config=cfg, log=lambda *_: None)
    bad = tmp_path / "b.pdf"
    bad.write_bytes("%PDF-1.4 假文件".encode("utf-8"))
    ingest(str(bad), config=cfg, log=lambda *_: None)

    evs = [e for e in L.read_feed_audit()
           if e.get("event") in ("feed_published", "feed_failed")]
    assert len(evs) >= 2, "两条事件都该落进 feed.log"
    lines = {e.get("line") for e in evs}
    assert len(lines) > 1, "所有事件 line 相同 ⇒ 包装层没 +1，行号字段作废"
    for e in evs:
        # 标准头五项：时间 / 等级 / 模块 / 行号 / 内容
        assert e.get("ts") and e.get("level") and e.get("module") and e.get("line")
        assert e.get("msg")
        assert e["module"].endswith("feed.service"), "module 应是业务调用点"


def test_日志_失败事件是error级(tmp_path, cfg):
    from agentmemhub import logs as L
    bad = tmp_path / "b.pdf"
    bad.write_bytes("%PDF-1.4 假文件".encode("utf-8"))
    ingest(str(bad), config=cfg, log=lambda *_: None)
    ev = [e for e in L.read_feed_audit() if e.get("event") == "feed_failed"][-1]
    assert ev["level"] == "error", "失败必须能按等级筛出来"



# ---------------------------------------------------------------------------
# kb 页面渲染
# ---------------------------------------------------------------------------

def test_kb页面_frontmatter可解析且值安全():
    doc = FeedDocument(doc_id="d1", title='带: 冒号 "与引号" 的标题',
                       markdown="# 标题\n\n正文", kind="text",
                       source_ref="C:\\path\\a.md", doc_time=1700000000)
    page = render_kb_page(doc)
    head = page.split("---")[1]
    assert "origin: external" in head
    # 值用 JSON 编码 ⇒ 含冒号/引号的标题不会把 frontmatter 写坏，且能被 yaml 读回
    import yaml
    fm = yaml.safe_load(head)
    assert fm["title"] == doc.title and fm["doc"] == "d1"
    assert fm["doc_time"] == 1700000000


def test_kb页面_低质量带warnings字段():
    doc = FeedDocument(doc_id="d2", title="t", markdown="正文", kind="pdf",
                       source_ref="a.pdf", doc_time=0, quality=0.4,
                       warnings=["表格可能失真", "公式丢失"])
    fm = __import__("yaml").safe_load(render_kb_page(doc).split("---")[1])
    assert fm["quality"] == 0.4 and "表格可能失真" in fm["warnings"]
