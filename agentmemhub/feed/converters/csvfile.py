# -*- coding: utf-8 -*-
"""CSV / TSV · 分隔符文本表：**零依赖**直通成 markdown 管道表。

为什么它比看起来重要
==================
CSV 是**导出格式的默认选择**（台账、报表、名单、监控导出）：几乎每个系统都
"能导出 CSV"。所以它的量往往比 xlsx 大，脏法也更多 —— 它只有字节：
没有元数据、没有类型、连"用什么分隔"都不写在文件里。

三处必须处理（每条都对应真实文件里的一种翻车）
==========================================
1. **中文 CSV 大量是 GBK** —— Excel 在中文 Windows 上"另存为 CSV"默认用
   系统 ANSI 编码（GBK），不是 UTF-8。按 UTF-8 硬解出来是**整份乱码**，
   而且"看起来像数据"，比报错难发现得多。所以依次试
   `utf-8-sig`（Excel 导 UTF-8 时带 BOM）→ `utf-8` → `gb18030`（GBK 超集）；
2. **分隔符不一定是逗号** —— 欧洲区域设置导出分号，很多系统导出制表符
   （`.tsv`，或"另存为文本"）。判据是**首行引号外**出现最多的候选分隔符：
   首行通常是表头、分隔最规整；而"引号外"这个限定是必须的 ——
   一条带逗号的说明就能把计数带偏；
3. **类型全丢** —— CSV 里 `2026-10-15` 是字符串，`00123` 也是字符串。
   这里**坚决不做类型推断**：把 `00123` 变成 `123` 是**改数据**
   （工号、邮编、批次号的前导零有含义），比"没识别出这是个日期"严重得多。
   表格里最不能犯的错是"悄悄改了值"。

能力边界：**不做编码以外的内容改写**（不拆列、不合并、不去重）。
认领只按后缀 —— 无后缀的字节不猜"它是不是 CSV"：
纯文本里含逗号太常见，猜错的代价是"一份笔记被当成表格"。
"""
from __future__ import annotations

import io
from pathlib import Path

from ..base import BaseConverter, FeedDocument, FeedError
from ..mdtable import MAX_COLS, MAX_ROWS, cell_text, table_md
from ..titles import clip_title, is_junk_title

#: 认领的扩展名
EXTS = (".csv", ".tsv")
#: 依次尝试的编码（顺序不能动，理由见模块 docstring）
_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030")
#: 候选分隔符（按"常见程度"排，仅用于并列时的取舍）
_DELIMS = (",", ";", "\t", "|")


def _decode(data: bytes) -> tuple[str, str, list[str]]:
    """按候选编码解码。返回 `(文本, 实际编码, 告警)`。

    **解不出来也不失败**：用 replace 兜底并告警 —— 有一两个字是问号，
    远好过整份资料进不来（质量自评会把它标出来）。
    """
    for enc in _ENCODINGS:
        try:
            return data.decode(enc), enc, []
        except UnicodeDecodeError:
            continue
    return (data.decode("utf-8", "replace"), "utf-8/replace",
            ["编码无法确定，已按 utf-8 尽力解码（可能有替换字符）"])


def _count_outside_quotes(text: str, delim: str) -> int:
    """数 `delim` 在**引号外**出现的次数（状态机，两个引号翻转一次）。

    为什么不能在引号内计数：`"北京, 上海"` 这一个单元格里就有一个逗号，
    参与计数会让"内容里逗号多、分隔符其实是分号"的文件判错。
    """
    n = 0
    in_quotes = False
    for ch in text:
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == delim and not in_quotes:
            n += 1
    return n


def _delimiter_of(text: str, suffix: str) -> str:
    """推断分隔符：`.tsv` 固定制表符；其余看**首行**的引号外计数。"""
    if suffix == ".tsv":
        return "\t"
    head = "\n".join(text.splitlines()[:20])
    counts = {d: _count_outside_quotes(head, d) for d in _DELIMS}
    best = max(_DELIMS, key=lambda d: counts[d])
    return best if counts[best] > 0 else ","


def _rows_of(text: str, delim: str) -> tuple[list[list[str]], bool, bool]:
    """解析成文本行。返回 `(行, 行被截断, 列被截断)`。

    `newline=""` 是 csv 模块的**官方要求**（让它自己处理 `\\r\\n`）：
    若交给 StringIO 先规范化换行，引号内的跨行字段会被切错行。
    """
    import csv

    rows: list[list[str]] = []
    row_capped = col_capped = False
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delim)
    for raw in reader:
        if len(rows) >= MAX_ROWS:
            row_capped = True
            break
        cells = [cell_text(v) for v in raw]
        while cells and not cells[-1]:
            cells.pop()
        if not cells or not any(cells):
            continue
        if len(cells) > MAX_COLS:
            col_capped = True
            cells = cells[:MAX_COLS]
        rows.append(cells)
    return rows, row_capped, col_capped


class CsvConverter(BaseConverter):
    """`.csv` / `.tsv` 分隔符文本表（**零依赖、零改写**）。"""

    name = "csv"
    version = "1"
    #: 认领的后缀（**类属性**，供格式白名单从注册表收集 —— 见 `feed/formats.py`）
    EXTS = EXTS

    def can_handle(self, *, ref: str, kind: str = "",
                   data: bytes | None = None) -> bool:
        if kind == "csv":
            return True
        if kind and kind not in ("url",):
            return False
        return Path(str(ref)).suffix.lower() in EXTS

    def convert(self, *, ref: str, data: bytes, meta: dict) -> FeedDocument:
        text, enc, warns = _decode(data)
        suffix = Path(str(ref)).suffix.lower()
        delim = _delimiter_of(text, suffix)
        rows, row_capped, col_capped = _rows_of(text, delim)
        if not rows:
            raise FeedError("CSV 里没有任何可读的行（空文件，或只有空行/空列）",
                            kind="empty", retryable=False)
        if row_capped:
            warns.append("表格超过 %d 行，只转译了前 %d 行" % (MAX_ROWS, MAX_ROWS))
        if col_capped:
            warns.append("表格有超过 %d 列的宽表，超出部分未转译" % MAX_COLS)

        stem = Path(str(ref)).stem
        if stem and not is_junk_title(stem):
            title, did = clip_title(stem)
            title_src = "stem_clipped" if did else "stem"
        else:
            title, title_src = "表格", "fallback"

        ncols = max(len(r) for r in rows)
        md = "\n".join(["# %s" % title, "",
                        "> %s 表格 · 共 %d 行 × %d 列"
                        % ("TSV" if delim == "\t" else "CSV", len(rows), ncols),
                        "", table_md(rows)]).strip() + "\n"
        # 时间：CSV **没有**文档自身时间可用（纯字节），只能退到文件 mtime
        doc_time = int(meta.get("mtime") or meta.get("now") or 0)
        truncated = bool(row_capped or col_capped)
        return FeedDocument(
            doc_id=meta["doc_id"], title=title[:200], markdown=md, kind="csv",
            source_ref=str(ref), doc_time=doc_time, converter=self.stamp,
            quality=0.7 if truncated else 1.0, warnings=warns,
            meta={"rows": len(rows), "cols": ncols, "encoding": enc,
                  "delimiter": "\\t" if delim == "\t" else delim,
                  "truncated": truncated, "title_source": title_src,
                  "doc_time_source": "mtime" if meta.get("mtime") else "now",
                  "chars": len(md)},
        )
