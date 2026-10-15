# -*- coding: utf-8 -*-
"""T2a · 普通网页：已抓好的 HTML → markdown（trafilatura 抽取 + 图片本地化）。

**转换器不出网**：字节由调用方（`service` 的取回层）抓好后传进来。这样
① 转换器是纯函数、可测（测试喂夹具即可）；② 出网口只有一处（`feed.fetch.fetch`），
熔断与替身都只针对它；③ 换 Playwright 渲染兜底时也不用改转换器。
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

from .. import embeddata as ED
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
    #    `base_dir` 非空 = 这是一份**本地另存**的 HTML：相对路径的图按文件系统读
    put_image = meta.get("put_image")
    localized = 0
    if callable(put_image):
        try:
            data, localized, img_warns = F.localize_html_images(
                data, page_url=ref, put_bytes=put_image,
                referer=(meta.get("referer") or ""),
                base_dir=meta.get("base_dir") or None)
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
             or Path(str(ref) if _is_local(ref) else urlparse(ref).path).name
             or _domain(ref) or str(ref))
    # 本地另存的网页没有 HTTP 头，**文件 mtime 比"投喂时刻"更接近文档时间**
    doc_time, t_src = F.doc_time_with_source(
        m, headers, fallback=(meta.get("mtime") if _is_local(ref)
                              else meta.get("now")) or 0)
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

    # ④ **空壳抢救**：正文只有字段标签（数据由 JS 渲染）时，去文件里把内嵌 JSON 取回来。
    #    真实素材：60 份"脚本录制工具"报告 —— 157 KB 的 HTML、可见文本 136 字，
    #    数据是 `var resultData = {…}`（27 KB）。**它在文件里，不需要开浏览器渲染**。
    #    没有这一步的话，`ratio` 会因为"抽取覆盖了全部可见文本"而给出 1.00，
    #    知识库里就躺着一堆看起来正常、实际是空壳的页面。
    rescued, rescue_warns = ED.rescue(original, md, label=title)
    if rescued:
        md = rescued                          # 空壳正文被"骨架 + 还原数据"替换掉
        warnings.extend(rescue_warns)
        quality = min(quality, 0.9)           # 数据取回来了，但结构是还原的
    elif rescue_warns:
        warnings.extend(rescue_warns)
        quality = min(quality, 0.4)           # 空壳且没救回来 ⇒ 重降，逼出人工复核

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


#: 认领的**本地另存网页**扩展名。M3 的文档里承诺过这条兜底通路
#: （"浏览器另存为 HTML 后 `feed add <文件>`"），但 `can_handle` 当时只认
#: `http(s)://` —— 承诺的那条路**根本走不通**（2026-10-15 扫用户文档目录时发现：
#: 61 个本地 .html 一个都进不来）。
LOCAL_EXTS = (".html", ".htm", ".xhtml")


def _is_local(ref: str) -> bool:
    return not str(ref).lower().startswith(("http://", "https://"))


class UrlConverter(BaseConverter):
    """普通网页（T2a）—— **同时也收本地另存的网页**。

    两者的字节形态完全一样（都是 HTML），差别只在"图片相对路径怎么解析"
    （URL join vs 文件系统目录）与"文档时间取哪来"（HTTP 头 vs 文件 mtime）。
    所以共用一套抽取逻辑，只把这两处分叉（`meta.base_dir` / `meta.mtime`）。
    """

    name = "url"
    version = "1"
    #: 认领的 kind（`service` 对 URL 会显式传 kind="url"）
    KINDS = ("url", "web", "http", "https", "html")

    def can_handle(self, *, ref: str, kind: str = "",
                   data: bytes | None = None) -> bool:
        if kind and kind not in self.KINDS:
            return False
        if not _is_local(ref):
            return True
        # 本地文件：只认网页扩展名（`.txt`/`.md` 归 T1，别抢）
        if Path(str(ref)).suffix.lower() in LOCAL_EXTS:
            return True
        # 没有扩展名但字节是 HTML（工具导出的报告常这样）
        return bool(data) and re.match(rb"\s*<(?:!doctype\s+html|html)\b",
                                       bytes(data[:64]), re.I) is not None

    def convert(self, *, ref: str, data: bytes, meta: dict) -> FeedDocument:
        # 本地另存网页给一个更准确的 kind —— 它**不是** URL
        local = bool(meta.get("base_dir")) or _is_local(ref)
        return build_document(data=data, ref=ref, meta=meta,
                              kind="html" if local else "url",
                              name=self.name, version=self.version,
                              converter_stamp=self.stamp)
