# -*- coding: utf-8 -*-
"""LLM 调用的**总时长上限**与**重试层数**。

2026-10-20 实测背景：一轮 wiki 增量跑了 18 分钟，其中约 800 秒是**白等** ——
一次 L2 编译调用跑了 **975.3 秒**才返回（`ok=True`、`attempt=1`）。
两个根因：

1. **`timeout` 是 socket 空闲超时，不是总时长**。OpenAI 兼容接口是非流式的：
   服务端在算的时候一个字节都不发，于是 `timeout: 900` 全程不触发，调用可以
   无限拖下去。
2. **重试层数叠乘**。`call_json(tries=3)` 外面套 `complete_json(max_retries=2)`，
   最坏是 9 次长调用。

第 1 条的实现踩过一个坑，值得记住：**在 Windows 上 `socket.shutdown()` 无法
中断已经阻塞的 `recv`** —— 实测 0.5 秒时 shutdown 成功返回，`read()` 仍等到
服务端 3 秒后才抛 `BrokenPipeError`。所以最终用「工作线程 + `join` 超时」，
主线程到点直接放弃等待。
"""
from __future__ import annotations

import http.server
import sys
import threading
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


# ---------------------------------------------------------------------------
# 配置读取
# ---------------------------------------------------------------------------

def test_配置能读出总时长上限():
    from agentmemhub.llm import LLMConfig
    c = LLMConfig.from_dict({"endpoint": "e", "api_key": "k", "model": "m",
                             "max_total_seconds": 600})
    assert c.max_total_seconds == 600.0
    # 不写 = 不限（默认，保持既有行为）
    assert LLMConfig.from_dict({}).max_total_seconds == 0.0


# ---------------------------------------------------------------------------
# 端到端：一个"发了响应头就挂住"的本地服务
# ---------------------------------------------------------------------------

class _SlowHandler(http.server.BaseHTTPRequestHandler):
    """回 200 与响应头，只发 5 字节就挂住 —— 模拟"上游活着但不给结果"。"""

    protocol_version = "HTTP/1.1"

    def do_POST(self):                     # noqa: N802 —— 基类约定
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "500")     # 声称 500 字节
        self.end_headers()
        self.wfile.write(b'{"cho')                    # 只给 5 字节
        self.wfile.flush()
        time.sleep(3)

    def log_message(self, *args):          # 静音
        pass


def test_总时长超上限会被放弃等待_而不是干等():
    """服务端"给了响应头就挂住"时，到点必须放弃 —— 这是非流式调用的典型病态。

    实现上不能依赖"关 socket 中断 read"（Windows 上无效，见模块 docstring），
    所以断言的是**主线程到点返回**，而不是"连接被掐断"。
    """
    from agentmemhub.llm import LLMClient, LLMConfig, LLMTransientError

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _SlowHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = LLMClient(LLMConfig(
            endpoint="http://127.0.0.1:%d/chat/completions" % port,
            api_key="k", model="m", timeout=30, max_total_seconds=0.5,
            max_retries=0))
        t0 = time.time()
        with pytest.raises(LLMTransientError) as ei:
            client.complete_json("sys", "user", max_tokens=10)
        elapsed = time.time() - t0

        msg = str(ei.value)
        assert "总时长超过" in msg, "报错要说清是「到点放弃等待」"
        assert "max_total_seconds" in msg, "报错要指向具体配置项"
        assert elapsed < 1.5, "join 到点就该返回，实测 %.2fs（别等到 socket 超时）" % elapsed
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# 重试层数
# ---------------------------------------------------------------------------

def test_call_json瞬态错误不再叠加重试():
    """内层已按 max_retries 重试过，外层再重试一次就是 3 × 3 次长调用。"""
    import wiki_aggregate as wa
    from agentmemhub.llm import LLMTransientError

    class _Boom:
        calls = 0

        def complete_json(self, *a, **kw):
            self.calls += 1
            raise LLMTransientError("boom")

    c = _Boom()
    with pytest.raises(LLMTransientError):
        wa.call_json(c, "sys", "user", 100, tries=3, tag="测试")
    assert c.calls == 1, "瞬态错误只该调一次（重试归内层）"


def test_call_json解析失败仍在外层重试():
    """内层不重试的错误（空内容 / JSON 解析失败）必须由外层兜住。"""
    import wiki_aggregate as wa

    class _Flaky:
        calls = 0

        def complete_json(self, *a, **kw):
            self.calls += 1
            if self.calls == 1:
                raise ValueError("模型返回空内容")
            return {"ok": True}

    c = _Flaky()
    r = wa.call_json(c, "sys", "user", 100, tries=3, tag="测试")
    assert r == {"ok": True} and c.calls == 2, "第一次解析失败、第二次成功"
