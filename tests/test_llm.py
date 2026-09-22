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


def _http_error(code: int, body: str = "",
                headers: dict | None = None) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://x", code, "err", headers or {},
        io.BytesIO(body.encode("utf-8")))


def _client(results, **cfg_kw) -> LLMClient:
    cfg = LLMConfig(endpoint="http://x/v1/chat/completions", api_key="k",
                    model="m", backoff_base=0.0, **cfg_kw)
    return LLMClient(cfg, _opener=_FakeOpener(results))


@pytest.fixture(autouse=True)
def _no_rate_wait(monkeypatch):
    """限流闸门在测试里不产生**真实等待**。

    429 未给 `Retry-After` 时会把全局闸门推后 `_RATE_DEFAULT_WAIT`（默认 20 秒），
    后续每个请求都要等在闸门前 —— 测试里必须置 0，否则一个 429 用例就白等 20 秒。
    """
    from agentmemhub import llm as _llm
    monkeypatch.setattr(_llm, "_RATE_DEFAULT_WAIT", 0.0, raising=False)
    monkeypatch.setattr(_llm, "_RATE_UNTIL", 0.0, raising=False)
    yield
    _llm._RATE_UNTIL = 0.0


@pytest.fixture(autouse=True)
def _reset_usage():
    """累计用量是**模块级全局**，测试之间必须隔离（否则互相污染）。"""
    from agentmemhub import llm as _llm
    _llm.usage_reset()
    yield
    _llm.usage_reset()


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


# ── 429 限流：全局闸门 + Retry-After ───────────────────────────────────
#
# 组级/会话级并发之后，多个 worker 会几乎同时撞上同一个 RPM/TPM 窗口。若各自
# 独立退避，它们会在同一时刻一起恢复、一起重试，再次撞限（"退避共振"）——
# 所以退避必须是**进程级共享**的。

def test_retry_after_解析秒数日期与非法值():
    import email.utils
    import time as _t

    from agentmemhub.llm import _parse_retry_after

    assert _parse_retry_after({"Retry-After": "12"}) == 12.0
    assert _parse_retry_after({"Retry-After": " 3.5 "}) == 3.5
    assert _parse_retry_after({}) is None
    assert _parse_retry_after(None) is None
    assert _parse_retry_after({"Retry-After": "不是数字也不是日期"}) is None
    # HTTP-date 形式（RFC 允许两种写法）
    future = email.utils.formatdate(_t.time() + 30, usegmt=True)
    v = _parse_retry_after({"Retry-After": future})
    assert v is not None and 20 < v <= 31


def test_限流_异常带上标记与retry_after():
    # 注意：429 会把总尝试次数放宽到 max_retries+3，所以即使 max_retries=0
    # 也会重试到 3 次 —— 必须给足预设响应，否则是测试自己的假 opener 抛错
    opener = _FakeOpener([_http_error(429, "slow down", {"Retry-After": "1"})] * 3)
    c = LLMClient(LLMConfig(endpoint="http://x/v1/chat/completions",
                            api_key="k", model="m", backoff_base=0.0,
                            max_retries=0), _opener=opener)
    with pytest.raises(LLMTransientError) as ei:
        c.complete_json("s", "u")
    assert ei.value.is_rate_limit is True
    assert ei.value.retry_after == 1.0
    assert opener.calls == 3
    # 非限流的瞬态不应被标成 rate limit
    opener2 = _FakeOpener([_http_error(503, "down")])
    c2 = LLMClient(LLMConfig(endpoint="http://x/v1/chat/completions",
                             api_key="k", model="m", backoff_base=0.0,
                             max_retries=0), _opener=opener2)
    with pytest.raises(LLMTransientError) as ei2:
        c2.complete_json("s", "u")
    assert ei2.value.is_rate_limit is False


def test_限流_闸门被推后且过期后不阻塞(monkeypatch):
    import time as _t

    from agentmemhub import llm as _llm

    monkeypatch.setattr(_llm, "_RATE_UNTIL", 0.0)
    _llm._rate_backoff(30.0)
    assert _llm._RATE_UNTIL > _t.time() + 25

    # 只延长不退让：较小的值不能把闸门提前
    _llm._rate_backoff(1.0)
    assert _llm._RATE_UNTIL > _t.time() + 25

    # 闸门过期后不阻塞
    _llm._RATE_UNTIL = 0.0
    t0 = _t.time()
    _llm._rate_wait()
    assert _t.time() - t0 < 0.5


