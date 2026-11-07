"""历史记忆批量自动评分器。

背景：importBundle 导入的历史 trace 不参与引擎自动进化链（无 rewardDirty
标记），value 停留在启发式初值。旧引擎的 feedback 接口（POST /api/v1/feedback
带 traceId）会立即重算 value/rHuman，但 magnitude 被 0~1 钳制、value 是反馈
加权平均——只能表达极性分（positive/negative 二值），写不了任意连续分。

本模块按旧引擎的规则实现自动化打分：
- 复用引擎已配置的 LLM（engine config.yaml 的 llm 段，openai_compatible）
- 按 reward 的三轴思想（目标达成 / 过程质量 / 用户价值）逐条评估
- 判定 positive（值得保留）/ neutral（一般）/ negative（无价值或噪音）
- 三档 verdict 都记入跳过清单（neutral 不写 value 但不再重评）；网关内容审核
  拒评（code 1301）确定性无法评估 → 归 neutral 记账，不重复送、不算 error
- 通过 feedback 接口批量写入 → 引擎立即重算每条记忆的 value/priority，
  语义检索排序随之生效

约束：只通过引擎公开 API 交互，不改引擎源码与数据库；key 只读不落日志。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from agentmemhub import engine_api
from agentmemhub.llm import (  # 统一 LLM 纪律：重试 / 退避 / max_total_seconds /
    ContentFilterRejected,     # 全局限流闸门 / 审核拒评不重试（与蒸馏同源）
    LLMClient, LLMConfig, LLMError)

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
# 重跑「自动评分」时跳过——避免批量 verdict 覆盖/稀释手动打分）。三档 verdict
# （positive/neutral/negative）评估过都记入此清单：positive/negative 顺带写
# value，neutral 只记「已评」不写值——否则这批每次都重新枚举、白耗一次 LLM。
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


def mark_scored_many(trace_ids: list[str]) -> None:
    """批量记入已评清单（一次文件写）——neutral 判定这类「评过但不写 value」的
    条目用它，避免每条各写一遍全量 JSON。"""
    global _scored_cache
    ids = {t for t in trace_ids if t}
    if not ids:
        return
    with _cache_lock:
        s = _load_scored()
        before = len(s)
        s |= ids
        _scored_cache = s
        if len(s) == before:
            return
        try:
            _cache_path().write_text(
                json.dumps(sorted(s), ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass


def _mark_scored_inmem(trace_id: str) -> None:
    """只更新内存缓存的已评记账（**不写盘**）——批量评分循环里逐条用它，
    跑完由 `mark_scored_many` 一次性落盘。此前每条成功写入都调 `mark_scored`
    全量重写 JSON 文件（第 i 条写 i 行），4800 条评分的文件写入量是 O(n²)。

    权衡：进程在批中途崩溃时，已写 feedback 但未落盘的条目下次会重评一次
    （重评只是再投一次同向 feedback，幂等无害），换来批量路径写盘 O(n)。
    单条手动场景（👍/👎）仍直接用 `mark_scored` 立即落盘。
    """
    global _scored_cache
    with _cache_lock:
        s = _load_scored()
        s.add(trace_id)
        _scored_cache = s


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
    """读 LLM 配置（**与蒸馏同源**）：优先 agentmemhub.yaml 的 `llm` 段。

    评分与蒸馏共用同一份 LLM 接入（换模型/换服务商只改一处）；
    该段缺项时回退引擎自己的 config.yaml（历史环境兼容）。
    key 只读不打印。
    """
    from agentmemhub import config as hub_config
    llm = hub_config.config().llm or {}
    endpoint = str(llm.get("endpoint") or "")
    api_key = str(llm.get("api_key") or "")
    model = str(llm.get("model") or "")
    if not (endpoint and api_key and model):
        raise RuntimeError(
            "LLM 未配置：在 agentmemhub.yaml 的 llm 段填 endpoint/api_key/model"
            "（或 scripts/sync_llm_from_zcode.py 一键同步）")
    return {"endpoint": endpoint, "api_key": api_key, "model": model,
            "headers": dict(llm.get("headers") or {})}


def evaluate_trace(trace: dict, llm_cfg: dict,
                   timeout: Optional[float] = None) -> str:
    """LLM 三轴评估一条 trace，返回 verdict（positive|neutral|negative）。

    2026-11-06 起**改走 `llm.py` 的统一纪律**（此前用 urllib 裸连，实测撞过
    `read operation timed out` 白评一轮）：瞬态错误（429/5xx/网络抖动）按
    `llm.max_retries` 指数退避重试、429 走进程级全局限流闸门、
    `llm.max_total_seconds` 兜住非流式调用"一个字节都不发"的假超时——
    这些与蒸馏链路同源同配置。网关内容审核确定性拒评（智谱 1301 等）抛
    `ContentFilterRejected`，不重试。其余 LLM 错误抛 `LLMError`（含响应体
    原因，便于日志定位）。`llm_cfg` 除 endpoint/api_key/model/headers 外
    还接受 timeout / max_retries / backoff_base / max_total_seconds 等
    `llm` 段键（此前这些键对评分不生效）。
    模型输出解析失败（ValueError）维持旧行为：视作 neutral，不报错。
    """
    user_text = (trace.get("userText") or "").strip()[:800]
    agent_text = (trace.get("agentText") or "").strip()[:1200]
    if not user_text and not agent_text:
        return "neutral"
    # 记忆类（蒸馏结论 / Agent 原子记忆）的正文是「一条结论」，不是对话。
    # 套「用户：… 助手：…」模板会让模型按"对话有没有完成任务"去判，
    # 评错对象 —— 单条记忆该问的是"这条结论值不值得留下"。
    if trace.get("kind") == "memory":
        payload = f"记忆内容：\n{agent_text or user_text}"
    else:
        payload = f"任务内容：\n用户：{user_text}\n助手：{agent_text}"
    # 净化由 complete_json 内部统一做（scrub_text），这里不再重复
    cfg = LLMConfig.from_dict(
        llm_cfg, timeout=timeout, temperature=0.1, max_tokens=80)
    try:
        data = LLMClient(cfg).complete_json(
            _SYSTEM_PROMPT, payload, max_tokens=80, temperature=0.1)
    except ValueError:
        # 模型返回了内容但解不出 JSON：与旧 _parse_verdict 兜底一致 → neutral
        return "neutral"
    verdict = str(data.get("verdict") or "").strip().lower()
    return verdict if verdict in ("positive", "neutral", "negative") else "neutral"


def _parse_verdict(content: str) -> str:
    """旧版宽松解析（保留供历史调用方 / 测试；evaluate_trace 已改走
    complete_json + extract_json 的容错链，此函数不再是主路径）。"""
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


def _backend_is_rag() -> bool:
    """恒 True —— MemOS 回退后端已移除；保留函数只为不动旧调用方。"""
    return engine_api._backend_is_rag()


def list_all_traces() -> list[dict[str, Any]]:
    """引擎全部 traces（轮次视图，id 为可回指的锚点）。"""
    from agentmemhub import rag_bridge
    return rag_bridge.all_traces()


def list_trace_ids() -> list[str]:
    """引擎全部 trace id（不取正文，廉价）——增量评分先筛 id 再定点读。"""
    from agentmemhub import rag_bridge
    return rag_bridge.turn_refs()


def list_traces_by_ids(ids: set[str]) -> list[dict[str, Any]]:
    """定点读指定 trace id（零全量枚举）；缺失 id 由调用方 diff 出 missing。"""
    from agentmemhub import rag_bridge
    return rag_bridge.traces_by_ids(ids)


def count_unscored(*, base_url: str = "", traces: Optional[list[dict]] = None) -> int:
    """统计未评分记忆条数（供定时/定量触发判断），不做 LLM 评估。

    只读 id 集合（不读正文 blob）与已评清单求差——与 run_score_all 全量模式
    同一枚举口径。traces 测试注入用。
    """
    ids = ([t.get("id") for t in traces] if traces is not None
           else list_trace_ids())
    scored = _load_scored()
    return sum(1 for i in ids if i not in scored)


def sync_episode_r_task(*, trace_ids: Optional[list[str]] = None,
                        episode_ids: Optional[list[str]] = None) -> int:
    """把会话评分同步进 `conv_scores`（引擎侧 episode.r_task 的对应物）。

    内置引擎下由 `rag_bridge.sync_conv_scores` 全量重算——feedback 写入时已
    滚动更新，这里用于兜底保持一致。`trace_ids` / `episode_ids` 仅为兼容
    旧调用方签名保留。
    """
    from agentmemhub import rag_bridge
    return int(rag_bridge.sync_conv_scores().get("conversations", 0))


def run_score_all(*, emit: Optional[Callable[[str], None]] = None,
                  base_url: str = "", limit: int = 0,
                  dry_run: bool = False,
                  workers: int = 1,
                  skip_scored: bool = True,
                  on_progress: Optional[Callable[[int, int], None]] = None,
                  traces: Optional[list[dict]] = None,
                  only_ids: Optional[set[str]] = None,
                  ) -> dict[str, Any]:
    """批量自动评分引擎内历史记忆（默认跳过已评过的——含手动 👍/👎）。

    数据来源三级策略（零无谓全量枚举）：
    - only_ids 给定（score --pending / --ids）：list_traces_by_ids 定点只读，
      引擎库全表枚举不发生；请求了但库里没有的 id 计入 summary.missing；
    - 全量模式：list_trace_ids 廉价取 id → 减去已评清单 → 只定点读未评正文
      （重跑不再每次全库读所有 blob；已评数不进循环，无逐条「跳过」刷屏）；
    - traces 注入（测试用）：按原样使用，only_ids/skip_scored 作过滤器。
    emit(line)：进度实时输出（行内 [已处理/总数]）；
    on_progress(done, total)：结构化进度回调（面板进度条，逐条处理调用）；
    workers 并发；skip_scored 默认跳过已评。
    返回 {evaluated, skipped, positive, neutral, negative, errors, missing, dryRun}。
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    out = emit or (lambda s: None)
    llm_cfg = read_engine_llm()
    root = (base_url or "").rstrip("/")
    summary = {"evaluated": 0, "skipped": 0, "positive": 0, "neutral": 0,
               "negative": 0, "errors": 0, "missing": 0, "dryRun": bool(dry_run)}
    lock = threading.Lock()
    scored = _load_scored() if skip_scored else set()
    if traces is not None:
        # 测试注入：直接用传入列表，only_ids/skip_scored 作为过滤器
        all_traces = traces
        if only_ids:
            have = {t.get("id") for t in all_traces}
            missing_ids = sorted(only_ids - have)
            summary["missing"] = len(missing_ids)
            all_traces = [t for t in all_traces if t.get("id") in only_ids]
    elif only_ids:
        # score --pending / --ids：只定点读队列里的 id，零全量枚举
        all_traces = list_traces_by_ids(only_ids)
        have = {t.get("id") for t in all_traces}
        missing_ids = sorted(only_ids - have)
        summary["missing"] = len(missing_ids)
        if missing_ids:
            out(f"⚠ {len(missing_ids)} 个 id 未在引擎库中找到，跳过（如 {missing_ids[0]}…）")
    else:
        # 全量评分：先廉价取 id 集、减去已评清单，再只定点读未评正文——
        # 重跑不再每次全库读所有 blob（旧实现 list_all_traces 全量枚举）
        todo = list_trace_ids()
        if skip_scored:
            before = len(todo)
            todo = [i for i in todo if i not in scored]
            summary["skipped"] += before - len(todo)
        all_traces = list_traces_by_ids(set(todo)) if todo else []
    total = len(all_traces)
    if limit:
        all_traces = all_traces[:limit]
    written_ids: list[str] = []
    neutral_ids: list[str] = []   # 评过但判「一般」——不写 value，但要记入跳过清单

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
        else:
            out(f"{label} 评估 {tid} …")
            try:
                verdict = evaluate_trace(t, llm_cfg)
            except ContentFilterRejected:
                # 网关内容审核确定性拒评：无法评估 → 视作 neutral（不写 value），
                # 记入跳过清单，下次不再重复送这条（否则每次评分都卡这 1 条报错）
                with lock:
                    summary["neutral"] += 1
                    if not dry_run:
                        neutral_ids.append(tid)
                out(f"{label} → neutral（内容审核拒评，无法评估；不写 value"
                    + ("、记入跳过清单）" if not dry_run else "、dry-run 不记录）"))
            except Exception as e:
                with lock:
                    summary["errors"] += 1
                out(f"{label} ✗ 评估失败: {e}")
            else:
                with lock:
                    summary[verdict] += 1
                if verdict == "neutral":
                    if not dry_run:
                        with lock:
                            neutral_ids.append(tid)
                        out(f"{label} → neutral（一般，不写 value，已记入跳过清单）")
                    else:
                        out(f"{label} → neutral（一般，dry-run 不记录）")
                elif dry_run:
                    out(f"{label} → {verdict}（dry-run 不写入）")
                else:
                    try:
                        engine_api.engine_request(
                            "POST", "/api/v1/feedback",
                            body={"channel": "explicit", "polarity": verdict,
                                  "magnitude": 1.0, "traceId": tid},
                            base=root, timeout=30)
                        _mark_scored_inmem(tid)  # 内存记账防批内双评；落盘统一在批末
                        written_ids.append(tid)
                        out(f"{label} → {verdict} ✓ 已写入")
                    except Exception as e:
                        with lock:
                            summary["errors"] += 1
                        out(f"{label} ✗ 写入失败: {e}")
        if on_progress is not None:
            try:
                on_progress(idx, total)
            except Exception:
                pass

    if all_traces:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            list(ex.map(work, all_traces))
    # 已评清单统一一次落盘（written + neutral 合并）：批内靠内存记账防双评，
    # 文件写入从 O(n²)（每条全量重写）降为 O(n)（一次）
    flush_ids = written_ids + neutral_ids
    if flush_ids:
        mark_scored_many(flush_ids)
    if neutral_ids:
        out(f"已记入 {len(neutral_ids)} 条 neutral 到跳过清单（下次不再重复评估）")
    if written_ids:
        try:
            n = sync_episode_r_task(trace_ids=written_ids)
            out(f"已同步 {n} 个 episode 的 r_task（viewer 评分标签）")
        except Exception as e:
            out(f"r_task 同步失败（不影响已写入反馈）: {e}")
    return summary


