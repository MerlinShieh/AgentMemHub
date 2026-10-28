# -*- coding: utf-8 -*-
"""PPTX · 演示文稿：把**分页 + 版面层级**还原成 markdown。

它和 DOCX 是同一类（OOXML：ZIP + XML，语义结构在文件里），所以同样**确定性、零 LLM**。
差别在两处，都要单独处理：

1. **页是天然的一级结构**：`## 第 N 页` 就是它的 `w:body`，
   和 PDF 的"页"不同 —— PPT 的页是作者**刻意分的**，不是排版折行；
2. **备注（notes）常常比正文更有信息量**：演讲稿、补充说明都写在那里。
   实测用户那 4 份评审 PPT 每份都有备注 —— 丢了备注等于丢了讲稿。

能力边界（明说，不假装）
======================
· **图文分离**：图片落 `mirror/`，但"图片画在第几行"表达不出来
  （PPT 是**版面**不是流式文档，形状靠坐标定位，没有"行"的概念）；
· **图表（chart）不是文字**：`python-pptx` 能读出图表背后的**数据表**，
  但读不出"这张图说明了什么"—— 那是 T4c 多模态的活，不在这里硬凑；
· **动画/切换/母版**不进正文：它们是呈现方式，不是语义。
"""
from __future__ import annotations

import io
from pathlib import Path

from ..base import BaseConverter, FeedDocument, FeedError
from ..mdtable import table_md
from ..titles import TITLE_MAX, clean_meta_title, clip_title, is_junk_title
from .office import doc_time_of

#: 认领的扩展名（`.pptm` 是带宏的同构格式）
EXTS = (".pptx", ".pptm", ".ppsx")
#: 单份演示文稿的页数上限（实测最大 36 页；防极端文件把投喂拖死）
MAX_SLIDES = 300
#: 内嵌图片上限
MAX_IMAGES = 60
#: 有文字的页**占比**低于它，就认为这份 PPT 基本是图。
#: ⚠️ 必须用占比而不是绝对页数：单页 PPT 必然只有 1 页有文字，
#: 拿"≤1 页"去判会把一份正常的一页 PPT 判成"正文都在图片里"（写测试时踩到）。
THIN_TEXT_RATIO = 0.3


def _one_line(text, limit: int = 400) -> str:
    s = " ".join(str(text or "").split())
    return s[:limit] + ("…" if len(s) > limit else "")


def _iter_shapes(shapes, depth: int = 0):
    """遍历形状，**分组形状要递归**（PPT 里把几个框组合起来是常态）。"""
    if depth > 6:
        return
    for shape in shapes:
        try:
            if shape.shape_type is not None and str(shape.shape_type).startswith("GROUP"):
                yield from _iter_shapes(shape.shapes, depth + 1)
                continue
        except Exception:                    # noqa: BLE001
            pass
        yield shape


def _shape_text(shape) -> str:
    try:
        if shape.has_text_frame:
            return _one_line(shape.text_frame.text)
    except Exception:                        # noqa: BLE001
        pass
    return ""


def _shape_table(shape) -> list[list[str]] | None:
    try:
        if not shape.has_table:
            return None
        table = shape.table
        rows = []
        for row in table.rows:
            rows.append([_one_line(c.text, 120) for c in row.cells])
        return rows
    except Exception:                        # noqa: BLE001
        return None


def _shape_picture(shape, *, put_image, state: dict) -> str:
    """图片落 `mirror/`，返回 markdown 引用（取不到就返回空串）。

    ⚠️ **先判它是不是图片**：`shape.image` 只存在于 `Picture` 类上，
    对线条、自选图形、空文本框调用会抛异常。早先那版对每个"没有文字的形状"
    都试一遍 ⇒ 把大量普通形状记成"图片读取失败"（实测：一份 8 页 PPT 报了
    "8 张图片读取失败"，实际它只有 4 张图）—— **假告警比漏报更伤**。

    ⚠️ 图片在 PPT 里**没有"行位置"**可挂 —— 所以 markdown 里统一附在该页文字之后，
    而不是假装能插回原文位置（PPT 是版面，不是流式文档）。
    """
    if not callable(put_image):
        return ""
    try:
        from pptx.enum.shapes import MSO_SHAPE_TYPE
        if shape.shape_type != MSO_SHAPE_TYPE.PICTURE:
            return ""
    except Exception:                        # noqa: BLE001 —— 判不了类型就别猜
        return ""
    try:
        image = shape.image
        blob = image.blob
        if not blob:
            return ""
        if state["images"] >= MAX_IMAGES:
            state["image_capped"] = True
            return ""
        state["images"] += 1
        suffix = "." + (getattr(image, "ext", "") or "png")
        name = "slide-%03d-img-%02d%s" % (state["page"], state["images"], suffix)
        return put_image("pptx/%s" % name, blob)
    except Exception:                        # noqa: BLE001 —— 真的是图片但读不出来
        state["broken_images"] += 1
        return ""


def _slide_notes(slide) -> str:
    """备注 —— 实测比正文更有信息量（讲稿/补充说明写在这里）。"""
    try:
        if not slide.has_notes_slide:
            return ""
        frame = slide.notes_slide.notes_text_frame
        text = _one_line(frame.text, 2000) if frame is not None else ""
        # 备注页里常有一个"页码占位符"，它不是内容
        return "" if text.isdigit() else text
    except Exception:                        # noqa: BLE001
        return ""


