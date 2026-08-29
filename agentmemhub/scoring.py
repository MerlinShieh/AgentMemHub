"""MemOS 历史记忆批量自动评分器。

背景：importBundle 导入的历史 trace 不参与引擎自动进化链（无 rewardDirty
标记），value 停留在启发式初值。MemOS 的 feedback 接口（POST /api/v1/feedback
带 traceId）会立即重算 value/rHuman，但 magnitude 被 0~1 钳制、value 是反馈
加权平均——只能表达极性分（positive/negative 二值），写不了任意连续分。

本模块"用 MemOS 的方式"实现自动化打分：
- 复用引擎已配置的 LLM（engine config.yaml 的 llm 段，openai_compatible）
- 按 MemOS reward 的三轴思想（目标达成 / 过程质量 / 用户价值）逐条评估
- 判定 positive（值得保留）/ neutral（一般）/ negative（无价值或噪音）
- 通过 feedback 接口批量写入 → 引擎立即重算每条记忆的 value/priority，
  语义检索排序随之生效

约束：只通过引擎公开 API 交互，不改引擎源码与数据库；key 只读不落日志。
"""
from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

from agentmemhub import memos_daemon

_SYSTEM_PROMPT = (
    "你是记忆质量评估器。判断一条 Agent 任务记忆是否值得长期保留。\n"
    "按三轴评估：目标达成度（任务是否完成、结论是否可靠）、过程质量（是否有"
    "可复用的教训/步骤/发现）、用户价值（对未来任务是否可能有帮助）。\n"
    "双随机输出 JSON：{\"verdict\": \"positive\" | \"neutral\" | \"negative\", "
    "\"reason\": \"一句话理由\"}\n"
    "positive=值得保留（达成目标或有重要教训）；negative=无价值或纯噪音；"
    "neutral=一般，不置可否。"
)

# ---------------------------------------------------------------------------
# 已评清单：<data_dir>/scored_traces.json（手动 👍/👎 与批量评分共同累计，
# 重跑「自动评分」时跳过——避免批量 verdict 覆盖/稀释手动打分）
# ---------------------------------------------------------------------------

_scored_cache: Optional[set[str]] = None
_cache_lock = threading.Lock()


def _cache_path() -> Path:
    from agentmemhub import config
    return config.config().data_dir / "scored_traces.json"


def _load_scored() -> set[str]:
    global _scored_cache
    p = _cache_path()
    try:
        if _scored_cache is None and p.exists():
            _scored_cache = set(json.loads(p.read_text(encoding="utf-8")))
    except Exception:
        _scored_cache = set()
    return _scored_cache or set()


