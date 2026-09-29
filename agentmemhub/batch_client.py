# -*- coding: utf-8 -*-
"""小米 MiMo 批量推理（Batch API）的 HTTP 客户端。

## 为什么单独一层

批量推理的契约与实时 LLM 完全不同：**先上传 JSONL 文件、再建异步任务、隔几小时
回来查状态、最后下载两份结果文件**（成功与错误分开）。把它封在这里，上层
（离线运行器、CLI、将来的通知）只面对五六个动作，不重复处理 multipart、
错误分类与重试策略。

## 实测契约（2026-09-29 真凭据探测，详见 docs/modules/batch-offline.md）

| 动作 | 端点 | 实测 |
|---|---|---|
| 上传 | `POST /v1/files`（multipart，`purpose=batch`） | 200；文件 **30 天**过期 |
| 建任务 | `POST /v1/batches` | **201**；初始状态 `created`（文档未列） |
| 查状态 | `GET /v1/batches/{id}` | 200 |
| 列表 | `GET /v1/batches` | 200（**文档没写这个接口**，但对账靠它） |
| 取消 | `POST /v1/batches/{id}/cancel` | 200 → `cancelling`，再查才 `cancelled` |
| 下载 | `GET /v1/files/{file_id}/content` | 200；JSONL 文本 |

错误体统一形如 `{"error": {"message", "type", "code"}}`，`code` 实测有
`invalid_api_key` / `batch_not_found` —— 错误分类就靠它 + HTTP 状态码。

## 重试策略：**建任务绝不自动重试**

其余动作都幂等：上传重复只是多一个临时文件（30 天后自清）；查询/下载只读；
取消重复调用无害。但**建任务不幂等** —— 同一个 `input_file_id` 提交两次就是两个
任务、两份计费。所以 `create()` 失败时**不重试**，把"到底建没建成"交给上层用
`find_batch()` 对账（按 `name` + `input_file_id` 匹配）。这正是那条"文档说没有、
实测有"的列表接口的价值：没有它，建任务超时后只能人工去控制台看。

## 测试沙箱熔断

`AGENTMEMHUB_TEST_SANDBOX=1` 时**任何请求都直接抛**（进程级环境变量，不随
`monkeypatch` 撤销 —— 与 `wiki_triggers._targets()` 同一道闸）。理由是这批调用
**真的花钱**：测试必须注入假 `opener`（见 `tests/test_batch_client.py`），
而熔断保证"忘了注入也不会真发出去"。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable, Optional

#: 与 `wiki_triggers` / `distill` 共用同一道进程级熔断闸
SANDBOX_ENV = "AGENTMEMHUB_TEST_SANDBOX"

#: 默认端点路径（批量只支持 OpenAI completions 形态）
DEFAULT_ENDPOINT_PATH = "/v1/chat/completions"

#: 错误码里出现这些字样即判为鉴权问题（配置错误，重试无用）
_AUTH_HINTS = ("invalid_api_key", "authentication", "unauthorized", "permission",
               "invalid token", "api key")
#: 余额 / 配额类（要通知人，不是重试能解决的）
_BALANCE_HINTS = ("balance", "insufficient", "quota", "arrears", "欠费", "余额",
                  "exceeded your current")
#: 不存在（文件过期 30 天、任务 id 拼错）
_NOTFOUND_HINTS = ("not_found", "not found", "does not exist")


class BatchError(RuntimeError):
    """批量接口错误基类。

    `code` 来自厂商错误体的 `error.code`，`status` 是 HTTP 状态码 ——
    两者一起决定调用方该"重试 / 换实时 / 通知人"。
    """

    def __init__(self, message: str, *, status: int = 0, code: str = "",
                 body: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code
        self.body = body


class BatchAuthError(BatchError):
    """鉴权失败（key 无效 / 未配置）→ 配置问题，**回退实时并通知人**。"""


class BatchBalanceError(BatchError):
    """余额或配额不足 → **回退实时并通知人**（充值前离线通道不可用）。"""


class BatchNotFoundError(BatchError):
    """任务或文件不存在（文件 30 天过期最常见）→ 该批数据作废回待更新池。"""


class BatchTransientError(BatchError):
    """网络瞬态 / 5xx / 429 → 幂等动作可重试。"""


class BatchUnknownStateError(BatchError):
    """**建任务结果未知**（网络超时，服务端可能已创建）。

    绝不能当普通失败处理：盲目重试会建出第二个任务、重复计费。上层的正确动作是
    调 `find_batch()` 对账，确认到底建成没。
    """


def classify_error(status: int, body: str) -> BatchError:
    """把 HTTP 状态码 + 错误体归成上面那几种（纯函数，便于测试）。"""
    code, message = "", ""
    try:
        err = (json.loads(body or "{}") or {}).get("error") or {}
        code = str(err.get("code") or "")
        message = str(err.get("message") or "")
    except Exception:                       # noqa: BLE001 —— 非 JSON 就用原文
        pass
    text = ("%s %s" % (code, message)).lower()
    detail = "%s%s" % (code or status, ("：" + message) if message else "")
    if status in (401, 403) or any(h in text for h in _AUTH_HINTS):
        return BatchAuthError("批量接口鉴权失败（%s）" % detail,
                              status=status, code=code, body=body[:400])
    if any(h in text for h in _BALANCE_HINTS):
        return BatchBalanceError("批量接口余额/配额不足（%s）" % detail,
                                 status=status, code=code, body=body[:400])
    if status == 404 or any(h in text for h in _NOTFOUND_HINTS):
        return BatchNotFoundError("批量接口对象不存在（%s）" % detail,
                                  status=status, code=code, body=body[:400])
    if status >= 500 or status == 429:
        return BatchTransientError("批量接口暂时不可用（HTTP %d %s）" % (status, detail),
                                   status=status, code=code, body=body[:400])
    return BatchError("批量接口返回 HTTP %d（%s）" % (status, detail),
                      status=status, code=code, body=body[:400])


def _multipart(fields: dict[str, str],
               files: dict[str, tuple[str, bytes]]) -> tuple[bytes, str]:
    """手工编码 multipart/form-data（urllib 没有现成助手，且不想为此加依赖）。"""
    boundary = "----AgentMemHubBatch" + uuid.uuid4().hex
    out = bytearray()
    for k, v in fields.items():
        out += ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                % (boundary, k, v)).encode("utf-8")
    for k, (fname, data) in files.items():
        out += ('--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\n'
                'Content-Type: application/jsonl\r\n\r\n' % (boundary, k, fname)).encode("utf-8")
        out += data + b"\r\n"
    out += ("--%s--\r\n" % boundary).encode("utf-8")
    return bytes(out), "multipart/form-data; boundary=%s" % boundary


def _opener():
    """HTTP opener：默认**强制直连**，`AGENTMEMHUB_LLM_PROXY` 可显式指定代理。

    与 `agentmemhub/llm.py::_llm_opener` 同一策略、读同一个环境变量 —— 批量与实时
    走同一套网络出口策略，排查网络问题时不必记两套。
    """
    proxy = os.environ.get("AGENTMEMHUB_LLM_PROXY", "").strip()
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {}))


class MiMoBatchClient:
    """小米 MiMo 批量推理客户端（上传 / 建任务 / 查询 / 列表 / 取消 / 下载）。

    `cfg` 就是 `Config.llm_batch`（已合并默认值的段字典）。`opener` 可注入 ——
    测试注入假实现即可全程不发真请求（另有沙箱熔断兜底）。
    """

    def __init__(self, cfg: dict[str, Any], *, log: Optional[Callable[..., None]] = None,
                 opener=None):
        self.cfg = dict(cfg or {})
        self.endpoint = str(self.cfg.get("endpoint") or "").rstrip("/")
        self.api_key = str(self.cfg.get("api_key") or "").strip()
        self.model = str(self.cfg.get("model") or "")
        self.completion_window = str(self.cfg.get("completion_window") or "24h")
        self.timeout = float(self.cfg.get("timeout") or 60)
        self.max_retries = max(1, int(self.cfg.get("max_retries") or 1))
        self._opener = opener or _opener()
        self._log = log
        if not self.endpoint:
            raise BatchAuthError("未配置 llm_batch.endpoint")

    # -- 内部 -------------------------------------------------------------

    def _audit(self, event: str, **fields: Any) -> None:
        """落 `logs/wiki.log`（批量动作是**流水线层**的事，与 r 实时调用同一条流）。"""
        try:
            from agentmemhub.logs import audit_wiki
            audit_wiki({"event": event, **fields})
        except Exception:                   # noqa: BLE001 —— 日志永不阻塞主流程
            pass
        if self._log is not None:
            try:
                self._log(event, fields)
            except Exception:               # noqa: BLE001
                pass

    def _request(self, method: str, path: str, *, body: Optional[bytes] = None,
                 content_type: str = "", retries: Optional[int] = None,
                 timeout: Optional[float] = None) -> str:
        """发一次请求，返回响应文本（非 2xx 抛分类后的 `BatchError`）。

        `retries` 可覆盖默认值：**`create()` 传 1**（不重试），理由见模块 docstring。
        """
        if os.environ.get(SANDBOX_ENV):
            raise BatchError("测试沙箱禁止访问批量接口（%s 已置）" % SANDBOX_ENV)
        if not self.api_key:
            raise BatchAuthError("未配置 llm_batch.api_key（小米开放平台的 sk- key）")
        url = self.endpoint + path
        attempts = max(1, int(retries if retries is not None else self.max_retries))
        last: Optional[Exception] = None
        for i in range(attempts):
            req = urllib.request.Request(url, data=body, method=method)
            req.add_header("Authorization", "Bearer " + self.api_key)
            if content_type:
                req.add_header("Content-Type", content_type)
            try:
                with self._opener.open(req, timeout=timeout or self.timeout) as resp:
                    return resp.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                text = ""
                try:
                    text = e.read().decode("utf-8", "replace")
                except Exception:           # noqa: BLE001
                    pass
                err = classify_error(int(getattr(e, "code", 0) or 0), text)
                if isinstance(err, BatchTransientError) and i + 1 < attempts:
                    last = err
                    time.sleep(1.5 * (i + 1))
                    continue
                raise err from None
            except Exception as e:          # noqa: BLE001 —— 网络层（超时/DNS/连接）
                last = e
                if i + 1 < attempts:
                    time.sleep(1.5 * (i + 1))
                    continue
                raise BatchTransientError(
                    "批量接口请求失败（%s: %s）" % (type(e).__name__, str(e)[:160])) from None
        raise BatchTransientError("批量接口重试耗尽：%s" % (str(last)[:160],))

    # -- 六个动作 ----------------------------------------------------------

    def upload(self, jsonl_text: str, *, filename: str = "input.jsonl") -> dict[str, Any]:
        """上传请求文件，返回 file 对象（`id` 即后续建任务要用的 `input_file_id`）。"""
        payload, ctype = _multipart({"purpose": "batch"},
                                    {"file": (filename, jsonl_text.encode("utf-8"))})
        t0 = time.time()
        text = self._request("POST", "/files", body=payload, content_type=ctype)
        data = json.loads(text or "{}")
        self._audit("batch_upload", file_id=data.get("id", ""), bytes=data.get("bytes", 0),
                    seconds=round(time.time() - t0, 1))
        return data

    def create(self, input_file_id: str, *, name: str = "",
               endpoint: str = DEFAULT_ENDPOINT_PATH,
               completion_window: str = "", n_requests: int = 0) -> dict[str, Any]:
        """建批量任务。

        **不重试**（`retries=1`）：建任务不幂等，失败后重试可能建出第二个任务、
        重复计费。网络超时会抛 `BatchUnknownStateError`（结果未知），调用方应当用
        `find_batch()` 对账 —— 而不是自己重试。
        """
        body = json.dumps({
            "input_file_id": input_file_id,
            "endpoint": endpoint,
            "completion_window": completion_window or self.completion_window,
            "name": name or "agentmemhub-wiki-l2",
        }, ensure_ascii=False).encode("utf-8")
        t0 = time.time()
        try:
            text = self._request("POST", "/batches", body=body,
                                 content_type="application/json", retries=1)
        except BatchTransientError as e:
            raise BatchUnknownStateError(
                "建任务结果未知（%s）—— 不要重试，先用 find_batch() 对账" % str(e)[:160],
                status=e.status, code=e.code, body=e.body) from None
        data = json.loads(text or "{}")
        self._audit("batch_create", batch_id=data.get("id", ""),
                    status=data.get("status", ""),
                    completion_window=data.get("completion_window", ""),
                    n_requests=n_requests, name=data.get("name", ""),
                    seconds=round(time.time() - t0, 1))
        return data

    def retrieve(self, batch_id: str) -> dict[str, Any]:
        """查任务状态（只读，可积极重试）。"""
        t0 = time.time()
        data = json.loads(self._request("GET", "/batches/" + batch_id) or "{}")
        self._audit("batch_retrieve", batch_id=batch_id,
                    status=str(data.get("status") or "").lower(),
                    counts=data.get("request_counts") or {},
                    seconds=round(time.time() - t0, 1))
        return data

    def list_batches(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """列最近的批量任务（**文档未写、实测存在**）。

        用途就一个：**建任务超时后的对账** —— 凭 `name` / `input_file_id` 确认那笔
        提交到底建成没有，避免重复提交、重复计费。
        """
        t0 = time.time()
        text = self._request("GET", "/batches?limit=%d" % int(limit))
        data = json.loads(text or "{}")
        items = data.get("data") if isinstance(data, dict) else None
        out = [x for x in (items or []) if isinstance(x, dict)]
        self._audit("batch_list", n=len(out), seconds=round(time.time() - t0, 1))
        return out

    def cancel(self, batch_id: str) -> dict[str, Any]:
        """取消任务（**异步**：返回 `cancelling`，要再查一次才是 `cancelled`）。

        已终局的任务再调不报错（厂商对幂等调用友好）—— 调用方不必先查后取消。
        """
        try:
            data = json.loads(self._request("POST", "/batches/%s/cancel" % batch_id) or "{}")
        except BatchNotFoundError:
            return {"id": batch_id, "status": "not_found"}
        self._audit("batch_cancel", batch_id=batch_id,
                    status=str(data.get("status") or "").lower())
        return data

    def download(self, file_id: str) -> str:
        """下载结果文件内容（JSONL 文本）。"""
        t0 = time.time()
        text = self._request("GET", "/files/%s/content" % file_id)
        self._audit("batch_download", file_id=file_id, bytes=len(text.encode("utf-8")),
                    lines=len([ln for ln in text.splitlines() if ln.strip()]),
                    seconds=round(time.time() - t0, 1))
        return text

    # -- 对账 -------------------------------------------------------------

    def find_batch(self, *, name: str = "", input_file_id: str = "",
                   limit: int = 30) -> Optional[dict[str, Any]]:
        """在最近的任务里找**同一个提交**（按 `name` 优先、`input_file_id` 兜底）。

        这是"建任务结果未知"的**唯一安全出路**：不重试、不猜，先问服务端。
        """
        try:
            items = self.list_batches(limit=limit)
        except BatchError:
            return None
        if name:
            for b in items:
                if str(b.get("name") or "") == name:
                    return b
        if input_file_id:
            for b in items:
                if str(b.get("input_file_id") or "") == input_file_id:
                    return b
        return None
