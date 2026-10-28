# -*- coding: utf-8 -*-
"""失败清单测试：分类、累积、汇总、单独重跑的目标提取。

这套机制的意义是"长任务跑挂之后能补救"——所以重点测三件事：
  1. 错误归类准不准（quota / auth 必须被识别成致命类）
  2. 跨次运行能不能累积（否则补跑一次就把历史失败冲掉了）
  3. 致命错误要在汇总里醒目提示（重试无意义，得让人先解决）
"""
from __future__ import annotations

import json

from agentmemhub.failures import FailureLog, classify


# ---------------------------------------------------------------------------
# 分类
# ---------------------------------------------------------------------------

def test_classify_余额与额度():
    assert classify("HTTP 402: insufficient balance") == "quota"
    assert classify("您当前的余额不足") == "quota"
    assert classify("You exceeded your current quota") == "quota"


def test_classify_鉴权():
    assert classify("HTTP 401 Unauthorized") == "auth"
    assert classify("invalid_api_key") == "auth"
    assert classify("HTTP 403 forbidden") == "auth"


def test_classify_模型不可用():
    assert classify('unsupported_model: "deepseek-flash"') == "model"
    assert classify("model not found") == "model"


def test_classify_限流与瞬态():
    assert classify("HTTP 429 too many requests") == "rate"
    assert classify("HTTP 520 upstream temporarily unavailable") == "transient"
    assert classify("TimeoutError: The read operation timed out") == "transient"
    assert classify("URLError: [WinError 10060]") == "transient"


def test_classify_格式问题():
    assert classify("ValueError: 无法从模型输出解析 JSON：'```json") == "format"
    assert classify("模型返回空内容") == "format"


def test_classify_未知不乱猜():
    assert classify("SomethingWeirdHappened") == "unknown"
    assert classify("") == "unknown"
    assert classify(None) == "unknown"


# ---------------------------------------------------------------------------
# 记录与累积
# ---------------------------------------------------------------------------

def test_记录与读取(tmp_path):
    fl = FailureLog(tmp_path / "f.jsonl")
    fl.record(stage="l1", target="qwen/abc", error="HTTP 520")
    fl.record(stage="l1", target="zcode/def", error="模型返回空内容")
    assert len(fl.entries()) == 2
    assert [e["kind"] for e in fl.entries()] == ["transient", "format"]


def test_跨次运行累积(tmp_path):
    """补跑一轮后，历史的失败不能丢 —— 否则汇总永远只看得到最后一次。"""
    p = tmp_path / "f.jsonl"
    FailureLog(p).record(stage="l1", target="a", error="timeout")
    fl2 = FailureLog(p)                       # 模拟进程重启
    fl2.record(stage="l1", target="b", error="timeout")
    assert len(fl2.entries()) == 2


def test_坏行不影响读取(tmp_path):
    p = tmp_path / "f.jsonl"
    fl = FailureLog(p)
    fl.record(stage="l1", target="a", error="timeout")
    with open(p, "a", encoding="utf-8") as f:
        f.write("这不是 json\n")
    assert len(FailureLog(p).entries()) == 1


def test_写失败不影响主流程(tmp_path):
    """清单写不进去（如目录不可写）也绝不能抛给调用方。"""
    fl = FailureLog(tmp_path / "nodir" / "deep" / "f.jsonl")
    fl.record(stage="l1", target="a", error="x")   # 正常会建目录；不抛即通过


# ---------------------------------------------------------------------------
# 重跑目标
# ---------------------------------------------------------------------------

def test_重跑目标去重且按阶段过滤(tmp_path):
    fl = FailureLog(tmp_path / "f.jsonl")
    fl.record(stage="l1", target="qwen/a", error="timeout")
    fl.record(stage="l1", target="qwen/a", error="timeout")   # 重复
    fl.record(stage="l1", target="zcode/b", error="timeout")
    fl.record(stage="l2", target="domain-x", error="timeout")
    assert fl.targets("l1") == ["qwen/a", "zcode/b"]
    assert fl.targets("l2") == ["domain-x"]


def test_resolve_后不再进重跑列表(tmp_path):
    fl = FailureLog(tmp_path / "f.jsonl")
    fl.record(stage="l1", target="qwen/a", error="timeout")
    assert fl.targets("l1") == ["qwen/a"]
    assert fl.resolve("l1", "qwen/a") == 1
    assert fl.targets("l1") == []


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------

def test_汇总按原因分组(tmp_path):
    fl = FailureLog(tmp_path / "f.jsonl")
    for t in ("a", "b", "c"):
        fl.record(stage="l1", target=t, error="TimeoutError: timed out")
    fl.record(stage="l1", target="d", error="无法从模型输出解析 JSON")
    s = fl.summary("l1")
    assert "transient" in s and "format" in s
    assert "4 条" in s


def test_汇总对致命错误醒目提示(tmp_path):
    fl = FailureLog(tmp_path / "f.jsonl")
    fl.record(stage="l1", target="a", error="HTTP 402 insufficient balance")
    s = fl.summary("l1")
    assert "余额" in s, "余额不足必须提示用户去充值"
    assert "需人工处理" in s


def test_汇总全清时给正面反馈(tmp_path):
    fl = FailureLog(tmp_path / "f.jsonl")
    assert "没有未解决的失败项" in fl.summary("l1")


def test_已解决的失败不进汇总(tmp_path):
    fl = FailureLog(tmp_path / "f.jsonl")
    fl.record(stage="l1", target="a", error="timeout")
    fl.resolve("l1", "a")
    assert "没有未解决的失败项" in fl.summary("l1")


def test_汇总会给出清单路径与重跑提示(tmp_path):
    fl = FailureLog(tmp_path / "f.jsonl")
    fl.record(stage="l1", target="a", error="timeout")
    s = fl.summary("l1")
    assert str(fl.path) in s, "要给出清单文件的真实路径，用户才知道去哪看"
    assert "--retry-failed" in s, "要告诉用户怎么单独重跑，而不是全量重来"
