"""长任务的失败清单：记录 → 分类 → 汇总 → 支持单独重跑。

为什么需要它
============
长任务（wiki 编译动辄几十分钟、数百次 LLM 调用）必然遇到预期外错误：
网络波动、上游 520、模型返回非法 JSON、余额不足、并发限流、模型下线……
`fail-open` 只保证了"不中断整批"，但**没解决"事后怎么办"**——失败项散落在
控制台输出里，进程一关就没了，想补只能全量重来。

本模块提供三件事：
  1. **结构化落盘**（JSONL）：每条失败一条记录，含阶段 / 目标 / 错误类型 /
     尝试次数 / 原始报错。跨次运行累积，不会互相覆盖。
  2. **分类汇总**：把错误归成 quota / auth / model / rate / transient / format /
     unknown，跑完打印一份人可读的报告。**quota 与 auth 会醒目提示**——
     那两类重试毫无意义，继续跑只是白烧时间和额度。
  3. **单独重跑**：按阶段取出失败目标，只重跑这些（`--retry-failed`），
     不必全量。

与 `logs/wiki.log` 的分工
========================
`wiki.log` 是**流水账**（成功的调用也记，用于追进度与成本）；
失败清单是**待办**（只有失败，且可直接驱动重跑）。两者互补，不合并。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Iterable

#: 错误归类规则：按顺序匹配，先命中先用。key 一律小写比对。
_KINDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # 全局性错误——重试无意义，必须让用户立刻知道
    ("quota", ("insufficient", "quota", "credit", "billing", "余额", "402",
               "exceeded your current quota", "payment")),
    ("auth", ("401", "403", "unauthorized", "invalid api key", "invalid_api_key",
              "鉴权", "permission denied", "forbidden")),
    ("model", ("unsupported_model", "model not found", "no such model",
               "does not exist", "模型不存在", "not supported on this endpoint")),
    # 可重试的
    ("rate", ("429", "rate limit", "too many requests", "限流", "并发上限")),
    ("transient", ("timeout", "timed out", "incompleteread", "remote disconnected",
                   "connection", "network", "upstream", "502", "503", "504", "520",
                   "temporarily unavailable", "winerror")),
    ("format", ("无法从模型输出解析 json", "返回空内容", "jsondecodeerror",
                "unterminated", "expecting", "json")),
)

#: 需要立刻提醒用户的错误类型（重试与继续跑都无意义）
FATAL_KINDS = ("quota", "auth", "model")


def classify(error: Any) -> str:
    """把异常或错误文本归类。未知一律返回 `unknown`（不猜）。"""
    low = str(error or "").lower()
    for kind, keys in _KINDS:
        if any(k in low for k in keys):
            return kind
    return "unknown"


class FailureLog:
    """失败清单（JSONL 只追加，跨次运行累积）。

    线程安全：wiki 编译是多线程的，多个 worker 可能同时落一条失败。
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._entries: list[dict] = []
        self._load()

    def _load(self) -> None:
        """载入既有条目 —— 支持"多轮补跑"时把历史失败也纳入汇总。

        `resolved` 用**追加一条 tombstone**（`kind="resolved"`）表达，读时回放：
        该 (stage, target) 的历史失败统一标记为已解决。这么设计是因为
        CLI 与面板可能**同时**持有清单，重写整个文件会互相覆盖；追加天然并发安全。
        """
        self._entries = []
        if not self.path.exists():
            return
        raw: list[dict] = []
        try:
            for line in self.path.read_text(encoding="utf-8",
                                            errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    raw.append(json.loads(line))
                except Exception:
                    continue
        except Exception:
            return
        resolved = {(e.get("stage"), e.get("target")) for e in raw
                    if e.get("kind") == "resolved" or e.get("resolved") is True}
        for e in raw:
            if e.get("kind") == "resolved":
                continue                      # tombstone 本身不是失败记录
            if (e.get("stage"), e.get("target")) in resolved:
                e = dict(e, resolved=True)
            self._entries.append(e)

    def record(self, *, stage: str, target: str, error: Any,
               attempts: int = 1, resolved: bool = False,
               **extra) -> dict:
        """记一条失败。`target` 要能被重跑入口复用（如 'source/cid'）。"""
        entry = {
            "ts": time.time(),
            "stage": stage,
            "target": str(target),
            "kind": classify(error),
            "error": str(error)[:500],
            "attempts": int(attempts),
            "resolved": bool(resolved),
        }
        entry.update(extra)
        with self._lock:
            self._entries.append(entry)
        self._append(entry)
        return entry

    def resolve(self, stage: str, target: str) -> int:
        """把某目标的历史失败标记为已解决（重跑成功时调用）。返回影响条数。

        **追加一条 tombstone 落盘**，而不是只改内存 —— 否则"销账"只对当前进程
        有效，下次读清单失败又回来了（实测踩过，是测试抓出来的）。
        """
        with self._lock:
            n = 0
            for e in self._entries:
                if e.get("stage") == stage and e.get("target") == target \
                        and not e.get("resolved"):
                    e["resolved"] = True
                    n += 1
        if n:
            self._append({"ts": time.time(), "stage": stage, "target": target,
                          "kind": "resolved", "resolved": True,
                          "error": "", "attempts": 0})
        return n

    def _append(self, entry: dict) -> None:
        """追加一行（写失败静默：记录绝不能反过来影响主流程）。"""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def entries(self, stage: str | None = None,
                unresolved_only: bool = False) -> list[dict]:
        return [e for e in self._entries
                if (stage is None or e.get("stage") == stage)
                and (not unresolved_only or not e.get("resolved"))]

    def targets(self, stage: str) -> list[str]:
        """该阶段尚未解决的失败目标（去重、保序）—— 单独重跑用这个。"""
        seen, out = set(), []
        for e in self.entries(stage, unresolved_only=True):
            t = e.get("target")
            if t and t not in seen:
                seen.add(t)
                out.append(t)
        return out

    def summary(self, stage: str | None = None, top: int = 8) -> str:
        """人可读的失败汇总（跑完打印给用户看）。"""
        rows = self.entries(stage, unresolved_only=True)
        if not rows:
            return "✅ 没有未解决的失败项"

        by_kind: dict[str, list[dict]] = {}
        for e in rows:
            by_kind.setdefault(e.get("kind") or "unknown", []).append(e)

        lines = ["⚠️ 未解决的失败 %d 条，按原因分类：" % len(rows)]
        for kind in sorted(by_kind, key=lambda k: (-len(by_kind[k]), k)):
            items = by_kind[kind]
            flag = "  ← **需人工处理**" if kind in FATAL_KINDS else ""
            lines.append("   %-10s %4d 条%s" % (kind, len(items), flag))
            for e in items[:top]:
                lines.append("      %s  %s" % (e.get("target", "?")[:52],
                                               str(e.get("error", ""))[:80]))
            if len(items) > top:
                lines.append("      …（其余 %d 条见 %s）" % (len(items) - top, self.path))

        fatal = [k for k in by_kind if k in FATAL_KINDS]
        if fatal:
            lines.append("")
            lines.append("❌ 检测到 **%s** 类错误 —— 这类重试与继续跑都无意义，"
                         "请先解决再重跑：" % "、".join(fatal))
            if "quota" in fatal:
                lines.append("   · 余额/额度不足：去 Command Code 充值或换模型档位")
            if "auth" in fatal:
                lines.append("   · 鉴权失败：检查 agentmemhub.yaml 的 api_key 是否有效")
            if "model" in fatal:
                lines.append("   · 模型不可用：确认 model 名在 /provider/v1/models 里存在")
        lines.append("")
        lines.append("失败清单：%s" % self.path)
        lines.append("单独重跑失败项：加 --retry-failed（不必全量重来）")
        return "\n".join(lines)


def default_path(near: Path | str, name: str = "failures.jsonl") -> Path:
    """失败清单默认落点：与产出目录同级，便于跟着产物一起归档。"""
    return Path(near) / name