def _title_of(cp, page_titles: list[str], ref: str) -> tuple[str, str]:
    """标题：core `title` → 首页标题 → 文件名。"""
    t = clean_meta_title(getattr(cp, "title", "") or "")
    if t:
        return t, "core_title"
    for cand in page_titles:
        if cand and 2 <= len(cand) <= 120:
            clipped, did = clip_title(cand)
            return clipped, "first_slide_clipped" if did else "first_slide"
    stem = Path(str(ref)).stem
    if not is_junk_title(stem):
        return stem, "stem"
    return "演示文稿", "fallback"


class PptxConverter(BaseConverter):
    """`.pptx` 演示文稿（OOXML，与 `.docx` 同类：确定性、零 LLM）。"""

    name = "pptx"
    version = "1"
    #: 认领的后缀（**类属性**，供格式白名单从注册表收集 —— 见 `feed/formats.py`）
    EXTS = EXTS

    def can_handle(self, *, ref: str, kind: str = "",
                   data: bytes | None = None) -> bool:
        if kind == "pptx":
            return True
        if kind and kind not in ("url",):
            return False
        if Path(str(ref)).suffix.lower() in EXTS:
            return True
        # 没有扩展名：ZIP 里含 `ppt/presentation.xml` 才算（别抢 .docx / .xmind）
        if data and bytes(data[:4]) == b"PK\x03\x04":
            try:
                import zipfile
                with zipfile.ZipFile(io.BytesIO(data)) as z:
                    return "ppt/presentation.xml" in set(z.namelist())
            except Exception:                # noqa: BLE001
                return False
        return False

    def convert(self, *, ref: str, data: bytes, meta: dict) -> FeedDocument:
        try:
            from pptx import Presentation
        except ImportError:                  # pragma: no cover —— 依赖在 pyproject 里
            raise FeedError("未安装 python-pptx（`uv sync` 可装上）",
                            kind="unsupported", retryable=False)
        try:
            prs = Presentation(io.BytesIO(data))
        except Exception as e:               # noqa: BLE001
            raise FeedError("PPTX 结构无法解析（文件可能损坏，或不是真正的 .pptx）：%s"
                            % str(e)[:120], kind="convert", retryable=False)

        put_image = meta.get("put_image")
        state = {"images": 0, "page": 0, "broken_images": 0, "image_capped": False,
                 "tables": 0}
        warns: list[str] = []
        pages: list[tuple[str, list[str]]] = []      # (页标题, 该页 markdown 块)
        page_titles: list[str] = []

        slides = list(prs.slides)
        if len(slides) > MAX_SLIDES:
            warns.append("演示文稿共 %d 页，超过单份处理上限 %d 页，只转译前 %d 页"
                         % (len(slides), MAX_SLIDES, MAX_SLIDES))
            slides = slides[:MAX_SLIDES]

        for i, slide in enumerate(slides):
            state["page"] = i + 1
            blocks: list[str] = []
            try:
                head_shape = slide.shapes.title
            except Exception:                # noqa: BLE001
                head_shape = None
            head = _one_line(head_shape.text) if head_shape is not None else ""
            head = head[:TITLE_MAX * 2]
            if head:
                page_titles.append(head)
            for shape in _iter_shapes(slide.shapes):
                rows = _shape_table(shape)
                if rows:
                    state["tables"] += 1
                    tmd = table_md(rows)
                    if tmd:
                        blocks.append(tmd)
                    continue
                text = _shape_text(shape)
                if text and text != head:
                    blocks.append(text)
                    continue
                ref_path = _shape_picture(shape, put_image=put_image, state=state)
                if ref_path:
                    blocks.append("![](%s)" % ref_path)
            notes = _slide_notes(slide)
            if notes:
                blocks.append("> 备注：%s" % notes)
            pages.append((head, blocks))

        text_pages = sum(1 for _h, b in pages if b)
        # 用**占比**判"基本是图"（见 THIN_TEXT_RATIO 的说明）
        thin = bool(pages) and (text_pages / len(pages)) < THIN_TEXT_RATIO
        if thin:
            warns.append("整份演示文稿只有 %d/%d 页有可抽取的文字 —— 很可能正文都在图片里"
                         "（PPT 的图文分离是结构性限制），建议人工复核"
                         % (text_pages, len(pages)))
        if state["broken_images"]:
            warns.append("有 %d 张图片读取失败（未落 mirror/）" % state["broken_images"])
        if state["image_capped"]:
            warns.append("内嵌图片超过 %d 张，其余未抽取" % MAX_IMAGES)

        cp = getattr(prs, "core_properties", None)
        title, title_src = _title_of(cp, page_titles, ref)
        md = self._render(title, pages, total=len(slides))
        doc_time, time_src = doc_time_of(cp, meta)

        quality = 1.0
        if thin:
            quality = min(quality, 0.5)
        return FeedDocument(
            doc_id=meta["doc_id"], title=title[:200], markdown=md, kind="pptx",
            source_ref=str(ref), doc_time=doc_time, converter=self.stamp,
            quality=round(quality, 2), warnings=warns,
            meta={"slides": len(slides), "text_pages": text_pages,
                  "tables": state["tables"], "images_extracted": state["images"],
                  "title_source": title_src, "doc_time_source": time_src,
                  "chars": len(md)},
        )

    @staticmethod
    def _render(title: str, pages: list[tuple[str, list[str]]], *, total: int) -> str:
        out: list[str] = ["# %s" % title, "",
                          "> 演示文稿 · 共 %d 页" % total]
        for i, (head, blocks) in enumerate(pages):
            out.append("")
            out.append("## 第 %d 页%s" % (i + 1, ("：%s" % head) if head else ""))
            if blocks:
                out.append("")
                out.extend(blocks)
        return "\n".join(out).strip() + "\n"
