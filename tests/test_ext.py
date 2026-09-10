"""P2 外置接口契约：Judge（fail-closed）与 ValueProvider（有界 boost/衰减/过滤）。"""
from __future__ import annotations

import logging
import time

import pytest

from asrag.ext import (
    BOOST_CAP,
    DictValueProvider,
    LLMFinalJudge,
    apply_value_boost,
    decayed_value,
    safe_cutoff,
)
from asrag.embedder import OnnxEmbedder
from asrag.search import Hit, hybrid_search

_qlog = logging.getLogger("asrag.test.ext")
_qlog.addHandler(logging.NullHandler())


@pytest.fixture()
def embedder(project_settings):
    return OnnxEmbedder(project_settings.active_spec, batch_size=4, log=_qlog)


def _hit(uid, score):
    return Hit(unit_id=uid, source="s", conversation_id="c", seq=uid,
               role="user", turn_key=None, time=None, title=None,
               text=f"文本{uid}", score=score)


# ── Judge ──────────────────────────────────────────────────────────────

def test_safe_cutoff_rules():
    hits = [_hit(1, 1.0), _hit(2, 0.8), _hit(3, 0.75), _hit(4, 0.3)]
    kept = safe_cutoff(hits, max_keep=5, floor_ratio=0.7)
    assert [h.unit_id for h in kept] == [1, 2, 3], "≥0.7×top 保留，尾差剔除"
    many = [_hit(i, 1.0) for i in range(10)]
    assert len(safe_cutoff(many, max_keep=5)) == 5
    only = safe_cutoff([_hit(1, 1.0), _hit(2, 0.1)])
    assert [h.unit_id for h in only] == [1]
    assert safe_cutoff([]) == []


class _GoodBackend:
    def __init__(self):
        self.seen_prompt = ""

    def complete_json(self, prompt):
        self.seen_prompt = prompt
        return {"keep": [2, 0, 9], "sufficient": True}  # 9 越界应被忽略


def test_llm_judge_reorder_and_cap():
    be = _GoodBackend()
    j = LLMFinalJudge(backend=be, max_keep=5, log=_qlog)
    hits = [_hit(i, 1.0 - i * 0.01) for i in range(5)]
    out = j.filter("查询", hits)
    assert [h.unit_id for h in out] == [2, 0], "按 LLM 给定顺序，越界序号忽略"
    assert "keep" in be.seen_prompt
    # 防锚定：prompt 不得携带分数/名次
    assert "score=" not in be.seen_prompt and "vec=" not in be.seen_prompt


class _BrokenBackend:
    def complete_json(self, prompt):
        raise RuntimeError("网络炸了")


def test_llm_judge_fail_closed():
    """fail-closed：后端异常必须退回机械 safe_cutoff，绝不放大故障。"""
    j = LLMFinalJudge(backend=_BrokenBackend(), max_keep=5, log=_qlog)
    hits = [_hit(1, 1.0), _hit(2, 0.8), _hit(3, 0.2)]
    out = j.filter("q", hits)
    assert [h.unit_id for h in out] == [1, 2]


class _GarbageBackend:
    def complete_json(self, prompt):
        return {"keep": "not-a-list"}


def test_llm_judge_garbage_output_fail_closed():
    j = LLMFinalJudge(backend=_GarbageBackend(), max_keep=5, log=_qlog)
    out = j.filter("q", [_hit(1, 1.0), _hit(2, 0.9)])
    assert len(out) >= 1, "至少保 1 条"


# ── ValueProvider / 价值偏置 ───────────────────────────────────────────

def test_decayed_value_half_life():
    now = time.time()
    assert decayed_value(1.0, 0) == 1.0
    assert decayed_value(1.0, 30 * 86400) == pytest.approx(0.5, rel=1e-6)
    assert decayed_value(1.0, 90 * 86400) == pytest.approx(0.125, rel=1e-6)


def test_apply_value_boost_cap_and_filter():
    rel = {1: 0.8, 2: 0.7, 3: 0.6, 4: 0.5}
    meta = {1: (None,), 2: (None,), 3: (None,), 4: (None,)}
    values = {1: 5.0, 2: 0.4, 3: -1.0}  # 1 超高价值→必须被帽住；3 负值→剔除
    out, dropped = apply_value_boost(rel, meta, values, now=time.time())
    assert dropped == [3], "value<=0 默认硬过滤（priority>0 闸门）"
    assert out[1] == pytest.approx(0.8 + BOOST_CAP), "boost 封顶 ≤0.3"
    assert out[2] == pytest.approx(0.7 + 0.3 * 0.4)
    assert out[4] == 0.5, "无评分候选不受影响"
    out2, dropped2 = apply_value_boost(rel, meta, values,
                                       include_low_value=True, now=time.time())
    assert not dropped2 and 3 in out2, "复盘模式放开负值（repair 入口思想）"


def test_apply_value_decay_uses_unit_time():
    now = 1_800_000_000.0
    old_ts = now - 30 * 86400  # 恰好一个半衰期
    rel = {1: 0.8}
    out, _ = apply_value_boost(rel, {1: (old_ts,)}, {1: 1.0}, now=now)
    assert out[1] == pytest.approx(0.8 + 0.3 * 0.5, rel=1e-6)


# ── 与 hybrid_search 的集成 ────────────────────────────────────────────

def test_hybrid_with_value_provider_drops_low(project_settings, embedder,
                                              fixture_source_db, tmp_path):
    """端到端：provider 判负的单元在 hybrid 结果里消失，include_low_value 找回。"""
    import dataclasses

    from asrag.ingest import open_index, run_ingest

    idx = tmp_path / "rag.db"
    run_ingest(project_settings, embedder=embedder,
               source_db=fixture_source_db, index_db=idx, batch_size=4, log=_qlog)
    settings = dataclasses.replace(project_settings, index_db=idx)
    conn = open_index(idx)
    victim = conn.execute(
        "SELECT id FROM units WHERE text LIKE 'bat 批处理%'").fetchone()[0]
    others = [r[0] for r in conn.execute(
        "SELECT id FROM units WHERE id<>?", (victim,))]
    conn.close()
    # victim 判负、其余给 0（中性）
    table = {victim: -1.0}
    table.update({i: 0.0 for i in others})
    vp = DictValueProvider(table)
    q = "bat 批处理为什么报 was unexpected 错误"
    hits = hybrid_search(settings, q, embedder=embedder, k=7, log=_qlog,
                         value_provider=vp)
    assert victim not in {h.unit_id for h in hits}
    hits2 = hybrid_search(settings, q, embedder=embedder, k=7, log=_qlog,
                          value_provider=vp, include_low_value=True)
    assert victim in {h.unit_id for h in hits2}


def test_hybrid_with_judge_safe_cutoff(project_settings, embedder,
                                       fixture_source_db, tmp_path):
    import dataclasses

    from asrag.ext import safe_cutoff
    from asrag.ingest import run_ingest

    idx = tmp_path / "rag.db"
    run_ingest(project_settings, embedder=embedder,
               source_db=fixture_source_db, index_db=idx, batch_size=4, log=_qlog)
    settings = dataclasses.replace(project_settings, index_db=idx)

    class _J:
        def filter(self, q, hs):
            return safe_cutoff(hs, max_keep=2)

    hits = hybrid_search(settings, "批处理括号报错怎么解决", embedder=embedder,
                         k=7, log=_qlog, judge=_J())
    assert len(hits) <= 2