def test_限流_闸门有上限防卡死(monkeypatch):
    """服务端给个离谱的 Retry-After（比如一天）不能把任务卡死。"""
    import time as _t

    from agentmemhub import llm as _llm

    monkeypatch.setattr(_llm, "_RATE_UNTIL", 0.0)
    _llm._rate_backoff(86400)
    assert _llm._RATE_UNTIL <= _t.time() + _llm._RATE_MAX_WAIT + 1


def test_限流_比普通瞬态多给重试轮次():
    """RPM 窗口是分钟级，默认 3 次尝试常常不够 —— 限流放宽到 max_retries+3 次尝试。"""
    n = 4                    # 普通瞬态上限 3 次尝试，这里要 5 次才成功
    opener = _FakeOpener(
        [_http_error(429, "rate limited", {"Retry-After": "0"})] * n + [_ok()])
    c = LLMClient(LLMConfig(endpoint="http://x/v1/chat/completions",
                            api_key="k", model="m", backoff_base=0.0,
                            max_retries=2), _opener=opener)
    assert c.complete_json("s", "u") == {"memories": []}
    assert opener.calls == n + 1        # 5 次尝试 > 普通瞬态的 3 次上限

    # 对照：普通瞬态（503）在同样 max_retries 下只会尝试 3 次
    o2 = _FakeOpener([_http_error(503, "down")] * 5)
    c2 = LLMClient(LLMConfig(endpoint="http://x/v1/chat/completions",
                             api_key="k", model="m", backoff_base=0.0,
                             max_retries=2), _opener=o2)
    with pytest.raises(LLMTransientError):
        c2.complete_json("s", "u")
    assert o2.calls == 3


# ── 累计调用耗时：与墙钟分开的"真实工作量"口径 ────────────────────────
#
# 任务汇总里的时长是**墙钟**，机器一休眠就被撑大：实测一次 L2 汇总显示
# 33052 秒（9.2 小时），据此误判成"上游重试了 9 小时"，实际约 1 小时。
# 累计调用耗时只算真正在等 LLM 的时间（休眠时不会有请求在跑），不会失真。

def test_累计耗时_成功请求会累加():
    from agentmemhub import llm as _llm

    r = _ok('{"memories": []}')
    r["usage"] = {"prompt_tokens": 10, "completion_tokens": 5}
    c = _client([r])
    c.complete_json("s", "u")
    u = _llm.usage_snapshot()
    assert u["calls"] == 1
    assert u["prompt"] == 10 and u["completion"] == 5
    assert u["seconds"] > 0.0, "成功请求必须计入调用耗时"


def test_累计耗时_响应无usage也记耗时():
    """响应不带 usage 时 `calls` 不计（成本口径），但时间是实打实花掉的。"""
    from agentmemhub import llm as _llm

    c = _client([{"choices": [{"message": {"content": '{"a": 1}'}}]}])
    c.complete_json("s", "u")
    u = _llm.usage_snapshot()
    assert u["calls"] == 0
    assert u["seconds"] > 0.0


def test_累计耗时_多次请求累加且可重置():
    from agentmemhub import llm as _llm

    rs = []
    for _ in range(3):
        r = _ok()
        r["usage"] = {"prompt_tokens": 1, "completion_tokens": 1}
        rs.append(r)
    c = _client(rs)
    for _ in range(3):
        c.complete_json("s", "u")
    u = _llm.usage_snapshot()
    assert u["calls"] == 3 and u["seconds"] > 0.0

    _llm.usage_reset()
    u2 = _llm.usage_snapshot()
    assert u2["calls"] == 0 and u2["seconds"] == 0.0


def test_累计耗时_不计退避等待(monkeypatch):
    """退避等待不算"在等 LLM" —— 否则限流时又会把口径搞脏。"""
    import time as _t

    from agentmemhub import llm as _llm

    opener = _FakeOpener([_http_error(503, "down"), _ok()])
    c = LLMClient(LLMConfig(endpoint="http://x/v1/chat/completions",
                            api_key="k", model="m", backoff_base=1.0,
                            max_retries=1), _opener=opener)
    t0 = _t.time()
    c.complete_json("s", "u")
    wall = _t.time() - t0
    secs = _llm.usage_snapshot()["seconds"]
    assert wall >= 1.0            # 确实退避等待过
    assert secs < wall            # 但等待的那 1 秒没被算进调用耗时
