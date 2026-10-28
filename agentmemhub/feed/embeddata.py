# -*- coding: utf-8 -*-
"""内嵌数据抢救：**数据在 HTML 里、但不在正文里**的时候。

问题（真实素材，2026-10-15）
==========================
用户文档目录里有 60 份"脚本录制工具"导出的测试报告：HTML 有 157 KB，
静态可见文本只有 **136 字** —— 全是「用例名称:」「步骤总数:」这类**字段标签，没有值**。
真正的数据在 `<script>` 里：`var resultData = {"testResult": [...]}`（27 KB）。

trafilatura 抽到的就是那些标签，而质量自评按"**抽取覆盖率**"算，
给它打了 **1.00** —— 抽取确实"覆盖"了全部可见文本。
于是知识库里会躺 60 个**看起来正常、实际是空壳**的页面。
这是"指标算得对、却不说明问题"的典型：**覆盖率 ≠ 信息量**。

两条处置（先取回、取不回才打标）
==============================
1. **把数据取回来**：它在文件里，不非要渲染不可 —— 开 Playwright 是杀鸡用牛刀，
   而解析 JSON 更准、更快、**且确定**（渲染结果受浏览器版本影响，JSON 解析不会）；
2. 取不回来就**如实打标**（不让空壳冒充满分），并给出可执行指引。

⚠️ 为什么触发条件要**窄**
========================
通用地从任意网页里挖 JSON 很容易挖出一堆无关配置（主题、埋点、路由表）。
所以只在"静态正文明显是个空壳"时才动手（`looks_like_label_shell`），
且要求数据块有足够体量（`MIN_DATA_BYTES`）。宁可漏救，不可乱灌。
"""
from __future__ import annotations

import json
import re
from typing import Any

#: 内嵌数据块的**体量下限**：太小的大半是配置（主题色、埋点开关）而不是内容。
#:
#: ⚠️ 这个值定过一次错：最初写 2048，结果把**记录本来就少的报告**误判成"没数据"——
#: 真实素材里一份只有 3 条测试记录的报告，数据是 1049 字节，恰好在阈值之下，
#: 于是 60 份报告里有 16 份没救回来（2026-10-15 批量复验时发现）。
#: 关键是"能不能渲染成表格"（`json_to_markdown` 里的 `_find_table`），
#: 字节数只用来挡掉真正的碎片。256 足够挡配置、又不误伤小报告。
MIN_DATA_BYTES = 256
#: 一次最多救几块（防止把整页 JS 数据都灌进正文）
MAX_BLOCKS = 4
#: 渲染表格的行数/列数上限（防爆）
MAX_ROWS = 200
MAX_COLS = 12
#: 单个单元格最长字符数
MAX_CELL = 80

#: `<script>` 块（含属性）
_SCRIPT_RE = re.compile(r"<script[^>]*>(.*?)</script>", re.S | re.I)
#: JS 里给变量赋 JSON 值的形态：`var resultData = {…}` / `const x = […]`
#: ⚠️ 前导用 `\b` 而**不是**"空白/分号/花括号"的白名单 —— 最常见的写法是
#: `<script>var x = {…}`，`var` 前面紧挨着的是 `>`，白名单会把它整个漏掉
#: （2026-10-15 被单元测试抓到：真实素材恰好有换行缩进才躲过这一劫）。
_VAR_ASSIGN_RE = re.compile(
    r"\b(?:var|let|const)\s+([A-Za-z_$][\w$]*)\s*=\s*", re.M)
#: 以冒号/全角冒号结尾的"裸标签行"（`用例名称:`）
_LABEL_END_RE = re.compile(r"[:：]\s*$")


def looks_like_label_shell(md: str, *, min_lines: int = 5,
                           ratio: float = 0.3) -> bool:
    """"这段正文是个空壳吗" —— 判据是**裸标签的密度**。

    真实素材的形态：23 行正文里有 11 行是「xxx:」（字段名后面什么都没有）——
    因为值由 JS 填。正常文档不会这样（标签后面总跟着值）。

    为什么用"比例"而不是"绝对条数"：短文档本来就可能有一两个以冒号结尾的行。
    """
    lines = [ln.strip() for ln in (md or "").splitlines() if ln.strip()]
    if len(lines) < min_lines:
        return False
    labels = sum(1 for ln in lines if _LABEL_END_RE.search(ln)
                 and len(ln) <= 40)          # 长句以冒号结尾不算"裸标签"
    return labels / len(lines) >= ratio


def extract_embedded_json(html: bytes, *,
                          min_bytes: int = MIN_DATA_BYTES) -> list[tuple[str, Any]]:
    """从 `<script>` 里抽出内嵌 JSON 数据，返回 `[(变量名, 解析结果)]`。

    用 `json.JSONDecoder.raw_decode` 而不是正则配对：它能从任意位置精确解析出
    **一个完整 JSON 值并在结束处停住**，天然处理嵌套与字符串里的括号 ——
    正则做这件事必然会在大对象上出错。

    `min_bytes` 可注入（默认 `MIN_DATA_BYTES`）：体量下限是**生产判据**，
    但测试要造小样本才好写断言。
    """
    text = html.decode("utf-8", "replace")
    out: list[tuple[str, Any]] = []
    decoder = json.JSONDecoder()
    seen: set[str] = set()
    for m in _VAR_ASSIGN_RE.finditer(text):
        name = m.group(1)
        i = m.end()
        while i < len(text) and text[i] in " \t\r\n":
            i += 1
        if i >= len(text) or text[i] not in "{[":
            continue
        try:
            obj, _end = decoder.raw_decode(text, i)
        except Exception:                    # noqa: BLE001 —— 不是严格 JSON 就跳过
            continue
        if not isinstance(obj, (dict, list)):
            continue
        try:
            size = len(json.dumps(obj, ensure_ascii=False))
        except Exception:                    # noqa: BLE001
            continue
        if size < min_bytes:
            continue
        key = "%s:%d" % (name, size)
        if key in seen:
            continue
        seen.add(key)
        out.append((name, obj))
        if len(out) >= MAX_BLOCKS:
            break
    return out


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        s = value.replace("|", "\\|").replace("\n", " ").strip()
        return s[:MAX_CELL] + ("…" if len(s) > MAX_CELL else "")
    if isinstance(value, (list, tuple)):
        return _cell(" / ".join(_cell(v) for v in value[:4]))
    if isinstance(value, dict):
        for k in ("name", "title", "text", "label", "value"):
            if k in value:
                return _cell(value[k])
        return _cell(", ".join("%s=%s" % (k, _cell(v)) for k, v in list(value.items())[:3]))
    return ""


