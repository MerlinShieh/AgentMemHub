# -*- coding: utf-8 -*-
"""转译层契约：**统一交付物** `FeedDocument` + 可插拔转换器 `Converter`。

为什么要有这一层
================
投喂进来的东西五花八门（md / 网页 / 公众号 / PDF / 图片），但下游只该认识**一种**形态。
所以转译层的职责是"把 N 种输入收敛成 1 种输出"，输出就是 `FeedDocument`：

    原件字节 → [转换器] → FeedDocument(markdown + 溯源 + 质量自评) → 登记 → 发布

新增一种格式 = 新写一个 `Converter` 子类 + 注册，**不动主干**（与 `adapters/` 同构）。

与 `adapters/` 的区别（别混）
============================
· `adapters/`：**采集**内部 Agent 会话（DSH / Qwen / ZCode…）→ conversations/events
· `feed/`：**转译**外部素材（文件 / URL）→ 统一 md → 知识库页面层

两者形态同构（注册表 + 认领 + 统一产物），但数据流方向与服务对象完全不同，不合并。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class FeedError(Exception):
    """转译失败。带**机器可读的原因分类**与是否可重试 —— 两者都要落库。

    为什么要 `kind`：失败原因决定处置方式（`unsupported` 要人换格式、
    `fetch` 要重试、`needs_llm` 要开开关），只留一句错误文本没法自动分流。
    """

    def __init__(self, msg: str, *, kind: str = "convert",
                 retryable: bool = True):
        super().__init__(msg)
        self.kind = kind
        self.retryable = retryable


#: 质量自评低于它的产出**仍然发布**，但打标（`feed list --low-quality` 可筛）。
#: 为什么不直接拒收：转译质量是**连续谱**（PDF 表格失真 60% 也还是比没有强），
#: 硬拒会让"部分可用"的素材直接进不来；而静默发布又会让失真混进知识库。
#: 折中是"发布 + 可见地打标"，把判断权留给人（§7.5）。
QUALITY_WARN = 0.5


@dataclass
class FeedDocument:
    """转译层的统一交付物。**所有转换器的输出都必须是它。**

    字段设计对齐两条硬约束：
      · `doc_time` 是**文档自身时间**（发布/修改），不是投喂时刻 —— 召回侧的时间衰减
        按"知识产生时间"算（与 `distill._origin_time` 同一语义），填错会让衰减全错；
      · `quality` + `warnings` 是**质量自评**：既是给人看的指标，也是"要不要升级到
        LLM 兜底"的**触发器**（见 `docs/feed-adapter-plan.md` §7.0.1）。
    """

    doc_id: str                 # 内容锚：sha256(原件字节) 前 16 位
    title: str
    markdown: str               # **最终 md**（统一交付物）
    kind: str                   # text | url | wechat | pdf | image
    source_ref: str             # 原始路径或 URL（人可读的"从哪来"）
    doc_time: int               # 文档自身时间（epoch 秒）
    mirror: dict = field(default_factory=dict)   # 镜像文件名 → 相对路径
    converter: str = ""         # `名字@版本`（重转判据）
    quality: float = 1.0        # 0~1 自评
    warnings: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.markdown or "").strip():
            # 空正文绝不能进知识库：它会变成"标题看着正当、内容却是空"的召回单元
            # （`wiki_index.project_pages` 里也有同一道闸，两处都拦）
            raise FeedError("转译结果正文为空", kind="empty", retryable=False)
        self.quality = max(0.0, min(1.0, float(self.quality)))
        self.warnings = [str(w) for w in (self.warnings or [])]

    @property
    def low_quality(self) -> bool:
        return self.quality < QUALITY_WARN or bool(self.warnings)


class BaseConverter:
    """转换器基类：只提供 `stamp`（`名字@版本`）。

    为什么要有它而不是只留 Protocol：`Converter` 是**结构化协议**（用于类型说明与
    `isinstance` 检查），而 Protocol 里的默认实现**不会被子类继承** ——
    新转换器只实现 `can_handle`/`convert` 时拿不到 `stamp`（实测踩到
    `AttributeError: 'TextConverter' object has no attribute 'stamp'`）。
    所以把公共实现放在真基类里，转换器继承它，协议继续负责"必须有哪些方法"。
    """

    #: 转换器名（也进 `FeedDocument.converter`）
    name: str = ""
    #: 版本号：**换实现就要 +1** —— 它落库后是"哪些素材该重转"的判据
    version: str = "0"

    @property
    def stamp(self) -> str:
        return "%s@%s" % (self.name, self.version)


@runtime_checkable
class Converter(Protocol):
    """转换器协议。**认领在前、转换在后**，两者分离便于"未知类型显式报错"。"""

    #: 转换器名（也进 `FeedDocument.converter`）
    name: str
    #: 版本号：**换实现就要 +1** —— 它落库后是"哪些素材该重转"的判据
    version: str

    def can_handle(self, *, ref: str, kind: str = "",
                   data: bytes | None = None) -> bool:
        """能不能处理这个输入。按注册顺序询问，先认领者胜。"""
        ...

    def convert(self, *, ref: str, data: bytes, meta: dict) -> FeedDocument:
        """转译。失败抛 `FeedError`（带 kind / retryable）。"""
        ...

    @property
    def stamp(self) -> str:
        """`名字@版本` —— 落进 `materials.converter`。"""
        ...
