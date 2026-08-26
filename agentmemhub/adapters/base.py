"""AgentMemHub Adapter 统一接口。

每个 Agent Harness 一个 adapter，负责把原始数据（SQLite / JSONL / 压缩流）
读取并归一化为统一的会话结构（store.replace_source 可消费的格式）：
    session = {
        "source", "id", "title", "cwd", "created_at", "updated_at",
        "model", "meta", "events": [Event, ...]
    }

设计原则：
- 统一接口：所有 adapter 都实现 AgentAdapter 的 discover/locate/load
- 不同实现：内部读取逻辑完全不同（SQLite / JSONL / zstd）
- 最小可用优先：先通全链路，后续再增强
"""
from __future__ import annotations

import abc
from pathlib import Path
from typing import Any, Optional


class AgentAdapter(abc.ABC):
    """Agent 数据源适配器基类。"""

    #: 来源标识（与 store 的 source 一致）
    source: str = ""
    #: 展示名
    label: str = ""

    @abc.abstractmethod
    def candidate_paths(self) -> list[Path]:
        """返回可能的数据路径（有序，第一个存在的优先）。"""
        raise NotImplementedError

    def locate(self) -> Optional[Path]:
        """返回第一个存在的数据路径；都找不到返回 None。"""
        for p in self.candidate_paths():
            if p.exists():
                return p
        return None

    @abc.abstractmethod
    def load(self, path: Path) -> list[dict[str, Any]]:
        """从给定路径读取并返回会话列表（统一结构，可直接入库）。

        每个元素:
            {"source", "id", "title", "cwd", "created_at", "updated_at",
             "model", "meta", "events": [Event]}
        """
        raise NotImplementedError

    def describe(self) -> dict[str, Any]:
        """返回状态摘要（供 UI / CLI 展示）。"""
        path = self.locate()
        return {
            "source": self.source,
            "label": self.label,
            "located": path is not None,
            "path": str(path) if path else None,
        }
