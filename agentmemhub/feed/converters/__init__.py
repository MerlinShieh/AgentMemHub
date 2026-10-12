# -*- coding: utf-8 -*-
"""内置转换器集合（按**注册顺序**认领 —— 先注册者优先）。

加一个格式 = 写一个 `Converter` + 加进 `BUILTIN`，主干不动。
顺序有意义：特化转换器必须排在通用转换器**前面**（例如将来的公众号
`wechat.py` 要排在通用 `url.py` 之前，否则链接会被通用实现先认领）。
"""
from __future__ import annotations

from .text import TextConverter

#: 内置转换器类（顺序 = 认领优先级）
BUILTIN = (
    TextConverter,      # T1：md / txt 直通（零依赖）
    # T2 url / wechat（M3）、T3 pdf（M4）、T4 image（M4）后续加入
)
