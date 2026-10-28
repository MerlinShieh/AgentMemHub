# -*- coding: utf-8 -*-
"""知识库页面清单（面板「wiki知识库」页的数据源）。

三个决定（都写在这里，免得下次又要重新想）
========================================
1. **以磁盘为准，不查投影表**：投影（`wiki --action index-pages`）是运维动作、会滞后 ——
   2026-10-18 实测：磁盘 **424** 页、投影 **399** 行。面板要看的是**知识库本身**，
   磁盘上有什么就显示什么；"能不能被召回"是另一件事（由投影决定），不该让
   面板的内容随它起落。
2. **域 = 相对根的第一段目录**：L2 按 `NN-域名/页面.md` 组织，第一段就是域；
   投喂交付物是平铺的 `<doc_id>.md`，没有域（返回空串）。
3. **origin 由"第几个根"决定**：`page_roots()` 的第 0 个是自有 L2（`native`），
   其余是投喂交付物（`external`）—— 与投影层的 origin 规则同源，不另立一套。

安全边界：读页面**只允许**在 `page_roots()` 之内（resolve 后比对），
挡 `../` 穿越与绝对路径乱指。
"""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

__all__ = ["list_pages", "read_page", "WikiPageError"]

_FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.S)
_TITLE_MAX = 200


class WikiPageError(ValueError):
    """页面清单/读取的业务错误（路由层翻成 4xx）。"""


def _roots(config=None) -> list[tuple[Path, str]]:
    """`[(绝对根路径, origin)]`，顺序即 `page_roots()` 的顺序。"""
    from agentmemhub import wiki_index

    if config is None:
        from agentmemhub.config import config as _cfg
        config = _cfg()
    l2 = None
    try:
        l2 = config.wiki.get("out_l2")
    except Exception:                       # noqa: BLE001 —— 配置形状异常不该让面板挂
        l2 = None
    raw = wiki_index.page_roots(l2, config=config)
    out: list[tuple[Path, str]] = []
    for i, r in enumerate(raw):
        p = Path(r)
        try:
            p = p.resolve()
        except OSError:
            pass
        # 第 0 根 = 自有 L2（native），其余 = 投喂交付物（external）
        out.append((p, "native" if i == 0 else "external"))
    return out


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """切出 `(frontmatter 键值, 正文)`。没有 frontmatter 时前者为空 dict。"""
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        k, v = line.split(":", 1)
        meta[k.strip()] = v.strip().strip('"\'')
    return meta, text[m.end():]


def _domain_of(rel_parts: tuple[str, ...]) -> str:
    """域 = 相对根的第一段目录；只在根下的文件没有域。"""
    return rel_parts[0] if len(rel_parts) > 1 else ""


def _inside(p: Path, root: Path) -> bool:
    try:
        p.relative_to(root)
        return True
    except ValueError:
        return False


def _page_meta(p: Path, root: Path) -> dict[str, Any]:
    """读一页的"清单级"元信息（**只读文件头**，不读全文）。"""
    rel = p.relative_to(root)
    meta: dict[str, str] = {}
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            head = f.read(4096)
        meta, _body = _split_frontmatter(head)
    except OSError:
        pass
    return {
        "title": (meta.get("title") or p.stem)[:_TITLE_MAX],
        "domain": _domain_of(rel.parts),
        "rel": rel.as_posix(),
        "type": meta.get("type") or "",
        "compiled_at": meta.get("compiled_at") or "",
        "sources": meta.get("sources") or "",
        "path": str(p),
    }


def list_pages(*, origin: str = "", domain: str = "", q: str = "",
               page: int = 1, page_size: int = 100, config=None) -> dict[str, Any]:
    """列出知识库页面（按域分组 + 分页 + 标题搜索）。

    `origin`：`native` / `external` / 留空（全部）。`domain`：精确匹配（留空=全部）。
    """
    roots = _roots(config)
    want_origin = str(origin or "").strip().lower()
    if want_origin not in ("", "native", "external"):
        raise WikiPageError("origin 只能是 native | external（留空=全部）")
    want_domain = str(domain or "").strip()
    needle = str(q or "").strip().lower()

    items: list[dict[str, Any]] = []
    domain_counts: dict[tuple[str, str], int] = {}
    origin_counts: dict[str, int] = {"native": 0, "external": 0}
    for root, org in roots:
        if not root.is_dir():
            continue
        for p in root.rglob("*.md"):
            if not p.is_file():
                continue
            meta = _page_meta(p, root)
            domain_counts[(meta["domain"], org)] = \
                domain_counts.get((meta["domain"], org), 0) + 1
            origin_counts[org] = origin_counts.get(org, 0) + 1
            if want_origin and org != want_origin:
                continue
            if want_domain and meta["domain"] != want_domain:
                continue
            if needle and needle not in meta["title"].lower():
                continue
            meta["origin"] = org
            items.append(meta)

    # 稳定排序：先域再标题（中文按码位，够用；要更讲究再上 locale 排序）
    items.sort(key=lambda x: (x["domain"], x["title"]))
    total = len(items)
    size = max(1, int(page_size))
    pages = max(1, math.ceil(total / size)) if total else 1
    start = (max(1, int(page)) - 1) * size

    return {
        "total": total,
        "page": max(1, int(page)),
        "pageSize": size,
        "pageCount": pages,
        "items": items[start:start + size],
        "domains": [{"name": d, "origin": o, "count": n}
                    for (d, o), n in sorted(domain_counts.items())],
        "originCounts": origin_counts,
        "roots": [{"path": str(r), "origin": o, "exists": r.is_dir()}
                  for r, o in roots],
    }


def read_page(raw_path: str, *, config=None) -> dict[str, Any]:
    """读一页完整内容（markdown + 渲染后 HTML + frontmatter）。

    **只允许 `page_roots()` 之内的文件** —— 面板是本地服务，但"能读任意路径"
    这种口子不该存在（`?path=C:\\Windows\\win.ini` 就该被拒）。
    """
    roots = _roots(config)
    if not roots:
        raise WikiPageError("没有配置任何知识库根（检查 wiki.out_l2 / feed.kb_dir）")
    try:
        p = Path(str(raw_path)).resolve()
    except (OSError, ValueError):
        raise WikiPageError("路径不合法：%s" % raw_path)
    hit = None
    for root, org in roots:
        if _inside(p, root):
            hit = (root, org)
            break
    if hit is None:
        raise WikiPageError("路径不在知识库根内：%s" % raw_path)
    root, org = hit
    if not p.is_file() or p.suffix.lower() != ".md":
        raise WikiPageError("页面不存在或不是 .md：%s" % raw_path)

    text = p.read_text(encoding="utf-8", errors="replace")
    meta, body = _split_frontmatter(text)
    rel = p.relative_to(root)
    from agentmemhub import mdmini
    title = (meta.get("title") or p.stem)[:_TITLE_MAX]
    return {
        "path": str(p),
        "rel": rel.as_posix(),
        "origin": org,
        "domain": _domain_of(rel.parts),
        "title": title,
        "frontmatter": meta,
        "markdown": body,
        "html": mdmini.render(body, title=title),
        "chars": len(body),
    }
