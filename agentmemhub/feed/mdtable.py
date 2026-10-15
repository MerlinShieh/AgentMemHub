# -*- coding: utf-8 -*-
"""二维单元格 → markdown 管道表（PDF / DOCX / XLSX / CSV 四条转换通路共用）。

为什么单独一层：表格是"结构提取"里唯一需要**重建语义**的部分
（文本是抄写，表格是重排），几个转换器各写一份必然漂移 ——
一边补空列一边不补，同一份素材从 PDF 进来和从 DOCX 进来会长得不一样。

共用范围还包括**规模上限**（`MAX_ROWS` / `MAX_COLS` / `MAX_CELL`）：
csv 与 xlsx 各定一套，等于同一张表两条通路两个结果。
"""
from __future__ import annotations

import datetime as _dt

#: 单张表的行数上限。超了**截断并告警**（不静默）：表格类素材进知识库是为了
#: "能被读到关键几行"，1000 行以后的内容对召回单元只意味着检索噪声。
MAX_ROWS = 1000
#: 单张表的列数上限（宽表同理）
MAX_COLS = 60
#: 单元格文本上限 —— 有人会把整段说明塞进一格，那会把表撑得没法看
MAX_CELL = 300


def cell_text(value, *, limit: int = MAX_CELL) -> str:
    """单元格**值** → 人类可读文本（日期 / 布尔 / 时长的表达）。

    为什么必须单独一步、不能直接 `str(value)`：
      · **XLSX 的日期在文件里是序列号**（`45000` = 1900 纪元的第 45000 天），
        靠 `styles.xml` 的数字格式才能还原。openpyxl 帮我们还原成 `datetime`，
        但直接 `str()` 会得到 `2026-10-15 00:00:00` —— 一整列日期都拖着
        无意义的时间部分，所以**零点的 datetime 只给日期**；
      · 布尔要给 `TRUE` / `FALSE`（Excel 里的显示样式），不是 Python 的 `True`；
      · `None` 给空串（空单元格在 md 表里就是空格子，不是 `None` 字面量）。

    注意 `datetime` 是 `date` 的子类 ⇒ **必须先判 `datetime`**，
    否则判别顺序一颠倒，带时间的单元格会被当成纯日期（静默丢时间）。
    """
    if value is None:
        return ""
    if isinstance(value, _dt.datetime):
        if (value.hour, value.minute, value.second, value.microsecond) == (0, 0, 0, 0):
            s = value.strftime("%Y-%m-%d")
        else:
            s = value.strftime("%Y-%m-%d %H:%M:%S")
    elif isinstance(value, _dt.date):
        s = value.strftime("%Y-%m-%d")
    elif isinstance(value, _dt.time):
        s = value.strftime("%H:%M:%S")
    elif isinstance(value, _dt.timedelta):
        # Excel 里的"时长"（如 01:30:00）—— `str()` 给出的就是人们熟悉的形态
        s = str(value)
    elif isinstance(value, bool):
        s = "TRUE" if value else "FALSE"
    else:
        s = str(value)
    s = " ".join(s.split())
    return s[:limit] + ("…" if len(s) > limit else "")


def clean_cell(value) -> str:
    """单元格文本安全化：`|` 会破坏管道表结构，换行会破坏行结构。"""
    if value is None:
        return ""
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ").strip()


def table_md(rows, *, label: str = "") -> str:
    """渲染成管道表。返回空串表示"没有可用内容"（调用方据此跳过）。

    两处容错都是**真实素材逼出来的**，不是防御性编程：
      · 列数不齐 —— 合并单元格与跨页表格会让 `extract()` 给出参差的行；
      · 全空行 —— 空白分隔行在 PDF 表格里很常见，渲染出来只是噪声。
    """
    clean: list[list[str]] = []
    for row in rows or []:
        if not row:
            continue
        cells = [clean_cell(c) for c in row]
        if any(cells):
            clean.append(cells)
    if not clean:
        return ""
    ncol = max(len(r) for r in clean)
    clean = [r + [""] * (ncol - len(r)) for r in clean]
    lines: list[str] = []
    if label:
        lines += [label, ""]
    lines.append("| %s |" % " | ".join(clean[0]))
    lines.append("|%s|" % "|".join([" --- "] * ncol))
    lines += ["| %s |" % " | ".join(r) for r in clean[1:]]
    return "\n".join(lines)
