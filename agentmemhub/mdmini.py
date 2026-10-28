# -*- coding: utf-8 -*-
"""极小 markdown → HTML（只服务面板「知识库」页的正文渲染）。

为什么不引 markdown 库
=====================
面板要渲染的是**我们自己 wiki 产物**里固定的那个子集（标题 / 段落 / 列表 / 表格 /
引用 / 行内代码 / `[mNNN]` 引用标记）。引一个库要多一条依赖，且 raw HTML 透传是
默认行为 —— 外部投喂的内容直接塞进 DOM 是不能接受的，还得再引一个消毒库。

所以这里走 **先转义、再套结构**：所有文本一律 `html.escape` 之后才拼标签，
内容里写 `<script>` 只会显示成字面的文本。代价是**不支持内嵌 HTML**（我们本来也不用），
收益是"渲染"与"安全"两件事在同一处说清楚、可被测试钉死。

支持的语法（按实际页面统计）：ATX 标题 `#`~`####`、围栏代码块、无序/有序列表、
管道表格、引用、水平线、段落；行内代码 / 粗体 / 斜体 / 链接（**仅 http/https**）/
`[mNNN]` 引用徽章。
**不支持**：嵌套列表、内嵌 HTML、脚注、图片（L2 页面里没有）。
"""
from __future__ import annotations

import html
import re

__all__ = ["render", "render_body"]

_H_RE = re.compile(r"^(#{1,4})\s+(.*)$")
_UL_RE = re.compile(r"^\s{0,3}[-*+]\s+(.*)$")
_OL_RE = re.compile(r"^\s{0,3}(\d+)[.)]\s+(.*)$")
_HR_RE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")
_FENCE = "```"


def _inline(text: str) -> str:
    """行内标记。**入口先转义** —— 之后插入的都是我们自己生成的标签。"""
    # ⚠️ `quote=True` 是安全阀，别改回去：下面链接那一步把 URL 直接放进
    # `href="…"`，若 `"` 不被转义，正文里写 `[x](http://a/"onmouseover="alert(1))`
    # 就能闭合属性注入事件处理器 —— 而外部投喂的 PDF/网页正是不可信正文。
    t = html.escape(str(text or ""), quote=True)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", t)
    # 斜体：只排除**相邻**的 `*`（粗体已被上一步吃掉）。
    # ⚠️ 不能用 `(?<![*\w])` —— Python 的 `\w` 含中文，写"已有*斜体*"时
    # 前一个字是汉字会命中 `\w`，斜体就永远认不出来（实测踩到）。
    t = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", t)
    # 链接**只放行 http/https**（`javascript:` 之类一律不生成 a 标签）
    t = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)",
               r'<a href="\2" target="_blank" rel="noopener noreferrer">\1</a>', t)
    # 引用标记 `[m3236]` → 小徽章（L2 页面的溯源锚，读者一眼能看出处）。
    # `data-mid` 是 `distilled_memories.id`：面板据此把徽章做成**可点**（点开看原记忆），
    # 而 `m3236` 这段文本仍原样保留 —— 没有 JS 时它依旧是可读的溯源标记，不会变成空壳。
    t = re.sub(r"\[(m(\d+))\]", r'<span class="md-cite" data-mid="\2">\1</span>', t)
    return t


def _cells(row: str) -> list[str]:
    r = row.strip()
    if r.startswith("|"):
        r = r[1:]
    if r.endswith("|"):
        r = r[:-1]
    return [c.strip() for c in r.split("|")]


def _table(rows: list[str]) -> str:
    """管道表 → `<table>`。第 2 行是 `---` 分隔（已被调用方确认）。"""
    head = _cells(rows[0])
    body = [_cells(r) for r in rows[2:]]
    out = ['<table class="md-table"><thead><tr>']
    out.extend("<th>%s</th>" % _inline(c) for c in head)
    out.append("</tr></thead><tbody>")
    for row in body:
        # 列数不齐时补空，避免表格塌陷（与 mdtable.table_md 同一容错取向）
        row = row + [""] * (len(head) - len(row))
        out.append("<tr>%s</tr>" % "".join("<td>%s</td>" % _inline(c)
                                           for c in row[:len(head)]))
    out.append("</tbody></table>")
    return "".join(out)


def render_body(md: str) -> str:
    """markdown → HTML 片段（**不含** `<article>` 外壳，便于测试与复用）。"""
    lines = str(md or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    para: list[str] = []
    i, n = 0, len(lines)

    def flush() -> None:
        if para:
            out.append("<p>%s</p>" % _inline(" ".join(para)))
            para.clear()

    while i < n:
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith(_FENCE):                 # 围栏代码块
            flush()
            i += 1
            buf: list[str] = []
            while i < n and not lines[i].strip().startswith(_FENCE):
                buf.append(lines[i])
                i += 1
            i += 1                                      # 跳过收尾围栏
            out.append('<pre class="md-pre"><code>%s</code></pre>'
                       % html.escape("\n".join(buf)))
            continue
        if not stripped:                                # 空行
            flush()
            i += 1
            continue
        if _HR_RE.match(line):                          # 水平线
            flush()
            out.append("<hr>")
            i += 1
            continue
        m = _H_RE.match(line)                           # 标题
        if m:
            flush()
            lv = len(m.group(1))
            out.append("<h%d>%s</h%d>" % (lv, _inline(m.group(2)), lv))
            i += 1
            continue
        if stripped.startswith("|") and i + 1 < n and _TABLE_SEP_RE.match(lines[i + 1]):
            flush()                                     # 表格
            rows: list[str] = []
            while i < n and lines[i].strip().startswith("|"):
                rows.append(lines[i])
                i += 1
            out.append(_table(rows))
            continue
        if stripped.startswith(">"):                    # 引用（连续行合并）
            flush()
            buf = []
            while i < n and lines[i].strip().startswith(">"):
                buf.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            out.append("<blockquote>%s</blockquote>" % _inline(" ".join(buf)))
            continue
        if _UL_RE.match(line):                          # 无序列表
            flush()
            items = []
            while i < n and _UL_RE.match(lines[i]):
                items.append(_inline(_UL_RE.match(lines[i]).group(1)))
                i += 1
            out.append("<ul>%s</ul>" % "".join("<li>%s</li>" % x for x in items))
            continue
        if _OL_RE.match(line):                          # 有序列表
            flush()
            items = []
            while i < n and _OL_RE.match(lines[i]):
                items.append(_inline(_OL_RE.match(lines[i]).group(2)))
                i += 1
            out.append("<ol>%s</ol>" % "".join("<li>%s</li>" % x for x in items))
            continue
        para.append(stripped)
        i += 1
    flush()
    return "\n".join(out)


def render(md: str, *, title: str = "") -> str:
    """带外壳的完整渲染（面板直接插进容器）。`title` 只用于无障碍标注。"""
    body = render_body(md)
    aria = ' aria-label="%s"' % html.escape(title, quote=True) if title else ""
    return '<article class="md-body"%s>%s</article>' % (aria, body)
