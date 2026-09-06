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
        """返回数据路径。

        优先统一配置 agents.<source> 的显式覆盖（未配置/不存在则回退官方默认探测）。
        """
        try:
            from agentmemhub import config
            override = config.config().agent_path(self.source)
        except Exception:
            override = None
        if override is not None and override.exists():
            return override
        for p in self.candidate_paths():
            if p.exists():
                return p
        return None

    @abc.abstractmethod
    def load(self, path: Path, only_ids: Optional[set[str]] = None) -> list[dict[str, Any]]:
        """从给定路径读取并返回会话列表（统一结构，可直接入库）。

        每个元素:
            {"source", "id", "title", "cwd", "created_at", "updated_at",
             "model", "meta", "events": [Event]}

        only_ids：增量提示——只返回这些 id 的会话。能廉价预过滤的实现应遵守
        （把过滤下推到事件读取之前）；无法可靠预过滤的实现可以忽略该参数
        返回超集，调用方按 updated_at 对比幂等兜底。
        """
        raise NotImplementedError

    def list_sessions(self, path: Path) -> Optional[list[dict[str, Any]]]:
        """轻量会话清单（不读事件正文）：[{"id", "updated_at"}, ...]。

        供会话级增量对比（与库内 updated_at 比较，只重读变化的会话）。
        返回 None 表示该源没有廉价的清单能力 → ingest 整源重扫（等同全量，
        行为不变）。默认 None，子类按数据源能力覆写。
        """
        return None

    def source_freshness(self, path: Path) -> Optional[float]:
        """整源新鲜度信号（Unix 秒）；None = 无此信号。

        兜底「会话清单看不见的变更」：如 workbuddy 审计日志追加只改文件
        mtime，不体现在 sessions 表。信号时间 > 上次同步水位 → 整源重扫
        （force 模式 upsert，忽略 updated_at 对比）。
        """
        return None

    def describe(self) -> dict[str, Any]:
        """返回状态摘要（供 UI / CLI 展示）。"""
        path = self.locate()
        return {
            "source": self.source,
            "label": self.label,
            "located": path is not None,
            "path": str(path) if path else None,
        }
