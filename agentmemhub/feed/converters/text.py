# -*- coding: utf-8 -*-
"""T1 · 一等公民：md / txt 纯文本直通（**零依赖、无损**）。

为什么它是第一档
================
纯文本**不需要解析**：没有版式、没有反爬、没有 OCR —— 拿到字节就能用。
所以它同时是"全链路的验证载体"：M1 用它端到端跑通（登记 → raw → kb），
把链路问题与解析问题分开暴露。

它也是**质量基准**：`quality=1.0`（无损直通）。后面 T2/T3/T4 的质量自评都以它为参照。
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from ..base import BaseConverter, FeedDocument

#: 认领的扩展名
EXTS = (".md", ".markdown", ".txt", ".text")

#: 依次尝试的编码。**GB18030 兜底中文**：Windows 上的老 txt 大量是 GBK，
#: 而 GB18030 是 GBK 的超集（能解 GBK，也能解更新的国标），一个就够。
_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030")

_TITLE_RE = re.compile(r"^title:\s*(.+)$", re.M)
_H1_RE = re.compile(r"^#\s+(.+)$", re.M)
_DATE_RE = re.compile(r"^date:\s*([0-9]{4}-[0-9]{2}-[0-9]{2})", re.M)
#: 围栏代码块（``` … ```）
_FENCE_RE = re.compile(r"^```.*?^```", re.M | re.S)


def _strip_fences(text: str) -> str:
    """去掉围栏代码块。

    为什么标题提取必须排除它：代码块里的 `# xxx` 是**代码注释**（Python / shell），
    不是 markdown 标题。2026-10-12 实测：一份含 Python 片段的测试笔记，
    标题被取成了首个 `# 打开百度首页` 注释（`_H1_RE` 直接匹配到了块内）。
    去掉围栏后该文件没有真 H1 ⇒ 正确回落到文件名。
    """
    return _FENCE_RE.sub("", text or "")


def _decode(data: bytes) -> tuple[str, str, list[str]]:
    """按候选编码解码。返回 `(文本, 实际编码, 告警)`。

    为什么不用 `chardet` 之类：多一个依赖，而这里只有三种现实可能
    （UTF-8 / UTF-8-BOM / 国标）。**解不出来也不失败** —— 用 replace 兜底并告警，
    因为"有一两个字是问号"远好过"整份资料进不来"（质量自评会把它标出来）。
    """
    for enc in _ENCODINGS:
        try:
            return data.decode(enc), enc, []
        except UnicodeDecodeError:
            continue
    return (data.decode("utf-8", "replace"), "utf-8/replace",
            ["编码无法确定，已按 utf-8 尽力解码（可能有替换字符）"])


def _split_front(text: str) -> tuple[str, str]:
    """切出 `(frontmatter, 正文)`。没有 frontmatter 时前者为空串。"""
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            return parts[1], parts[2]
    return "", text


def _title_of(front: str, body: str, stem: str) -> str:
    m = _TITLE_RE.search(front or "")
    if m and m.group(1).strip():
        return m.group(1).strip().strip('"\'')
    # H1 只在**围栏代码块之外**找（块内的 `#` 是代码注释，见 `_strip_fences`）
    m = _H1_RE.search(_strip_fences(body))
    if m and m.group(1).strip():
        return m.group(1).strip()
    return stem


def _doc_time_of(front: str, body: str, meta: dict) -> int:
    """**文档自身时间**优先，其次文件 mtime，最后才用当前时刻。

    为什么严格按这个顺序：召回侧的时间衰减按"知识产生时间"算
    （与 `distill._origin_time` 同一语义）—— 用投喂时刻会让旧资料显得"刚产生"。
    """
    m = _DATE_RE.search(front or "")
    if m:
        try:
            return int(time.mktime(time.strptime(m.group(1), "%Y-%m-%d")))
        except Exception:                   # noqa: BLE001 —— 坏日期就往下走
            pass
    mt = meta.get("mtime")
    if mt:
        return int(mt)
    return int(time.time())


class TextConverter(BaseConverter):
    """md / txt 直通。**不加任何加工** —— 转换与加工是两件事（§7.0.1）。"""

    name = "text"
    version = "1"
    #: 认领的后缀（**类属性**，供格式白名单从注册表收集 —— 见 `feed/formats.py`）
    EXTS = EXTS

    def can_handle(self, *, ref: str, kind: str = "",
                   data: bytes | None = None) -> bool:
        if kind and kind != "text":
            return False
        return Path(str(ref)).suffix.lower() in EXTS

    def convert(self, *, ref: str, data: bytes, meta: dict) -> FeedDocument:
        stem = Path(str(ref)).stem
        text, enc, warns = _decode(data)
        front, body = _split_front(text)
        is_md = Path(str(ref)).suffix.lower() in (".md", ".markdown")
        title = _title_of(front, body, stem)
        if is_md:
            md = text                                   # md 原样保留（含 frontmatter）
        else:
            # txt 只能"包一层最小结构"：给它一个标题，正文不动
            md = "# %s\n\n%s" % (title, body.strip())
        return FeedDocument(
            doc_id=meta["doc_id"],
            title=title,
            markdown=md,
            kind="text",
            source_ref=str(ref),
            doc_time=_doc_time_of(front, body, meta),
            converter=self.stamp,
            quality=1.0,                                # 无损直通
            warnings=warns,
            meta={"encoding": enc, "chars": len(md),
                  "frontmatter": bool(front)},
        )
