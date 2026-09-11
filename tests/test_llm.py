"""LLM 客户端测试：JSON 容错提取、审核拒评不重试、瞬态退避重试、脱敏提示。

全部走注入的假 opener，不触网。退避基数置 0 保证测试瞬时完成。
"""
from __future__ import annotations

import io
import json
import urllib.error

import pytest

from agentmemhub.llm import (
    ContentFilterRejected,
    LLMClient,
    LLMConfig,
    LLMError,
    LLMTransientError,
    client_from_config,
    extract_json,
    is_content_filter,
    scrub_text,
)


class _FakeResp:
    def __init__(self, payload: dict):
        self._b = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeOpener:
    """按序返回预设结果（dict=成功响应，Exception=抛出）；记录调用次数。"""

    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    def open(self, req, timeout=None):
        self.calls += 1
        r = self.results.pop(0) if self.results else RuntimeError("无更多预设响应")
        if isinstance(r, Exception):
            raise r
        return _FakeResp(r)


def _ok(content: str = '{"memories": []}') -> dict:
    return {"choices": [{"message": {"content": content}}]}


def _http_error(code: int, body: str = "") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://x", code, "err", {}, io.BytesIO(body.encode("utf-8")))


def _client(results, **cfg_kw) -> LLMClient:
    cfg = LLMConfig(endpoint="http://x/v1/chat/completions", api_key="k",
                    model="m", backoff_base=0.0, **cfg_kw)
    return LLMClient(cfg, _opener=_FakeOpener(results))


# ── extract_json：模型输出容错 ─────────────────────────────────────────

def test_extract_json_plain():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_with_code_fence():
    raw = '```json\n{"memories": [{"type": "fact"}]}\n```'
    assert extract_json(raw)["memories"][0]["type"] == "fact"


def test_extract_json_with_surrounding_prose():
    raw = '好的，提取结果如下：\n{"memories": []}\n以上。'
    assert extract_json(raw) == {"memories": []}


def test_extract_json_rejects_non_object():
    with pytest.raises(ValueError):
        extract_json('[1, 2, 3]')


def test_extract_json_rejects_empty():
    with pytest.raises(ValueError):
        extract_json("")
    with pytest.raises(ValueError):
        extract_json("   ")


def test_extract_json_rejects_garbage():
    with pytest.raises(ValueError, match="无法"):
        extract_json("完全不是 JSON")


# ── 文本净化与审核判定 ────────────────────────────────────────────────

def test_scrub_text_strips_zero_width():
    assert scrub_text("正\u200b文\u200c内容") == "正文内容"
    assert scrub_text("行1\n行2\t制表") == "行1\n行2\t制表"   # 换行/制表保留


def test_is_content_filter_judgement():
    assert is_content_filter(400, '{"error": {"code": "1301"}}')
    assert is_content_filter(400, "包含contentFilter")
    assert is_content_filter(400, "敏感内容被拦截")
    assert not is_content_filter(400, "普通参数错误")
    assert not is_content_filter(429, "1301")      # 非 400 不算


# ── 配置校验（不泄漏 key）─────────────────────────────────────────────

def test_config_complete_and_hint_no_key_leak():
    cfg = LLMConfig(endpoint="http://x", api_key="", model="m")
    assert not cfg.complete()
    hint = cfg.missing_hint()
    assert "api_key" in hint
    assert "http://x" not in hint                   # 不回显 endpoint/key 值


def test_config_from_dict_defaults_and_override():
    cfg = LLMConfig.from_dict({"endpoint": "e", "api_key": "k", "model": "m"})
    assert (cfg.endpoint, cfg.api_key, cfg.model) == ("e", "k", "m")
    assert cfg.temperature == 0.0 and cfg.timeout == 60.0
    assert cfg.max_retries == 2
    c2 = LLMConfig.from_dict({"model": "m"}, endpoint="e2", api_key="k2")
    assert c2.endpoint == "e2" and c2.model == "m"


def test_client_from_config():
    c = client_from_config({"endpoint": "e", "api_key": "k", "model": "m"})
    assert isinstance(c, LLMClient) and c.cfg.model == "m"


# ── 调用：成功 / 审核 / 瞬态重试 ──────────────────────────────────────