def mark_scored(trace_id: str) -> None:
    """记录一条记忆已评分（批量成功写入或手动 👍/👎 后调用）。"""
    global _scored_cache
    with _cache_lock:
        s = _load_scored()
        if trace_id in s:
            return
        s.add(trace_id)
        _scored_cache = s
        try:
            _cache_path().write_text(
                json.dumps(sorted(s), ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass


def clear_scored() -> None:
    """清空已评清单（测试/重评用）。"""
    global _scored_cache
    with _cache_lock:
        _scored_cache = set()
        try:
            _cache_path().unlink(missing_ok=True)
        except Exception:
            pass


def read_engine_llm() -> dict[str, Any]:
    """读引擎 config.yaml 的 LLM 配置（endpoint/model/apiKey；key 只读不打印）。"""
    import yaml
    home = memos_daemon.engine_home()
    if home is None:
        raise RuntimeError("未找到引擎 home（先配置 memos.home 或确认引擎默认位置）")
    p = home / "config.yaml"
    if not p.exists():
        raise RuntimeError(f"引擎配置不存在：{p}")
    cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    llm = cfg.get("llm") or {}
    endpoint = llm.get("endpoint") or ""
    api_key = llm.get("apiKey") or ""
    model = llm.get("model") or ((llm.get("models") or {}).get("summary") if isinstance(llm.get("models"), dict) else "")
    if not endpoint or not api_key or not model:
        raise RuntimeError("引擎 config.yaml 的 llm 段不完整（endpoint/apiKey/model）")
    return {"endpoint": endpoint, "api_key": api_key, "model": model}


def evaluate_trace(trace: dict, llm_cfg: dict, timeout: float = 45) -> str:
    """LLM 三轴评估一条 trace，返回 verdict（positive|neutral|negative）。"""
    user_text = (trace.get("userText") or "").strip()[:800]
    agent_text = (trace.get("agentText") or "").strip()[:1200]
    if not user_text and not agent_text:
        return "neutral"
    body = {
        "model": llm_cfg["model"],
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": f"任务内容：\n用户：{user_text}\n助手：{agent_text}"},
        ],
        "temperature": 0.1,
        "max_tokens": 80,
    }
    req = urllib.request.Request(
        llm_cfg["endpoint"],
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {llm_cfg['api_key']}"},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    verdict = _parse_verdict(content)
    return verdict


def _parse_verdict(content: str) -> str:
    import re
    low = content.strip().lower()
    m = re.search(r'"verdict"\s*:\s*"(\w+)"', low)
    if m:
        v = m.group(1)
        if v in ("positive", "neutral", "negative"):
            return v
    if "positive" in low:
        return "positive"
    if "negative" in low:
        return "negative"
    return "neutral"


def run_score_all(*, emit: Optional[Callable[[str], None]] = None,
                  base_url: str = "", limit: int = 0,
                  dry_run: bool = False,
                  workers: int = 1,
                  skip_scored: bool = True) -> dict[str, Any]:
    """批量自动评分引擎内历史记忆（默认跳过已评过的——含手动 👍/👎）。

    emit(line)：进度实时输出，行内带 "[已处理/总数]"（面板/CLI 显示进度）；
    workers：LLM 评估/写入是 IO 密集，>1 用线程池并发（默认 1=串行）；
    skip_scored：跳过已评清单中的 trace（默认开）。评分成功（或手动打分）
    会记入清单，重跑不再覆盖。
    返回 {evaluated, skipped, positive, neutral, negative, errors, dryRun}。
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    out = emit or (lambda s: None)
    llm_cfg = read_engine_llm()
    root = (base_url or memos_daemon.base_url()).rstrip("/")
    summary = {"evaluated": 0, "skipped": 0, "positive": 0, "neutral": 0,
               "negative": 0, "errors": 0, "dryRun": bool(dry_run)}
    lock = threading.Lock()
    scored = _load_scored() if skip_scored else set()
    offset = 0
    page = 100
    total: Optional[int] = None

    def work(t: dict) -> None:
        tid = t.get("id", "")
        with lock:
            summary["evaluated"] += 1
            idx = summary["evaluated"]
        label = f"[{idx}/{total}]" if total else f"[{idx}]"
        if tid in scored:
            with lock:
                summary["skipped"] += 1
            out(f"{label} {tid} 已评过，跳过（手动或此前批量）")
            return
        out(f"{label} 评估 {tid} …")
        try:
            verdict = evaluate_trace(t, llm_cfg)
        except Exception as e:
            with lock:
                summary["errors"] += 1
            out(f"{label} ✗ 评估失败: {e}")
            return
        with lock:
            summary[verdict] += 1
        if verdict == "neutral":
            out(f"{label} → neutral（一般，不写入）")
            return
        if dry_run:
            out(f"{label} → {verdict}（dry-run 不写入）")
            return
        try:
            memos_daemon.engine_request(
                "POST", "/api/v1/feedback",
                body={"channel": "explicit", "polarity": verdict,
                      "magnitude": 1.0, "traceId": tid},
                base=root, timeout=30)
            mark_scored(tid)          # 写入成功 → 进入已评清单（重跑跳过）
            out(f"{label} → {verdict} ✓ 已写入")
        except Exception as e:
            with lock:
                summary["errors"] += 1
            out(f"{label} ✗ 写入失败: {e}")

    while True:
        res = memos_daemon.engine_request(
            "GET", f"/api/v1/traces?limit={page}&offset={offset}&groupByTurn=1",
            base=root, timeout=30)
        traces = res.get("traces") or []
        if not traces:
            break
        if total is None:
            total = int(res.get("total") or 0)
        remain = None if not limit else limit - summary["evaluated"]
        batch = traces
        if remain is not None:
            batch = traces[:remain]
        if not batch:
            break
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            list(ex.map(work, batch))          # map 保持批内顺序（并发执行）
        offset += len(batch)
        if limit and summary["evaluated"] >= limit:
            break
        if offset >= int(res.get("total") or 0):
            break
    return summary