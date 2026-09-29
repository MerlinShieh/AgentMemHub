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


def fetch_results(client, job: BatchJob) -> dict[str, dict[str, Any]]:
    """下载一个任务的结果文件并解析成 `{custom_id: {ok, data|error}}`。

    成功文件与错误文件是**两份**：前者是成功请求的结果，后者是每条失败的**原因**。
    两份都要读 —— 只读成功文件的话，"哪些请求没算出来"就无从知道，只能整体重跑。
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
        for cid, r in parsed.items():
            if cid:
                results[cid] = r
        _audit("batch_consumed", batch_id=job.batch_id, file_id=fid, kind=kind,
               n=len([c for c in parsed if c]))
    return results


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

    save_state(state_path, [j for j in jobs if not j.voided])
    _audit("batch_import", n_jobs=len(jobs), n_consumed=len(consumed),
           n_voided=len(voided), n_waited=len(waited))
    return {"jobs": jobs, "decisions": decisions, "results": results,
            "consumed": consumed, "voided": voided, "waited": waited}