def test_complete_json_success():
    c = _client([_ok('{"memories": [{"type": "decision", "content": "x"}]}')])
    out = c.complete_json("sys", "user")
    assert out["memories"][0]["type"] == "decision"


def test_complete_json_incomplete_config_raises():
    c = LLMClient(LLMConfig(endpoint="", api_key="", model=""),
                  _opener=_FakeOpener([]))
    with pytest.raises(LLMError, match="未配置完整"):
        c.complete_json("s", "u")


def test_content_filter_not_retried():
    """审核拒评是确定性错误：只调一次，立即抛出。"""
    opener = _FakeOpener([_http_error(400, '{"error":{"code":"1301"}}')])
    c = LLMClient(LLMConfig(endpoint="http://x/v1/chat/completions", api_key="k", model="m",
                            backoff_base=0.0), _opener=opener)
    with pytest.raises(ContentFilterRejected):
        c.complete_json("s", "u")
    assert opener.calls == 1


def test_nontransient_4xx_not_retried():
    """401 等确定性错误不重试（可能是 key 错，重试无意义）。"""
    opener = _FakeOpener([_http_error(401, "unauthorized")])
    c = LLMClient(LLMConfig(endpoint="http://x/v1/chat/completions", api_key="k", model="m",
                            backoff_base=0.0), _opener=opener)
    with pytest.raises(LLMError):
        c.complete_json("s", "u")
    assert opener.calls == 1


def test_transient_retried_then_success():
    """429 先失败、后成功：重试生效，最终返回结果。"""
    c = _client([_http_error(429, "rate limited"), _ok('{"memories": []}')])
    assert c.complete_json("s", "u") == {"memories": []}
    assert c._opener.calls == 2


def test_transient_exhausts_retries():
    c = _client([_http_error(503, "down"), _http_error(503, "down")],
                max_retries=1)
    with pytest.raises(LLMTransientError):
        c.complete_json("s", "u")
    assert c._opener.calls == 2          # 1 次初始 + 1 次重试


def test_network_error_is_transient():
    c = _client([urllib.error.URLError("connection reset"),
                 _ok('{"memories": []}')])
    assert c.complete_json("s", "u") == {"memories": []}


def test_bad_json_raises_value_error_not_retried():
    """解析失败不是网络问题：直接抛 ValueError（由调用方决定重试/跳过）。"""
    opener = _FakeOpener([_ok("这不是 JSON")])
    c = LLMClient(LLMConfig(endpoint="http://x/v1/chat/completions", api_key="k", model="m",
                            backoff_base=0.0), _opener=opener)
    with pytest.raises(ValueError):
        c.complete_json("s", "u")
    assert opener.calls == 1


# ── 截断抢救（推理模型被 max_tokens 截断时，保住已完整的条目）──

def test_extract_json_salvages_truncated_output():
    """JSON 写到一半被截断 → 抢救出前面完整的条目（而非整片丢弃）。"""
    truncated = ('{"memories": ['
                 '{"type": "fact", "topic": "甲", "content": "完整的一条", "confidence": "high"},'
                 '{"type": "decision", "topic": "乙", "content": "另一条完整", "confidence": "medium"},'
                 '{"type": "fact", "topic": "丙", "content": "这条被截断在中间没有闭合')
    out = extract_json(truncated)
    assert len(out["memories"]) == 2
    assert out["memories"][0]["content"] == "完整的一条"
    assert out["memories"][1]["topic"] == "乙"


def test_extract_json_salvages_when_array_unclosed():
    """数组未闭合但对象完整（尾部缺 ]}）→ 同样抢救。"""
    truncated = ('{"memories": ['
                 '{"type": "fact", "topic": "甲", "content": "唯一完整条目", "confidence": "high"}')
    out = extract_json(truncated)
    assert out["memories"][0]["content"] == "唯一完整条目"


def test_extract_json_no_salvage_when_nothing_complete():
    """首条就不完整 → 无可抢救，仍报错（不能返回空结果掩盖问题）。"""
    with pytest.raises(ValueError):
        extract_json('{"memories": [{"type": "fact", "content": "截断在')


def test_extract_json_salvage_does_not_mask_other_shapes():
    """非 memories 结构且不可解析 → 仍报错（抢救逻辑不误伤）。"""
    with pytest.raises(ValueError):
        extract_json("完全是自然语言，没有 JSON")
