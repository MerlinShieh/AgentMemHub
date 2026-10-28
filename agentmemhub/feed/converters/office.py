# -*- coding: utf-8 -*-
"""OOXML 三件套（DOCX / PPTX / XLSX）的**公共**小工具。

为什么单独一层：这三条通路的元数据模型本来就是同一个 —— `docProps/core.xml`
里的 `dcterms:modified` / `dcterms:created`，`python-docx` / `python-pptx` /
`openpyxl` 都把它暴露成 `core_properties`。各写一份的下场是"同一份文档从三种
格式进来 `doc_time` 不一样"，而 `doc_time` 直接喂召回侧的**时间衰减**，
错一天就错一天的分。

只放**真正共用**的东西。格式特有的逻辑（标题取值顺序、表格重建、图片处理）
留在各自模块 —— 它们看着像、其实判据不同（PPT 的"标题"是占位符、
XLSX 根本没有标题概念），硬合并会造出一个满是 if 的假共用层。
"""
from __future__ import annotations

import time
from datetime import timezone


def doc_time_of(cp, meta: dict) -> tuple[int, str]:
    """**文档自身时间**优先：core 的 modified → created → 文件 mtime → 当前时刻。

    为什么严格按这个顺序：召回侧的时间衰减按"知识产生时间"算
    （与 `distill._origin_time` 同一语义）—— 用投喂时刻会让一份 2019 年的
    旧资料显得"刚刚产生"，在按时间排序的召回里顶掉真正的新内容。

    第二个返回值是**取值来源**：它进 `meta` 供排查用。实测踩过一次
    —— 某 PDF 的 `/CreationDate` 丢了时区后缀，按本地时间解析后 doc_time
    整体偏了 8 小时；有来源标注时，"这份为什么时间是错的"一眼能看出是哪一档。

    退出码无、异常无：任何一档坏掉都往下走，最后一档一定有值。
    """
    for attr, src in (("modified", "core_modified"), ("created", "core_created")):
        v = getattr(cp, attr, None) if cp is not None else None
        if v is not None and hasattr(v, "timestamp"):
            try:
                if v.tzinfo is None:
                    # OOXML 的时间语义是 **UTC**（`…T12:00:00Z`），但读回来的
                    # `datetime` 未必带 tzinfo：python-docx / python-pptx 会保留，
                    # **openpyxl 会丢掉**。而 naive 的 `.timestamp()` 按**本地时区**
                    # 解释 ⇒ 东八区下 doc_time 整整偏 8 小时。
                    # 2026-10-15 实测：core.xml 里是 `2026-01-01T12:00:00Z`，
                    # openpyxl 读回 `datetime(2026,1,1,12,0)`（tzinfo=None），
                    # `.timestamp()` 得出的是"北京时间 12:00"那一刻 —— 早 8 小时。
                    # 这与 PDF 那档"`/CreationDate` 丢掉 `Z`"是**同一个坑的第二次出现**，
                    # 所以修在共用层，三条通路一起受益。
                    v = v.replace(tzinfo=timezone.utc)
                return int(v.timestamp()), src
            except Exception:                # noqa: BLE001 —— 坏时间戳就退下一档
                pass
    if meta.get("mtime"):
        return int(meta["mtime"]), "mtime"
    return int(meta.get("now") or time.time()), "now"
