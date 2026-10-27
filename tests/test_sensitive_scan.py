"""`scripts/sensitive_scan.py` 是推送前的最后一道闸 —— 闸门本身必须有测试。

为什么值得单独测：它失效的方式全是「静默放行」，而它又是唯一会在 `git push`
之前真的拦住东西的环节。规则改错不会让任何功能报错，只会在某次推送里把本机
路径或真实凭据带出去。这里只测「规则集合长什么样」，不测扫描算法（那是
`_scan` 的机械循环，出错会立刻看见）。
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sensitive_scan.py"


def _load():
    """按文件路径加载脚本（`scripts/` 不是包，不能 import）。"""
    spec = importlib.util.spec_from_file_location("sensitive_scan_ut", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_两字符用户名不生成致命规则(monkeypatch, capsys):
    """2 字符 ASCII 用户名不能生成致命规则，但必须告警。

    `\\bLi\\b` 会命中 `label` / `lib` 这类子串，裸字面量更糟 —— 闸门恒红
    等于没人看。而静默跳过等于这条规则根本不存在，所以要走告警分支。
    """
    mod = _load()
    # 拆开拼，别在源码里留下完整的「盘符 + Users + 用户名」字面量 ——
    # 这条测试本身会被 `sensitive_scan.py` 扫到，样本写全了闸门就恒红。
    short_user = "lj"                          # 两字符，用来走"过短"分支
    monkeypatch.setenv("USERNAME", short_user)
    monkeypatch.setenv("USER", short_user)
    monkeypatch.setenv("USERPROFILE", "C:/Users/" + short_user)
    pats = mod._username_patterns()
    assert not any("lj" in k for k in pats), pats
    assert "过短" in capsys.readouterr().err


def test_工作区根路径也算致命():
    """项目根的上一级（本机工作区根）比项目根短，根规则匹配不到它 —— 曾是漏网。"""
    mod = _load()
    pats = mod._local_path_patterns()
    workspace = str(Path(_SCRIPT).resolve().parents[1].parent)
    # 规则是 `re.escape(路径)` 的结果，所以比较时也要 escape 一次
    expected = re.escape(workspace).lower()
    assert any(p.pattern.lower() == expected for p in pats.values()), pats


def test_占位符不误报真令牌要报():
    """`sk-test-placeholder` 这类夹具必须放过，真形态必须抓到。"""
    mod = _load()
    pat = mod._fatal_patterns()["OpenAI/厂商 key (sk-)"]
    assert not pat.search("sk-test-placeholder-1234567890")
    assert not pat.search("sk-dummy-value-abcdefghijklmn")
    assert pat.search("sk-" + "a1B2c3D4" * 4)
