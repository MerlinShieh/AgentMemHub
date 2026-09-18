# -*- coding: utf-8 -*-
"""LLM 格式修复链路测试。

背景：便宜的主模型偶发用 ```json 围栏包裹输出、或输出被截断，反复重试也修不好，
但**那份内容本身是有价值的**。修复模型（`repair_model`）负责把它转成规范 JSON。

这里锁住三件事：
  1. 没配 `repair_model` 时行为**完全不变**（仍抛 ValueError）
  2. 配了且修复成功 → 返回修复结果，且第二次调用确实换成了修复模型
  3. 修复失败 → **仍按原语义抛错**（修复不是"掩盖失败"，
     否则调用方会把"没修好"当成"成功了"）
"""
from __future__ import annotations

import pytest

from agentmemhub.llm import LLMClient, LLMConfig


def _resp(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


def _cfg(**over) -> LLMConfig:
    d = {"endpoint": "https://x/y", "api_key": "k", "model": "main",
         "max_retries": 0}
    d.update(over)
    return LLMConfig.from_dict(d)


# ---------------------------------------------------------------------------
# 默认行为不能变
# ---------------------------------------------------------------------------

def test_未配修复模型时行为不变(monkeypatch):
    """最关键的一条：默认必须与改动前完全一致。

    注意样例要用**真的会让 extract_json 失败**的内容：
    ```json 围栏它本来就能剥掉，真正修不好的是**被截断**的输出。
    """
    c = LLMClient(_cfg())
    monkeypatch.setattr(c, "_post",
                        lambda body, timeout: _resp('{"a": 1, "b":'))
    assert c.cfg.repair_model == ""
    with pytest.raises(ValueError):
        c.complete_json("s", "u")


def test_修复模型配置能解析():
    assert LLMClient(_cfg(repair_model="strong")).cfg.repair_model == "strong"


# ---------------------------------------------------------------------------
# 修复成功路径
# ---------------------------------------------------------------------------

def test_修复成功则返回修复结果(monkeypatch):
    c = LLMClient(_cfg(repair_model="strong"))
    calls: list[str] = []

    def fake_post(body, timeout):
        calls.append(body["model"])
        if body["model"] == "main":
            return _resp('{"a": 1, "b":')       # 截断 → 解析失败
        return _resp('{"a": 1}')                 # 修复模型给出干净 JSON

    monkeypatch.setattr(c, "_post", fake_post)
    assert c.complete_json("s", "u") == {"a": 1}
    assert calls == ["main", "strong"], "第二次必须换成修复模型"


def test_修复时把任务要求一并带给修复模型(monkeypatch):
    """修复器要知道"该转成什么形状"，否则只能自由发挥。"""
    c = LLMClient(_cfg(repair_model="strong"))
    seen: dict = {}

    def fake_post(body, timeout):
        if body["model"] == "strong":
            seen["user"] = body["messages"][1]["content"]
            return _resp('{"ok": 1}')
        return _resp("not json")

    monkeypatch.setattr(c, "_post", fake_post)
    c.complete_json("系统提示词内容", "用户输入内容", max_tokens=100)
    assert "系统提示词内容" in seen["user"]
    assert "用户输入内容" in seen["user"]


def test_修复模型用低温保证可复现(monkeypatch):
    c = LLMClient(_cfg(repair_model="strong"))
    seen: dict = {}

    def fake_post(body, timeout):
        if body["model"] == "strong":
            seen["temperature"] = body["temperature"]
            return _resp('{"ok": 1}')
        return _resp("not json")

    monkeypatch.setattr(c, "_post", fake_post)
    c.complete_json("s", "u")
    assert seen["temperature"] == 0.0


# ---------------------------------------------------------------------------
# 修复失败不能改变语义
# ---------------------------------------------------------------------------

def test_修复也失败则仍抛错(monkeypatch):
    c = LLMClient(_cfg(repair_model="strong"))
    monkeypatch.setattr(c, "_post",
                        lambda body, timeout: _resp("还是不是 JSON"))
    with pytest.raises(ValueError):
        c.complete_json("s", "u")


def test_修复调用自身抛异常不影响原语义(monkeypatch):
    c = LLMClient(_cfg(repair_model="strong"))

    def fake_post(body, timeout):
        if body["model"] == "strong":
            raise RuntimeError("修复模型炸了")
        return _resp('{"a": 1, "b":')      # 截断，确保会走到修复分支

    monkeypatch.setattr(c, "_post", fake_post)
    with pytest.raises(ValueError):
        c.complete_json("s", "u")


def test_空内容不触发修复(monkeypatch):
    """没有内容可修，不该白跑一次付费调用。"""
    c = LLMClient(_cfg(repair_model="strong"))
    calls: list[str] = []

    def fake_post(body, timeout):
        calls.append(body["model"])
        return _resp("")

    monkeypatch.setattr(c, "_post", fake_post)
    with pytest.raises(ValueError):
        c.complete_json("s", "u")
    assert calls == ["main"]


def test_正常输出不触发修复(monkeypatch):
    c = LLMClient(_cfg(repair_model="strong"))
    calls: list[str] = []

    def fake_post(body, timeout):
        calls.append(body["model"])
        return _resp('{"ok": 1}')

    monkeypatch.setattr(c, "_post", fake_post)
    assert c.complete_json("s", "u") == {"ok": 1}
    assert calls == ["main"], "一次就成功不该多花钱"


# ---------------------------------------------------------------------------
# 提示词约束
# ---------------------------------------------------------------------------

def test_修复提示词禁止编造内容():
    """修复只该"转格式"，不该自己造内容 —— 那会污染知识库。"""
    assert "不要自己编造" in LLMClient._REPAIR_SYSTEM
