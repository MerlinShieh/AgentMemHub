"""R1 进程级 embedder 单例：默认构造路径必须复用缓存（模型百 MB 级，禁止重建）。"""
from __future__ import annotations

import logging

import pytest

from agentmemhub.rag.runtime import get_embedder, get_active_embedder, reset_embedders

_qlog = logging.getLogger("asrag.test.rt")
_qlog.addHandler(logging.NullHandler())


@pytest.fixture(autouse=True)
def _clean_cache():
    reset_embedders()
    yield
    reset_embedders()


def test_same_spec_returns_same_instance(project_settings):
    spec = project_settings.active_spec
    a = get_embedder(spec)
    b = get_embedder(spec, batch_size=a.batch_size)
    assert a is b
    assert get_active_embedder(project_settings) is a


def test_batch_size_partitions_cache(project_settings):
    spec = project_settings.active_spec
    assert get_embedder(spec, batch_size=8) is not get_embedder(spec, batch_size=64)


def test_ingest_default_uses_singleton(project_settings,
                                       fixture_source_db, tmp_path):
    """run_ingest 不传 embedder 时走缓存：两次调用的单例必须是同一实例。"""
    from agentmemhub.rag import ingest as ing
    from agentmemhub.rag import runtime

    captured = []
    orig = runtime.get_embedder

    def spy(spec, **kw):
        emb = orig(spec, **kw)
        captured.append(emb)
        return emb

    ing.get_embedder = spy
    try:
        ing.run_ingest(project_settings, source_db=fixture_source_db,
                       index_db=tmp_path / "a.db", batch_size=4, log=_qlog,
                       limit=4)
        ing.run_ingest(project_settings, source_db=fixture_source_db,
                       index_db=tmp_path / "b.db", batch_size=4, log=_qlog,
                       limit=4)
    finally:
        ing.get_embedder = orig
    assert len(captured) == 2 and captured[0] is captured[1]
