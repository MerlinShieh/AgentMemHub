"""P2 外置接口（接线契约层）：引擎本体零 LLM、零评分依赖，能力全部由调用方注入。

设计对齐 MemOS 机制分析（docs/recall-roadmap.md）：
- Judge：召回后的"终审"阶段。LLMJudge 走调用方注入的后端协议；
  fail-closed —— 后端异常/输出畸形时退回机械安全截断（safe_cutoff），绝不放大故障。
- ValueProvider：记忆价值分的"读侧 join 钩子"。评分归 AgentMemHub 写策略层，
  本引擎只按 src 无关的 unit_id 读取 {id: value}，做 ≤0.3 有界 boost +
  30 天半衰期衰减 + value<=0 过滤（include_low_value 可放开，对应 MemOS
  priority>0 硬闸门 + tie-breaker 定位）。

接线示例（AgentMemHub 读侧）：
    judge = LLMFinalJudge(backend=my_llm_backend, max_keep=5)
    vp    = DictValueProvider(memos_load_values(unit_ids))
    hits  = hybrid_search(settings, q, judge=judge, value_provider=vp,
                          exclude_session=(source, conv_id))
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, Sequence

if TYPE_CHECKING:  # 避免 search ↔ ext 运行时循环导入
    from .search import Hit

# ── Judge：召回终审 ─────────────────────────────────────────────────────


class Judge(Protocol):
    def filter(self, query: str, hits: Sequence["Hit"]) -> list["Hit"]: ...


class JudgeBackend(Protocol):
    """调用方实现的 LLM 后端。返回 {"keep": [降序序号...], "sufficient": bool}。"""

    def complete_json(self, prompt: str) -> dict: ...


def safe_cutoff(hits: Sequence["Hit"], *, max_keep: int = 5,
                floor_ratio: float = 0.7) -> list["Hit"]:
    """机械安全截断：score ≥ floor_ratio×top 且 ≤max_keep，至少保 1 条。
    这是 fail-closed 的兜底路径，也是无 LLM 时的确定性终审。"""
    if not hits:
        return []
    top = hits[0].score
    kept = [h for h in hits[:max_keep] if h.score >= floor_ratio * top]
    return kept or [hits[0]]


@dataclass
class LLMFinalJudge:
    """LLM 精筛+重排（照 MemOS llm-filter 纪律：temperature0、剔除分数防锚定、
    fail-closed 退 safe_cutoff）。后端由调用方注入，引擎不 import 任何 LLM SDK。"""

    backend: JudgeBackend
    max_keep: int = 5
    log: logging.Logger | None = None

    def _prompt(self, query: str, hits: Sequence["Hit"]) -> str:
        # 只给内容语义，刻意剔除时间戳/通道名次/分数（防锚定）
        lines = [f"查询：{query}", "候选记忆："]
        for i, h in enumerate(hits):
            head = f"[{i}] ({h.title or h.conversation_id})"
            lines.append(f"{head} {h.text[:400]}")
        lines.append(
            '与查询相关、值得注入的候选？输出 JSON '
            ' {"keep": [按相关性降序的候选序号], "sufficient": true/false}'
            f"，最多 {self.max_keep} 个。不相关就返回空 keep。")
        return "\n".join(lines)

    def filter(self, query: str, hits: Sequence["Hit"]) -> list["Hit"]:
        if not hits:
            return []
        log = self.log or logging.getLogger("asrag.judge")
        try:
            out = self.backend.complete_json(self._prompt(query, hits))
            keep = out.get("keep")
            if not isinstance(keep, list):
                raise ValueError(f"keep 非列表: {keep!r}")
            picked = []
            for idx in keep:
                i = int(idx)
                if 0 <= i < len(hits) and hits[i] not in picked:
                    picked.append(hits[i])
            result = picked[: self.max_keep]
            log.info("judge llm query=%r in=%d kept=%d sufficient=%s",
                     query, len(hits), len(result), out.get("sufficient"))
            return result
        except Exception as ex:  # fail-closed：退回机械截断，绝不放大故障
            result = safe_cutoff(hits, max_keep=self.max_keep)
            log.warning("judge backend failed (%s: %s) → safe_cutoff kept=%d",
                        type(ex).__name__, ex, len(result))
            return result


# ── ValueProvider：读侧价值 join ────────────────────────────────────────


class ValueProvider(Protocol):
    def values(self, unit_ids: Sequence[int]) -> dict[int, float]: ...


@dataclass
class DictValueProvider:
    """最简实现：预取好的 {unit_id: value}（AgentMemHub 可按 src_id join 后喂入）。"""
    table: dict[int, float]

    def values(self, unit_ids: Sequence[int]) -> dict[int, float]:
        return {i: self.table[i] for i in unit_ids if i in self.table}


HALF_LIFE_DAYS = 30.0
BOOST_CAP = 0.3  # MemOS ranker.ts:449 同源结论：价值只做有界 tie-breaker


def decayed_value(value: float, age_seconds: float,
                  half_life_days: float = HALF_LIFE_DAYS) -> float:
    """V·0.5^(Δt/半衰期)。age 未知（<=0）按不衰减处理（保守给全值）。"""
    if age_seconds <= 0:
        return value
    days = age_seconds / 86400.0
    return value * (0.5 ** (days / half_life_days))


def apply_value_boost(
    relevance: dict[int, float],
    units_meta: dict[int, tuple[float | None, int | None]],
    values: dict[int, float],
    *,
    now: float | None = None,
    weight_cap: float = BOOST_CAP,
    half_life_days: float = HALF_LIFE_DAYS,
    include_low_value: bool = False,
    no_decay: set[int] | None = None,
) -> tuple[dict[int, float], list[int]]:
    """对 {unit_id: relevance} 施加价值偏置。

    units_meta: {id: (time_epoch, None占位) } —— 引擎自带的事件时间戳用于衰减。
    no_decay：用户手动加权的 unit 集合——锁定的价值**不随时间衰减**
    （用户意志优先于新陈代谢）。返回 (新 relevance, 被硬过滤掉的 id)。
    value<=0 默认剔除（对应 MemOS priority>0 闸门；include_low_value=True
    保留，供复盘场景）。
    """
    now = now if now is not None else time.time()
    nd = no_decay or set()
    out: dict[int, float] = {}
    dropped: list[int] = []
    for uid, rel in relevance.items():
        if uid not in values:
            out[uid] = rel
            continue
        v = values[uid]
        if v <= 0 and not include_low_value:
            dropped.append(uid)
            continue
        ts = units_meta.get(uid, (None,))[0]
        age = (now - ts) if ts else 0.0
        eff = max(v, 0.0) if uid in nd else max(
            decayed_value(max(v, 0.0), age, half_life_days), 0.0)
        boost = weight_cap * eff
        out[uid] = rel + min(boost, weight_cap)
    return out, dropped