def run_score_incremental(*, emit: Optional[Callable[[str], None]] = None,
                          base_url: str = "", limit: int = 0,
                          dry_run: bool = False,
                          workers: int = 1,
                          on_progress: Optional[Callable[[int, int], None]] = None,
                          ) -> dict[str, Any]:
    """评分统一入口（CLI / 控制台 / 看板共用）：增量优先。

    依据 watermarks 的 pending_score 队列（sync 推送成功后自动入队的新 trace）：
    - 队列非空 → 只评队列里的 id（定点读引擎库，零全量枚举）；
    - 队列空   → 全量扫描未评（先廉价筛 id、只定点读未评正文，无逐条「跳过」刷屏）。
    队列消费规则：非 dry-run 且本轮无失败 → 已处理 id 出队；有失败 → 队列原样
    保留下次重试（已成功条由已评清单保护，重跑绝不双评）。limit 为队列消费
    上限（超出部分留在队列）。
    返回 run_score_all 的 summary，附加 mode（pending|full）。
    """
    from agentmemhub import config, watermarks
    out = emit or (lambda s: None)
    data_dir = config.config().data_dir
    st = watermarks.load_state(data_dir)
    ids = list(st.get("pending_score") or [])
    if not ids:
        r = run_score_all(emit=out, base_url=base_url, limit=limit,
                          dry_run=dry_run, workers=workers,
                          on_progress=on_progress)
        r["mode"] = "full"
        return r
    take = ids[:limit] if limit else ids
    rest = ids[len(take):]
    out(f"增量评分：pending_score 队列 {len(ids)} 条，本轮处理 {len(take)} 条"
        f"（定点读取，不做全量枚举）…")
    r = run_score_all(emit=out, base_url=base_url, dry_run=dry_run,
                      workers=workers, on_progress=on_progress,
                      only_ids=set(take))
    if not dry_run:
        # 有失败：整队列保留下次重试；无失败：已处理条出队
        st["pending_score"] = ids if r.get("errors", 0) else rest
        watermarks.save_state(data_dir, st)
    r["mode"] = "pending"
    return r