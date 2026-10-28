# -*- coding: utf-8 -*-
"""M3：网页 / 公众号转译（T2a / T2b）。

**绝不出网**：夹具是真实样本裁出来的（`tests/fixtures/feed/`），取回层一律 mock。
这也是本项目第三次"测试静默产生真实副作用"教训后的纪律 —— 出网口只有
`feed.fetch.fetch` 一处，且它自带沙箱熔断。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from agentmemhub.feed import fetch as F
from agentmemhub.feed import get_converter
from agentmemhub.feed.base import FeedError
from agentmemhub.feed.converters import wechat as W
from agentmemhub.feed.service import ingest

FIX = Path(__file__).resolve().parent / "fixtures" / "feed"
WECHAT_URL = "https://mp.weixin.qq.com/s/RO8-iHoyno70FvC8L7Vxuw"
DOC_URL = "https://www.pushplus.plus/doc/guide/openApi.html"


class _FakeCfg:
    def __init__(self, root: Path):
        self.root = root
        self.paths = {"db": root / "materials.db", "store": root / "store",
                      "kb": root / "kb"}

    def feed_paths(self):
        return dict(self.paths)

    def feed(self):
        # 只放真实存在的键（`feed.convert.*` / `feed.fetch.renderer` 是没有读取点的
        # 假开关，2026-10-15 已从 DEFAULT_FEED 删除，测试里的假配置也不该再复刻它们）
        return {"fetch": {"timeout": 20}}

    @property
    def wiki(self):
        return {"out_l2": ""}


# ---------------------------------------------------------------------------
# 本地另存的网页（**M3 承诺过、实际走不通的那条路**）
# ---------------------------------------------------------------------------

def test_认领_本地另存的网页(tmp_path):
    """M3 的文档里写了"浏览器另存为 HTML 后 `feed add <文件>`"，
    但 `UrlConverter.can_handle` 当时只认 `http(s)://` —— 那条兜底通路**根本走不通**。
    2026-10-15 扫用户文档目录时发现：61 个本地 `.html` 一个都进不来。"""
    p = tmp_path / "saved.html"
    p.write_bytes("<html><body><p>正文</p></body></html>".encode("utf-8"))
    conv = get_converter(ref=str(p), kind="")
    assert conv is not None and conv.name == "url"


def test_认领_无扩展名的HTML报告(tmp_path):
    """测试工具导出的报告常常没有扩展名，靠**字节**认。"""
    p = tmp_path / "report"
    p.write_bytes(b"<!DOCTYPE html>\n<html><body><p>x</p></body></html>")
    conv = get_converter(ref=str(p), kind="", data=p.read_bytes())
    assert conv is not None and conv.name == "url"


def test_本地网页_图片按文件系统解析而不是urljoin(tmp_path):
    """本地另存网页的图片是**文件系统相对路径**（`assets/a.png`）——
    拿 `urljoin` 去解析只会得到一堆打不开的地址。"""
    from agentmemhub.feed.converters.url import UrlConverter

    (tmp_path / "assets").mkdir()
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    (tmp_path / "assets" / "a.png").write_bytes(png)
    html = tmp_path / "page.html"
    html.write_text(
        '<html><body><img src="assets/a.png"><p>%s</p></body></html>'
        % ("本地网页的正文内容。" * 40), encoding="utf-8")
    seen: dict[str, bytes] = {}

    def put_image(url, blob):
        seen[url] = blob
        return "assets/localized.png"

    doc = UrlConverter().convert(
        ref=str(html), data=html.read_bytes(),
        meta={"doc_id": "x", "mtime": 0, "now": 0, "base_dir": str(tmp_path),
              "put_image": put_image})
    assert doc.kind == "html", "本地网页不是 URL，kind 要说清楚"
    assert seen, "相对路径的图片没被本地化"
    assert next(iter(seen.values()))[:4] == b"\x89PNG"


def test_本地网页_文档时间用文件mtime而不是投喂时刻(tmp_path):
    """本地文件没有 HTTP 头 ⇒ 用 mtime 比"投喂时刻"更接近文档时间
    （时间进召回侧衰减，不是装饰）。"""
    from agentmemhub.feed.converters.url import UrlConverter

    html = tmp_path / "page.html"
    html.write_text("<html><body><p>%s</p></body></html>"
                    % ("正文内容够长才不会被判空。" * 30), encoding="utf-8")
    doc = UrlConverter().convert(
        ref=str(html), data=html.read_bytes(),
        meta={"doc_id": "x", "mtime": 1700000000, "now": 1800000000,
              "base_dir": str(tmp_path)})
    assert doc.doc_time == 1700000000, doc.meta.get("doc_time_source")


@pytest.fixture()
def cfg(tmp_path):
    return _FakeCfg(tmp_path)


def _fake_fetch(page: bytes, *, status: int = 200, headers: dict | None = None,
                image: bytes = b"\x89PNG\r\n\x1a\nFAKEIMAGE"):
    """替身取回层：页面返回夹具，图片返回假字节。**记录调用过的 URL。"""
    calls: list[str] = []

    def _f(url, **kw):
        calls.append(str(url))
        if str(url) == WECHAT_URL or str(url) == DOC_URL or str(url).startswith("https://example"):
            return F.Fetched(url=str(url), status=status, body=page,
                             headers=headers or {"last-modified": "Wed, 08 Oct 2026 08:26:51 GMT"})
        return F.Fetched(url=str(url), status=200, body=image, headers={})

    _f.calls = calls
    return _f


# ---------------------------------------------------------------------------
# 认领与定向工具
# ---------------------------------------------------------------------------

def test_认领_公众号特化优先于通用():
    assert get_converter(ref=WECHAT_URL).name == "wechat"
    assert get_converter(ref=DOC_URL).name == "url"
    assert get_converter(ref="a.md").name == "text"
    # 域名不含 mp.weixin 的链接不该被特化认领
    assert get_converter(ref="https://mp.weixin.qq.com.evil.com/s/x").name == "url"


def test_切js_content按div配对不腰斩():
    """**不能用"第一个 `</div>` 就截断"** —— 正文里有嵌套 div（图片容器/引用块），
    实测首个 `</div>` 距正文起点 76,623 字符，那样会把正文腰斩。"""
    html = ('<div id="js_content"><p>第一段</p><div class="img">'
            '<img src="x.jpg"></div><p>第二段</p></div><div id="tail">尾巴</div>')
    frag = W.slice_js_content(html)
    assert "第一段" in frag and "第二段" in frag and "尾巴" not in frag
    assert frag.rstrip().endswith("</div>")


def test_发布时间_元素优先但不信var_ct():
    """⚠️ 真页**没有** `#publish_time` 元素（实测它只出现在 URL 编码的 JSON 里）；
    而 `var ct` 更接近**渲染时刻**，不能当发布时间。所以：
      · 有 `id="publish_time"` 元素 → 用它；
      · 只有 `var ct` → 返回 0（交给 metadata 的日期，`doc_time_source` 会标出来）。
    """
    html = ('<script>var ct = "1791790200";</script>'
            '<em id="publish_time">2024-09-26 08:30</em>')
    ts = W.parse_publish_time(html)
    import time
    assert ts > 0 and time.strftime("%Y-%m-%d", time.localtime(ts)) == "2024-09-26"
    # 只有 var ct：**不采用**（宁可回落到 metadata 日期，也不要一个错的"发布时间"）
    assert W.parse_publish_time('<script>var ct = "1791790200";</script>') == 0


# ---------------------------------------------------------------------------
# 抽取（真实夹具）
# ---------------------------------------------------------------------------

def test_夹具_公众号能抽出正文与元数据():
    raw = (FIX / "wechat_article.html").read_bytes()
    md, meta = F.extract_article(raw, WECHAT_URL)
    assert len(md) > 2000, "真实文章正文不该这么短"
    assert "Jenkins" in md or "Docker" in md
    assert meta.get("title")
    assert str(meta.get("date") or "").startswith("2024-09-26")


def test_夹具_文档站能抽出正文():
    raw = (FIX / "doc_site.html").read_bytes()
    md, meta = F.extract_article(raw, DOC_URL)
    assert len(md) > 5000
    assert "pushplus" in md.lower()


# ---------------------------------------------------------------------------
# 端到端（mock 取回层）
# ---------------------------------------------------------------------------

def test_端到端_公众号投喂落三段(tmp_path, cfg, monkeypatch):
    raw = (FIX / "wechat_article.html").read_bytes()
    monkeypatch.setattr(F, "fetch", _fake_fetch(raw))

    r = ingest(WECHAT_URL, config=cfg, log=lambda *_: None)

    assert r["ok"], r
    assert r["converter"] == "wechat@1" and r["kind"] == "wechat"
    assert "Jenkins" in r["title"] or "Docker" in r["title"] or "虚拟机" in r["title"]
    # 文档时间用**发布日**（2024-09-26），不是抓取时刻
    import time
    assert time.strftime("%Y", time.localtime(r["doc_time"])) == "2024"

    # 第 1 段：原件 = 抓到的原始字节（逐字节一致）
    assert (cfg.paths["store"] / r["raw_path"]).read_bytes() == raw
    # 第 2 段：落地页源码
    mirror = cfg.paths["store"] / "mirror" / r["doc_id"] / "index.html"
    assert mirror.is_file() and mirror.read_bytes() == raw
    # 第 3 段：交付物 frontmatter 的 source_ref 是 URL
    kb = Path(r["kb_path"]).read_text(encoding="utf-8")
    assert 'source_ref: "https://mp.weixin.qq.com/s/' in kb
    assert "origin: external" in kb


def test_端到端_普通网页投喂(tmp_path, cfg, monkeypatch):
    raw = (FIX / "doc_site.html").read_bytes()
    monkeypatch.setattr(F, "fetch", _fake_fetch(raw))
    r = ingest(DOC_URL, config=cfg, log=lambda *_: None)
    assert r["ok"] and r["kind"] == "url" and r["converter"] == "url@1"
    assert (cfg.paths["store"] / r["raw_path"]).read_bytes() == raw


def test_端到端_图片本地化并改写为相对引用(tmp_path, cfg, monkeypatch):
    """外链图片必须落本地：图床有 **referer 防盗链**且外链会失效，
    而知识库要能长期读（这是 `mirror/` 的用途之一）。

    ⚠️ 实测 trafilatura 的 markdown **会丢图片**（真页 14 张 → 0 张），所以本地化做在
    **HTML 层面**：图必定进 `mirror/`；抽取器丢图时如实记警告（不静默失真）。
    """
    raw = (FIX / "wechat_article.html").read_bytes()
    fake = _fake_fetch(raw)
    monkeypatch.setattr(F, "fetch", fake)

    r = ingest(WECHAT_URL, config=cfg, log=lambda *_: None)
    assert r["ok"]
    imgs = sorted((cfg.paths["store"] / "mirror" / r["doc_id"] / "images").glob("*"))
    assert imgs, "图片该落在 mirror/<doc_id>/images/ 下"
    assert any(str(u).startswith("https://mmbiz.qpic.cn") for u in fake.calls), \
        "确实去下载了外链图片"
    # `index.html` = **抓到的原始源码**（镜像语义是"当时下载到了什么"），含外链是对的；
    # 改写版另存 `localized.html`（图片已换成本地引用）
    mdir = cfg.paths["store"] / "mirror" / r["doc_id"]
    assert (mdir / "index.html").read_bytes() == raw, "镜像必须是原始字节"
    loc = (mdir / "localized.html").read_bytes()
    assert b"mmbiz.qpic.cn" not in loc, "改写版的图片引用应是本地路径"
    # 抽出来的正文里没有图片引用 ⇒ 必须**有警告**说明这件事
    row = [m for m in __import__("agentmemhub.feed.service", fromlist=["x"])
           .list_materials(config=cfg)][0]
    assert row["warnings"], "抽取器丢图必须留下可见警告，而不是静默丢"


def test_端到端_同一URL重复投喂幂等(tmp_path, cfg, monkeypatch):
    raw = (FIX / "doc_site.html").read_bytes()
    monkeypatch.setattr(F, "fetch", _fake_fetch(raw))
    r1 = ingest(DOC_URL, config=cfg, log=lambda *_: None)
    r2 = ingest(DOC_URL, config=cfg, log=lambda *_: None)
    assert r1["ok"] and r2["ok"] and r2["skipped"] == "duplicate"
    assert r1["doc_id"] == r2["doc_id"], "内容没变 ⇒ 同一 doc_id"


# ---------------------------------------------------------------------------
# 反爬 / JS 渲染 / 兜底指引
# ---------------------------------------------------------------------------

def test_同一URL字节变了也原地更新不产生重复(tmp_path, cfg, monkeypatch):
    """**URL 的身份是"最终 URL"，不是内容 hash**。

    动态页面字节每次都变（时间戳 / CSRF / 广告位）—— 2026-10-12 真机验收实测：
    同一篇公众号文章两次抓取 sha 不同，按内容判身份会让**每投喂一次多一条素材**。
    """
    from agentmemhub.feed import service

    raw1 = (FIX / "wechat_article.html").read_bytes()
    raw2 = raw1 + "<!-- 第二次抓取的动态内容 -->".encode("utf-8")
    monkeypatch.setattr(F, "fetch", _fake_fetch(raw1))
    r1 = ingest(WECHAT_URL, config=cfg, log=lambda *_: None)
    monkeypatch.setattr(F, "fetch", _fake_fetch(raw2))
    r2 = ingest(WECHAT_URL, config=cfg, log=lambda *_: None)

    assert r1["ok"] and r2["ok"]
    assert r1["doc_id"] == r2["doc_id"], "同 URL 必须复用同一身份（原地更新）"
    assert r2.get("updated_existing") is True
    assert len(service.list_materials(config=cfg)) == 1, "不该出现第二条"

    # 字节都没变 ⇒ 真幂等（连转换都跳过）
    monkeypatch.setattr(F, "fetch", _fake_fetch(raw2))
    r3 = ingest(WECHAT_URL, config=cfg, log=lambda *_: None)
    assert r3["skipped"] == "duplicate" and r3["doc_id"] == r1["doc_id"]


def test_反爬验证页_不硬闯且给兜底指引(tmp_path, cfg, monkeypatch):
    page = "<html><body><h2>环境异常</h2><p>请在微信客户端打开链接</p></body></html>".encode()
    monkeypatch.setattr(F, "fetch", _fake_fetch(page))
    r = ingest(WECHAT_URL, config=cfg, log=lambda *_: None)
    assert not r["ok"] and r["error_kind"] == "fetch"
    assert "--stdin" in r["error"] and "另存" in r["error"], "必须给出可操作的兜底通路"


def test_JS渲染判据():
    """SPA 客观判据：正文占比 <3% 且命中框架标记。"""
    spa = ('<html><body><div id="app"></div><script>window.__NEXT_DATA__={}</script>'
           + "x" * 30000 + "</body></html>").encode()
    assert F.is_probably_js_rendered(spa, "") is True
    # 正文占比高的静态页不算
    good = ("<html><body><article>" + "正文内容" * 3000 + "</article></body></html>").encode()
    assert F.is_probably_js_rendered(good, "正文内容" * 3000) is False


def test_取回层沙箱熔断():
    """唯一出网口在测试里必须熔断（不能靠"测试记得 mock"）。"""
    with pytest.raises(FeedError) as ei:
        F.fetch("https://example.com/")
    assert ei.value.kind == "fetch" and not ei.value.retryable


def test_超大响应中止():
    """防呆：抓到一个无限流会拖死整批（实测真页 3.6 MB，8 MB 上限留足余量）。"""
    import inspect
    sig = inspect.signature(F.fetch)
    assert sig.parameters["max_bytes"].default == 8 * 1024 * 1024


def test_doc_time优先元数据再Last_Modified():
    import time
    now = int(time.time())
    # ① 元数据日期优先
    t1 = F.doc_time_of({"date": "2020-01-02"}, {"last-modified": "Wed, 08 Oct 2026 08:26:51 GMT"},
                       fallback=now)
    assert time.strftime("%Y-%m-%d", time.localtime(t1)) == "2020-01-02"
    # ② 没元数据 → Last-Modified
    t2 = F.doc_time_of({}, {"last-modified": "Wed, 08 Oct 2026 08:26:51 GMT"}, fallback=now)
    assert t2 != now and time.strftime("%Y", time.localtime(t2)) == "2026"
    # ③ 都没有 → 兜底
    assert F.doc_time_of({}, {}, fallback=now) == now
