"""AgentMemHub Adapter 注册表。

统一入口：get_adapter(source) / all_adapters() / load_source(source) / load_all_source()
"""
from __future__ import annotations

from typing import Optional

from .base import AgentAdapter
from .opencode import OpenCodeAdapter
from .zcode import ZCodeAdapter
from .hermes import HermesAdapter
from .qwen import QwenAdapter
from .qodercn import QoderCnAdapter
from .workbuddy import WorkBuddyAdapter
from .dsh import DshAdapter
from .trae import TraeAdapter

#: 全部内置 adapter（按 source 名索引）
ADAPTERS: dict[str, AgentAdapter] = {
    a.source: a for a in (
        ZCodeAdapter(), OpenCodeAdapter(), HermesAdapter(),
        WorkBuddyAdapter(), QwenAdapter(), QoderCnAdapter(), DshAdapter(),
        TraeAdapter(),
    )
}

#: 有身份验证/路径可探测顺序（仅用于展示/自动发现）
DEFAULT_ORDER = ("zcode", "opencode", "hermes", "workbuddy", "qwen", "qodercn", "dsh", "trae")


def get_adapter(source: str) -> Optional[AgentAdapter]:
    """按 source 名取 adapter。"""
    return ADAPTERS.get(source)


def all_adapters() -> list[AgentAdapter]:
    """返回全部 adapter（按 DEFAULT_ORDER）。"""
    return [ADAPTERS[s] for s in DEFAULT_ORDER if s in ADAPTERS]


def load_source(source: str, located: bool = True) -> list[dict]:
    """加载单个 source 的全部会话。只要 adapter 能 locate 到路径就加载。"""
    a = get_adapter(source)
    if a is None:
        return []
    p = a.locate()
    if p is None:
        return []
    return a.load(p)


def load_all(located: bool = True) -> dict[str, list[dict]]:
    """加载所有 adapter 的会话，返回 {source: [sessions]}。"""
    result: dict[str, list[dict]] = {}
    for a in all_adapters():
        try:
            p = a.locate()
            if p is None:
                result[a.source] = []
                continue
            result[a.source] = a.load(p) or []
        except Exception:
            result[a.source] = []
    return result


def describe_all() -> list[dict]:
    """返回所有 adapter 的状态摘要。"""
    return [a.describe() for a in all_adapters()]
