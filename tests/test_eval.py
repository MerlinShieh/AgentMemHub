"""eval 模块测试：yaml 解析、命中判定纯函数、小库端到端报表结构。"""
from __future__ import annotations

import dataclasses
import logging

import pytest

from asrag.embedder import OnnxEmbedder
from asrag.eval import EvalCase, first_hit_rank, format_report, load_cases, run_eval
from asrag.ingest import run_ingest
from asrag.search import Hit

_qlog = logging.getLogger("asrag.test.eval")
_qlog.addHandler(logging.NullHandler())


def _hit(uid, text, title=""):
    return Hit(unit_id=uid, source="s", conversation_id="c", seq=1, role="user",
               turn_key=None, time=None, title=title, text=text, score=1.0)


def test_eval_case_matching():
    case = EvalCase(id="x", query="q", expect_any=("批处理", "括号"))
    assert case.matched(["原因是括号出现在代码块"])
    assert case.matched(["无关文本", "标题含批处理二字"])
    assert not case.matched(["完全无关", ""])


def test_first_hit_rank():
    case = EvalCase(id="x", query="q", expect_any=("括号",))
    hits = [_hit(1, "甲"), _hit(2, "含括号的乙"), _hit(3, "括号丙")]
    assert first_hit_rank(hits, case) == 2
    assert first_hit_rank([_hit(1, "甲")], case) is None


def test_load_cases_and_unique_ids(tmp_path):
    p = tmp_path / "q.yaml"
    p.write_text(
        "cases:\n"
        "  - id: a\n    query: 甲问题\n    expect_any: [甲]\n"
        "  - id: b\n    query: 乙问题\n    expect_any: [乙, 也]\n",
        encoding="utf-8",
    )
    cases = load_cases(p)
    assert len(cases) == 2 and cases[1].expect_any == ("乙", "也")
    p.write_text(
        "cases:\n  - id: a\n    query: x\n    expect_any: [甲]\n"
        "  - id: a\n    query: y\n    expect_any: [乙]\n",
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match="唯一"):
        load_cases(p)


@pytest.fixture()
def mini_rag(project_settings, embedder, fixture_source_db, tmp_path):
    """fixture 源库 → 小索引，供端到端 eval。"""
    idx = tmp_path / "rag.db"
    run_ingest(project_settings, embedder=embedder, source_db=fixture_source_db,
               index_db=idx, batch_size=4, log=_qlog)
    return dataclasses.replace(project_settings, index_db=idx)


@pytest.fixture()
def embedder(project_settings):
    return OnnxEmbedder(project_settings.active_spec, batch_size=4, log=_qlog)


def test_run_eval_report_shape(mini_rag):
    cases = [
        EvalCase(id="bat", query="批处理括号报错怎么解决", expect_any=("括号", "批处理")),
        EvalCase(id="never", query="完全不存在的话题量子藤", expect_any=("不存在的期望词",)),
    ]
    rep = run_eval(mini_rag, cases, k=3, modes=("vector", "fts", "hybrid"),
                   log=_qlog)
    assert rep["cases"] == 2 and rep["k"] == 3
    for mode in ("vector", "fts", "hybrid"):
        m = rep["modes"][mode]
        assert 0.0 <= m["recall@k"] <= 1.0
        assert m["first_hit_ranks"]["never"] is None, "无中生有必须不命中"
        assert m["first_hit_ranks"]["bat"] is not None, "fixture 内话题至少 fts 命中"
    text = format_report(rep)
    assert "recall@k" in text and "hybrid" in text


def test_shipped_queries_yaml_parses(project_settings):
    cases = load_cases(project_settings.root / "eval" / "queries.yaml")
    assert len(cases) >= 15
    assert all(c.query != c.id and c.expect_any for c in cases)
