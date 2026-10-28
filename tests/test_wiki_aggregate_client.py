# -*- coding: utf-8 -*-
"""L2 LLM 客户端的沙箱熔断（2026-10-08）。

背景：本项目出过一次"测试真的调了付费 LLM"的事故 ——
`tests/test_offline_e2e.py` 的「坏 JSON」用例没注入假 client，
`wiki_batch._repair_client()` 就默认造了**真**客户端，于是真发了 HTTPS 请求、
卡满 900 秒超时；而测试把日志重定向到 tmp，**生产日志里一行痕迹都没有**，
从日志完全看不出发生过。

修法是"在唯一构造点兜死"，而不是"指望每个测试都记得 mock"。
本文件守住 `wiki_aggregate.make_client()` —— 它是最后一处
"缺省会自动构造真实外呼客户端"的路径（另两处：`wiki_batch._repair_client()`
在沙箱下返回 `None`、`batch_client` 抛 `BatchError`）。三处同源同值，
都用 `logs.SANDBOX_ENV`（`AGENTMEMHUB_TEST_SANDBOX`）。
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def test_沙箱下拒绝构造真实客户端(monkeypatch):
    """conftest 的 session 级夹具已置位该变量；这里再显式置一次，单跑本文件也成立。"""
    monkeypatch.setenv("AGENTMEMHUB_TEST_SANDBOX", "1")
    from wiki_aggregate import make_client

    with pytest.raises(RuntimeError) as ei:
        make_client()

    msg = str(ei.value)
    assert "AGENTMEMHUB_TEST_SANDBOX" in msg   # 指出是哪个变量拦下的
    assert "mock" in msg                       # 给出补救办法，而不只是"不允许"


def test_熔断在构造前生效_不读配置(monkeypatch):
    """熔断必须在**读配置、建 client 之前**返回 —— 否则配置里的凭据已经被摸过了。"""
    monkeypatch.setenv("AGENTMEMHUB_TEST_SANDBOX", "1")
    import agentmemhub.config as hub_config
    from wiki_aggregate import make_client

    touched = []
    monkeypatch.setattr(
        hub_config, "config",
        lambda: touched.append("config") or SimpleNamespace(wiki_l2={}))

    with pytest.raises(RuntimeError):
        make_client()

    assert touched == [], "熔断前不该先读配置（会读到真凭据）"


def test_非沙箱下正常构造(monkeypatch):
    """熔断不能误伤生产路径：摘掉沙箱变量后，应真的走到构造，且配置取自 `wiki.l2.llm`。"""
    monkeypatch.delenv("AGENTMEMHUB_TEST_SANDBOX", raising=False)
    import agentmemhub.config as hub_config
    import agentmemhub.llm as llm
    from wiki_aggregate import make_client

    seen = {}

    def fake_from_config(cfg):
        seen["cfg"] = cfg
        return SimpleNamespace(cfg=SimpleNamespace(thinking=""))

    monkeypatch.setattr(
        hub_config, "config",
        lambda: SimpleNamespace(wiki_l2={"llm": {"model": "fake"}}))
    monkeypatch.setattr(llm, "client_from_config", fake_from_config)

    client = make_client("on")

    assert seen["cfg"] == {"model": "fake"}     # 第二级配置：wiki.l2.llm
    assert client.cfg.thinking == "on"          # thinking 覆写生效
