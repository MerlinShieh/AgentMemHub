"""OnnxEmbedder 契约测试：golden 向量回归 + 维度/归一化 + 语义区分度 + 漂移守门。

golden 基准生成于 2026-09-09（onnxruntime 1.29.0, q8 量化, CLS pooling + L2），
并与 Node transformers.js 实测交叉核对（cos 0.51/0.35 同量级）。
"""
from __future__ import annotations

import dataclasses
import logging

import numpy as np
import pytest

from asrag.embedder import OnnxEmbedder

GOLDEN_TEXT = "会话向量化的核心是把对话嵌入为向量"
# 首 8 维基准（float32）
GOLDEN_FIRST8 = np.array(
    [-0.013366, 0.046775, 0.030627, -0.005896,
     -0.041308, 0.022837, -0.058459, 0.039048],
    dtype=np.float32,
)
REL = "记忆召回采用向量与全文混合检索"
UNREL = "今天天气不错适合出去散步"

_null = logging.getLogger("asrag.test.null")
_null.addHandler(logging.NullHandler())


@pytest.fixture(scope="module")
def embedder(project_settings):
    return OnnxEmbedder(project_settings.active_spec, log=_null)


def test_golden_vector_regression(embedder):
    v = embedder.encode_query(GOLDEN_TEXT)
    assert v.shape == (embedder.spec.dim,)
    np.testing.assert_allclose(v[:8], GOLDEN_FIRST8, rtol=1e-3, atol=1e-4)


def test_normalized_and_shape(embedder):
    m = embedder.encode_passages([GOLDEN_TEXT, REL, UNREL])
    assert m.shape == (3, embedder.spec.dim)
    assert np.allclose(np.linalg.norm(m, axis=1), 1.0, atol=1e-4)


def test_semantic_discrimination(embedder):
    """同主题对 cos 必须显著高于无关对——量化模型的核心可用性断言。"""
    q = embedder.encode_query(GOLDEN_TEXT)
    m = embedder.encode_passages([REL, UNREL])
    cos_rel, cos_unrel = float(q @ m[0]), float(q @ m[1])
    assert cos_rel > cos_unrel, f"{cos_rel} <= {cos_unrel}"
    assert cos_unrel < 0.45 and cos_rel > 0.4


def test_empty_batch(embedder):
    m = embedder.encode_passages([])
    assert m.shape == (0, embedder.spec.dim)


def test_truncation_guards_huge_input(embedder):
    """2.6M 字符异常消息（源库实测存在）不得炸窗口。"""
    v = embedder.encode_passages(["长" * 3_000_000])
    assert v.shape == (1, embedder.spec.dim)
    assert np.isfinite(v).all()


def test_padding_drift_is_semantically_bounded(embedder):
    """q8 量化特性：per-tensor 激活量化使批量结果随 batch 组成产生微漂移
    （实测 max_abs≈0.016，cos≈0.995）。语义层面必须等价——以此锁定契约。
    注意：位级可复现要求 ingest 固定 batch_size 与稳定排序（见 AGENTS.md 不变量）。"""
    text = "增量摄取按水位推进 src_id 幂等"
    solo = embedder.encode_passages([text])[0]
    padded = embedder.encode_passages([text, "短"])[0]
    assert float(solo @ padded) > 0.99


def test_dim_mismatch_fails_loudly(project_settings):
    """注册表漂移（如 dim 写错/换模型忘更新）必须立刻抛错，禁止静默截断。"""
    bad = dataclasses.replace(project_settings.active_spec, dim=384)
    e = OnnxEmbedder(bad, log=_null)
    with pytest.raises(ValueError, match="注册表声明|漂移"):
        e.encode_passages(["维度守门测试"])


def test_query_prefix_applied(project_settings):
    spec = project_settings.active_spec
    assert spec.query_prefix == "", "bge-zh 默认无前缀；此测试锁定契约"
    prefixed = dataclasses.replace(spec, query_prefix="查询：")
    a = OnnxEmbedder(spec, log=_null).encode_passages(["混合检索"])
    b = OnnxEmbedder(prefixed, log=_null).encode_query("混合检索")
    assert not np.allclose(a, b, atol=1e-5), "前缀必须实际改变输出"
