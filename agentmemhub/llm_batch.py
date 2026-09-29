# -*- coding: utf-8 -*-
"""离线批处理（Batch API）的请求/结果编解码与运行器协议。

## 为什么要有这一层

厂商的离线批处理是**异步 + 跨进程**的：把一批调用写成 JSONL 上传，平台在空闲
时段逐条执行，之后再把结果 JSONL 下载回来。这和「一次调用、当场拿结果」的同步
模型根本不同 —— 调用方必须能**把请求交出去、把凭据存下来、在另一次运行里读回
结果**，而不是只会 `client.complete_json()` 这种同步原子操作。

## 本项目里它接在哪

`scripts/wiki_aggregate.py` 的**域内细分**（`stage_plan`）是最适合离线化的环节：
它天然是「一批互相独立的小请求」（一个域一个分批一条），失败代价低、对延迟不敏感，
而且**正是它在大域上超时**才逼出了这个需求。

接缝是 `stage_plan(..., runner=...)`：

    · 默认 `SyncRunner`  —— 当场逐条调用（实时路径，当前生产用的就是它）
    · 换成离线运行器    —— 把请求写成 JSONL、抛 `PendingError` 让调用方保留现场

## 本模块提供什么（都不需要厂商参与就能测）

- `BatchRequest` / `batch_to_jsonl()` / `load_batch_results()`：OpenAI Batch 兼容格式
- `SyncRunner`：当场逐条执行
- `PendingError`：离线运行器在「结果还没回来」时**必须抛它**，让调用方把「缺结果」
  与「调用失败」分开处理（前者要保留现场，后者才该降级）

## 尚未实现（接入时的落点）

真正的**上传 / 轮询 / 下载**（各家端点与鉴权不同，且当前无凭据可测）。
接入步骤与状态机见 [`docs/llm-batch-offline.md`](../docs/llm-batch-offline.md)。

⚠️ **在真正接通之前，不要加 `llm.mode: batch` 这类配置键** —— 本项目已经三次踩过
「假开关」（配置项没有任何读取点、默认值恰好等于硬编码值，于是既不报错也不让测试
变红，能潜伏数月）。这里的做法是：**先把不需要厂商的那一半做完并测好**，
配置键等接通时再加。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

#: OpenAI Batch 兼容的端点路径（多数兼容实现沿用这个）
DEFAULT_URL = "/v1/chat/completions"


@dataclass
class BatchRequest:
    """一次待执行的 LLM 调用（离线批处理的**最小单位**）。

    custom_id 是**跨进程的凭据**：上传与回读可能隔了几小时、甚至在不同的机器上，
    所以它必须自带定位信息（哪个阶段、哪个域、第几批），不能只是序号。
    """

    custom_id: str
    system: str
    user: str
    max_tokens: int = 16000
    temperature: float = 0.2
    #: 只用于日志/可读性，不进请求体
    tag: str = ""

    def body(self, model: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """请求体。**与实时路保持一致**：正文先过 `scrub_text()`（剥离零宽/控制
        字符，那些码点最易触发审核与解析异常），可选项（`thinking` /
        `reasoning_effort` 之类）由调用方通过 `extra` 传入 —— 批量段是独立配置，
        这些开关不该在这里猜。
        """
        from agentmemhub.llm import scrub_text
        b: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "system", "content": scrub_text(self.system)},
                         {"role": "user", "content": scrub_text(self.user)}],
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }
        if extra:
            b.update(extra)
        return b


class PendingError(RuntimeError):
    """离线运行器专用：**结果尚未返回**（不是失败）。

    调用方收到它应当**保留现场**（把域/成员/编号映射持久化），等结果回读后从断点
    继续；把它当成普通异常去降级，就会把「还没算」误判成「算不出来」。
    """

    def __init__(self, message: str = "", *, pending_ids: Iterable[str] = ()):
        super().__init__(message)
        self.pending_ids = list(pending_ids)


def batch_to_jsonl(requests: Iterable[BatchRequest], model: str,
                   *, url: str = DEFAULT_URL,
                   extra: dict[str, Any] | None = None) -> str:
    """把请求编成 **JSONL 文本**（一行一个请求，OpenAI Batch 兼容）。

    末尾保留换行 —— 各家的上传接口对此要求不一，但带换行是更安全的那个。
    `extra` 是逐条附加进请求体的可选项（见 `BatchRequest.body`），保证离线路与
    实时路**发出去的东西一致**。
    """
    lines = []
    for r in requests:
        lines.append(json.dumps(
            {"custom_id": r.custom_id, "method": "POST", "url": url,
             "body": r.body(model, extra)},
            ensure_ascii=False))
    return ("\n".join(lines) + "\n") if lines else ""


def _content_of(line: dict) -> str | None:
    """从一条结果里取出模型返回的正文（兼容 OpenAI Batch 的结果形状）。"""
    resp = line.get("response") or {}
    if int(resp.get("status_code") or 0) != 200:
        return None
    body = resp.get("body") or {}
    choices = body.get("choices") or []
    if not choices:
        return None
    msg = choices[0].get("message") or {}
    content = msg.get("content")
    return content if isinstance(content, str) else None


def load_batch_results(text: str) -> dict[str, dict[str, Any]]:
    """解析结果 JSONL → `{custom_id: {"ok": bool, "data"|"error": ...}}`。

    **不抛异常**：逐行容错，坏行记成 `{"ok": False, "error": ...}`。
    理由与 `distill` 的批量读取一致 —— 一批几千条里坏一行，不该让整轮回读白跑；
    调用方按 custom_id 逐个决定是重试还是降级。
    """
    from agentmemhub.llm import extract_json

    out: dict[str, dict[str, Any]] = {}
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            line = json.loads(raw)
        except Exception as e:
            out.setdefault("", {"ok": False,
                                "error": "结果行不是合法 JSON：%s" % str(e)[:120]})
            continue
        cid = str(line.get("custom_id") or "")
        if not cid:
            continue
        if line.get("error"):
            out[cid] = {"ok": False,
                        "error": str(line["error"])[:200]}
            continue
        content = _content_of(line)
        if content is None:
            out[cid] = {"ok": False, "error": "结果里没有可用的正文（状态码/形状异常）"}
            continue
        try:
            out[cid] = {"ok": True, "data": extract_json(content)}
        except Exception as e:
            # **保留原文**：坏 JSON 有两条出路（重试 / 交给修复模型），两条都需要
            # 原文。丢掉它就只剩"退化成成员各自成页"这一条路（不丢内容，但质量差）。
            out[cid] = {"ok": False, "content": content,
                        "error": "正文不是可解析的 JSON：%s" % str(e)[:120]}
    return out


class Runner(Protocol):
    """执行一批请求的协议 —— 离线化的**唯一接缝**。"""

    def run(self, requests: list[BatchRequest]) -> list[dict[str, Any]]:
        """按**入参顺序**返回每条的解析结果（dict）。

        失败语义由实现决定：同步实现可以抛异常，离线实现**应当**在结果未回来时
        抛 `PendingError`。
        """
        ...


@dataclass
class SyncRunner:
    """当场逐条执行（实时路径）。

    `call` 由调用方注入，形如 `call(system, user, max_tokens, tag) -> dict` ——
    这样本模块不必知道项目的重试/日志包装（`wiki_aggregate.call_json`），
    避免为了一个接缝把日志与重试逻辑搬到公共层。

    **单条失败隔离**：某一条抛异常时记 `{}` 而不是让整批炸掉。这一点很关键 ——
    细分里「整批失败」的旧行为会一路冒泡成「整域退化为一个页面」，正是 2026-09-25
    两次丢页事故的放大器；现在一条失败只让**那一条覆盖的成员各自成页**（不丢内容）。
    `PendingError` 例外：它是「结果还没回来」，必须原样抛出，不能被降级吞掉。
    """

    call: Callable[[str, str, int, str], dict[str, Any]]
    #: 单条失败时的回调（记日志/记失败清单），由调用方注入
    on_error: Callable[[BatchRequest, Exception], None] | None = None
    #: 已执行过的请求（测试与排查用）
    calls: list[BatchRequest] = field(default_factory=list)

    def run(self, requests: list[BatchRequest]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for r in requests:
            self.calls.append(r)
            try:
                out.append(self.call(r.system, r.user, r.max_tokens, r.tag))
            except PendingError:
                raise
            except Exception as e:
                if self.on_error is not None:
                    self.on_error(r, e)
                out.append({})
        return out


class BatchUploader(Protocol):
    """把 JSONL 交到厂商手上的**两个动作**（生产实现是 `batch_client.MiMoBatchClient`）。

    只要求这两个：编排（何时提交、提交什么、结果怎么回读）属于 `OfflineRunner`
    与上层。测试注入一个假实现即可**全程不发真请求**。
    """

    def upload(self, jsonl_text: str, *, filename: str = ...) -> dict[str, Any]: ...

    def create(self, input_file_id: str, *, name: str = ...,
               n_requests: int = ...) -> dict[str, Any]: ...


@dataclass
class OfflineRunner:
    """把一个域的请求**交给厂商**，然后抛 `PendingError` 保留现场。

    与 `SyncRunner` 的差别只有"谁执行、什么时候执行"：请求怎么切分、批内编号怎么
    映射回全局编号、遗漏成员怎么兜底 —— 全是同一段代码。所以两条路产物一致，
    切换只改一个 `runner=` 参数。

    抛 `PendingError` 而不是返回结果：结果要几小时后才有，**"还没算"不等于
    "算不出来"**。上层收到它就落盘现场、本轮提前结束，**绝不进降级分支**。
    """

    uploader: "BatchUploader"
    model: str = ""
    stage: str = "l2plan"
    #: 建任务用的 name —— 建任务超时后的**对账**（`find_batch(name=…)`）靠它
    job_name: str = ""
    #: 附加进请求体的可选项（如 `{"thinking": {"type": "disabled"}}`），与实时路对齐
    extra: dict[str, Any] | None = None
    #: 提交成功后的回调（上层用它把 `BatchJob` 落盘）。**在抛 PendingError 之前调用**
    on_submitted: Callable[[dict[str, Any]], None] | None = None
    #: 已提交记录（测试与排查用）
    submissions: list[dict[str, Any]] = field(default_factory=list)

    def run(self, requests: list[BatchRequest]) -> list[dict[str, Any]]:
        reqs = list(requests or [])
        if not reqs:
            return []
        jsonl = batch_to_jsonl(reqs, self.model, extra=self.extra)
        up = self.uploader.upload(jsonl, filename="%s.jsonl" % self.stage) or {}
        info: dict[str, Any] = {
            "stage": self.stage,
            "input_file_id": str(up.get("id") or ""),
            "n_requests": len(reqs),
            "custom_ids": [r.custom_id for r in reqs],
            "submitted_at": time.time(),
            "batch_id": "",
            "provider_status": "",
            "expire_at": 0.0,
        }
        # 建任务**不重试**（理由见 batch_client）：失败可能是"结果未知"，
        # 那种情况交给上层用 `find_batch()` 对账，不能在这里盲目再发一次。
        b = self.uploader.create(info["input_file_id"],
                                 name=self.job_name or ("agentmemhub-%s" % self.stage),
                                 n_requests=len(reqs)) or {}
        info["batch_id"] = str(b.get("id") or "")
        info["provider_status"] = str(b.get("status") or "").lower()
        info["expire_at"] = float(b.get("expires_at") or 0.0)
        self.submissions.append(info)
        if self.on_submitted is not None:
            # **先落现场、再抛异常**：反过来的话，PendingError 若被上层吞掉或进程
            # 中断，"已交给厂商"这件事就丢了 —— 下轮会重复提交、重复付费。
            self.on_submitted(info)
        raise PendingError(
            "已提交离线批量任务 %s（%d 条请求，stage=%s）—— 结果尚未返回，保留现场"
            % (info["batch_id"] or "(未知 id)", len(reqs), self.stage),
            pending_ids=info["custom_ids"])


# ---------------------------------------------------------------------------
# 离线任务的「标记 + 现场」与处置决策
#
# 用户视角的完整流程：
#   ① 这批要更新的数据交出去了 → **给它打个标记**（免得下次重复提交/重复付费）
#   ② 之后回来看任务状态     → 已完成的结果就消费
#   ③ 还在等厂商的那部分     → **也可以选择立即更新**（改走实时 LLM）
#
# ①③ 都是纯逻辑，不需要厂商参与就能写对、测好 —— 下面就是。
# ---------------------------------------------------------------------------

#: 结果已齐 → 直接消费
CONSUME = "consume"
#: 部分到位且已超预算 → 先用已到的，**剩下的改走实时**
PARTIAL = "partial"
#: 还没好、且在预算内 → 继续等
WAIT = "wait"
#: 一条都没到且已超预算 → 整批改走实时
FALLBACK = "fallback"

#: 默认落盘文件名（放产物目录，与 manifest_l2.json / failures.jsonl 同级）
STATE_NAME = "batch_state.json"

#: 厂商任务的**终局**状态：到了这里就不会再变，继续等就是白等
#: （厂商不支持任务内重试/续跑，失败只能新建任务）。
TERMINAL_STATES = ("failed", "expired", "cancelled")


@dataclass
class BatchJob:
    """一次离线提交的**凭据 + 现场** —— 也就是「已交给厂商」的那个标记。

    为什么必须持久化：离线是**异步跨进程**的。提交发生在这一次运行，取结果发生在
    另一次（可能几小时后、甚至换机器）。少了这份状态，下次跑起来就**不知道哪些
    数据已经交出去了** —— 要么重复提交（重复付费），要么把"还没算"当成"算不出来"。

    标记的粒度是**域**（`items` 里每个 custom_id 指向它属于哪个域、第几批）：这与
    wiki 的更新单位一致 —— `manifest`/`align` 判定"哪些域脏了"，脏域再按批提交。
    """

    batch_id: str
    provider: str = ""
    stage: str = "l2plan"
    submitted_at: float = 0.0
    #: custom_id -> {"domain": str, "batch_no": int}
    items: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: 域名 -> 该域的成员快照。**必须原样保留**：回读时要用**全局编号**取成员，
    #: 而分批编号是相对批内的 —— 没有这份快照就还原不回去。
    members: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    #: custom_id -> {"ok": bool, "data"|"error": ...}（回读后填）
    results: dict[str, dict[str, Any]] = field(default_factory=dict)

    # -- 厂商侧的可观测字段（回读 / 作废 / 过期判定都要用）-------------------
    #: 上传得到的输入文件 id（重新提交、审计对账要用）
    input_file_id: str = ""
    #: 成功结果文件 id（`completed` 后才有；`cancelled` 时保留已完成部分）
    output_file_id: str = ""
    #: 错误结果文件 id（有失败请求时才有值）
    error_file_id: str = ""
    #: 厂商原始状态，**一律 lower()**：created / validating / in_progress /
    #: finalizing / completed / failed / expired / cancelling / cancelled
    provider_status: str = ""
    #: 任务过期时刻（epoch 秒）。文件与结果只留 30 天，过期后回读报 `batch_not_found`
    expire_at: float = 0.0
    #: 已作废（取消 / 模式切换 / 终局失败）→ 它覆盖的域**回到待更新池**
    voided: bool = False
    voided_reason: str = ""

    def pending_ids(self) -> list[str]:
        """已提交但还没拿到结果的 custom_id。"""
        return sorted(k for k in self.items if k not in self.results)

    def domains(self) -> list[str]:
        return sorted({str(v.get("domain", "")) for v in self.items.values()} - {""})

    @property
    def results_state(self) -> str:
        """**结果**齐了没：`complete` / `partial` / `pending`。

        刻意**不叫 `status`** —— 厂商也有一个 status（任务处于哪个阶段，见
        `provider_status`），同名不同义最容易误用（2026-09-29 随 M1 改名）。
        """
        if not self.items or len(self.results) >= len(self.items):
            return "complete"
        return "partial" if self.results else "pending"

    def is_terminal(self) -> bool:
        """厂商侧是否已到终局（`failed` / `expired` / `cancelled`）。

        到了终局就**不该再等 deadline**：它永远不会再变，继续等只是白等。
        """
        return self.provider_status in TERMINAL_STATES

    def void(self, reason: str = "") -> list[str]:
        """作废这个任务，返回它覆盖的**域名列表**（供上层放回待更新池）。

        为什么"放回待更新池"不需要搬数据：提交轮**不刷 manifest**，那批输入本来
        就还是"脏"的；作废只是把"已交给厂商"这个显式标记清掉，隐式差集自动接管。
        """
        self.voided = True
        self.voided_reason = str(reason or "")
        return self.domains()

    def to_dict(self) -> dict[str, Any]:
        return {"batch_id": self.batch_id, "provider": self.provider,
                "stage": self.stage, "submitted_at": self.submitted_at,
                "items": self.items, "members": self.members,
                "results": self.results,
                "input_file_id": self.input_file_id,
                "output_file_id": self.output_file_id,
                "error_file_id": self.error_file_id,
                "provider_status": self.provider_status,
                "expire_at": self.expire_at,
                "voided": self.voided, "voided_reason": self.voided_reason}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "BatchJob":
        # 一律用 get 取值：**旧版 state 文件没有新字段**也要能读回来（向后兼容）
        return cls(batch_id=str(d.get("batch_id", "")),
                   provider=str(d.get("provider", "")),
                   stage=str(d.get("stage", "l2plan")),
                   submitted_at=float(d.get("submitted_at") or 0.0),
                   items=dict(d.get("items") or {}),
                   members=dict(d.get("members") or {}),
                   results=dict(d.get("results") or {}),
                   input_file_id=str(d.get("input_file_id", "")),
                   output_file_id=str(d.get("output_file_id", "")),
                   error_file_id=str(d.get("error_file_id", "")),
                   provider_status=str(d.get("provider_status", "")).lower(),
                   expire_at=float(d.get("expire_at") or 0.0),
                   voided=bool(d.get("voided")),
                   voided_reason=str(d.get("voided_reason", "")))


def decide(job: BatchJob, *, now: float, deadline_s: float = 0.0) -> str:
    """对一批离线任务做**处置决策**（纯函数，便于测试与将来加策略）。

    这就是「等不及的可以改走实时」那条规则的落点：

    ============  ==========================================================
    返回值         含义
    ============  ==========================================================
    `CONSUME`     结果齐了 → 直接用，不必再花钱
    `WAIT`        还没好、且在预算内 → 继续等下一轮
    `PARTIAL`     部分到位但已超预算 → **先用已到的，剩下的改走实时**
    `FALLBACK`    一条都没到且已超预算 → **整批改走实时**
    ============  ==========================================================

    `deadline_s <= 0` 表示**不限时**（一直等）—— 离线批处理本来就慢，默认不该催；
    限时是调用方"我这次就想拿到结果"时临时给的。

    **终局优先于 deadline**：厂商已 `failed` / `expired` / `cancelled` 时立刻给
    结论，不等超时 —— 否则终局失败会被当成"还在等"而干等到 deadline。

    **已完成也优先于 deadline**：`completed` 的意思正是"结果已经在那儿了"，与
    等多久无关。漏掉这一条时，`completed` 且 `deadline_s <= 0`（**默认就是
    不限时**）的任务会被判成 `WAIT` —— 等于永远不去取结果。
    """
    if not job.pending_ids():
        return CONSUME
    if job.provider_status == "completed":
        return CONSUME
    if job.is_terminal():
        return PARTIAL if job.results else FALLBACK
    if deadline_s <= 0 or (now - job.submitted_at) < deadline_s:
        return WAIT
    return PARTIAL if job.results else FALLBACK


def save_state(path: Path, jobs: list[BatchJob]) -> None:
    """把「已提交离线任务」的标记落盘（先写临时文件再替换，避免半截文件）。"""
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps([j.to_dict() for j in jobs],
                              ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_state(path: Path) -> list[BatchJob]:
    """读回标记；文件不存在或损坏时返回空表。

    **不抛异常**：没有标记只意味着"没提交过离线任务"，不该让整轮跑不动。
    """
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(raw, list):
        return []
    out: list[BatchJob] = []
    for d in raw:
        if isinstance(d, dict) and d.get("batch_id"):
            out.append(BatchJob.from_dict(d))
    return out
