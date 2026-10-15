# -*- coding: utf-8 -*-
"""XMIND · 思维导图：把**层级结构**还原成 markdown 嵌套列表。

为什么它值得优先做（而不是"以后再说"）
====================================
思维导图是**结构化知识的原生形态** —— 分支、决策、取舍关系天生带层级，
这恰恰是纯文本最难表达的。用户那 14 份业务导图里最大的一份有
**20 个画布 / 2861 个节点**，这种密度用普通文档根本写不出来。
而格式又极其友好：`.xmind` 就是 ZIP，新版内含 `content.json`（一棵 `rootTopic` 递归树），
**零依赖可解**。

⚠️ 两个形态都要认，且**顺序不能反**
================================
· **新版（XMind 2020+）**：`content.json` —— 结构清晰，**优先用它**；
· **旧版（XMind 8）**：`content.xml`（命名空间 `urn:xmind:xmap:xmlns:content:2.0`）。

实测（2026-10-15，用户 14 份导图）：**每个文件里两个都有**，但 `content.xml`
**全都是同一份 4,309 字节的空壳**（旧版兼容留的占位），真数据在 `content.json`。
所以顺序反了会解析出一份"看起来成功、实际空"的导图 —— 又一个静默失真。

能力边界（明说）
==============
· 只取**文字层级**：XMind 的配色/图标/关系线（`relationships`）/边界（`boundaries`）
  不进 markdown —— 它们是视觉强调，不是语义；
· 备注（`notes`）**压成行内**：markdown 的嵌套列表里塞多行会打断层级；
· 主题的**相对位置**（左/右分支）不保留：思维导图的分支方向是布局，不是语义。
"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any

from ..base import BaseConverter, FeedDocument, FeedError
from ..titles import clip_title, is_junk_title, looks_like_title

#: 认领的扩展名
EXTS = (".xmind",)
#: 单份导图的节点上限（最大实测 2861，留足余量；防极端文件把投喂拖死）
MAX_TOPICS = 20000
#: 备注压成行内时的截断长度
MAX_NOTE = 200
#: 单个主题标题的截断长度
MAX_TITLE = 200
#: 低于这个节点数就认为"这份导图基本是空的"
THIN_TOPICS = 3
#: XMind 旧版 XML 的命名空间
_NS = "{urn:xmind:xmap:xmlns:content:2.0}"


def _one_line(text: Any, limit: int = MAX_TITLE) -> str:
    """多行压成一行 —— markdown 的嵌套列表**不能**被换行打断。"""
    s = " ".join(str(text or "").split())
    return s[:limit] + ("…" if len(s) > limit else "")


def _topic_extra(topic: dict) -> str:
    """主题上的附加信息：标签 / 备注 / 超链接（都压成行内）。"""
    bits: list[str] = []
    labels = topic.get("labels") or []
    if isinstance(labels, list) and labels:
        bits.append("`%s`" % "、".join(_one_line(x, 20) for x in labels[:5]))
    notes = ((topic.get("notes") or {}).get("plain") or {})
    content = notes.get("content")
    if content:
        bits.append("（备注：%s）" % _one_line(content, MAX_NOTE))
    href = topic.get("href")
    if href:
        bits.append("<%s>" % _one_line(href, 120))
    return (" " + " ".join(bits)) if bits else ""


def _walk_topic(topic: dict, depth: int, out: list[str], budget: list[int]) -> None:
    """递归展开一棵主题树（`budget` 是剩余节点配额，用列表做可变计数）。"""
    if not isinstance(topic, dict) or budget[0] <= 0:
        return
    budget[0] -= 1
    title = _one_line(topic.get("title"))
    if title:
        out.append("%s- %s%s" % ("  " * min(depth, 20), title, _topic_extra(topic)))
    children = topic.get("children") or {}
    for key in ("attached", "detached", "summary"):
        for child in (children.get(key) or []):
            _walk_topic(child, depth + 1, out, budget)


def _walk_xml_topic(elem, depth: int, out: list[str], budget: list[int]) -> None:
    """旧版 `content.xml` 的递归（结构同 JSON，只是换了表达）。"""
    if budget[0] <= 0:
        return
    budget[0] -= 1
    title_el = elem.find(_NS + "title")
    title = _one_line(title_el.text if title_el is not None else "")
    if title:
        out.append("%s- %s" % ("  " * min(depth, 20), title))
    children = elem.find(_NS + "children")
    if children is None:
        return
    for topics in children.findall(_NS + "topics"):
        for child in topics.findall(_NS + "topic"):
            _walk_xml_topic(child, depth + 1, out, budget)


def _sheets_from_json(data: bytes) -> tuple[list[tuple[str, list[str]]], int]:
    """解析 `content.json` → `([(画布标题, 行列表)], 节点数)`。"""
    try:
        parsed = json.loads(data.decode("utf-8", "replace"))
    except Exception as e:                   # noqa: BLE001
        raise FeedError("content.json 无法解析：%s" % str(e)[:100],
                        kind="convert", retryable=False)
    if isinstance(parsed, dict):             # 容错：单画布对象也认
        parsed = [parsed]
    if not isinstance(parsed, list):
        raise FeedError("content.json 结构不是画布数组", kind="convert",
                        retryable=False)
    sheets: list[tuple[str, list[str]]] = []
    total = 0
    budget = [MAX_TOPICS - total]
    for i, sheet in enumerate(parsed):
        if not isinstance(sheet, dict):
            continue
        root = sheet.get("rootTopic") or {}
        root_title = _one_line(root.get("title"))
        sheet_title = _one_line(sheet.get("title"))
        # XMind 的默认画布名（"思维导图"/"Sheet 1"）没有信息量 ⇒ 回落到中心主题
        if not sheet_title or is_junk_title(sheet_title):
            sheet_title = root_title
        name = sheet_title or root_title or "画布 %d" % (i + 1)
        lines: list[str] = []
        before = budget[0]
        _walk_topic(root, 0, lines, budget)
        total += before - budget[0]
        if lines:
            sheets.append((name, lines))
    return sheets, total


def _sheets_from_xml(data: bytes) -> tuple[list[tuple[str, list[str]]], int]:
    """解析旧版 `content.xml`（XMind 8）。"""
    from xml.etree import ElementTree as ET

    try:
        root = ET.fromstring(data)
    except Exception as e:                   # noqa: BLE001
        raise FeedError("content.xml 无法解析：%s" % str(e)[:100],
                        kind="convert", retryable=False)
    sheets: list[tuple[str, list[str]]] = []
    total = 0
    budget = [MAX_TOPICS]
    for i, sheet in enumerate(root.findall(_NS + "sheet")):
        topic = sheet.find(_NS + "topic")
        if topic is None:
            continue
        name_el = sheet.find(_NS + "title")
        root_title = topic.find(_NS + "title")
        name = (_one_line(name_el.text if name_el is not None else "")
                or _one_line(root_title.text if root_title is not None else "")
                or "画布 %d" % (i + 1))
        lines: list[str] = []
        before = budget[0]
        _walk_xml_topic(topic, 0, lines, budget)
        total += before - budget[0]
        if lines:
            sheets.append((name, lines))
    return sheets, total


def _title_of(sheets: list[tuple[str, list[str]]], ref: str) -> tuple[str, str]:
    """标题：第一张画布的中心主题 → 画布名 → 文件名。

    为什么用**中心主题**：思维导图的根节点就是这份图的名字，
    而且比文件名可靠（用户常把导图存成「副本」「未命名」这类名字）。
    """
    if sheets and sheets[0][1]:
        first = sheets[0][1][0].lstrip("- ").split("（备注")[0].strip()
        if looks_like_title(first):
            t, _clipped = clip_title(first)
            return t, "root_topic"
    stem = Path(str(ref)).stem
    if not is_junk_title(stem):
        return stem, "stem"
    return "思维导图", "fallback"


class XmindConverter(BaseConverter):
    """`.xmind` 思维导图（零依赖：ZIP + JSON / XML）。"""

    name = "xmind"
    version = "1"

    def can_handle(self, *, ref: str, kind: str = "",
                   data: bytes | None = None) -> bool:
        if kind == "xmind":
            return True
        if kind and kind not in ("url",):
            return False
        if Path(str(ref)).suffix.lower() in EXTS:
            return True
        # 没有扩展名：ZIP 里含 `content.json`/`content.xml` 才算（`.docx`/`.pptx` 也是 ZIP）
        if data and bytes(data[:4]) == b"PK\x03\x04":
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as z:
                    names = set(z.namelist())
                return "content.json" in names or "content.xml" in names
            except Exception:                # noqa: BLE001
                return False
        return False

    def convert(self, *, ref: str, data: bytes, meta: dict) -> FeedDocument:
        warns: list[str] = []
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except Exception as e:               # noqa: BLE001
            raise FeedError("不是有效的 xmind（ZIP 打不开）：%s" % str(e)[:100],
                            kind="convert", retryable=False)
        with zf:
            names = set(zf.namelist())
            sheets: list[tuple[str, list[str]]] = []
            total = 0
            used = ""
            # ⚠️ 顺序硬约束：**先 json 再 xml**。实测用户 14 份导图里 content.xml
            #    全都是同一份 4,309 字节的空壳，先读它只会得到一份空导图。
            if "content.json" in names:
                try:
                    sheets, total = _sheets_from_json(zf.read("content.json"))
                    used = "content.json"
                except FeedError as e:
                    warns.append("content.json 解析失败：%s" % str(e)[:80])
            if not sheets and "content.xml" in names:
                try:
                    sheets, total = _sheets_from_xml(zf.read("content.xml"))
                    used = "content.xml"
                except FeedError as e:
                    warns.append("content.xml 解析失败：%s" % str(e)[:80])

        if not sheets:
            raise FeedError("这份 xmind 里没解析出任何主题（%s）—— "
                            "可能是空导图，或用了尚未支持的内部结构"
                            % (used or "没有 content.json/content.xml"),
                            kind="empty", retryable=False)

        title, title_src = _title_of(sheets, ref)
        parts: list[str] = ["# %s" % title, "",
                            "> 思维导图 · %d 个画布 · %d 个主题 · 取自 `%s`"
                            % (len(sheets), total, used)]
        # 单画布不加 `## 画布名` 小节：那份导图的名字就是文档标题，再加一层纯属重复
        # （与 PDF"单页不加页码"同一条道理）。多画布才需要分节。
        multi = len(sheets) > 1
        for name, lines in sheets:
            if multi:
                parts.append("")
                parts.append("## %s" % name)
            parts.append("")
            parts.extend(lines)
        md = "\n".join(parts).strip() + "\n"

        quality = 1.0
        if total <= THIN_TOPICS:
            warns.append("整份导图只有 %d 个主题，内容很少（可能是空壳导图）" % total)
            quality = 0.5
        if len(sheets) > 1:
            warns.append("含 %d 个画布：已按画布分节，跨画布的关系线未保留"
                         % len(sheets))

        return FeedDocument(
            doc_id=meta["doc_id"], title=title[:200], markdown=md, kind="xmind",
            source_ref=str(ref),
            doc_time=int(meta.get("mtime") or 0) or int(meta.get("now") or 0),
            converter=self.stamp, quality=round(quality, 2), warnings=warns,
            meta={"sheets": len(sheets), "topics": total, "source": used,
                  "title_source": title_src, "chars": len(md)},
        )
