"""MemOS 历史记忆批量自动评分器。

背景：importBundle 导入的历史 trace 不参与引擎自动进化链（无 rewardDirty
标记），value 停留在启发式初值。MemOS 的 feedback 接口（POST /api/v1/feedback
带 traceId）会立即重算 value/rHuman，但 magnitude 被 0~1 钳制、value 是反馈
加权平均——只能表达极性分（positive/negative 二值），写不了任意连续分。

本模块"用 MemOS 的方式"实现自动化打分：
- 复用引擎已配置的 LLM（engine config.yaml 的 llm 段，openai_compatible）
- 按 MemOS reward 的三轴思想（目标达成 / 过程质量 / 用户价值）逐条评估
- 判定 positive（值得保留）/ neutral（一般）/ negative（无价值或噪音）
- 三档 verdict 都记入跳过清单（neutral 不写 value 但不再重评）；网关内容审核
  拒评（code 1301）确定性无法评估 → 归 neutral 记账，不重复送、不算 error
- 通过 feedback 接口批量写入 → 引擎立即重算每条记忆的 value/priority，
  语义检索排序随之生效

约束：只通过引擎公开 API 交互，不改引擎源码与数据库；key 只读不落日志。
"""
from __future__ import annotations

import json
import os
import threading
import urllib.error
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


def _llm_opener() -> urllib.request.OpenerDirector:
    """LLM 评估专用 opener：**强制直连**。

    Python urllib 默认会自动采用 Windows 系统代理（注册表）与 *_proxy 环境变量
    ——Clash 等工具开关/切节点会让 LLM 请求时通时断（TLS 握手被掐 =
    UNEXPECTED_EOF）。这里显式置空代理表，直连不受本机代理状态影响；
    确需走代理的私有部署用环境变量 AGENTMEMHUB_LLM_PROXY 显式指定。
    """
    proxy = os.environ.get("AGENTMEMHUB_LLM_PROXY", "").strip()
    return urllib.request.build_opener(
        urllib.request.ProxyHandler(
            {"http": proxy, "https": proxy} if proxy else {}))


class ContentFilterRejected(Exception):
    """LLM 网关因输入内容审核（bigmodel code 1301 等）确定性拒评——非网络/鉴权错误，
    重试永远 400，应按「无法评估」处理（视作 neutral、记入跳过清单、不写 value）。"""


def _scrub_text(s: str) -> str:
    """剥离零宽/控制字符（0x200b 等不可见码点最易触发审核/解析异常），
    保留 \n\t；用于评估前净化正文。"""
    if not s:
        return s
    import unicodedata
    keep = []
    for ch in s:
        cat = unicodedata.category(ch)
        if ch in ("\n", "\t") or cat not in ("Cc", "Cf", "Cs", "Co", "Cn"):
            keep.append(ch)
    return "".join(keep)


def _is_content_filter(code: int, body: str) -> bool:
    """判定是否内容审核类 400（智谱 1301 / contentFilter / 敏感字样）。"""
    if code != 400 or not body:
        return False
    return ("1301" in body) or ("contentFilter" in body) or ("敏感内容" in body) \
        or ("不安全" in body)


