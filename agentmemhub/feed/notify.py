# -*- coding: utf-8 -*-
"""投喂结果 → 通知（**底层复用 `agentmemhub/notify.py`，不新增推送开关**）。

为什么投喂需要通知
================
投喂天然是"跑一会儿、人不在跟前"的操作：批量投几百份文件、PDF 转换几十秒到几分钟、
网页取回还要走代理。跑完只把结果打在控制台，等于**只有守着终端的人才知道结果**。

三条纪律（每条都对应本项目踩过的坑）
==================================
1. **不新增推送开关**：复用既有的 `push_when` 五开关。feed 用 `category="feed"` ——
   它既不是 `memory` 也不在 `WIKI_CATEGORIES`，所以**只受等级开关控制**：
   默认配置下 `info=false`（成功不打扰）、`warning=true` / `error=true`（失败必达）。
   这正是想要的默认，**不必再加一个 `push_when.feed`** —— "加开关必须有消费点"
   的纪律反过来也成立：别为了一个已经合理的默认去加键（加了就多一个僵尸键候选）。
2. **通知是旁路，绝不能影响投喂**：推送通道挂了、通知库写不进去，投喂本身都必须
   照常成功。所以这一层**吞掉所有异常**，只留一行日志。
3. **批量不逐条推**（防刷屏的关键）：一次投几百份，若逐条通知，标题各不相同
   ⇒ 去重窗口形同虚设 ⇒ 用户手机被刷爆。所以批量只在**结束时推一条汇总**
   （`notify_batch`），单条投喂（CLI/HTTP/MCP 的 add）才逐条通知。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .base import QUALITY_WARN

#: 通知类别（面板可按它筛选；也决定推送策略走**等级**分支而不是类别分支）
CATEGORY = "feed"

#: 这些 `error_kind` 是**代码 bug**（不是素材或环境的问题）⇒ 用 error 级送到人眼前。
#: 其余（格式不支持、文件不存在、扫描件待 OCR…）warning 就够。
_BUG_KINDS = frozenset({"crash", "registry"})

#: 正文上限（通知库放得下，推送渠道也有长度限制）
_BODY_MAX = 800


def _clip(text: Any, limit: int = 200) -> str:
    s = " ".join(str(text or "").split())
    return s[:limit] + ("…" if len(s) > limit else "")


def _name_of(res: dict[str, Any]) -> str:
    """给人看的素材名：标题 → 来源文件名 → doc_id → 兜底。"""
    for cand in (res.get("title"), Path(str(res.get("ref") or "")).name,
                 res.get("doc_id")):
        if str(cand or "").strip():
            return _clip(cand, 60)
    return "未命名素材"


def describe(res: dict[str, Any]) -> tuple[str, str, str]:
    """投喂结果 → `(等级, 标题, 正文)`。

    公开这个函数是为了**测试直接断言它**，而不必反过来去翻通知库 ——
    "哪一级、说什么"本身就是这里最需要守住的东西（等级错了要么漏报、要么刷屏）。
    """
    name = _name_of(res)
    kind = str(res.get("kind") or "").strip() or "-"
    if res.get("ok"):
        quality = float(res.get("quality") or 0.0)
        warns = [str(w) for w in (res.get("warnings") or [])]
        if quality < QUALITY_WARN or warns:
            level, head = "warning", "投喂完成但需复核"
        else:
            level, head = "info", "投喂成功"
        lines = ["标题：%s" % name, "格式：%s · 质量：%.2f" % (kind, quality)]
        if warns:
            lines.append("告警：%s" % _clip("；".join(warns), 300))
        if res.get("kb_path"):
            lines.append("交付物：%s" % res["kb_path"])
        if res.get("doc_id"):
            lines.append("doc_id：%s" % res["doc_id"])
        return level, "%s：%s" % (head, name), "\n".join(lines)[:_BODY_MAX]

    ek = str(res.get("error_kind") or "").strip()
    # 代码 bug 与"素材/环境问题"必须分开：前者要人去看代码，后者人换个文件就行
    level = "error" if ek in _BUG_KINDS else "warning"
    lines = ["来源：%s" % _clip(res.get("ref"), 200)]
    if ek:
        lines.append("原因分类：%s" % ek)
    lines.append("原因：%s" % _clip(res.get("error") or "（无）", 400))
    lines.append("可用 `feed retry %s` 重跑" % res["doc_id"] if res.get("doc_id")
                 else "在认领之前就失败了（原件已入 raw/，补上转换器后可重跑）")
    return level, "投喂失败：%s" % name, "\n".join(lines)[:_BODY_MAX]


def _send(level: str, title: str, body: str, meta: dict) -> None:
    from ..notify import notify as _notify
    _notify(level, CATEGORY, title, body, meta=meta)


def notify_result(res: dict[str, Any], *, log=None) -> None:
    """按一条投喂结果发通知。**绝不抛**（投喂的成败都不该被通知拖累）。"""
    try:
        if res.get("dry_run"):
            return          # dry-run 承诺"一个字都不写" —— 其中也包括不打扰人
        if res.get("skipped"):
            return          # 幂等重复：不打扰（"重投一次推一次"是最烦的那种）
        level, title, body = describe(res)
        _send(level, title, body,
              {"source": "feed", "doc_id": res.get("doc_id") or "",
               "kind": res.get("kind") or "", "ok": bool(res.get("ok")),
               "error_kind": res.get("error_kind") or ""})
        if log:
            log("  （已通知：%s %s）" % (level, title))
    except Exception as e:                      # noqa: BLE001 —— 旁路，永不抛
        if log:
            try:
                log("  （通知未发出，不影响投喂：%s: %s）"
                    % (type(e).__name__, str(e)[:120]))
            except Exception:                   # noqa: BLE001
                pass


def notify_batch(summary: dict[str, Any], *, log=None) -> None:
    """批量投喂的**汇总**通知（一条，见模块 docstring 第 3 条）。**绝不抛**。"""
    try:
        total = int(summary.get("total") or 0)
        if not total or summary.get("dryRun"):
            return
        ok = int(summary.get("ok") or 0)
        failed = int(summary.get("failed") or 0)
        skipped = int(summary.get("skipped") or 0)
        level = "warning" if failed else "info"
        # 标题**带计数**：同一结果的重复批量会被去重压掉，而"这次比上次多失败 3 份"
        # 不会（若标题固定不带数字，连"第一次成功、第二次失败"都可能被压掉）。
        title = "批量投喂：%d 份 · 成功 %d · 失败 %d" % (total, ok, failed)
        lines = ["成功 %d / 跳过 %d / 失败 %d（共 %d）" % (ok, skipped, failed, total)]
        errs = summary.get("errors") or []
        for e in errs[:8]:
            lines.append("· %s → %s" % (Path(str(e.get("path") or "")).name,
                                        _clip(e.get("error"), 120)))
        if len(errs) > 8:
            lines.append("…（其余 %d 条失败见 logs/feed.log 或 `feed batch --json`）"
                         % (len(errs) - 8))
        _send(level, title, "\n".join(lines)[:_BODY_MAX],
              {"source": "feed", "batch": True, "total": total,
               "failed": failed, "skipped": skipped})
        if log:
            log("  （已通知：%s %s）" % (level, title))
    except Exception as e:                      # noqa: BLE001 —— 旁路，永不抛
        if log:
            try:
                log("  （批量通知未发出，不影响投喂：%s: %s）"
                    % (type(e).__name__, str(e)[:120]))
            except Exception:                   # noqa: BLE001
                pass
