# -*- coding: utf-8 -*-
"""T2a · 普通网页：已抓好的 HTML → markdown（trafilatura 抽取 + 图片本地化）。

**转换器不出网**：字节由调用方（`service` 的取回层）抓好后传进来。这样
① 转换器是纯函数、可测（测试喂夹具即可）；② 出网口只有一处（`feed.fetch.fetch`），
熔断与替身都只针对它；③ 换 Playwright 渲染兜底时也不用改转换器。
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from .. import fetch as F
from ..base import BaseConverter, FeedDocument, FeedError

#: 抽取质量闸：低于它说明"这次没抽到东西"，值得升级/兜底（§7.5 的质量自评）
MIN_ARTICLE_CHARS = 200


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:                       # noqa: BLE001
        return ""


def build_document(*, data: bytes, ref: str, meta: dict, kind: str,
                   name: str, version: str, converter_stamp: str,
                   title_hint: str = "", time_hint: int = 0,
                   prefer_js_content: bool = False) -> FeedDocument:
    """HTML → `FeedDocument` 的共用实现（T2a/T2b 共用，避免两处各写一份）。

    顺序很讲究：**先本地化图片（HTML 层面）→ 再抽取 → 再存镜像**。
    图片必须在抽取**之前**改写，因为 trafilatura 的 markdown 序列化实测会丢图片
    （真页 14 张 `<img>` → markdown 0 张）；改在 HTML 层才能保证图进 `mirror/`。
    """
    headers = meta.get("headers") or {}
    warnings: list[str] = []
    original = data                        # **抓到的原始字节**（mirror 要存的就是它）

    # ① 图片：HTML 层面本地化（下载 + 把 src 换成本地相对路径）
    put_image = meta.get("put_image")
    localized = 0
    if callable(put_image):
        try:
            data, localized, img_warns = F.localize_html_images(
                data, page_url=ref, put_bytes=put_image,
                referer=(meta.get("referer") or ""))
            warnings.extend(img_warns)
        except Exception as e:              # noqa: BLE001 —— 图片处理失败不该毁掉整篇
            warnings.append("图片本地化失败：%s" % str(e)[:100])

    # ② 抽取正文（公众号：通用抽取太短时回落到定向 `#js_content`）
    md, m = F.extract_article(data, ref)
    engine = "trafilatura"
    if prefer_js_content and len(md) < MIN_ARTICLE_CHARS:
        from .wechat import slice_js_content
        frag = slice_js_content(data.decode("utf-8", "replace"))
        if frag:
            md2, m2 = F.extract_article(frag.encode("utf-8"), ref)
            if len(md2) > len(md):
                md, engine = md2, "js_content"
                m = {**m, **{k: v for k, v in m2.items() if v}}

    # 反爬/异常页：**不硬闯**，如实报错并指向兜底通路（另存 HTML / --stdin）
    why = F.looks_like_verify_page(data)
    if why and len(md) < MIN_ARTICLE_CHARS:
        raise FeedError("这不是正文页（命中「%s」）—— 请用兜底通路："
                        "浏览器另存为 HTML 后 feed add <文件>，或 feed add --stdin 粘贴正文"
                        % why, kind="fetch", retryable=True)
    if not md.strip():
        if F.is_probably_js_rendered(data, md):
            raise FeedError("正文疑似由 JS 渲染（静态 HTML 里没有内容）—— "
                            "装 Playwright 并置 feed.fetch.renderer=playwright 可自动处理，"
                            "否则请另存为 HTML 或 --stdin 粘贴",
                            kind="fetch", retryable=True)
        raise FeedError("抽取结果为空（页面可能不是文章页）",
                        kind="convert", retryable=True)

    # ③ 第 2 段物料：**落地页源码**（用户点名的"下载的源码或网页镜像"）。
    #    存的是**抓到的原始字节**，不是改写版 —— 镜像的语义是"当时下载到了什么"；
    #    改写版（图片已本地化）另存一份，便于复现与排查。
    put_mirror = meta.get("put_mirror")
    if callable(put_mirror):
        try:
            put_mirror("index.html", original)
            if localized:
                put_mirror("localized.html", data)
        except Exception as e:              # noqa: BLE001 —— 镜像写失败不该毁掉转译
            warnings.append("镜像保存失败：%s" % str(e)[:80])

    # 抽取器把图丢了就**如实说**（质量自评要可见，不能静默失真）
    if localized and "![" not in md:
        warnings.append("抽取器未保留正文图片位置（已本地化 %d 张，见 mirror/）"
                        % localized)

    title = (title_hint or str(m.get("title") or "").strip()
             or (Path(urlparse(ref).path).name or _domain(ref) or ref))
    doc_time, t_src = F.doc_time_with_source(m, headers,
                                             fallback=meta.get("now") or 0)
    if time_hint:
        doc_time, t_src = time_hint, "publish_time"

    # 质量自评：**抽出的正文占"可见文本"的比例**（不是占原始 HTML 字节）。
    # 实证：公众号真页 3.6 MB 里可见文本只有 4,320 字 —— 拿原始字节当分母会把
    # "抽得完美"判成 0.1%（2026-10-12 真机验收踩到并修正）。
    visible = F.visible_text_len(original) or len(md)
    ratio = len(md) / max(1, visible)
    quality = 1.0 if ratio >= 0.5 else max(0.3, min(1.0, ratio / 0.5))
    if any("图片下载失败" in w for w in warnings):
        quality = min(quality, 0.7)
    return FeedDocument(
        doc_id=meta["doc_id"], title=title[:200], markdown=md, kind=kind,
        source_ref=ref, doc_time=int(doc_time or 0),
        converter=converter_stamp, quality=round(quality, 2),
        warnings=warnings,
        meta={"mirror_html": "index.html", "images_localized": localized,
              "text_ratio": round(ratio, 4), "site": _domain(ref),
              "extract_engine": engine, "doc_time_source": t_src,
              "visible_chars": visible,
              "author": str(m.get("author") or ""),
              "sitename": str(m.get("sitename") or "")},
    )


class UrlConverter(BaseConverter):
    """普通网页（T2a）。"""

    name = "url"
    version = "1"
    #: 认领的 kind（`service` 对 URL 会显式传 kind="url"）
    KINDS = ("url", "web", "http", "https")

    def can_handle(self, *, ref: str, kind: str = "",
                   data: bytes | None = None) -> bool:
        if kind and kind not in self.KINDS:
            return False
        return str(ref).lower().startswith(("http://", "https://"))

    def convert(self, *, ref: str, data: bytes, meta: dict) -> FeedDocument:
        return build_document(data=data, ref=ref, meta=meta, kind="url",
                              name=self.name, version=self.version,
                              converter_stamp=self.stamp)
