"""通用 LLM 客户端（OpenAI 兼容 /chat/completions），供记忆蒸馏等文本生成任务使用。

与 `scoring.py` 的 LLM 调用**同源纪律**（该模块为评分专用，未直接复用其私有
函数，以免蒸馏与评分链路互相牵制）：
- **强制直连**：Python urllib 默认会采用 Windows 系统代理（注册表）与 *_proxy
  环境变量——Clash 等工具开关/切节点会让请求时通时断（TLS 握手被掐）。确需
  走代理用环境变量 `AGENTMEMHUB_LLM_PROXY` 显式指定；
- **审核拒评不重试**：网关内容审核类 400（智谱 1301 等）是确定性拒绝，
  重试永远失败 → 抛 `ContentFilterRejected`，由调用方按"无法处理"处置；
- **瞬态错误退避重试**：429 / 5xx / 网络异常 → `LLMTransientError`，按指数退避重试；
- **送模型前净化**：剥离零宽/控制字符（最易触发审核与解析异常）。

JSON 输出容错：模型常把 JSON 包在 ``` 围栏里或前后加解释文字，
`extract_json()` 依次尝试"围栏内容 → 首个 { 到末个 }"提取。
"""
from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional


class LLMError(Exception):
    """LLM 调用失败（不可重试的确定性错误，或重试耗尽）。"""


class ContentFilterRejected(LLMError):
    """内容审核确定性拒评——重试无意义，调用方应按'无法处理'处置。"""


class LLMTransientError(LLMError):
    """瞬态错误（429/5xx/网络/超时）——可退避重试。"""


#: 内容审核类响应特征（与 scoring.py 同源判定）
_FILTER_HINTS = ("1301", "contentFilter", "敏感内容", "不安全")

_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.I)


def build_opener() -> urllib.request.OpenerDirector:
    """LLM 专用 opener：默认强制直连；AGENTMEMHUB_LLM_PROXY 可显式指定代理。"""
    import os
    proxy = os.environ.get("AGENTMEMHUB_LLM_PROXY", "").strip()
    return urllib.request.build_opener(
        urllib.request.ProxyHandler(
            {"http": proxy, "https": proxy} if proxy else {}))


def scrub_text(s: str | None) -> str:
    """剥离零宽/控制字符（保留 \\n\\t）：最易触发审核与解析异常的码点。"""
    if not s:
        return ""
    return "".join(
        ch for ch in s
        if ch in ("\n", "\t")
        or unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Co", "Cn")
    )


def is_content_filter(code: int, body: str) -> bool:
    """判定是否内容审核类 400（智谱 1301 / contentFilter / 敏感字样）。"""
    if code != 400 or not body:
        return False
    return any(h in body for h in _FILTER_HINTS)


def extract_json(content: str) -> dict:
    """从模型输出提取 JSON 对象，容忍 ``` 围栏与前后解释文字。

    失败抛 ValueError（调用方决定重试或跳过）。
    """
    if not content or not content.strip():
        raise ValueError("模型返回空内容")
    text = content.strip()
    m = _FENCE_RE.search(text)
    candidates = [m.group(1).strip()] if m else []
    candidates.append(text)
    for cand in candidates:
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    # 退一步：首个 { 到末个 }（容忍前后夹杂解释文字）
    i, j = text.find("{"), text.rfind("}")
    if 0 <= i < j:
        try:
            obj = json.loads(text[i:j + 1])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    raise ValueError(f"无法从模型输出解析 JSON：{text[:200]!r}")


