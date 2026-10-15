# -*- coding: utf-8 -*-
"""M4 · T3 PDF：**四条路由**各自的行为 + 认领优先级 + 一个顺序约束的守护。

夹具是**合成样本**（`tests/fixtures/feed/make_fixtures_m4.py` 生成），因为它们守的是
**路由判据**：四条路由需要四份形态确定的 PDF，真实素材凑不齐、也无法断言"判对了没有"。
解析质量由真实素材端到端验收守（记录在 `docs/modules/feed.md`）。

**隔离**：注入 tmp 配置 —— 绝不碰真实的 `database/materials.db` 与 `exports/`。
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest

from agentmemhub.feed import get_converter
from agentmemhub.feed.base import FeedError
from agentmemhub.feed.converters.pdf import (ROUTE_ENCRYPTED, ROUTE_SCANNED,
                                             ROUTE_TABLE, ROUTE_TEXT, probe,
                                             render_pages, route)
from agentmemhub.feed.service import ingest

FIX = Path(__file__).resolve().parent / "fixtures" / "feed"


def _fx(name: str) -> bytes:
    return (FIX / name).read_bytes()


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
        return {"convert": {"llm": False, "ocr": "local"}}


@pytest.fixture()
def cfg(tmp_path):
    return _FakeCfg(tmp_path)


def _pdf(tmp_path, name: str, data: bytes) -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


def _quiet(*_a, **_kw):
    return None


# ---------------------------------------------------------------------------
# 探针与路由判据（纯函数，直接喂夹具字节）
# ---------------------------------------------------------------------------

def test_探针_文字版_判_T3a():
    info = probe(_fx("pdf_text.pdf"))
    assert info["pages"] == 2
    assert info["avg_chars"] >= 50
    assert info["tables"] == 0
    assert route(info) == ROUTE_TEXT
    assert info["title"] == "PDF text routing fixture"


def test_探针_表格版_判_T3b():
    info = probe(_fx("pdf_table.pdf"))
    assert info["tables"] >= 1, "夹具必须真的检出表格线条，否则测的不是 T3b 路由"
    assert info["table_engine"] == "pdfplumber"
    assert route(info) == ROUTE_TABLE


def test_探针_扫描件_判_T3c():
    info = probe(_fx("pdf_scanned.pdf"))
    assert info["pages"] == 1
    assert info["avg_chars"] < 50
    assert route(info) == ROUTE_SCANNED


def test_探针_加密档_判_T3d_且探针本身不抛异常():
    """**顺序约束守护**：未解密的 PdfReader 一旦访问 `pages` 就抛
    `FileNotDecryptedError` —— 探针若先读页数，加密档会变成"崩溃"而不是可处置的 T3d。
    """
    info = probe(_fx("pdf_encrypted.pdf"))          # 这一行不许抛
    assert info["encrypted"] is True
    assert info["decrypted"] is False
    assert route(info) == ROUTE_ENCRYPTED


def test_加密但空密码可打开时不判_T3d():
    """有些 PDF 只设了 owner 密码（禁止改），用户能正常打开 —— 不该被当成加密档拒收。"""
    import pypdf

    writer = pypdf.PdfWriter(clone_from=io.BytesIO(_fx("pdf_text.pdf")))
    writer.encrypt(user_password="", owner_password="owner-secret")
    buf = io.BytesIO()
    writer.write(buf)
    info = probe(buf.getvalue())
    assert info["encrypted"] is True and info["decrypted"] is True
    assert route(info) == ROUTE_TEXT


def test_路由_全是空页等于扫描件处置():
    """一页都没有 ⇒ 拿不到文本，与扫描件同等处置（不能让 0 除出 NaN 再判成 T3a）。"""
    assert route({"pages": 0, "avg_chars": 0.0}) == ROUTE_SCANNED


def test_元数据时间按UTC解析不偏小时():
    """夹具的 `/CreationDate` 是 `D:20260101120000Z`（**UTC**）。

    早先的实现正则里丢掉了末尾那个 `Z`，又用 `time.mktime` 按本地时间解释 ⇒
    `doc_time` 系统性偏 8 小时。时间不是装饰：召回侧的时间衰减按它算。
    """
    from datetime import datetime, timezone

    info = probe(_fx("pdf_text.pdf"))
    assert info["doc_time"] == int(
        datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc).timestamp())


def test_元数据标题被截断时用更完整的首行():
    """**真实素材逼出来的**（`Intel_Proprietary_License.pdf`）：`/Title` 被写坏了，
    停在 `[v2022.12`，而正文首行是完整的 `[v2022.12.20]` —— 元数据不能盲信。"""
    from agentmemhub.feed.converters.pdf import title_of

    info = {"title": "C2A Limited Distribution (Commercial Use) License Agreement [v2022.12",
            "first_line": "Limited Distribution License Agreement [v2022.12.20]"}
    title, src = title_of(info, "x.pdf")
    assert title == "Limited Distribution License Agreement [v2022.12.20]"
    assert src == "first_line"


def test_首行明显更短时不夺走元数据标题():
    """反向守护：首行只是元数据标题的一小段（截断了的页眉）时，元数据标题更完整。"""
    from agentmemhub.feed.converters.pdf import title_of

    title, src = title_of({"title": "2026年度技术报告", "first_line": "技术报告"}, "x.pdf")
    assert title == "2026年度技术报告" and src == "meta"


def test_元数据标题与首行无关时仍信元数据():
    """首行是页眉的情况很常见 —— 与元数据标题无关就不要拿它当标题。"""
    from agentmemhub.feed.converters.pdf import title_of

    title, src = title_of({"title": "季度技术报告", "first_line": "公司内部资料 请勿外传"},
                          "x.pdf")
    assert title == "季度技术报告" and src == "meta"


def test_Word导出脏标题前缀被剥掉():
    from agentmemhub.feed.converters.pdf import title_of

    title, src = title_of({"title": "Microsoft Word - 季度报告.pdf", "first_line": ""},
                          "x.pdf")
    assert title == "季度报告.pdf" and src == "meta"


def test_损坏字节_报_convert_且不可重试():
    with pytest.raises(FeedError) as ei:
        probe(b"this is definitely not a pdf")
    assert ei.value.kind == "convert"
    assert ei.value.retryable is False


# ---------------------------------------------------------------------------
# T3a：文字版端到端
# ---------------------------------------------------------------------------

def test_端到端_文字版PDF_出kb页且保住页码(tmp_path, cfg):
    r = ingest(str(_pdf(tmp_path, "text.pdf", _fx("pdf_text.pdf"))),
               config=cfg, log=_quiet)
    assert r["ok"] is True, r.get("error")
    assert r["kind"] == "pdf" and r["converter"] == "pdf@1"
    text = Path(r["kb_path"]).read_text(encoding="utf-8")
    assert "Agent Memory Hub - PDF routing fixture" in text
    assert 'kind: "pdf"' in text
    assert "## 第 2 页" in text, "多页 PDF 必须保住页码 —— 它是回原件的引用锚"
    assert "citation" in text


def test_文字版_单页不加页码小标题(tmp_path):
    """单页加 `## 第 1 页` 只是噪声（页数信息在 frontmatter 的 meta 里）。"""
    from agentmemhub.feed.converters.pdf import PdfConverter

    conv = PdfConverter()
    doc = conv.convert(ref="a.pdf", data=_fx("pdf_table.pdf"),
                       meta={"doc_id": "x", "mtime": 0, "now": 0})
    # 表格版是单页 —— 用来验证"单页不补页码"这条（换 T3a 单页同理）
    assert "## 第 1 页" not in doc.markdown


# ---------------------------------------------------------------------------
# T3b：表格版
# ---------------------------------------------------------------------------

def test_表格版_表格进markdown且表格文字不重复(tmp_path, cfg):
    r = ingest(str(_pdf(tmp_path, "t.pdf", _fx("pdf_table.pdf"))),
               config=cfg, log=_quiet)
    assert r["ok"] is True, r.get("error")
    text = Path(r["kb_path"]).read_text(encoding="utf-8")
    assert "| Region | Revenue | Growth |" in text
    assert "| North | 1280 | +12% |" in text
    # **剔除表格区域**的效果：表头文字在流式正文里不该再出现一次
    assert text.count("Region") == 1, "表格文字重复出现 ⇒ 表格区域没从正文里剔除"
    # 表格之外的行文必须还在（"保住表格却丢了正文"是回归不是进步）
    assert "surrounding prose" in text


def test_表格版_质量低于文字版但不是不合格(tmp_path, cfg):
    r_tbl = ingest(str(_pdf(tmp_path, "t.pdf", _fx("pdf_table.pdf"))),
                   config=cfg, log=_quiet)
    r_txt = ingest(str(_pdf(tmp_path, "x.pdf", _fx("pdf_text.pdf"))),
                   config=cfg, log=_quiet)
    assert r_txt["quality"] == 1.0
    assert 0.5 <= r_tbl["quality"] < r_txt["quality"]


# ---------------------------------------------------------------------------
# T3c：扫描件（本档只到"渲染 + 明确指路"）
# ---------------------------------------------------------------------------

def test_扫描件_报needs_ocr并把页面渲进mirror(tmp_path, cfg):
    r = ingest(str(_pdf(tmp_path, "scan.pdf", _fx("pdf_scanned.pdf"))),
               config=cfg, log=_quiet)
    assert r["ok"] is False
    assert r["error_kind"] == "needs_ocr"
    # 原件必须落盘（内容寻址：将来补上 OCR 通路能对着同一个 doc_id 续跑）
    assert r["raw_path"] and (cfg.paths["store"] / r["raw_path"]).is_file()
    pages = cfg.paths["store"] / "mirror" / r["doc_id"] / "pages"
    pngs = sorted(pages.glob("*.png"))
    assert len(pngs) == 1, "扫描件必须先渲染落 mirror，OCR 通路接上后才能续跑"
    assert pngs[0].read_bytes()[:4] == b"\x89PNG"
    # 错误信息必须**指路**（只说"失败了"等于把人扔在原地）
    assert "T4" in r["error"] or "OCR" in r["error"]
    # 没有正文 ⇒ 不发布 kb
    assert not list(cfg.paths["kb"].glob("*.md"))


def test_渲染函数_页数上限与替身可测():
    seen: dict[str, bytes] = {}
    n, warns = render_pages(_fx("pdf_scanned.pdf"),
                            put_mirror=lambda name, blob: seen.__setitem__(name, blob))
    assert n == 1 and not warns
    assert list(seen) == ["pages/page-001.png"]
    assert seen["pages/page-001.png"][:4] == b"\x89PNG"


def test_渲染函数_无写入口时不假装渲染成功():
    n, warns = render_pages(_fx("pdf_scanned.pdf"), put_mirror=None)
    assert n == 0 and warns and "mirror" in warns[0]


# ---------------------------------------------------------------------------
# T3d：加密档
# ---------------------------------------------------------------------------

def test_加密档_报encrypted不硬闯(tmp_path, cfg):
    r = ingest(str(_pdf(tmp_path, "enc.pdf", _fx("pdf_encrypted.pdf"))),
               config=cfg, log=_quiet)
    assert r["ok"] is False
    assert r["error_kind"] == "encrypted"
    assert "密码" in r["error"]
    # 原件仍然落盘（将来用户给出解锁版本可以对着同一个 doc_id 追溯）
    assert r["raw_path"]
    assert not list(cfg.paths["kb"].glob("*.md"))


# ---------------------------------------------------------------------------
# 认领：谁处理什么
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 真实素材逼出来的两条：误检表格会**吞掉正文**、首行被行宽硬切
# ---------------------------------------------------------------------------

def test_单行误检表格不算表格_正文一个字都不能少():
    """**真实素材（求职 PDF）踩到的静默丢内容**：pdfplumber 把一行等宽字体代码
    检成"1 行 2 列"的表格；照单全收的话那行字会被当表格**从正文里剔除**，
    而它又不足以构成表格 ⇒ 内容两头落空、永久丢失。

    这里同时守住三件事：路由不判 T3b、那行字仍在正文里、误检数量可查。
    """
    info = probe(_fx("pdf_false_table.pdf"))
    assert info["tables"] == 0, "1 行 2 列不算表格"
    assert info["tables_skipped"] >= 1, "「看见了但没当表格」必须可查（不静默）"
    assert route(info) == ROUTE_TEXT

    from agentmemhub.feed.converters.pdf import PdfConverter

    doc = PdfConverter().convert(
        ref="x.pdf", data=_fx("pdf_false_table.pdf"),
        meta={"doc_id": "x", "mtime": 0, "now": 0})
    # 被框住的那行必须还在（这正是当初丢掉的内容）
    assert "const ok = await el.isVisible();" in doc.markdown
    # 框外面的正文也不能被顺手削掉
    assert "must survive" in doc.markdown
    assert "| --- |" not in doc.markdown, "误检表格不该被渲染成 markdown 表格"
    assert doc.meta["tables"] == 0


def test_首行被行宽硬切时缝合成完整句子():
    """PDF 文本层里"一行"是**排版产物**：真实素材的首行停在 `…工作经`，
    `验。` 落在第二行。直接取首行当标题就是一句断掉的话。"""
    from agentmemhub.feed.converters.pdf import title_of

    info = probe(_fx("pdf_wrapped.pdf"))
    assert info["title"] == "", "夹具必须没有元数据标题，否则测的不是兜底路径"
    assert info["first_line"] == \
        "Quarterly review of the memory pipeline for the north region"
    title, src = title_of(info, "x.pdf")
    assert title == "Quarterly review of the memory pipeline for the north region"
    assert src == "first_line"


def test_首行过长时限长并如实打标():
    """标题是"替代品"必须让人知道 —— 像是原文的标题比占位标题更危险。"""
    from agentmemhub.feed.converters.pdf import PdfConverter
    from agentmemhub.feed.titles import TITLE_MAX

    doc = PdfConverter().convert(
        ref="x.pdf", data=_fx("pdf_false_table.pdf"),
        meta={"doc_id": "x", "mtime": 0, "now": 0})
    assert doc.meta["title_source"] == "first_line_clipped"
    assert doc.title.endswith("…") and len(doc.title) <= TITLE_MAX + 1
    assert any("首段前" in w for w in doc.warnings)
    assert doc.quality < 1.0


def test_认领_无扩展名但PDF魔数():
    conv = get_converter(ref="download", kind="", data=_fx("pdf_text.pdf"))
    assert conv is not None and conv.name == "pdf"


def test_认领_https链接的pdf归pdf而非url():
    """**注册顺序守护**：pdf 若排在 url 之后，`feed add https://…/x.pdf`
    会被 URL 转换器先认领，然后拿 trafilatura 去解析 PDF 字节。"""
    conv = get_converter(ref="https://example.com/report.pdf", kind="url",
                         data=_fx("pdf_text.pdf"))
    assert conv is not None and conv.name == "pdf"


def test_认领_普通链接仍归url():
    conv = get_converter(ref="https://example.com/a.html", kind="url",
                         data=b"<html><body>x</body></html>")
    assert conv is not None and conv.name in ("url", "wechat")


def test_认领_非pdf字节不归pdf():
    conv = get_converter(ref="download", kind="", data=b"hello world")
    assert conv is None or conv.name != "pdf"


def test_认领_显式kind能认领无扩展名():
    conv = get_converter(ref="无扩展名文件", kind="pdf", data=b"")
    assert conv is not None and conv.name == "pdf"
