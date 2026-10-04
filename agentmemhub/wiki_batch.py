# -*- coding: utf-8 -*-
"""离线批处理的**编排层**：查状态、回读、消费、作废。

三段式（见 `docs/batch-offline-plan.md` §3.1）里"**收回来**"的那半边在这里：

```
运行 1  提交细分          scripts/wiki_aggregate._submit_offline()
运行 2  回读细分结果 ──┐
        → 合并成页组    │  ← 本模块提供"回读"
        → 交编译任务    │     scripts/wiki_aggregate.submit_offline_compile()
运行 3  回读编译结果 ──┘
        → 写页 → linkfix → 刷 manifest（scripts/wiki_aggregate）
```

**产物写盘不在这里**（那归 `scripts/wiki_aggregate.py`）。本模块只负责三件事：
把结果安全取回来、把该作废的作废掉、把该让人知道的记成日志与通知。

## 两条纪律

1. **只读动作失败不抛**：查状态/下载失败都只记日志、跳过该任务 —— 一次回读不该
   因为某个任务查不到而整轮失败（结果还在厂商那边，下轮还能取）。
2. **终局即作废、不捞已完成部分**：这是 D2 的决策（"整批作废"）—— 取消 / 过期 /
   失败后，该任务覆盖的域全部回到待更新池，下次更新重新提交。已完成部分已计费但
   按此决策丢弃；重跑会再花一次，成本已知且接受。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Optional

from agentmemhub.llm_batch import (
    PARTIAL,
    STATE_NAME,
    WAIT,
    TERMINAL_STATES,
    BatchJob,
    decide,
    load_batch_results,
    load_state,
    save_state,
)

#: 通知写入器的签名（默认走 `agentmemhub.notify.notify`，测试可注入）
Notify = Callable[..., bool]


def _audit(event: str, **fields: Any) -> None:
    """落 `logs/wiki.log`（批量链路属于流水线层）。日志是旁路，永不抛。"""
    try:
        from agentmemhub.logs import audit_wiki
        audit_wiki({"event": event, **fields})
    except Exception:                       # noqa: BLE001
        pass


def _default_notify(level: str, category: str, title: str, body: str = "",
                    meta: Optional[dict] = None) -> bool:
    """默认通知器；通知模块本身也不会抛，这里再兜一层。"""
    try:
        from agentmemhub.notify import notify
        return bool(notify(level, category, title, body, meta=meta))
    except Exception:                       # noqa: BLE001
        return False


def poll(client, jobs: list[BatchJob]) -> dict[str, dict[str, Any]]:
    """刷新在飞任务的**厂商侧状态**（只读；单个失败不影响其它）。

    刷新的是"厂商跑到哪了"：`provider_status` / 两个结果文件 id / 过期时刻。
    它决定下一步是继续等还是可以取结果 —— 不看这个就只能在本地瞎等。
    """
    out: dict[str, dict[str, Any]] = {}
    for job in jobs:
        if not job.batch_id or job.voided:
            continue
        try:
            b = client.retrieve(job.batch_id) or {}
        except Exception as e:              # noqa: BLE001 —— 查不到不影响别的任务
            out[job.batch_id] = {"ok": False,
                                 "error": "%s: %s" % (type(e).__name__, str(e)[:160])}
            continue
        job.provider_status = str(b.get("status") or "").lower()
        job.output_file_id = str(b.get("output_file_id") or "")
        job.error_file_id = str(b.get("error_file_id") or "")
        if b.get("expires_at"):
            job.expire_at = float(b.get("expires_at") or 0.0)
        out[job.batch_id] = {"ok": True, "status": job.provider_status,
                             "counts": b.get("request_counts") or {}}
    return out


def _repair_client():
    """构造一个**实时** LLM client —— 离线用的 `MiMoBatchClient` 没有 `_repair_json`。

    取 `config().wiki_l2.llm`：与**离线编译段同一段配置**，所以 `repair_model` 的
    配置位置一致（它继承顶层 `llm`，也可在 `wiki.l2.llm` 就地覆盖）。构造失败返回
    `None`（修复是**尽力而为**的兜底，不能因为它挂掉而影响回读）。
    """
    try:
        from agentmemhub import config as hub_config
        from agentmemhub.llm import client_from_config
        return client_from_config(hub_config.config().wiki_l2.get("llm"))
    except Exception:                       # noqa: BLE001
        return None


def repair_broken_results(results: dict[str, dict[str, Any]], *,
                          repair_client=None,
                          max_repair: int = 50) -> dict[str, int]:
    """把"**有内容但 JSON 坏**"的条目交给 `repair_model` 转成规范 JSON（**就地修改**）。

    为什么需要它：离线回读只做 `extract_json()`，而**实时路有本地模型修复** —— 同一条
    输出，实时能救回、离线会白白退化成"成员各自成页"（内容不丢，但页面质量差）。
    这是"两条路产物应当一致"的一个真实缺口（2026-10-04 补上）。

    - `repair_client` 为空 ⇒ 自己构造一个实时 client（`_repair_client()`）；
    - **没配 `repair_model` 时什么都不做** —— 与 `LLMClient` 的既有语义一致
      （没配修复模型时，"失败就是失败"）；
    - `max_repair`：单轮最多修几条。**修复是实时调用、要花钱**，坏 500 条不能
      悄无声息地打 500 次；超出的留给下轮（它们仍带 `content`，不会丢）。

    返回 `{"tried", "repaired"}`（供审计与调用方判断）。
    """
    stats = {"tried": 0, "repaired": 0}
    if max_repair <= 0:
        return stats
    targets = [cid for cid, r in results.items()
               if not r.get("ok") and r.get("content")][:max_repair]
    if not targets:
        return stats
    if repair_client is None:
        repair_client = _repair_client()
    repair = getattr(repair_client, "_repair_json", None)
    if not callable(repair):
        return stats
    # `_repair_json(system, user, raw)`：离线结果里**没有原 prompt**，而修复器要做的
    # 只是"把非规范 JSON 转成规范" —— 不需要完整任务上下文，给通用要求即可。
    system = "把下面这段模型原始输出转换成任务要求的 JSON 对象，只输出 JSON。"
    for cid in targets:
        r = results[cid]
        stats["tried"] += 1
        try:
            fixed = repair(system, "", str(r.get("content") or ""))
        except Exception as e:              # noqa: BLE001 —— 修复失败就保持原样
            _audit("batch_repair", ok=False, custom_id=cid,
                   error="%s: %s" % (type(e).__name__, str(e)[:160]))
            continue
        if isinstance(fixed, dict) and fixed:
            r.pop("error", None)
            r["ok"] = True
            r["data"] = fixed
            r["repaired"] = True
            stats["repaired"] += 1
            _audit("batch_repair", ok=True, custom_id=cid)
    if stats["tried"]:
        _audit("batch_repair_done", tried=stats["tried"],
               repaired=stats["repaired"], max_repair=max_repair)
    return stats


def fetch_results(client, job: BatchJob, *,
                  repair_client=None) -> dict[str, dict[str, Any]]:
    """下载一个任务的结果文件并解析成 `{custom_id: {ok, data|error}}`。

    成功文件与错误文件是**两份**：前者是成功请求的结果，后者是每条失败的**原因**。
    两份都要读 —— 只读成功文件的话，"哪些请求没算出来"就无从知道，只能整体重跑。

    解析后**坏 JSON 走 `repair_model` 兜底**（见 `repair_broken_results`）——
    实时路一直有这一步，离线路此前缺，会导致同一条输出在两条路上质量不同。
    """
    results: dict[str, dict[str, Any]] = {}
    for fid, kind in ((job.output_file_id, "output"), (job.error_file_id, "error")):
        if not fid:
            continue
        try:
            text = client.download(fid)
        except Exception as e:              # noqa: BLE001 —— 下载失败留给下轮
            _audit("batch_download", ok=False, file_id=fid, kind=kind,
                   batch_id=job.batch_id,
                   error="%s: %s" % (type(e).__name__, str(e)[:160]))
            continue
        parsed = load_batch_results(text)
        # **修复兜底**：只对"有正文但 JSON 坏"的条目动手（成功的不碰、没正文的没法修）。
        try:
            repair_broken_results(parsed, repair_client=repair_client,
                                  max_repair=batch_max_repair())
        except Exception:                   # noqa: BLE001 —— 兜底失败就当没修
            pass
        for cid, r in parsed.items():
            if cid:
                results[cid] = r
        _audit("batch_consumed", batch_id=job.batch_id, file_id=fid, kind=kind,
               n=len([c for c in parsed if c]))
    return results


def resubmit_terminal(client, out: "Path | str") -> list[dict[str, Any]]:
    """把**已终局**的任务用**原输入文件**重新提交一遍，返回重建记录。

    为什么这样重提（而不是重跑一轮 `update`）：输入文件还躺在厂商那边（30 天有效），
    `create()` 直接指过去就行 —— 重新生成 JSONL 意味着重跑 L1 编译与归域，
    **白花一次实时费用**。平台自己的错误文案就是"任务排队执行过程中遇到异常，
    **请尝试重新创建任务**"（2026-09-30 实测：连官方文档的示例任务也因同一条失败）。

    只动**已终局**的任务（`failed` / `expired` / `cancelled`）：还在飞的绝不能重建，
    否则同一批数据会被提交两次、重复计费。
    """
    import time

    path = Path(out) / STATE_NAME
    jobs = list(load_state(path))
    done: list[dict[str, Any]] = []
    for job in jobs:
        if str(job.provider_status or "") not in TERMINAL_STATES:
            continue                        # 还在飞：不动它
        if not job.input_file_id:
            continue                        # 没有输入文件凭据，重建不了
        b = client.create(job.input_file_id,
                          name="agentmemhub-%s" % (job.stage or "l2plan"),
                          n_requests=len(job.items)) or {}
        old = job.batch_id
        job.batch_id = str(b.get("id") or "")
        job.provider_status = str(b.get("status") or "").lower()
        job.submitted_at = time.time()
        job.results = {}                    # 新任务的结果要从头收
        job.output_file_id = ""
        job.error_file_id = ""
        job.expire_at = float(b.get("expires_at") or 0.0)
        done.append({"stage": job.stage, "old": old, "new": job.batch_id,
                     "input_file_id": job.input_file_id,
                     "n_requests": len(job.items)})
        _audit("batch_resubmit", stage=job.stage, old_batch=old,
               batch_id=job.batch_id, n_requests=len(job.items))
    if done:
        save_state(path, jobs)
    return done


def finish(out: "Path | str", job: BatchJob, reason: str = "") -> None:
    """把任务标记为**已消费完**：作废 + 落盘（不在飞的任务不再留在 state 里）。

    这里的"作废"是"已处理"的意思，不是失败 —— 它让"哪些域还在飞"的判断不再把它
    算进去，避免同一批数据被重复提交、重复付费。
    """
    job.void(reason)
    state_path = Path(out) / STATE_NAME
    # ⚠️ 必须**按 batch_id 删掉磁盘上那一份**：传进来的 `job` 是调用方手里的内存
    # 对象（刚 `void()`），而磁盘上那份仍是作废之前的快照 —— 只过滤 `voided`
    # 是删不掉它的（实测踩过：落盘成功，任务却还留在 state 里）。
    save_state(state_path, [j for j in load_state(state_path)
                            if not j.voided and j.batch_id != job.batch_id])


# ---------------------------------------------------------------------------
# 通用收割基础设施（`agentmemhub/task_queue.py`）的**通道适配器**
# ---------------------------------------------------------------------------

class _OfflineHarvester:
    """把离线队列接成 `task_queue.Harvester`（只实现那两个方法）。

    **适配器的存在本身就是解耦的证据**：`task_queue` 只要求 `pending()` /
    `harvest()`，完全不知道 `batch_id` / `items` / `members` 这些东西。
    """

    def __init__(self, *, l1: Path, l2: Path, db: str, client, runner):
        self.l1, self.l2, self.db = Path(l1), Path(l2), db
        self.client, self.runner = client, runner

    def pending(self) -> list[Any]:
        """在飞任务（空列表 = 空闲 ⇒ 通用层零成本退出、连厂商都不查）。"""
        return [j for j in load_state(self.l2 / STATE_NAME) if not j.voided]

    def harvest(self, *, now: float) -> dict[str, Any]:
        """回读并**推进一段**（复用 `wiki._offline_advance()` 的三段式状态机）。"""
        from agentmemhub import wiki as _wiki
        r = _wiki._offline_advance(l1=self.l1, l2=self.l2, db=self.db,
                                   runner=self.runner, client=self.client,
                                   deadline_s=0.0)
        return dict(r) if r else {"step": "idle"}


def make_harvester(*, l1: "Path | str", l2: "Path | str", db: str = "",
                   client=None, runner=None) -> Optional["_OfflineHarvester"]:
    """装配离线通道的收割器；**没配 `llm_batch.api_key` 时返回 `None`**
    （通用层据此返回 `no_channel`，连队列都不读）。

    凭据一律从配置读（不硬编码），与 CLI 的 `batch-run` 走同一套装配逻辑。
    """
    if client is None:
        from agentmemhub.config import config
        from agentmemhub.llm_batch import OfflineRunner
        cfg = (config().llm_batch or {})
        if not str(cfg.get("api_key") or "").strip():
            return None
        from agentmemhub.batch_client import MiMoBatchClient
        client = MiMoBatchClient(cfg)
        runner = runner or OfflineRunner(uploader=client,
                                         model=str(cfg.get("model") or ""),
                                         stage="l2plan")
    return _OfflineHarvester(l1=Path(l1), l2=Path(l2), db=db,
                             client=client, runner=runner)


def expire_warn_s(cfg: Optional[dict] = None) -> float:
    """「即将过期」的预警阈值（秒）。配置位 `wiki.batch.expire_warn_s`，默认 6 小时。

    0 = 关闭预警。
    """
    if cfg is None:
        try:
            from agentmemhub.config import config
            cfg = ((config().wiki or {}).get("batch") or {})
        except Exception:                   # noqa: BLE001
            cfg = {}
    try:
        v = float((cfg or {}).get("expire_warn_s", 6 * 3600))
    except (TypeError, ValueError):
        v = 6.0 * 3600
    return max(0.0, v)


def batch_max_repair(cfg: Optional[dict] = None) -> int:
    """单轮回读**最多修几条坏 JSON**。配置位 `wiki.batch.max_repair`，默认 50。

    为什么要有上限：修复走的是**实时**调用（要花钱），而坏 JSON 可能成片出现
    （比如某轮上游不稳）—— 不能悄无声息地打几百次。超出的留给下轮，
    它们的 `content` 还在，不会丢。`0` = 完全不修。
    """
    if cfg is None:
        try:
            from agentmemhub.config import config
            cfg = ((config().wiki or {}).get("batch") or {})
        except Exception:                   # noqa: BLE001
            cfg = {}
    try:
        v = int((cfg or {}).get("max_repair", 50))
    except (TypeError, ValueError):
        v = 50
    return max(0, v)


def _warn_expiring(jobs: list[BatchJob], *, now: float, note: Notify) -> list[str]:
    """对"接近过期"的任务发**一次**预警（返回被预警的 batch_id 列表）。

    为什么要它：批量任务在 `completion_window`（默认 24h）内没被厂商调度完成就会变
    `expired` —— 该批数据**整批作废、回待更新池**，前面的等待全部白费。提前几小时
    知道，人可以做两个决定：调大 `llm_batch.completion_window` 重提，或者等下次再跑。

    **去重靠 `job.warned_expire` 这个持久化标记**，不是靠通知的去重窗口 —— 收割是
    高频事件（每 10 分钟一次、每次写入记忆也会走到），300 秒的窗口挡不住反复推。
    """
    warn_s = expire_warn_s()
    if warn_s <= 0:
        return []
    hit: list[str] = []
    for job in jobs:
        # `completed` **不算 `is_terminal()`**（它还没被消费，`decide()` 会立刻
        # CONSUME），但对"已经跑完、就等回读"的任务发"快过期"预警毫无意义 ——
        # 所以这里单独排除一次。
        if (job.voided or job.warned_expire or job.is_terminal()
                or job.provider_status == "completed" or not job.expire_at):
            continue
        left = float(job.expire_at) - now
        if not (0 < left < warn_s):
            continue
        job.warned_expire = True            # 先记下；`save_state` 由调用方收尾时做
        hit.append(job.batch_id)
        hours = left / 3600.0
        _audit("batch_expire_warn", batch_id=job.batch_id, stage=job.stage,
               hours_left=round(hours, 1), expire_at=job.expire_at,
               n_requests=len(job.items))
        try:
            note("warning", "batch", "离线批次即将过期",
                 "批次 %s（%s，%d 条请求）还有约 %.1f 小时到 completion_window；"
                 "到期仍未完成会**整批作废**、数据回待更新池 —— 要留它就把 "
                 "`llm_batch.completion_window` 调大后重提。"
                 % (job.batch_id, job.stage, len(job.items), hours),
                 meta={"batch_id": job.batch_id, "stage": job.stage,
                       "hours_left": round(hours, 1), "expire_at": job.expire_at,
                       "n_requests": len(job.items)})
        except Exception:                   # noqa: BLE001 —— 通知是旁路
            pass
    return hit


def import_results(client, out: "Path | str", *, now: Optional[float] = None,
                   deadline_s: float = 0.0,
                   notify_fn: Optional[Notify] = None) -> dict[str, Any]:
    """回读所有在飞任务：查状态 → 决策 → 取结果 → 落盘现场。

    返回值给上层（`wiki.py` / CLI）：

    ==============  =========================================================
    `jobs`          刷新后的任务（含 `provider_status` 与已消费的 `results`）
    `decisions`     `{batch_id: CONSUME/PARTIAL/WAIT/FALLBACK}`
    `results`       `{batch_id: {custom_id: {ok, data|error}}}`
    `consumed`      本次**取到新结果**的任务 id
    `voided`        本次作废的任务（终局）—— 它们覆盖的域回到待更新池
    `waited`        还在等（厂商没跑完、也还没超预算）的任务 id
    ==============  =========================================================
    """
    state_path = Path(out) / STATE_NAME
    jobs = [j for j in load_state(state_path) if not j.voided]
    now = float(now if now is not None else time.time())
    note = notify_fn or _default_notify

    decisions: dict[str, str] = {}
    results: dict[str, dict[str, Any]] = {}
    consumed: list[str] = []
    voided: list[dict[str, Any]] = []
    waited: list[str] = []

    if not jobs:
        _audit("batch_import", n_jobs=0)
        return {"jobs": [], "decisions": {}, "results": {},
                "consumed": [], "voided": [], "waited": []}

    poll(client, jobs)
    # **先看有没有快过期的**：它比"继续等"更该让人知道（见 `_warn_expiring`）。
    warned = _warn_expiring(jobs, now=now, note=note)
    for job in jobs:
        d = decide(job, now=now, deadline_s=deadline_s)
        decisions[job.batch_id] = d

        if job.is_terminal():
            # 厂商**不会再产出任何东西**了：作废、回池、通知人。
            # **不捞已完成部分** —— 这是 D2 的决策（整批作废），见模块 docstring。
            domains = job.void("厂商终局：%s" % job.provider_status)
            voided.append({"batch_id": job.batch_id, "status": job.provider_status,
                           "stage": job.stage, "domains": domains})
            _audit("batch_voided", batch_id=job.batch_id, stage=job.stage,
                   status=job.provider_status, n_domains=len(domains))
            if job.provider_status == "expired":
                _audit("batch_expired", batch_id=job.batch_id, stage=job.stage,
                       expire_at=job.expire_at)
            note("warning", "batch",
                 "离线任务已终局（%s）" % job.provider_status,
                 "batch=%s stage=%s：%d 个域回到待更新池，下次更新会重新提交"
                 % (job.batch_id, job.stage, len(domains)),
                 meta={"batch_id": job.batch_id, "stage": job.stage,
                       "status": job.provider_status, "domains": domains})
            continue

        if d == WAIT:
            waited.append(job.batch_id)
            continue

        # CONSUME / PARTIAL：把已经能取的结果读回来（已到的不重复花钱）
        got = fetch_results(client, job)
        if got:
            job.results.update(got)
            consumed.append(job.batch_id)
            # **取回结果后要重新决策**：决策是在取结果**之前**算的，那时 results
            # 还是空的 —— 超时的任务会被判成 FALLBACK（"一条都没到"），而实际
            # 已经取到一部分，正确结论是 PARTIAL（先用已到的，其余走实时）。
            decisions[job.batch_id] = decide(job, now=now, deadline_s=deadline_s)
            d = decisions[job.batch_id]
        results[job.batch_id] = dict(job.results)
        if d == PARTIAL and len(job.results) < len(job.items):
            pend = len(job.items) - len(job.results)
            note("warning", "batch",
                 "离线任务只到了一部分（%d/%d）"
                 % (len(job.results), len(job.items)),
                 "batch=%s stage=%s：按决策先用已到的，另外 %d 条改走实时"
                 % (job.batch_id, job.stage, pend),
                 meta={"batch_id": job.batch_id, "stage": job.stage,
                       "pending": pend})

    # **保留已作废的任务**（只标记、不删除）：`members` / `items` 现场是"重提"与
    # "回读落盘"的**唯一依据**，删掉就再也拼不回来 —— 2026-09-30 实测：守卫把
    # `validation_failed` 的任务作废后，12 条请求的成员快照一并丢失，只能重新生成
    # 一遍请求。**"不在飞"由 `voided` 标记表达，与"是否留在 state 里"是两件事。**
    save_state(state_path, jobs)
    _audit("batch_import", n_jobs=len(jobs), n_consumed=len(consumed),
           n_voided=len(voided), n_waited=len(waited))
    return {"jobs": jobs, "decisions": decisions, "results": results,
            "consumed": consumed, "voided": voided, "waited": waited}
