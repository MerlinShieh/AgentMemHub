# -*- coding: utf-8 -*-
"""离线批处理的**运维动作层**（CLI 与 HTTP 面板共用）。

为什么要单独一层：`batch-run` 这类动作要做"读配置 → 构造 client/runner → 调
`wiki.update(offline_client=…)`"——**CLI 与面板要的是同一件事**，只是一个打印文本、
一个返回 JSON。把装配与动作收在这里，两个入口都只剩"取参 / 渲染"。

约定：所有函数要么返回**结构化结果**、要么抛 `BatchOpsError`（带可读消息），
**一律不打印**（打印是入口层的事）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


class BatchOpsError(RuntimeError):
    """前置条件不满足（未配产出目录 / 未配凭据）—— HTTP 侧映射成 422。"""


#: CLI 的 `--l1/--l2` 覆盖（HTTP 面板不设它，一律走配置）。
#:
#: ⚠️ **这是进程级全局**：`set_paths()` 只在 CLI 的 `_cmd_wiki_batch()` 里调一次
#: （一次进程一条命令，天然安全）；**HTTP 路由绝不能调它** —— 那是长驻服务，
#: 并发的后台任务会互相踩。HTTP 一律走 `paths()` 从配置读。
_OVERRIDE: tuple[str, str] = ("", "")


def set_paths(l1: str = "", l2: str = "") -> None:
    """临时覆盖产出目录（CLI 的 `--l1/--l2` 用）；两个都空则回落配置。"""
    global _OVERRIDE
    _OVERRIDE = (l1 or "", l2 or "")


def paths() -> tuple[str, str, str]:
    """从配置取 `(l1, l2, db)`；未配产出目录时抛 `BatchOpsError`。"""
    if _OVERRIDE[0] and _OVERRIDE[1]:
        return _OVERRIDE[0], _OVERRIDE[1], ""
    from agentmemhub.config import config
    w = config().wiki or {}
    l1 = str(w.get("out_l1") or "")
    l2 = str(w.get("out_l2") or "")
    if not (l1 and l2):
        raise BatchOpsError("未配置 wiki.out_l1 / wiki.out_l2")
    return l1, l2, ""


def make_client():
    """构造批量客户端；未配 `llm_batch.api_key` 时抛 `BatchOpsError`。

    凭据一律从配置读（不硬编码），与 CLI / 收割器同源。
    """
    from agentmemhub.batch_client import MiMoBatchClient
    from agentmemhub.config import config
    cfg = config().llm_batch or {}
    if not str(cfg.get("api_key") or "").strip():
        raise BatchOpsError("未配置 llm_batch.api_key —— 离线通道用不了")
    return MiMoBatchClient(cfg), cfg


def make_runner(client, cfg):
    from agentmemhub.llm_batch import OfflineRunner
    return OfflineRunner(uploader=client, model=str(cfg.get("model") or ""),
                         stage="l2plan")


def precheck() -> None:
    """前置条件检查：产出目录 + 凭据。入口层在**起长任务之前**调它。

    为什么要单独一步：`run()` 是在后台线程里跑的，等它抛错只能得到"任务失败"；
    入口层（HTTP / CLI）应当**在提交任务前**就给出 422 / 可读错误。
    """
    paths()
    make_client()


def status() -> dict[str, Any]:
    """在飞任务 + 厂商侧状态 + `decide()` 建议（**只读**，不修改任何状态）。"""
    import time as _t

    from agentmemhub.llm_batch import STATE_NAME, decide, load_state

    _, l2, _ = paths()
    client, _ = make_client()
    jobs = [j for j in load_state(Path(l2) / STATE_NAME) if not j.voided]
    now = _t.time()
    rows: list[dict[str, Any]] = []
    for j in jobs:
        row: dict[str, Any] = {
            "batch_id": j.batch_id, "stage": j.stage,
            "submitted_at": j.submitted_at,
            "waited_s": round(now - j.submitted_at, 1),
            "results": len(j.results), "items": len(j.items),
            "domains": len(j.domains()),
            "provider_status": j.provider_status or "",
        }
        try:
            b = client.retrieve(j.batch_id) or {}
            row["provider_status"] = str(b.get("status") or "").lower()
            row["counts"] = b.get("request_counts") or {}
            row["output_file_id"] = b.get("output_file_id") or ""
            row["error_file_id"] = b.get("error_file_id") or ""
            j.provider_status = row["provider_status"]
        except Exception as e:              # noqa: BLE001 —— 查不到就如实说
            row["error"] = "%s: %s" % (type(e).__name__, str(e)[:160])
        row["suggestion"] = decide(j, now=now)
        rows.append(row)
    return {"inflight": rows, "n": len(rows)}


def run(*, deadline_s: float = 0.0, offline_plan: bool = False,
        db: str = "", workers: int = 0) -> dict[str, Any]:
    """**推进一段**：无在飞 → 提交轮；结果到位 → 交下一段 / 落盘。

    `offline_plan=True` 时恢复"细分也走离线"（两段式），默认只离线编译。
    """
    from agentmemhub import wiki

    l1, l2, _cfg_db = paths()
    client, cfg = make_client()
    return wiki.update(l1_dir=l1, l2_dir=l2, db=db or _cfg_db,
                       workers=workers,
                       offline_client=client, offline_runner=make_runner(client, cfg),
                       offline_deadline_s=deadline_s, offline_plan=offline_plan)


def cancel() -> dict[str, Any]:
    """取消并作废所有在飞任务（覆盖的域回到待更新池）。"""
    from agentmemhub import wiki_batch
    from agentmemhub.llm_batch import STATE_NAME, load_state

    _, l2, _ = paths()
    client, _ = make_client()
    jobs = [j for j in load_state(Path(l2) / STATE_NAME) if not j.voided]
    done: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for j in jobs:
        try:
            client.cancel(j.batch_id)
        except Exception as e:              # noqa: BLE001 —— 取消失败也要作废本地标记
            errors.append({"batch_id": j.batch_id,
                           "error": "%s: %s" % (type(e).__name__, str(e)[:160])})
        n = len(j.domains())
        wiki_batch.finish(Path(l2), j, "手动取消")
        done.append({"batch_id": j.batch_id, "domains": n})
    return {"cancelled": done, "errors": errors}


def resubmit() -> dict[str, Any]:
    """用**原输入文件**重建已终局的任务（平台侧 `validation_failed` 时用）。"""
    from agentmemhub import wiki_batch

    _, l2, _ = paths()
    client, _ = make_client()
    return {"resubmitted": wiki_batch.resubmit_terminal(client, Path(l2))}


def full_mode() -> str:
    """全量重跑走哪条通道（配置位 `wiki.full.mode`，默认 `realtime`）。

    ⚠️ **刻意只做开关、不做判断**：官方批量是实时的 5 折，但某些**订阅套餐**给出的
    实时额度折算下来可能更便宜 —— 哪个划算取决于用户的凭据，代码不该替它决定。
    """
    from agentmemhub.config import config
    seg = (config().wiki or {}).get("full") or {}
    m = str(seg.get("mode") or "realtime").strip().lower()
    return m if m in ("realtime", "offline") else "realtime"


def full_run(*, mode: str = "", db: str = "", workers: int = 0) -> dict[str, Any]:
    """**全量重跑**：重新归域 + 全部域重编 + 覆盖写盘（清掉多余域目录）。

    与增量的区别见 `wiki.update(full=True)` 的 docstring。`mode` 空则读
    `wiki.full.mode`；`offline` 时归域与细分走实时、**编译段（几百条）走离线批次**。
    """
    from agentmemhub import wiki

    l1, l2, cfg_db = paths()
    m = (mode or full_mode()).strip().lower()
    if m == "offline":
        client, cfg = make_client()
        return wiki.update(l1_dir=l1, l2_dir=l2, db=db or cfg_db, workers=workers,
                           offline_client=client,
                           offline_runner=make_runner(client, cfg), full=True)
    return wiki.update(l1_dir=l1, l2_dir=l2, db=db or cfg_db, workers=workers,
                       full=True)


def checks(limit: int = 50) -> dict[str, Any]:
    """**检测流水**（排障用：上次什么时候查的、为什么没动、结果如何）。"""
    from agentmemhub import task_queue

    _, l2, _ = paths()
    return {"checks": task_queue.read_checks(Path(l2), limit=limit)}
