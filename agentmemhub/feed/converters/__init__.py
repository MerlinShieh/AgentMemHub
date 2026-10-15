# -*- coding: utf-8 -*-
"""内置转换器集合（按**注册顺序**认领 —— 先注册者优先）。

加一个格式 = 写一个 `Converter` + 加进 `BUILTIN`，主干不动。
顺序有意义：特化转换器必须排在通用转换器**前面**（例如公众号
`wechat.py` 排在通用 `url.py` 之前，否则链接会被通用实现先认领）。
"""
from __future__ import annotations

from .docx import DocxConverter
from .pdf import PdfConverter
from .pptx import PptxConverter
from .text import TextConverter
from .url import UrlConverter
from .wechat import WechatConverter
from .xmind import XmindConverter

#: 内置转换器类（顺序 = 认领优先级）
BUILTIN = (
    WechatConverter,    # T2b：公众号特化 —— **必须排在通用 url 之前**，否则被它先认领
    PdfConverter,       # T3：PDF —— 同理必须排在 url 之前：
                        # `feed add https://…/report.pdf` 若被 URL 转换器先认领，
                        # 就会拿 trafilatura 去解析 PDF 字节（必然抽不出正文）
    DocxConverter,      # DOCX（T1 级难度的一档，见计划 §7.0.1）
    PptxConverter,      # PPTX（OOXML，与 DOCX 同类：确定性、零 LLM）
    XmindConverter,     # XMIND 思维导图（ZIP + JSON/XML，零依赖）
    UrlConverter,       # T2a：普通网页 + **本地另存的网页**
    TextConverter,      # T1：md / txt 直通（零依赖）
    # T4 image（OCR）后续加入
)
