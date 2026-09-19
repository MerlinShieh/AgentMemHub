# -*- coding: utf-8 -*-
"""LLM Wiki 服务层测试：失败清单查询 + 定向补跑入口。

这一层的意义是"**补跑不该只能靠人敲脚本**"——CLI / 面板 / 其它程序都走它。
所以测的重点是**接口契约**：

  · 查询返回可序列化的结构化数据（面板直接能吐给前端）
  · 没有失败项时给明确答复，而不是静默返回空
  · `quota` / `auth` 这类致命错误必须被标出来（重试无意义，得先让人处理）
  · 参数不合法时明确拒绝，而不是跑一半才炸
"""
from __future__ import annotations

import pytest

from agentmemhub import wiki
from agentmemhub.failures import FailureLog


def _mk(tmp_path, entries):
    out = tmp_path / "l2"
    out.mkdir(parents=True, exist_ok=True)
    fl = FailureLog(out / "failures.jsonl")
    for e in entries:
        fl.record(**e)
    return out


# ---------------------------------------------------------------------------
# 查：failed_summary
# ---------------------------------------------------------------------------

def test_摘要_没有清单时给出明确标记(tmp_path):
    s = wiki.failures_summary(tmp_path / "nope")
    assert s["exists"] is False
    assert s["total"] == 0
    assert s["needs_manual"] is False


def test_摘要_按原因分类(tmp_path):
    out = _mk(tmp_path, [
        {"stage": "l1", "target": "qwen/a", "error": "TimeoutError: timed out"},
        {"stage": "l1", "target": "qwen/b", "error": "模型返回空内容"},
        {"stage": "l2-compile", "target": "域X", "error": "无法从模型输出解析 JSON"},
    ])
    s = wiki.failures_summary(out)
    assert s["total"] == 3
    assert s["by_kind"]["transient"] == 1
    assert s["by_kind"]["format"] == 2


def test_摘要_致命错误被标出并置needs_manual(tmp_path):
    out = _mk(tmp_path, [
        {"stage": "l1", "target": "a", "error": "HTTP 402 insufficient balance"},
        {"stage": "l1", "target": "b", "error": "timeout"},
    ])
    s = wiki.failures_summary(out)
    assert s["needs_manual"] is True
    assert "quota" in s["fatal"]


def test_摘要_按阶段计数(tmp_path):
    out = _mk(tmp_path, [
        {"stage": "l1", "target": "a", "error": "timeout"},
        {"stage": "l1", "target": "b", "error": "timeout"},
        {"stage": "l2-plan", "target": "域A", "error": "timeout"},
    ])
    s = wiki.failures_summary(out)
    assert s["stages"]["l1"] == 2
    assert s["stages"]["l2-plan"] == 1
    assert s["stages"]["l2-compile"] == 0


def test_摘要_可JSON序列化(tmp_path):
    """面板要直接把它吐给前端，不能有不可序列化的东西。"""
    import json
    out = _mk(tmp_path, [{"stage": "l1", "target": "a", "error": "timeout"}])
    json.dumps(wiki.failures_summary(out), ensure_ascii=False)


def test_摘要_可按阶段过滤(tmp_path):
    out = _mk(tmp_path, [
        {"stage": "l1", "target": "a", "error": "timeout"},
        {"stage": "l2-compile", "target": "域A", "error": "timeout"},
    ])
    assert wiki.failures_summary(out, "l1")["total"] == 1


def test_已解决的失败不计入摘要(tmp_path):
    out = _mk(tmp_path, [{"stage": "l1", "target": "a", "error": "timeout"}])
    FailureLog(out / "failures.jsonl").resolve("l1", "a")
    assert wiki.failures_summary(out)["total"] == 0


# ---------------------------------------------------------------------------
# 查：retry_targets
# ---------------------------------------------------------------------------

def test_重跑目标去重(tmp_path):
    out = _mk(tmp_path, [
        {"stage": "l1", "target": "qwen/a", "error": "timeout"},
        {"stage": "l1", "target": "qwen/a", "error": "timeout"},
        {"stage": "l1", "target": "zcode/b", "error": "timeout"},
    ])
    assert wiki.retry_targets(out, "l1") == ["qwen/a", "zcode/b"]


# ---------------------------------------------------------------------------
# 做：retry_failed（不真跑 LLM —— 只验"没事可做"与参数校验两条早返回路径）
# ---------------------------------------------------------------------------

def test_补跑_没有失败项时明确返回(tmp_path):
    out = _mk(tmp_path, [])
    r = wiki.retry_failed(stage="l1", out_dir=out)
    assert r["retried"] == 0
    assert "没有待重跑" in r["message"]


def test_补跑_第二级缺src时明确拒绝(tmp_path):
    out = _mk(tmp_path, [
        {"stage": "l2-compile", "target": "域A", "error": "timeout"},
    ])
    r = wiki.retry_failed(stage="l2", out_dir=out, src="")
    assert r["retried"] == 0
    assert "src" in r["message"]


def test_补跑_第二级没有失败项时明确返回(tmp_path):
    out = _mk(tmp_path, [])
    r = wiki.retry_failed(stage="l2", out_dir=out, src=str(tmp_path))
    assert r["retried"] == 0
    assert "没有待重跑" in r["message"]


def test_补跑全部_没有失败项时给出说明(tmp_path):
    out = _mk(tmp_path, [])
    r = wiki.retry_all(out_dir=out, src=str(tmp_path))
    assert "没有待重跑的失败项" in r.get("message", "")


def test_阶段标签覆盖脚本实际写入的三种(tmp_path):
    """标签必须与脚本写的 stage 名一一对应，否则摘要里会漏项。"""
    assert set(wiki.STAGES) == {"l1", "l2-plan", "l2-compile"}