def _find_table(obj, depth: int = 0) -> list | None:
    """在 JSON 里找"最适合当表格"的部分 —— **`list[dict]`**（一组同构记录）。

    深度优先、取第一个命中：真实数据通常是 `{"testResult": [ {记录}, … ]}`。
    """
    if depth > 6:
        return None
    if isinstance(obj, list) and obj and all(isinstance(x, dict) for x in obj[:5]):
        return obj
    if isinstance(obj, dict):
        for v in obj.values():
            r = _find_table(v, depth + 1)
            if r:
                return r
    if isinstance(obj, list):
        for v in obj:
            r = _find_table(v, depth + 1)
            if r:
                return r
    return None


def json_to_markdown(obj, *, label: str = "") -> str:
    """内嵌 JSON → markdown。**表格优先**（一组同构记录就是一张表）。"""
    rows = _find_table(obj)
    if rows:
        keys: list[str] = []
        for r in rows:
            for k in r.keys():
                if k not in keys:
                    keys.append(k)
        keys = keys[:MAX_COLS]
        body = rows[:MAX_ROWS]
        lines = ["| %s |" % " | ".join(_cell(k) for k in keys),
                 "|%s|" % "|".join([" --- "] * len(keys))]
        for r in body:
            lines.append("| %s |" % " | ".join(_cell(r.get(k)) for k in keys))
        if len(rows) > MAX_ROWS:
            lines.append("")
            lines.append("（共 %d 条记录，此处只列前 %d 条）" % (len(rows), MAX_ROWS))
        head = "**%s**（%d 条记录）" % (label or "内嵌数据", len(rows))
        return head + "\n\n" + "\n".join(lines)
    if isinstance(obj, dict):
        lines = ["| 字段 | 值 |", "| --- | --- |"]
        for k, v in list(obj.items())[:MAX_ROWS]:
            lines.append("| %s | %s |" % (_cell(k), _cell(v)))
        return "**%s**\n\n" % (label or "内嵌数据") + "\n".join(lines)
    return ""


def _strip_label_lines(md: str) -> str:
    """去掉"裸标签行"（`用例名称:` 这种只有字段名、没有值的行）。

    数据取回来之后，这些骨架只剩噪声 —— 留着会让读者以为"这个报告没有数据"。
    保留其余内容（标题、章节名、以及**有值**的短行）。
    """
    kept: list[str] = []
    for ln in (md or "").splitlines():
        s = ln.strip()
        if not s or s in ("|", "||"):
            continue
        if _LABEL_END_RE.search(s) and len(s) <= 40:
            continue
        kept.append(s)
    return "\n".join(kept).strip()


def rescue(html: bytes, md: str, *, label: str = "",
           min_bytes: int = MIN_DATA_BYTES) -> tuple[str, list[str]]:
    """正文是个空壳时，把它**换成**"精简骨架 + 从内嵌数据还原的表格"。

    返回 `(新正文, 警告)`：
      · `("", [])`      —— 不是空壳，不用动（**绝大多数页面走这条**）；
      · `("", [警告])`  —— 是空壳但没救回来（调用方据此重降质量并逼出人工复核）；
      · `(新正文, [警告])` —— 救回来了。

    ⚠️ 触发条件刻意很窄（`looks_like_label_shell`）：通用地从网页里挖 JSON
    很容易挖出一堆无关配置（主题、埋点、路由表）。**宁可漏救，不可乱灌。**
    """
    if not looks_like_label_shell(md):
        return "", []
    blocks = extract_embedded_json(html, min_bytes=min_bytes)
    if not blocks:
        return "", ["正文只有字段标签、没有值（数据疑似由 JS 渲染），"
                    "且未能在文件里找到内嵌数据 —— 建议从源系统重新导出，"
                    "或在浏览器里打开后另存为完整页面"]
    parts: list[str] = []
    for name, obj in blocks:
        rendered = json_to_markdown(obj, label=label or name)
        if rendered:
            parts.append(rendered)
    if not parts:
        return "", ["正文只有字段标签、没有值；文件里虽有内嵌数据但无法渲染成表格"]
    skeleton = _strip_label_lines(md)
    note = ("> 原页面的正文由 JS 渲染（静态可见文本只有字段标签），"
            "以下数据取自**页面内嵌数据**。\n")
    body = (skeleton + "\n\n" if skeleton else "") + "\n\n".join(parts)
    return note + "\n" + body, [
        "正文的可见文本只有字段标签（数据由 JS 渲染），"
        "已从文件内嵌数据中取回 %d 块并转为表格" % len(parts)]
