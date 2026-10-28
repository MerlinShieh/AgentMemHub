# -*- coding: utf-8 -*-
"""外部投喂转译层（feed）：把 N 种外部素材收敛成 1 种统一交付物。

定位（与 `adapters/` 的区别）
============================
· `adapters/`：**采集**内部 Agent 会话（DSH / Qwen / ZCode…）
· `feed/`（本包）：**转译**外部素材（文件 / URL / PDF / 图片）→ 统一 md → 知识库页面层

两者同构（注册表 + 认领 + 统一产物）但数据流方向与服务对象完全不同，**不合并**。

设计文档
========
[`docs/feed-adapter-plan.md`](../../docs/feed-adapter-plan.md)（v2.2）——
单轨纯并列 / 三段保留 / 四级格式 / 脚本优先-LLM 兜底 的分工都在那里。
"""
from __future__ import annotations

from typing import Optional

from .base import QUALITY_WARN, Converter, FeedDocument, FeedError
from .converters import BUILTIN
from .store import MaterialStore, MaterialsDB, doc_id_of, sha256_of

__all__ = [
    "QUALITY_WARN", "Converter", "FeedDocument", "FeedError",
    "MaterialStore", "MaterialsDB", "doc_id_of", "sha256_of",
    "CONVERTERS", "all_converters", "get_converter", "register_converter",
]

#: 已注册转换器实例（顺序 = 认领优先级）
CONVERTERS: list = [cls() for cls in BUILTIN]


def all_converters() -> list:
    """全部已注册转换器（按认领顺序）。"""
    return list(CONVERTERS)


def get_converter(*, ref: str, kind: str = "",
                  data: bytes | None = None) -> Optional[Converter]:
    """按注册顺序问一遍"谁能处理"。**认不出来返回 None**（由调用方显式报错）。

    为什么不在内部兜底一个"通用转换器"：未知类型**必须显式失败** ——
    本项目踩过"未知 source 被静默跳过"的坑（什么都没发生、也不报错）。
    """
    for c in CONVERTERS:
        try:
            if c.can_handle(ref=ref, kind=kind, data=data):
                return c
        except Exception:                   # noqa: BLE001 —— 单个转换器认领出错不拦别人
            continue
    return None


def register_converter(conv: Converter, *, first: bool = False) -> None:
    """注册一个转换器。`first=True` 插到最前（特化实现优先认领）。"""
    if first:
        CONVERTERS.insert(0, conv)
    else:
        CONVERTERS.append(conv)
