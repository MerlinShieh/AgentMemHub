# -*- coding: utf-8 -*-
"""产物链接验收（Obsidian CLI 封装）的测试。

只测**纯逻辑与降级**，不依赖 Obsidian 真的在运行 —— 验收是旁路，最关键的性质是
"**取不到 Obsidian 时必须优雅退化，绝不能让产物流程挂掉**"。
"""
from __future__ import annotations

import pytest

from agentmemhub import obsidian


def test_找不到CLI时报可读错误(monkeypatch):
    monkeypatch.delenv("OBSIDIAN_CLI", raising=False)
    monkeypatch.setattr(obsidian.shutil, "which", lambda *a, **k: None)
    with pytest.raises(obsidian.ObsidianUnavailable, match="找不到 Obsidian CLI"):
        obsidian.find_cli()


def test_CLI路径按环境变量解析(monkeypatch):
    monkeypatch.setenv("OBSIDIAN_CLI", r"D:\tools\Obsidian\Obsidian.com")
    assert obsidian.find_cli() == r"D:\tools\Obsidian\Obsidian.com"
    assert obsidian.find_cli("X:/explicit") == "X:/explicit"      # 显式参数最优先


def test_vault名要名字不要路径():
    """CLI 的 `vault=` 要 **vault 名**；传路径会 "Vault not found."（实测踩过）。"""
    assert obsidian.as_vault_name(r"D:\data\a\out_l2_mimo") == "out_l2_mimo"
    assert obsidian.as_vault_name("out_l2_mimo") == "out_l2_mimo"
    assert obsidian.as_vault_name("") == ""


def test_没有未解析链接时CLI返回的是英文句而不是空数组(monkeypatch):
    """**关键边界**：观测到 0 时 CLI 返回 `No unresolved links found.` ——
    当成条目会**误报**（实测：明明已 0，却报"1 个目标 / 0 条"）。"""
    monkeypatch.setattr(obsidian, "run",
                        lambda *a, **k: "No unresolved links found.")
    assert obsidian.unresolved() == []


def test_带BOM的JSON能解析(monkeypatch):
    """CLI 输出带 UTF-8 BOM —— 不剥会解析失败、静默走兜底。"""
    monkeypatch.setattr(
        obsidian, "run",
        lambda *a, **k: '\ufeff[{"link":"某页","count":"3","sources":"a.md"}]')
    got = obsidian.unresolved()
    assert got == [{"link": "某页", "count": 3, "sources": "a.md"}]


def test_验收不可用时不算失败(monkeypatch):
    """Obsidian 没运行 → `available=False`（**不是失败**），流程照常。"""
    monkeypatch.setattr(obsidian, "app_running", lambda *a, **k: False)
    r = obsidian.verify()
    assert r["available"] is False and r["ok"] is False
    assert "未在运行" in r["error"]


def test_验收通过的唯一标准是未解析为0(monkeypatch):
    monkeypatch.setattr(obsidian, "app_running", lambda *a, **k: True)
    monkeypatch.setattr(obsidian, "run",
                        lambda *a, **k: "No unresolved links found.")
    r = obsidian.verify()
    assert r["available"] is True and r["ok"] is True and r["total_targets"] == 0

    monkeypatch.setattr(
        obsidian, "run",
        lambda *a, **k: '[{"link":"老板块","count":"5","sources":"x.md"}]')
    r2 = obsidian.verify()
    assert r2["ok"] is False and r2["total_targets"] == 1 and r2["total_links"] == 5


def test_不自动启动Obsidian(monkeypatch):
    """官方 CLI 在 Obsidian 未运行时会**把它启动起来**（文档明说）——
    自动流程绝不能因此弹出编辑器窗口，所以先探测、没开就跳过。"""
    monkeypatch.setattr(obsidian, "app_running", lambda *a, **k: False)
    called = []
    monkeypatch.setattr(obsidian, "run", lambda *a, **k: called.append(a) or "")
    obsidian.verify()
    assert called == [], "Obsidian 没运行就不该调 CLI"