@dataclass
class LLMConfig:
    """LLM 接入参数（来自统一配置，禁止硬编码）。"""

    endpoint: str = ""
    api_key: str = ""
    model: str = ""
    timeout: float = 60.0
    max_retries: int = 2          # 仅对瞬态错误生效
    backoff_base: float = 1.5     # 退避基数（秒）：base * 2^attempt
    max_tokens: int = 2048
    temperature: float = 0.0      # 蒸馏/抽取类任务恒 0，保证可复现
    #: provider 特定的额外请求头（配置驱动，不硬编码在客户端里）。
    #: 实测 OpenCode Go（opencode.ai/zen/go）需要 User-Agent（过 Cloudflare 1010）
    #: 与 x-opencode-session（路由亲和，缺失时 400 MissingSessionID）。
    headers: dict[str, str] = field(default_factory=dict)

    def complete(self) -> bool:
        """三项必填是否齐全（不打印 api_key）。"""
        return bool(self.endpoint and self.api_key and self.model)

    def missing_hint(self) -> str:
        """缺项提示（供报错文案，绝不回显 key）。"""
        miss = [k for k in ("endpoint", "api_key", "model") if not getattr(self, k)]
        return ("LLM 未配置完整，缺少：" + "、".join(miss)
                + "（在 agentmemhub.yaml 的 llm 段或 distillation.llm 段补齐）")

    @classmethod
    def from_dict(cls, cfg: dict | None, **overrides: Any) -> "LLMConfig":
        """从配置字典构造（蒸馏段或顶层 llm 段皆可）。"""
        d = dict(cfg or {})
        d.update({k: v for k, v in overrides.items() if v is not None})
        headers = d.get("headers") or {}
        return cls(
            endpoint=str(d.get("endpoint") or ""),
            api_key=str(d.get("api_key") or ""),
            model=str(d.get("model") or ""),
            timeout=float(d.get("timeout") or 60.0),
            max_retries=int(d.get("max_retries") if d.get("max_retries") is not None else 2),
            backoff_base=float(d.get("backoff_base") or 1.5),
            max_tokens=int(d.get("max_tokens") or 2048),
            temperature=float(d.get("temperature") if d.get("temperature") is not None else 0.0),
            headers={str(k): str(v) for k, v in headers.items()} if isinstance(headers, dict) else {},
        )


@dataclass
class LLMClient:
    """OpenAI 兼容 chat/completions 客户端（进程内复用，线程安全：无共享可变状态）。"""

    cfg: LLMConfig
    log: Optional[logging.Logger] = None
    _opener: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._opener is None:
            self._opener = build_opener()
        if self.log is None:
            self.log = logging.getLogger("agentmemhub.llm")

    # -- 单次请求 --------------------------------------------------------

    def _post(self, body: dict, timeout: float) -> dict:
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {self.cfg.api_key}"}
        if self.cfg.headers:
            headers.update(self.cfg.headers)      # provider 特定头（可覆盖默认）
        try:
            req = urllib.request.Request(
                self.cfg.endpoint,
                data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                headers=headers,
                method="POST")
        except ValueError as e:
            # endpoint 非法（缺协议头等）——配置错误，不重试
            raise LLMError(f"LLM endpoint 非法（检查配置）：{e}") from None
        try:
            with self._opener.open(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:400]
            except Exception:
                pass
            if is_content_filter(e.code, detail):
                raise ContentFilterRejected(
                    f"内容审核拒评（HTTP {e.code}）：{detail[:160]}") from None
            if e.code in (408, 409, 425, 429, 500, 502, 503, 504):
                raise LLMTransientError(f"HTTP {e.code}：{detail[:200]}") from None
            raise LLMError(f"HTTP {e.code}：{detail[:200]}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise LLMTransientError(f"{type(e).__name__}: {e}") from None

    # -- 对外：取结构化 JSON ---------------------------------------------

    def complete_json(self, system: str, user: str, *,
                      max_tokens: int | None = None,
                      temperature: float | None = None) -> dict:
        """调用 LLM 并解析为 JSON 对象。

        瞬态错误按指数退避重试（max_retries）；审核拒评立即抛出不重试；
        JSON 解析失败抛 ValueError（由调用方决定重试/跳过）。
        """
        if not self.cfg.complete():
            raise LLMError(self.cfg.missing_hint())
        body = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": scrub_text(system)},
                {"role": "user", "content": scrub_text(user)},
            ],
            "temperature": (self.cfg.temperature if temperature is None
                            else float(temperature)),
            "max_tokens": int(max_tokens or self.cfg.max_tokens),
        }
        last: Optional[Exception] = None
        for attempt in range(self.cfg.max_retries + 1):
            try:
                data = self._post(body, self.cfg.timeout)
                choices = data.get("choices") or []
                if not choices:
                    raise LLMTransientError(f"响应缺少 choices：{str(data)[:200]}")
                content = ((choices[0].get("message") or {}).get("content") or "")
                return extract_json(content)
            except ContentFilterRejected:
                raise
            except LLMTransientError as e:
                last = e
                if attempt < self.cfg.max_retries:
                    delay = self.cfg.backoff_base * (2 ** attempt)
                    self.log.warning("LLM 瞬态错误（第 %d 次），%.1fs 后重试：%s",
                                     attempt + 1, delay, e)
                    time.sleep(delay)
        raise last or LLMError("LLM 调用失败")


def client_from_config(cfg: dict | None) -> LLMClient:
    """从配置字典建客户端（cfg 形如 config.distillation["llm"]）。"""
    return LLMClient(LLMConfig.from_dict(cfg))
