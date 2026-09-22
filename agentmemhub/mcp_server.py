"""AgentMemHub MCP 记忆网关（stdio / Streamable HTTP 双传输）。

把本地记忆引擎（MemOS）的语义检索/读写包装成 MCP server，挂在
ZCode / OpenCode / Claude Code 等支持 MCP 的 Agent harness 上。

设计原则：
- 引擎生命周期完全由用户控制（看板 / `agentmemhub memos-daemon`）；
  本网关**只转发请求、从不拉起或停止引擎**，引擎离线时返回明确错误。
- 业务复用 memos_daemon.engine_request（含自动登录），不写本地库：
  检索/写入全部实时走引擎 HTTP API。
- 协议层（JSON-RPC 处理）与传输层分离，两种传输共用同一份逻辑。

传输方式：
- **stdio**（默认）：`python -m agentmemhub mcp`，由 Agent host 拉起子进程
  使用；本地个人场景。可用 console script `agentmemhub-mcp`（PATH 内）。
- **Streamable HTTP**（--http）：`python -m agentmemhub mcp --http
  [--bind 0.0.0.0] [--port 9100]`，单 endpoint `POST /mcp`（JSON-RPC，
  非流式响应；GET 405、DELETE 结束会话）；一台机器常驻，局域网/团队
  多个客户端共享同一个记忆引擎。默认只绑 127.0.0.1，团队共享需显式
  `--bind 0.0.0.0`（按需自设访问控制）。

Tools:
- memory_search(query, topK)  语义检索历史记忆（转发 /api/v1/memory/search）
- memory_recent(limit)        最近记忆时间线（/api/v1/traces）
- memory_stats()              引擎在线状态与记忆概要
- memory_save(content, tags)  写入一条记忆（构造 bundle → /api/v1/import）
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import time
import uuid
from typing import Any, Callable, Optional

from agentmemhub import memos_daemon

# ---- Streamable HTTP 软依赖 ----------------------------------------------
# fastapi 仅 --http 模式需要；stdio 模式不强制安装。顶部 import 是为了让
# FastAPI 能从模块全局命名空间解析 build_http_app 里的注解（函数内 import
# 不会注册到模块全局，否则注解会被当成查询参数）。
try:
    from fastapi import FastAPI  # noqa: F401
    from fastapi import Request  # noqa: F401
    _HAS_FASTAPI = True
except ImportError:
    _HAS_FASTAPI = False

SERVER_INFO = {"name": "agentmemhub-mcp", "version": "0.1.0"}

#: 引擎记忆空间的 agent 标识（与 web 网关 /api/memos/* 口径一致）
_AGENT = "hermes"

def _engine_hint() -> str:
    """引擎不可用时的提示（按后端区分：rag 进程内无守护，memos 才有 daemon）。"""
    from agentmemhub import memos_daemon
    if memos_daemon._backend_is_rag():
        return ("记忆索引不可用——检查 database/session_rag.db 与 models/ 是否就绪；"
                "首次使用先跑 `agentmemhub sync` 建立索引")
    return ("记忆引擎未运行——先启动：`agentmemhub memos-daemon start`，"
            "或看板「记忆引擎 → 启动」后重试")


def _auth_hint() -> str:
    from agentmemhub import memos_daemon
    if memos_daemon._backend_is_rag():
        return "记忆索引无鉴权面；若持续失败请检查索引库权限"
    return ("引擎已设密码且网关未登录：先运行 `agentmemhub memos-daemon "
            "--set-password <密码>` 保存密码后重试")


class _ToolError(Exception):
    """工具级错误（以 isError 结果返回给模型，而不是协议错误）。"""


def _clamp(v: Any, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def _trace_id(content: str, ts: int) -> str:
    digest = hashlib.sha256(f"mcp|{ts}|{content}".encode("utf-8")).hexdigest()
    return "mcp_" + digest[:16]


# ---------------------------------------------------------------------------
# Tools（模块级函数：返回展示文本；错误抛 _ToolError）
# ---------------------------------------------------------------------------

def _search(args: dict) -> str:
    _t0 = time.time()               # 记忆操作审计用：本次检索耗时
    q = str(args.get("query", "")).strip()
    if not q:
        raise _ToolError("memory_search 需要 query 参数")
    top = _clamp(args.get("topK"), 1, 30, 8)
    org = str(args.get("origin") or "").strip().lower()
    if org not in ("", "native", "external"):
        raise _ToolError("origin 必须是 native | external（留空=全部）")
    try:
        body = {"agent": _AGENT, "query": q}
        if org:
            body["origin"] = org
        # 标注调用方 → 决定用哪一档召回严格度（对应 `rag.retrieval.callers.mcp`）。
        # Agent 的自动召回通常希望"少而准"，可以比面板查询配得更严。
        from agentmemhub.rag.config import retrieval_caller
        with retrieval_caller("mcp"):
            res = memos_daemon.engine_request("POST", "/api/v1/memory/search",
                                              body=body, timeout=30)
            ov: dict = {}
            try:
                ov = memos_daemon.engine_request("GET", "/api/v1/overview",
                                                 timeout=8)
            except Exception:
                pass
    except memos_daemon.EngineAuthError:
        raise _ToolError(_auth_hint())
    except Exception as e:
        raise _ToolError(f"引擎检索失败：{e}")

    hits = res.get("hits") or []
    # 记忆操作事实流：**读也要留痕**（"什么时候读了什么"与写入同样重要——
    # 排查"某条记忆为什么没被想起"时，要能区分"没写过"和"写了但没召回"）
    try:
        from collections import Counter
        from agentmemhub import logs as _logs
        _logs.audit_memory({
            "event": "read", "ts": round(time.time(), 3), "path": "mcp",
            "actor": _AGENT, "query": q,
            "top": top, "hits": len(hits), "origin_filter": org or "all",
            "kinds": dict(Counter((h.get("kind") or "message") for h in hits)),
            "elapsed_ms": int((time.time() - _t0) * 1000),
        })
    except Exception:                       # noqa: BLE001 —— 审计旁路
        pass
    scope = {"native": "（仅自有记忆沉淀）", "external": "（仅外部投喂）"}.get(org, "")
    shown = hits[:top]
    head = (f"记忆检索「{q}」{scope}：{len(shown)} 条命中"
            f"（引擎在线，episodes={ov.get('episodes')}, traces={ov.get('traces')}）")
    if len(hits) > len(shown):
        # 引擎实际返回更多（curate 只做相关度截断、不硬砍条数）——必须说清
        # "共多少 / 给了多少"，否则 Agent 会以为库里只有这几条（实测踩过：
        # 头部写"19 条命中"却只列 8 条）。
        head += (f"\n（引擎共返回 {len(hits)} 条，按相关度取前 {len(shown)} 条；"
                 f"需要更多可调大 topK）")
    lines = [head, ""]
    tier_label = {"page": "知识页", "memory": "记忆", "message": "对话"}
    for h in shown:
        kind = h.get("kind") or "message"
        origin = h.get("origin") or "native"
        src_tag = "自有" if origin == "native" else "投喂"
        path = f" → {h.get('wikiPath')}" if h.get("wikiPath") else ""
        lines.append(f"- [{tier_label.get(kind, kind)}·{src_tag}] "
                     f"score={h.get('score')} {h.get('title') or ''}{path}")
        lines.append(f"  {h.get('snippet') or ''}")
    ctx = (res.get("injectedContext") or "").strip()
    if ctx:
        lines += ["", f"引擎建议注入上下文：{ctx[:400]}" + ("…" if len(ctx) > 400 else "")]
    return "\n".join(lines)


def _recent(args: dict) -> str:
    limit = _clamp(args.get("limit"), 1, 30, 8)
    try:
        res = memos_daemon.engine_request(
            "GET", f"/api/v1/traces?limit={limit}&offset=0&groupByTurn=1", timeout=15)
    except memos_daemon.EngineAuthError:
        raise _ToolError(_auth_hint())
    except Exception as e:
        raise _ToolError(f"引擎读取失败：{e}")

    traces = res.get("traces") or []
    if not traces:
        return "记忆库为空——尚无已写入的记忆。"
    lines = [f"最近记忆（{len(traces)} 条）:", ""]
    for t in traces:
        ts = t.get("ts") or 0
        if ts > 1e12:      # 毫秒 → 秒
            ts = ts / 1000
        try:
            when = time.strftime("%m-%d %H:%M", time.localtime(ts))
        except Exception:
            when = ""
        user = (t.get("userText") or "").strip().replace("\n", " ")
        agent = (t.get("agentText") or "").strip().replace("\n", " ")
        lines.append(f"- [{when}] value={t.get('value')} {user[:80]}")
        if agent:
            lines.append(f"    ↳ {agent[:120]}")
    return "\n".join(lines)


def _stats(args: dict) -> str:
    st = memos_daemon.daemon_status()
    if not st["online"]:
        raise _ToolError(_engine_hint())
    lines = [f"记忆引擎：在线（{st['base_url']}）"]
    s = st.get("summary") or {}
    lines.append(f"- 记忆总量：episodes={s.get('episodes')}，traces={s.get('traces')}")
    lines.append(f"- 语义检索：{'可用（模型 ' + str(s.get('embedding_model')) + '）' if s.get('embedding_ready') else '未就绪'}")
    lines.append(f"- LLM 评分：{'可用' if s.get('llm_available') else '不可用'}")
    lw = st.get("lightweight")
    mode = "轻量" if lw is True else ("完整进化" if lw is False else "引擎自管")
    lines.append(f"- 记忆模式：{mode}")
    lines.append(f"- 鉴权：{'已通过' if (st.get('auth') or {}).get('authenticated') else '未登录'}")
    return "\n".join(lines)


def _save(args: dict) -> str:
    content = str(args.get("content", "")).strip()
    if not content:
        raise _ToolError("memory_save 需要 content 参数")
    # 初始价值：Agent 用当前推理顺手给一个档位（可选），省掉为写入再外挂评分模型。
    # 用枚举而非连续分值——项目实证：Agent/LLM 对枚举的遵循率明显高于数字刻度。
    # 不传 = normal（= BASE_VALUE_AGENT_WRITE），与历史行为完全一致。
    # 引擎侧「存值不判值」，这里只是把策略层的判断翻译成起点值。
    from agentmemhub.rag.memstore import AGENT_IMPORTANCE_VALUES
    importance = str(args.get("importance") or "normal").strip().lower()
    if importance not in AGENT_IMPORTANCE_VALUES:
        raise _ToolError("importance 必须是 "
                         + " | ".join(AGENT_IMPORTANCE_VALUES))
    value = AGENT_IMPORTANCE_VALUES[importance]
    # 可选标签：只做透传（引擎不解释其语义），供面板筛选与溯源使用。
    # 非序列或空串一律忽略，避免写入脏数据。
    raw_tags = args.get("tags")
    tags = ([str(t).strip() for t in raw_tags if str(t).strip()]
            if isinstance(raw_tags, (list, tuple)) else [])
    ts = int(time.time() * 1000)
    tid = _trace_id(content, ts)
    bundle = {
        "version": 1,
        "traces": [{
            "id": tid, "episodeId": "mcp", "sessionId": "mcp",
            "ts": ts, "turnId": ts,
            "userText": content, "agentText": "",
            "summary": content[:200],
            "value": value, "alpha": 0.3,
            "priority": value,
            "tags": tags,
            "toolCalls": [], "agentThinking": None,
        }],
        "policies": [], "worldModels": [], "skills": [],
    }
    # 写后验证：import 偶发瞬时失败（SQLITE_BUSY 被引擎静默吞，返回 imported=0）
    # 时重试一次；仍失败则落日志并明确报错，不再伪装“已写入”。
    imported = skipped = 0
    resp: dict = {}
    for attempt in (1, 2):
        try:
            from agentmemhub.memos import push_bundle
            resp = push_bundle(bundle, memos_daemon.base_url())
        except memos_daemon.EngineAuthError:
            raise _ToolError(_auth_hint())
        except Exception as e:
            if attempt == 2:
                raise _ToolError(f"记忆写入失败：{e}")
            time.sleep(0.6)
            continue
        imported = int(resp.get("imported", 0) or 0)
        skipped = int(resp.get("skipped", 0) or 0)
        if imported > 0:
            break
        time.sleep(0.6)
    if imported == 0:
        try:
            from agentmemhub import logs
            logs.record(f"memory_save 写入未生效 id={tid} imported=0 skipped={skipped}",
                        level="error", actor="mcp", dest="cli")
        except Exception:
            pass
        raise _ToolError(f"记忆写入未生效（imported=0, skipped={skipped}），请稍后重试")
    # 单条入库后补一次增量向量（repair：只补缺失），失败不阻塞
    try:
        memos_daemon.engine_request(
            "POST", "/api/v1/embeddings/rebuild",
            body={"mode": "repair"}, timeout=300)
    except Exception:
        pass
    # 同步一条蒸馏表记录：MCP 直写的记忆此前不进 distilled_memories —— 记忆
    # 报表看不见、wiki 编译输入（status IN new/similar）完全不可见。落表后
    # 报表与 wiki 才能覆盖 Agent 主动写入的记忆（投影由下次蒸馏统一补齐）。
    # 全程旁路：失败只记日志，不影响 memory_save 的返回。
    try:
        from agentmemhub import distill as _distill
        from agentmemhub.rag.config import load_settings
        from agentmemhub.rag.ingest import open_index
        conf_map = {"high": "high", "normal": "medium", "low": "low"}
        # 可选 type：Agent 写记忆时通常知道类型（decision/lesson/...），默认 fact。
        mem_type = str(args.get("type") or "fact").strip().lower() or "fact"
        idx = open_index(load_settings().index_db)
        try:
            saved = _distill.save_direct_memory(
                idx, content=content, type_=mem_type,
                confidence=conf_map.get(importance, "medium"),
                # tid 形如 'mcp_<hash>'；面板去重与回填的口径是 'mcp:<hash>'
                #（substr 去掉 units.src_id 的 'mcp_' 前缀），这里必须对齐，
                # 否则 slice_key 变 'mcp:mcp_<hash>'，去重匹配不上 → 面板重复
                slice_key=f"mcp:{tid.removeprefix('mcp_')}",
                model="memory_save(mcp)",
                # 记忆操作事实流（logs/memory.log）的**协议层补充**：这些字段
                # 只有 Agent 侧拿得到（importance/tags/trace_id 都是入参），
                # 数据层记的是"落了哪条、内容是什么"。
                audit_extra={"path": "mcp", "trace_id": tid,
                             "importance": importance, "value": value,
                             "tags": tags, "actor": _AGENT})
            # 落表即补齐检索面：来源初始分 + 增量投影（含跨会话去重判定），
            # 评分/加权/召回立即可用，不等下次蒸馏（阈值与蒸馏配置同源）。
            # Agent 直写的初始分再按 importance 档位精调（0.8/0.6/0.4）——
            # 投影默认只给来源语义分，importance 是 Agent 对重要性的明确判断
            if saved in ("inserted", "revived"):
                from agentmemhub.rag.memstore import AGENT_IMPORTANCE_VALUES
                _distill.backfill_base_values(idx)
                pending = _distill._pending_projection(idx)
                if pending:
                    dedup = ((load_settings().distill or {}).get("dedup")
                             or {}) if hasattr(load_settings(), "distill") else {}
                    _distill.project_memories(
                        idx, load_settings(), pending,
                        duplicate_threshold=float(
                            dedup.get("cosine_duplicate") or 0.92),
                        similar_threshold=float(
                            dedup.get("cosine_similar") or 0.80))
                idx.execute(
                    "UPDATE unit_values SET value=? WHERE unit_id IN"
                    " (SELECT u.id FROM units u JOIN distilled_memories dm"
                    "  ON u.src_id = 'dst_' || dm.content_hash"
                    "  WHERE dm.slice_key=?)",
                    (AGENT_IMPORTANCE_VALUES[importance],
                     f"mcp:{tid.removeprefix('mcp_')}"))
        finally:
            idx.close()
    except Exception as e:                      # noqa: BLE001
        saved = f"error: {e}"
        try:
            from agentmemhub import logs
            logs.record(f"memory_save 蒸馏表同步失败 id={tid}: {e}",
                        level="error", actor="mcp", dest="cli")
        except Exception:
            pass
    # wiki 增量更新触发检测：挂在 MCP 写入时（用户定下的模型——每次写入
    # 过一遍检测，满足规则才触发，无后台定时任务）。异步 daemon 线程，
    # 不阻塞 MCP 响应；update 单飞行锁 + manifest 基线天然防重复。
    try:
        import threading as _th
        _th.Thread(target=_wiki_trigger_probe, daemon=True).start()
    except Exception:
        pass
    return (f"记忆已写入（id={tid}，imported={imported}）\n"
            f"内容：{content[:120]}" + ("…" if len(content) > 120 else ""))


def _wiki_trigger_probe() -> None:
    """wiki 触发器检测（daemon 线程入口）——全程旁路，绝不影响 MCP 响应。"""
    try:
        from agentmemhub import wiki_triggers
        wiki_triggers.on_memories_written()
    except Exception:
        pass


def _score(args: dict) -> str:
    """写后即评：给已写入的记忆打极性分（feedback explicit → 引擎立即重算 value/priority）。"""
    tid = str(args.get("trace_id") or args.get("traceId") or "").strip()
    if not tid:
        raise _ToolError("memory_score 需要 trace_id 参数")
    polarity = str(args.get("polarity") or "").strip().lower()
    if polarity not in ("positive", "neutral", "negative"):
        raise _ToolError("polarity 必须是 positive | neutral | negative")
    try:
        memos_daemon.engine_request(
            "POST", "/api/v1/feedback",
            body={"channel": "explicit", "polarity": polarity,
                  "magnitude": 1.0, "traceId": tid},
            timeout=30)
    except memos_daemon.EngineAuthError:
        raise _ToolError(_auth_hint())
    except Exception as e:
        raise _ToolError(f"评分失败（trace 不存在或引擎异常）：{e}")
    try:
        from agentmemhub.scoring import mark_scored
        mark_scored(tid)          # 进入已评清单，后续批量重跑不再覆盖/稀释
    except Exception:
        pass
    try:
        from agentmemhub.scoring import sync_episode_r_task
        sync_episode_r_task(trace_ids=[tid])   # 同步所在 episode 的 r_task，viewer 评分标签即时可见
    except Exception:
        pass
    return f"已评分（{tid} → {polarity}，引擎已重算 value/priority）"


_TOOL_HANDLERS: dict[str, Callable[[dict], str]] = {
    "memory_search": _search,
    "memory_recent": _recent,
    "memory_stats": _stats,
    "memory_save": _save,
    "memory_score": _score,
}

#: `note` 参数说明：Agent 补充的**意图**（为什么做这次操作）。
#: 调用本身（是什么、参数、结果、耗时）已由服务端在 _tools_call 自动记录，
#: 这个参数补充的是"为什么"——那部分只有 Agent 知道。
_NOTE_DESC = ("（可选）本次操作的意图/理由——为什么查这个、为什么值得记、"
              "为什么这样评分。只写入审计日志 logs/mcp.log，不参与检索与存储。")

_TOOLS: list[dict] = [
    {
        "name": "memory_search",
        "description": "从本地记忆引擎语义检索历史记忆。开始新任务、或用户问题涉及之前做过/讨论过的内容时调用。返回相关记忆条目（命中分/层级/摘要）与引擎建议注入上下文。引擎离线时返回明确错误。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索查询，自然语言描述想找的记忆主题"},
                "topK": {"type": "integer", "description": "返回条数（默认 8，最大 30）"},
                "origin": {
                    "type": "string",
                    "enum": ["native", "external"],
                    "description": (
                        "（可选）按数据来源筛选：native=自有记忆沉淀（会话蒸馏/Agent 直写/"
                        "自生成知识页），external=外部投喂（PDF/网页等素材）。"
                        "不传=两者都返回。返回结果每条的 origin 字段即其来源。"
                    ),
                },
                "note": {"type": "string", "description": _NOTE_DESC},
            },
            "required": ["query"],
        },
    },
    {
        "name": "memory_recent",
        "description": "查看最近写入记忆引擎的若干条记忆（时间线）。用于快速了解近期积累了什么、或确认一次写入是否生效。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "条数（默认 8，最大 30）"},
                "note": {"type": "string", "description": _NOTE_DESC},
            },
        },
    },
    {
        "name": "memory_stats",
        "description": "查询记忆引擎在线状态、记忆总量（episodes/traces）、语义检索与 LLM 评分可用性、记忆模式与鉴权状态。任何会话开始时先调用它以确认记忆功能可用。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "note": {"type": "string", "description": _NOTE_DESC},
            },
        },
    },
    {
        "name": "memory_save",
        "description": "把一条值得长期保留的事实/结论写入记忆引擎（独立于会话采集链路，即时入库并补向量）。适合在用户明确要求记住、或发现重要且可复用的结论时调用。写入成功返回 trace id；失败会明确报错。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "要保存的记忆内容（一句话结论或事实，可含少量上下文）"},
                "importance": {
                    "type": "string",
                    "enum": ["high", "normal", "low"],
                    "description": (
                        "重要度档位（可选，不传=normal），决定记忆的初始价值分。"
                        "high=技术沉淀/踩坑解法/架构决策/关键配置变更，或用户明确要求记住的；"
                        "normal=一般结论；low=临时性、局部细节。"
                        "请直接用自己的推理判断即可，无需外挂评分模型。"
                        "注意这只是起点，后续仍由真实使用（召回/反馈）演化。"
                    ),
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "可选标签（如项目名、领域、技术栈），存库供面板筛选与溯源。"
                        "引擎不解释标签语义，纯透传。"
                    ),
                },
                "type": {
                    "type": "string",
                    "enum": ["decision", "fact", "preference", "lesson"],
                    "description": (
                        "（可选）记忆类型，不传=fact。decision=做出的决策/选型；"
                        "fact=事实/结论/技术沉淀；preference=偏好/约定；"
                        "lesson=踩坑/教训。落在蒸馏表供面板筛选与 wiki 编译。"
                    ),
                },
                "note": {"type": "string", "description": _NOTE_DESC},
            },
            "required": ["content"],
        },
    },
    {
        "name": "memory_score",
        "description": "给已写入的记忆打极性分（写后即评）：positive=值得保留 / neutral=一般 / negative=无价值。通过 feedback 让引擎立即重算该记忆的 value/priority（检索排序生效）。用于 memory_save 之后紧接着评分已保存的记忆。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "trace_id": {"type": "string", "description": "memory_save 返回的 trace id"},
                "polarity": {"type": "string", "description": "positive | neutral | negative"},
                "note": {"type": "string", "description": _NOTE_DESC},
            },
            "required": ["trace_id", "polarity"],
        },
    },
]


# ---------------------------------------------------------------------------
# JSON-RPC / MCP 处理
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 调用审计：logs/mcp.log（Agent 侧记忆操作的事实记录，供溯源与查询）
#
# 为什么放在 _tools_call 而不是各工具内部：它是 stdio 与 Streamable HTTP
# **共用的唯一分发点**，包一层即 100% 覆盖，且五个工具零改动。
# 为什么不让 Agent 手动写日志：Skill 自己的记述是"这类依赖自觉的动作在高负载
# 下会漏触发"，而审计的价值全在完整性——漏一条就无法区分"没做"与"没记"。
# Agent 的**意图**（为什么）由可选参数 note 补充，随同一次调用一起落盘。
# ---------------------------------------------------------------------------

_AUDIT_TEXT_CAP = 400


def _audit_args(args: dict) -> dict:
    """审计用参数：正文只留摘要 + 字数。

    正文全文已在记忆库（凭结果里的 trace id 可回查），日志里再存一份既冗余，
    也让敏感内容二次落盘。
    """
    out: dict = {}
    for k, v in args.items():
        if k == "content" and isinstance(v, str):
            out["content_chars"] = len(v)
            out["content_head"] = v[:80]
        elif isinstance(v, str) and len(v) > 300:
            out[k] = v[:300] + "…"
        else:
            out[k] = v
    return out


def _audit_digest(text: str) -> dict:
    """从结果文本里轻量提取关键标识（提不到就留空，绝不影响主流程）。"""
    d: dict = {}
    m = re.search(r"\bid=(\S+?)[\s，,、)）]", text)
    if m:
        d["trace_id"] = m.group(1)
    m = re.search(r"(\d+)\s*条命中", text)
    if m:
        d["hits"] = int(m.group(1))
    return d


def _audited(name: str, args: dict, fn: Callable[[dict], str]) -> str:
    """执行工具并写审计（call + result 两条，以 call_id 关联）。

    记两条而不是一条：只记结果的话，"发起了却没有返回"（超时、进程被杀）就
    完全无迹可寻——而那恰恰是审计最该抓到的情况。
    """
    from agentmemhub.logs import audit_mcp

    cid = uuid.uuid4().hex[:12]
    t0 = time.perf_counter()
    base = {"kind": "mcp", "call_id": cid, "tool": name, "agent": _AGENT}
    audit_mcp({**base, "ts": round(time.time(), 3), "phase": "call",
               "args": _audit_args(args)})
    try:
        text = fn(args)
    except Exception as e:
        audit_mcp({**base, "ts": round(time.time(), 3), "phase": "result",
                   "ok": False,
                   "elapsed_ms": round((time.perf_counter() - t0) * 1000),
                   "error": f"{type(e).__name__}: {e}"[:300]})
        raise
    audit_mcp({**base, "ts": round(time.time(), 3), "phase": "result", "ok": True,
               "elapsed_ms": round((time.perf_counter() - t0) * 1000),
               "digest": _audit_digest(text),
               "result_head": text[:_AUDIT_TEXT_CAP]})
    return text


def _err(mid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": code, "message": message}}


class MCPHandler:
    """一次 stdio 会话：逐行读 JSON-RPC 请求，逐行回响应（可注入流做测试）。"""

    def __init__(self, stdin=None, stdout=None):
        self._in = stdin or sys.stdin
        self._out = stdout or sys.stdout

    def run(self) -> None:
        for line in self._in:
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            reply = self._dispatch(line)
            if reply is not None:
                self._out.write(json.dumps(reply, ensure_ascii=False) + "\n")
                self._out.flush()

    def _dispatch(self, line: str) -> Optional[dict]:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return _err(None, -32700, "Parse error")
        return self._dispatch_msg(msg)

    def _dispatch_msg(self, msg: Any) -> Optional[dict]:
        """JSON-RPC 消息 → 响应（stdio 与 Streamable HTTP 共用）。"""
        if not isinstance(msg, dict) or "method" not in msg:
            return _err(msg.get("id") if isinstance(msg, dict) else None,
                        -32600, "Invalid Request")
        mid = msg.get("id")
        method = msg["method"]
        params = msg.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        if mid is None:                       # 通知：不回复
            self._notify(method, params)
            return None
        try:
            return {"jsonrpc": "2.0", "id": mid,
                    "result": self._handle(method, params)}
        except _ToolError as e:
            return {"jsonrpc": "2.0", "id": mid,
                    "result": {"content": [{"type": "text", "text": str(e)}],
                               "isError": True}}
        except LookupError as e:
            return _err(mid, -32601, str(e))
        except Exception:
            return _err(mid, -32603, "Internal error")

    def _notify(self, method: str, params: dict) -> None:
        pass    # notifications/initialized 等通知无需动作

    def _handle(self, method: str, params: dict) -> Any:
        if method == "initialize":
            ver = params.get("protocolVersion") or "2024-11-05"
            return {"protocolVersion": ver,
                    "capabilities": {"tools": {}},
                    "serverInfo": SERVER_INFO}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": _TOOLS}
        if method == "tools/call":
            return self._tools_call(params)
        raise LookupError(f"unknown method: {method}")

    def _tools_call(self, params: dict) -> dict:
        name = params.get("name", "")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            args = {}
        fn = _TOOL_HANDLERS.get(name)
        if fn is None:
            raise LookupError(f"unknown tool: {name}")

        def _run(a: dict) -> str:
            # 引擎未运行时所有工具统一返回明确指引（网关不代管引擎生命周期）
            if memos_daemon.auth_state() is None:
                raise _ToolError(_engine_hint())
            return fn(a)

        # 审计包在引擎可用性检查**之外**：Agent 试图操作但引擎离线，同样是
        # 必须留痕的事实（否则"没做"与"做了但失败"无从区分）。
        return {"content": [{"type": "text", "text": _audited(name, args, _run)}]}


def _force_utf8_stdio() -> None:
    """把 stdio 切到 UTF-8（MCP 协议规定 UTF-8）。

    Windows 上 ``sys.stdin``/``sys.stdout`` 默认走 locale 编码（cp936）：
    不强制时中文会在 stdio 上损坏——客户端发来的 UTF-8 中文被按 cp936
    解码（读进来就已损坏，语义检索会直接报 tokenizer 错），服务端写出的
    中文被编成 cp936（对端按 UTF-8 解成乱码）。
    只对支持 ``reconfigure`` 的文本流生效，测试注入的 StringIO 不受影响。
    """
    for stream in (sys.stdin, sys.stdout):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")


def run_stdio() -> None:
    """stdio 传输：逐行读 stdin，逐行写响应（Agent host 拉起子进程）。"""
    _force_utf8_stdio()
    MCPHandler().run()


# ---------------------------------------------------------------------------
# Streamable HTTP 传输：单 endpoint POST /mcp（非流式 JSON 响应）
# 依赖 fastapi/uvicorn（属于 [web] extra）；stdio 模式不需要它们
# ---------------------------------------------------------------------------

def _http_err(mid: Any, code: int, message: str, status: int):
    from fastapi.responses import JSONResponse
    return JSONResponse(_err(mid, code, message), status_code=status)


def build_http_app():
    """构造 Streamable HTTP MCP server（POST /mcp；GET 405；DELETE 结束会话）。"""
    if not _HAS_FASTAPI:
        raise RuntimeError("--http 需要 web 依赖：uv pip install -e '.[web]'")
    import uuid

    from fastapi import FastAPI, Request
    from fastapi.concurrency import run_in_threadpool
    from fastapi.responses import JSONResponse, Response

    app = FastAPI(title="AgentMemHub MCP (Streamable HTTP)",
                  version=SERVER_INFO["version"],
                  docs_url=None, openapi_url=None)
    handler = MCPHandler()
    _sessions: set[str] = set()      # 宽松会话簿：记录但不强制校验

    @app.post("/mcp")
    async def mcp_post(request: Request):
        ctype = request.headers.get("content-type") or ""
        if "application/json" not in ctype:
            return _http_err(None, -32600,
                             "Content-Type must be application/json", 415)
        try:
            msg = json.loads((await request.body()).decode("utf-8"))
        except Exception:
            return _http_err(None, -32700, "Parse error", 400)
        if not isinstance(msg, dict) or "method" not in msg or "jsonrpc" not in msg:
            return _http_err(msg.get("id") if isinstance(msg, dict) else None,
                             -32600, "Invalid Request", 400)
        sid = request.headers.get("mcp-session-id")
        if msg.get("method") == "initialize":
            sid = sid or uuid.uuid4().hex
            _sessions.add(sid)
        # 工具调用/引擎请求是阻塞 IO，放线程池避免卡事件循环
        reply = await run_in_threadpool(handler._dispatch_msg, msg)
        if reply is None:                 # 通知（如 notifications/initialized）
            return Response(status_code=202)
        headers = {"Mcp-Session-Id": sid} if sid else {}
        return JSONResponse(reply, headers=headers)

    @app.get("/mcp")
    def mcp_get():
        # 无服务器主动推送：GET SSE 流不支持，明确 405
        return Response(status_code=405, headers={"Allow": "POST, DELETE"})

    @app.delete("/mcp")
    def mcp_delete(request: Request):
        sid = request.headers.get("mcp-session-id")
        if sid:
            _sessions.discard(sid)
        return Response(status_code=204)

    return app


def run_http(host: str = "127.0.0.1", port: int = 9100) -> None:
    """常驻 Streamable HTTP 服务：一台机器共享记忆引擎给多个客户端。"""
    try:
        import uvicorn
    except ImportError:      # pragma: no cover
        raise SystemExit("--http 需要 web 依赖：uv pip install -e '.[web]'")
    uvicorn.run(build_http_app(), host=host, port=port, log_level="info")


def main() -> None:
    """stdio 入口（python -m agentmemhub mcp_server / console script）。"""
    run_stdio()


if __name__ == "__main__":
    main()