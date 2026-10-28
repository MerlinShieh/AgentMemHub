# -*- coding: utf-8 -*-
"""T2b · 公众号特化（`mp.weixin.qq.com`）。

实测结论（2026-10-12，用真实文章页）
====================================
· **难点在"访问"，不在"解析"**：同一链接不带浏览器 UA 只拿到 18 KB 桩页，
  带 UA 才拿到 3.6 MB 真页（`fetch.DEFAULT_UA` / `WECHAT_UA` 就是为此）；
  而 DOM **极其规整** —— 正文恒在 `id="js_content"`，标题在 `var msg_title`
  或 `#activity-name`，发布时间在 `#publish_time`。
· **trafilatura 对本样本效果很好**（抽出 3,305 字 + title + date），所以**不另写抽取器**；
  定向抽取只作为**兜底**（抽出来太短时）—— 这正是 §7.5「质量自评 → 升级」的模式。

⚠️ `var ct` **不是**发布时间（实测它等于抓取时刻）；真正的发布日在 `#publish_time`。
"""
from __future__ import annotations

import re
import time

from .url import MIN_ARTICLE_CHARS, build_document
from ..base import BaseConverter, FeedDocument
from .. import fetch as F

#: 认领的域名
DOMAINS = ("mp.weixin.qq.com",)

_MSG_TITLE_RE = re.compile(r"var\s+msg_title\s*=\s*['\"](.*?)['\"]")
_OG_TITLE_RE = re.compile(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\'](.*?)["\']',
                          re.I | re.S)
_ACTIVITY_RE = re.compile(r'id=["\']activity-name["\'][^>]*>(.*?)<', re.S)
_PUBLISH_RE = re.compile(r'id=["\']publish_time["\'][^>]*>(.*?)<', re.S)
_JS_CONTENT_AT = 'id="js_content"'


def slice_js_content(html: str) -> str:
    """切出 `id="js_content"` 那个 div 的**完整片段**（按 `<div>` 配对找闭合）。

    为什么不"找到第一个 `</div>` 就截断"：正文里必然有嵌套 div（图片容器、引用块），
    那样会**把正文腰斩**（实测首个 `</div>` 距起点 76,623 字符 —— 正是嵌套结构）。
    """
    i = html.find(_JS_CONTENT_AT)
    if i < 0:
        return ""
    start = html.rfind("<div", 0, i)
    if start < 0:
        return ""
    depth, j = 0, start
    while j < len(html):
        if html.startswith("<div", j):
            depth += 1
        elif html.startswith("</div>", j):
            depth -= 1
            if depth == 0:
                return html[start:j + len("</div>")]
        j += 1
    return html[start:]                     # 页面被截断：能拿多少拿多少


def _text_of(html: str, pattern: re.Pattern) -> str:
    m = pattern.search(html or "")
    if not m:
        return ""
    s = re.sub(r"<[^>]+>", "", m.group(1))
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&")
          .replace("&quot;", '"').replace("&#39;", "'")
          .replace("&lt;", "<").replace("&gt;", ">"))
    return re.sub(r"\s+", " ", s).strip()


def parse_publish_time(html: str) -> int:
    """从 `#publish_time` 取发布时间（epoch 秒）。取不到返回 0。

    为什么不信 `var ct`：实测它等于**抓取时刻**，用它会让时间衰减全错
    （与 `distill._origin_time` 的时间语义冲突）。
    """
    txt = _text_of(html, _PUBLISH_RE)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                "%Y年%m月%d日 %H:%M", "%Y年%m月%d日"):
        try:
            return int(time.mktime(time.strptime(txt, fmt)))
        except Exception:                   # noqa: BLE001 —— 换个格式再试
            continue
    return 0


class WechatConverter(BaseConverter):
    """公众号文章。**先用通用抽取，太短才定向兜底**（实测通用抽取对本样本够用）。"""

    name = "wechat"
    version = "1"

    def can_handle(self, *, ref: str, kind: str = "",
                   data: bytes | None = None) -> bool:
        if kind and kind not in ("wechat", "url", "web"):
            return False
        low = str(ref).lower()
        if not low.startswith(("http://", "https://")):
            return False
        # ⚠️ 必须比**主机名**，不能比子串：`mp.weixin.qq.com.evil.com` 含
        # "mp.weixin.qq.com"，用 `in` 判断会把仿冒域名交给特化转换器（有测试盯着）。
        try:
            from urllib.parse import urlparse
            host = (urlparse(low).netloc or "").split(":")[0]
        except Exception:                       # noqa: BLE001
            return False
        return any(host == d or host.endswith("." + d) for d in DOMAINS)

    def convert(self, *, ref: str, data: bytes, meta: dict) -> FeedDocument:
        html = data.decode("utf-8", "replace")
        # 标题：`var msg_title` → `og:title` → `#activity-name` → 抽取器的 metadata
        title = (_text_of(html, _MSG_TITLE_RE) or _text_of(html, _OG_TITLE_RE)
                 or _text_of(html, _ACTIVITY_RE))
        pub = parse_publish_time(html)      # ⚠️ 不信 `var ct`（实测更接近渲染时刻）
        doc = build_document(
            data=data, ref=ref, meta=meta, kind="wechat",
            name=self.name, version=self.version,
            converter_stamp=self.stamp,
            title_hint=title, time_hint=pub,
            prefer_js_content=True)         # 通用抽取太短时回落到 `#js_content`
        # 只有**连元数据日期都拿不到**时才告警：实测真页没有 `#publish_time` 元素
        # （只出现在 URL 编码的 JSON 里），但 trafilatura 的 metadata 能给出正确日期
        # —— 那种情况不是问题，不该报（否则警告会天天响，人就麻木了）。
        if doc.meta.get("doc_time_source") == "fallback":
            doc.warnings = list(doc.warnings) + ["未取到发布时间（元素与元数据都没有）"]
        return doc