def evaluate_trace(trace: dict, llm_cfg: dict, timeout: float = 45) -> str:
    """LLM 三轴评估一条 trace，返回 verdict（positive|neutral|negative）。

    正文净化后再送；网关内容审核确定性拒评抛 ContentFilterRejected，其它
    HTTP/网络错误抛出并带上响应体原因（便于日志定位，不再只有一句 Bad Request）。
    """
    user_text = _scrub_text((trace.get("userText") or "").strip())[:800]
    agent_text = _scrub_text((trace.get("agentText") or "").strip())[:1200]
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
    try:
        with _llm_opener().open(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        if _is_content_filter(e.code, detail):
            raise ContentFilterRejected(detail or "内容审核拒绝") from None
        raise RuntimeError(f"HTTP {e.code} {e.reason}"
                           + (f": {detail}" if detail else "")) from None
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


def _engine_db_path() -> Path:
    home = memos_daemon.engine_home()
    if home is None:
        raise RuntimeError("未找到引擎 home（先配置 memos.home 或确认引擎默认位置）")
    return home / "data" / "memos.db"


def list_all_traces() -> list[dict[str, Any]]:
    """只读枚举引擎库全部 traces（绕开 listTraces 的 500 行分页窗口上限）。

    引擎 listTraces 在 repo 层把 fetch 窗口钳到最新 500 行（clampLimit 500），
    超过 500 条时 API 分页取不到更早的 trace——评分需要全量，故只读直连
    引擎 SQLite 枚举（仅 SELECT，不修改引擎数据；feedback 写入仍走 API）。
    """
    import sqlite3
    db = _engine_db_path()
    if not db.exists():
        raise RuntimeError(f"引擎记忆库不存在：{db}（先启动引擎或确认 memos.home）")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT id, user_text, agent_text FROM traces ORDER BY ts").fetchall()
        return [{"id": r[0], "userText": r[1] or "", "agentText": r[2] or ""}
                for r in rows]
    finally:
        conn.close()


def list_trace_ids() -> list[str]:
    """只读枚举引擎库全部 trace id（不取正文，廉价）——增量评分先筛 id 再定点读。"""
    import sqlite3
    db = _engine_db_path()
    if not db.exists():
        raise RuntimeError(f"引擎记忆库不存在：{db}（先启动引擎或确认 memos.home）")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return [r[0] for r in conn.execute("SELECT id FROM traces ORDER BY ts").fetchall()]
    finally:
        conn.close()


def list_traces_by_ids(ids: set[str]) -> list[dict[str, Any]]:
    """定点只读指定 trace id（增量评分路径：零全量枚举，500/批防变量数上限）。

    与 list_all_traces 同口径（id/userText/agentText）；不存在的 id 不报错，
    由调用方 diff 出 missing。
    """
    import sqlite3
    id_list = sorted({str(i) for i in ids if i})
    if not id_list:
        return []
    db = _engine_db_path()
    if not db.exists():
        raise RuntimeError(f"引擎记忆库不存在：{db}（先启动引擎或确认 memos.home）")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    result: list[tuple[Any, dict[str, Any]]] = []
    try:
        for i in range(0, len(id_list), 500):
            chunk = id_list[i:i + 500]
            marks = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT id, user_text, agent_text, ts FROM traces WHERE id IN ({marks})",
                tuple(chunk)).fetchall()
            result.extend((r[3], {"id": r[0], "userText": r[1] or "",
                                  "agentText": r[2] or ""}) for r in rows)
    finally:
        conn.close()
    result.sort(key=lambda x: x[0] or 0)   # 与 list_all_traces 同口径（ts 升序）
    return [d for _, d in result]


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
    """把 episode.r_task 同步为它受评 traces 的平均 value。

    viewer 的记忆列表「评分」标签读 episodeRTask（memory-core.ts:5598 的
    episodeRTask = episode?.rTask ?? null）——写入 r_task 后 viewer 不再把
    已评记忆显示为「待评分」。引擎零改动（直接写 episodes 表）；引擎自身
    reward 管线若日后运行会覆盖，本库无 rewardDirty 不触发、可接受。
    """
    import sqlite3
    db = _engine_db_path()
    if not db.exists():
        raise RuntimeError(f"引擎记忆库不存在：{db}")
    conn = sqlite3.connect(db, timeout=30)
    updated = 0
    try:
        if trace_ids:
            qmarks = ",".join("?" * len(trace_ids))
            rows = conn.execute(
                f"SELECT DISTINCT episode_id FROM traces WHERE id IN ({qmarks})",
                tuple(trace_ids)).fetchall()
            episode_ids = [r[0] for r in rows]
        if not episode_ids:
            episode_ids = [r[0] for r in conn.execute(
                "SELECT id FROM episodes").fetchall()]
        for ep in episode_ids:
            avg = conn.execute(
                "SELECT AVG(value) FROM traces WHERE episode_id=? AND value <> 0",
                (ep,)).fetchone()[0]
            conn.execute(
                "UPDATE episodes SET r_task=? WHERE id=?",
                (float(avg) if avg is not None else 0.0, ep))
            updated += 1
        conn.commit()
    finally:
        conn.close()
    return updated


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
    root = (base_url or memos_daemon.base_url()).rstrip("/")
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
                        memos_daemon.engine_request(
                            "POST", "/api/v1/feedback",
                            body={"channel": "explicit", "polarity": verdict,
                                  "magnitude": 1.0, "traceId": tid},
                            base=root, timeout=30)
                        mark_scored(tid)          # 写入成功 → 进入已评清单（重跑跳过）
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
    if neutral_ids:
        mark_scored_many(neutral_ids)             # 一次写盘：neutral 不再每次重评
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