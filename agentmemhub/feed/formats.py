# -*- coding: utf-8 -*-
"""投喂格式白名单的**单点**：哪些格式允许进来。

用户要求（2026-10-15）："以不同格式文件后缀进行控制数据输入（大小写不强制），
后续也可以通过调整不同的文档格式参数来控制哪些数据支持投喂"。

三条设计约束
============
1. **后缀驱动、大小写不敏感** —— `.PDF` 与 `.pdf` 等价。Windows 上文件名大小写本来就乱
   （`Report.PDF` 与 `report.pdf` 都常见），强制大小写只会制造无谓的失败；
2. **默认跟随转换器注册表** —— 不配就等于"当前支持的全部格式"。加一个转换器就自动支持，
   不必回来改白名单：**两处清单必然漂移**，而"明明支持却投不进来"是最难查的那类问题；
3. **运行时覆盖与 yaml 分开** —— 看板要能直接点选（像 `wiki_triggers` 那样写状态文件），
   但**不该反过来去改用户的 yaml 文件**。

读取优先级：**运行时覆盖 > yaml `feed.accept.exts` > 注册表全集**。

无后缀怎么办
==========
**放行，交给认领机制**。白名单的语义是"控制文件类型"，而 URL（`https://…/s/xxx`）
与工具导出的无扩展名报告本来就没有后缀可查；硬拦会把它们全挡在门外。
认领阶段仍会按内容/字节判型，判不出来照旧显式报错（不静默跳过）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable

from .base import FeedError

#: 运行时覆盖的落盘位置（相对程序根）—— 与 `wiki_triggers` 的状态文件同风格
_STATE_REL = "logs/feed_formats.json"


def _state_path() -> Path:
    """状态文件位置。测试可用 `AGENTMEMHUB_FEED_FORMATS_STATE` 重定向。

    ⚠️ 必须有这个逃生口：状态文件是**真实路径**，测试若直接写它会污染用户配置
    （本项目"测试静默改真实产物"的事故已经有过三次，见 AGENTS.md）。
    """
    override = os.environ.get("AGENTMEMHUB_FEED_FORMATS_STATE")
    if override:
        return Path(override)
    return Path("logs") / "feed_formats.json"


def _norm(ext: str) -> str:
    """后缀规范化：保证前导点、小写、去空白。大小写不敏感就靠这一处。"""
    e = str(ext or "").strip().lower()
    if not e:
        return ""
    return e if e.startswith(".") else "." + e


def supported_exts() -> list[str]:
    """**转换器注册表**当前能认领的全部后缀（排序去重）。

    这是"默认值"的来源 —— 与转换器是同一份事实，不需要人工同步。

    ⚠️ 取的是转换器类的 **`EXTS` 类属性**（不是模块级同名常量）。
    早先这里只 `getattr` 实例，而各转换器的 `EXTS` 是**模块级**常量
    ⇒ 收集出来恒为空、白名单退化成"什么都不许投"（写测试时立刻撞上）。
    所以每个转换器类必须显式声明 `EXTS`。
    """
    from . import all_converters

    out: set[str] = set()
    for conv in all_converters():
        for attr in ("EXTS", "LEGACY_EXTS"):
            for e in (getattr(conv, attr, ()) or ()):
                n = _norm(e)
                if n:
                    out.add(n)
    return sorted(out)


def _configured_exts(config=None) -> list[str] | None:
    """yaml 里配的白名单。**返回 `None` 表示没配**（= 跟随注册表）。

    为什么"没配"和"空清单"要归一成同一件事：`DEFAULT_FEED["accept"]["exts"]` 本身就是
    空列表，如果空列表算"显式禁止一切"，那么**所有不配这个键的用户**都会被默认禁止
    投喂任何文件（实测第一次跑就撞上：`source=config`、`allowed=[]`）。
    想临时禁用某格式，就在清单里**不列它** —— 不需要"空 = 全禁"这个语义。
    """
    try:
        if config is None:
            from agentmemhub.config import config as _cfg
            config = _cfg()
        feed_cfg = getattr(config, "feed", None)
        if callable(feed_cfg):
            feed_cfg = feed_cfg()
        accept = (feed_cfg or {}).get("accept") or {}
        exts = accept.get("exts")
        if not exts:
            return None
        return [_norm(e) for e in exts if _norm(e)]
    except Exception:                        # noqa: BLE001 —— 配置读不到就用默认
        return None


def _overrides() -> list[str] | None:
    """运行时覆盖（看板写的状态文件）。没写过 / 空清单都返回 `None`（见上）。"""
    p = _state_path()
    try:
        if not p.is_file():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        exts = (data or {}).get("exts")
        if not exts:
            return None
        return [_norm(e) for e in exts if _norm(e)]
    except Exception:                        # noqa: BLE001 —— 状态文件坏了不该让投喂瘫掉
        return None


def allowed_exts(config=None) -> list[str]:
    """最终生效的白名单：运行时覆盖 > yaml > 注册表全集。"""
    for source in (_overrides(), _configured_exts(config)):
        if source is not None:
            return sorted(set(source))
    return supported_exts()


def source_of(config=None) -> str:
    """当前白名单来自哪里 —— 看板要能显示"这是谁定的"。"""
    if _overrides() is not None:
        return "override"
    if _configured_exts(config) is not None:
        return "config"
    return "registry"


def is_allowed(name_or_ref: str, *, config=None) -> bool:
    """这个路径 / URL **的后缀**是否被允许。无后缀一律放行（见模块 docstring）。"""
    ext = _norm(Path(str(name_or_ref)).suffix)
    if not ext:
        return True
    return ext in set(allowed_exts(config))


def check(name_or_ref: str, *, config=None) -> None:
    """不在白名单就抛 `FeedError`（`kind=unsupported_format`，不可重试）。

    错误信息必须**带上当前允许的清单** —— 否则用户只知道"进不来"，
    还得自己去翻配置，这是最容易让人放弃的那一步。
    """
    if is_allowed(name_or_ref, config=config):
        return
    allowed = allowed_exts(config)
    raise FeedError(
        "不支持的格式：%s（当前允许：%s）—— 如需放开，改配置 `feed.accept.exts`，"
        "或调 `PUT /api/feed/formats`"
        % (Path(str(name_or_ref)).suffix or "（无后缀）",
           "、".join(allowed) if allowed else "（空清单，已被显式禁止）"),
        kind="unsupported_format", retryable=False)


def set_overrides(exts: Iterable[str] | None) -> dict[str, Any]:
    """设置运行时白名单（看板调用）。传 `None` 或空 = 清除覆盖。返回最新状态。"""
    p = _state_path()
    if exts is None:
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        return state()
    clean = sorted({_norm(e) for e in exts if _norm(e)})
    if not clean:
        # 空清单是"禁止一切"这个**危险动作**：必须显式传空列表才生效，
        # 而 `None` 是清除覆盖 —— 两者语义不同，不能混。
        clean = []
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"exts": clean}, ensure_ascii=False, indent=2),
                 encoding="utf-8")
    return state()


def clear_overrides() -> dict[str, Any]:
    return set_overrides(None)


def state(config=None) -> dict[str, Any]:
    """给接口/看板用的完整视图：生效清单 + 来源 + 三层的原始值。"""
    return {
        "allowed": allowed_exts(config),
        "source": source_of(config),
        "override": _overrides(),
        "configured": _configured_exts(config),
        "supported": supported_exts(),
        "stateFile": str(_state_path()),
    }
