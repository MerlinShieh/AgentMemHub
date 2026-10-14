# -*- coding: utf-8 -*-
"""二维单元格 → markdown 管道表（PDF / DOCX 两条转换通路共用）。

为什么单独一层：表格是"结构提取"里唯一需要**重建语义**的部分
（文本是抄写，表格是重排），两个转换器各写一份必然漂移 ——
一边补空列一边不补，同一份素材从 PDF 进来和从 DOCX 进来会长得不一样。
"""
from __future__ import annotations


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
