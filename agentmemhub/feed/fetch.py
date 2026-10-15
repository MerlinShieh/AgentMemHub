# -*- coding: utf-8 -*-
"""取回与抽取：HTTP 抓取（浏览器 UA + 代理 + 超时）→ 正文抽取 → 图片本地化。

为什么这些必须在**脚本**里做，而不是把原件交给 LLM（§7.0.1）
==========================================================
2026-10-12 用真实样本实测：**公众号文章页面 3.6 MB，去掉标签后正文只有 4,320 字**
（信噪比 **0.1%**）。把原件喂模型 ≈ 90 万 token 换 4 KB 内容，而且模型还要自己在
噪声里找正文。脚本先抽成纯文本再交给下游，便宜两三个数量级、且**确定性**。

UA 是硬需求（实测）
==================
同一个公众号链接：不带浏览器 UA 只拿到 **18 KB 的桩页**，带 UA 才拿到 **3.6 MB 真页**。
所以默认 UA 必须是浏览器形态 —— 这不是"伪装"，是**拿到作者意图公开的那份文档**。

代理
====
项目惯例是 `HTTP_PROXY=http://127.0.0.1:7897`；`httpx` 默认读环境变量（`trust_env`），
所以调用方只要把环境变量设好即可，不必在代码里硬编码代理地址。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urljoin, urlparse

from .base import FeedError

#: 浏览器形态 UA —— 见模块 docstring（无它拿不到公众号真页）
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

#: 微信内置浏览器 UA（公众号的"正常访客"形态）
WECHAT_UA = ("Mozilla/5.0 (Linux; Android 13; Pixel 7 Build/TQ3A.230805.001) "
             "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
             "Chrome/116.0.0.0 Mobile Safari/537.36 "
             "MicroMessenger/8.0.40.2420(0x28002837) WeChat/arm64")

#: markdown / HTML 里的图片引用
_IMG_RE = re.compile(r"!\[([^\]]*)\]\((https?://[^)\s]+)\)")
#: ⚠️ **不能限定 `https?://`**：相对路径的图片（`/static/a.png`、`assets/a.png`）极常见。
#: 早先那版只匹配绝对 URL ⇒ 相对路径的图**从来没被本地化过** ——
#: 公众号恰好全用绝对 URL 才没暴露，普通网站与本地另存的网页则一张都留不下
#: （2026-10-15 用本地 HTML 测试时发现）。改成匹配任意 src，由下游决定
#: "按 URL 取" 还是 "按文件系统读"。
_HTML_IMG_RE = re.compile(
    rb"<img[^>]+?(?:data-src|src)\s*=\s*[\"']([^\"']+)[\"']", re.I)
#: 不需要（也无法）取回的协议 —— `data:` 是内嵌 base64，本来就在文件里
_SKIP_SCHEMES = ("data:", "javascript:", "mailto:", "about:", "tel:")


@dataclass
class Fetched:
    """一次取回的结果。**原始字节必须留着** —— 它是落 `raw/` 的第 1 段物料。"""

    url: str                       # 最终 URL（跟随重定向之后）
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


def fetch(url: str, *, timeout: float = 20.0, ua: str = DEFAULT_UA,
          referer: str = "", extra_headers: Optional[dict] = None,
          max_bytes: int = 8 * 1024 * 1024) -> Fetched:
    """抓一个 URL。失败抛 `FeedError(kind="fetch")`（可重试）。

    `max_bytes` 是**防呆上限**：正常文章远小于它，而"抓到一个无限流"会拖死整批
    （实测那份公众号真页 3.6 MB，8 MB 上限留足余量）。
    """
    # **测试沙箱熔断**：这是本模块唯一的出网口，测试必须替身化
    from agentmemhub.logs import SANDBOX_ENV
    if os.environ.get(SANDBOX_ENV):
        raise FeedError("测试沙箱禁止出网（%s 已置）—— 请 mock `feed.fetch.fetch`"
                        % SANDBOX_ENV, kind="fetch", retryable=False)

    import httpx

    headers = {"User-Agent": ua,
               "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
               "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    if referer:
        headers["Referer"] = referer
    headers.update(extra_headers or {})
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True,
                          trust_env=True) as c:
            with c.stream("GET", url, headers=headers) as r:
                chunks: list[bytes] = []
                got = 0
                for chunk in r.iter_bytes():
                    chunks.append(chunk)
                    got += len(chunk)
                    if got > max_bytes:
                        raise FeedError("响应超过 %d MB 上限，已中止" % (max_bytes // 1048576),
                                        kind="fetch", retryable=False)
                return Fetched(url=str(r.url), status=int(r.status_code),
                               body=b"".join(chunks),
                               headers={k.lower(): v for k, v in r.headers.items()})
    except FeedError:
        raise
    except Exception as e:                  # noqa: BLE001 —— 网络层任何异常都算取回失败
        raise FeedError("取回失败：%s: %s" % (type(e).__name__, str(e)[:200]),
                        kind="fetch", retryable=True)


def extract_article(html: bytes, url: str = "") -> tuple[str, dict[str, Any]]:
    """正文抽取（trafilatura，输出 **markdown**）。返回 `(markdown, metadata)`。

    为什么用 markdown 而不是纯文本：正文里的**标题层级、列表、表格、代码块**是知识结构，
    纯文本会把它们全抹平（而我们的交付物本身就是 markdown）。

    抽不出来时返回空串（由调用方按质量自评决定"降级/兜底"，不在这里抛）。
    """
    try:
        import trafilatura
    except ImportError as e:                # pragma: no cover —— 依赖缺失要说清楚
        raise FeedError("缺少 trafilatura（正文抽取依赖）：%s" % e,
                        kind="convert", retryable=False)
    md = trafilatura.extract(html, url=url or None, output_format="markdown",
                             include_links=True, include_images=True,
                             include_tables=True, include_comments=False,
                             favor_recall=True) or ""
    meta: dict[str, Any] = {}
    try:
        m = trafilatura.extract_metadata(html, default_url=url or None)
        if m is not None:
            for k in ("title", "author", "date", "sitename", "url", "description"):
                v = getattr(m, k, None)
                if v:
                    meta[k] = v
    except Exception:                       # noqa: BLE001 —— 元数据抽不到不影响正文
        pass
    return md.strip(), meta


def visible_text_len(html: bytes) -> int:
    """页面**可见文本**的字数（去 script/style/标签）。

    这是衡量抽取质量的正确分母 —— 实证：公众号真页 3.6 MB 里可见文本只有 4,320 字，
    若拿"抽出的正文 / 原始 HTML 字节"算，**抽得完美也只有 0.1%**，会把好结果判成低质
    （2026-10-12 真机验收踩到）。
    """
    s = html.decode("utf-8", "replace")
    s = re.sub(r"<script.*?</script>|<style.*?</style>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<!--.*?-->", " ", s, flags=re.S)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"&nbsp;|&amp;|&quot;|&#39;|&lt;|&gt;", " ", s)
    return len(re.sub(r"\s+", " ", s).strip())


def doc_time_with_source(meta: dict, headers: dict, *, fallback: float
                         ) -> tuple[int, str]:
    """同 `doc_time_of`，但**连来源一起返回**（`metadata` / `headers` / `fallback`）。

    为什么要来源：调用方要据此决定"要不要告警缺发布时间"。公众号真页里
    **没有** `#publish_time` 元素（实测：它只出现在 URL 编码的 JSON 里），
    但 trafilatura 的 metadata **能给出正确日期** —— 那种情况不该告警。
    """
    import time
    d = str(meta.get("date") or "").strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return int(time.mktime(time.strptime(d[:len(fmt) + 2], fmt))), "metadata"
        except Exception:                   # noqa: BLE001 —— 换个格式再试
            continue
    lm = str((headers or {}).get("last-modified") or "")
    if lm:
        try:
            from email.utils import parsedate_to_datetime
            return int(parsedate_to_datetime(lm).timestamp()), "headers"
        except Exception:                   # noqa: BLE001
            pass
    return int(fallback), "fallback"


def doc_time_of(meta: dict, headers: dict, *, fallback: float) -> int:
    """文档自身时间：元数据日期 > HTTP `Last-Modified` > 兜底（抓取时刻）。

    严格按这个顺序的理由与 `distill._origin_time` 一致：召回的时间衰减按
    "知识产生时间"算，用抓取时刻会让旧文章显得"刚产生"。
    """
    return doc_time_with_source(meta, headers, fallback=fallback)[0]


def _local_image_path(src: str, base_dir) -> Optional[str]:
    """`src` 是"相对于本地 HTML 文件"的图片路径吗？是就返回它的实际路径。

    为什么要单独判：**本地另存的网页**（浏览器"另存为"、测试工具导出的报告）里，
    图片引用是**文件系统相对路径**（`xxx_files/1.png` 或 `../img/a.png`），
    拿 `urljoin` 去解析只会得到一堆打不开的 URL。
    """
    if not base_dir:
        return None
    s = (src or "").strip()
    if not s or s.startswith(("http://", "https://", "//", "data:", "mailto:",
                              "javascript:", "#")):
        return None
    try:
        p = Path(unquote(urlparse(s).path))
    except Exception:                        # noqa: BLE001
        return None
    if p.is_absolute():
        return str(p) if p.is_file() else None
    cand = Path(base_dir) / p
    return str(cand) if cand.is_file() else None


def localize_html_images(html: bytes, *, page_url: str, put_bytes,
                         referer: str = "", timeout: float = 20.0,
                         base_dir=None
                         ) -> tuple[bytes, int, list[str]]:
    """**在 HTML 层面**把外链图片换成本地引用（下载进 `mirror/`），再交给抽取器。

    为什么改在抽取**之前**：trafilatura 的 markdown 序列化**实测会丢图片**
    （真实公众号页 14 张 `<img>` → markdown **0 张**）。改在 HTML 层之后：
      · 图片**必定落进 `mirror/`**（三段保留里的第 2 段，知识库要能长期读）；
      · 抽取器若输出图片，引用**已经是本地相对路径** —— 不再依赖图床、不再受防盗链影响。

    `base_dir` 非空表示"这是一份**本地另存**的 HTML"：此时相对路径的图
    按**文件系统**读（见 `_local_image_path`），不走网络 —— 那些网页往往
    早就打不开了，但图片还躺在同级目录里。

    返回 `(新 HTML, 本地化张数, 警告)`。单张失败只记警告（一张图挂了不该让整篇进不来）。
    """
    urls: list[str] = []
    for m in _HTML_IMG_RE.finditer(html):
        u = m.group(1).decode("utf-8", "replace")
        if u not in urls:
            urls.append(u)
    if not urls:
        return html, 0, []
    mapping: dict[str, str] = {}
    warns: list[str] = []
    for u in urls:
        if u.lower().startswith(_SKIP_SCHEMES) or u.startswith("#"):
            continue                         # 内嵌图/锚点：本来就取不到，也不算失败
        local = _local_image_path(u, base_dir)
        if local is not None:
            try:
                mapping[u] = put_bytes(u, Path(local).read_bytes())
            except Exception as e:           # noqa: BLE001 —— 单张失败不阻断整篇
                warns.append("本地图片读取失败：%s（%s）" % (u[:50], str(e)[:60]))
            continue
        try:
            f = fetch(urljoin(page_url, u), timeout=timeout,
                      referer=referer or page_url)
            if f.status == 200 and f.body:
                mapping[u] = put_bytes(u, f.body)
            else:
                warns.append("图片下载失败（HTTP %s）：%s" % (f.status, u[:70]))
        except Exception as e:              # noqa: BLE001 —— 单张失败不阻断整篇
            warns.append("图片下载失败：%s（%s）" % (u[:50], str(e)[:60]))
    out = html
    for u, rel in mapping.items():
        out = out.replace(u.encode("utf-8"), rel.encode("utf-8"))
    return out, len(mapping), warns


def looks_like_verify_page(html: bytes) -> str:
    """识别"这不是文章，是反爬/异常页"。返回原因（空串 = 正常）。

    实测知识：非浏览器 UA 时公众号返回 18 KB 桩页；某些文章直接给"环境异常/去验证"。
    **检测到就不硬闯** —— 交给兜底通路（另存 HTML / `--stdin` 粘贴），
    因为反爬是军备竞赛，硬闯只会让整条链路变得不可预测（计划 §7.6）。
    """
    s = html[:200000].decode("utf-8", "replace")
    for mark in ("环境异常", "去验证", "请在微信客户端打开", "参数错误",
                 "该内容已被发布者删除", "此内容因违规无法查看",
                 "此内容发送失败无法查看", "你的网络环境存在风险"):
        if mark in s:
            return mark
    # 桩页特征：有验证脚本且几乎没有正文
    if len(html) < 40000 and "<title></title>" in s and "js_content" not in s:
        return "疑似反爬桩页（体积小、无正文容器、标题为空）"
    return ""


def is_probably_js_rendered(html: bytes, extracted: str) -> bool:
    """SPA 判据（**客观**）：抽出的正文相对 HTML 太小、且命中 SPA 标记。

    命中后的处置由 `feed.fetch.renderer` 决定（`none` = 失败 + 指引，
    `playwright` = 二级渲染兜底）—— 见计划 §7.2.1。
    """
    if len(html) < 20000:
        return False
    ratio = len(extracted) / max(1, len(html))
    if ratio >= 0.03:                       # 正文占比 ≥3% 就当静态页
        return False
    s = html[:400000].decode("utf-8", "replace")
    for mark in ("__NEXT_DATA__", 'id="app"', 'id="root"', "window.__NUXT__",
                 "data-reactroot"):
        if mark in s:
            return True
    return False


def same_site(a: str, b: str) -> bool:
    """同站判定（图片/链接重写时用）。"""
    try:
        return urlparse(a).netloc.split(":")[0] == urlparse(b).netloc.split(":")[0]
    except Exception:                       # noqa: BLE001
        return False
