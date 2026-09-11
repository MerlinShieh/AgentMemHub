"""AgentMemHub ↔ agentmemhub.rag 协议桥（R3）：以 MemOS 端点语义实现替换 HTTP 引擎。

设计（详见 docs/rag-bridge-switch-plan.md）：
- 唯一接缝：memos_daemon.engine_request/auth_state/daemon_status 在 backend=rag 时
  派发进本模块——MCP 五工具、web 网关、cli 的调用方代码与响应形状零改动；
- trace 语义 = 轮次视图（units 按 turn_key 聚合），refId 解析序：
  legacy_id → src_id → 数字 unit_id（保证 memory_score 跨系统回指）；
- 引擎「存值不判值」：feedback 聚合调 rag.memstore（确定性算术），
  LLM 评分策略仍留 agentmemhub/scoring.py；
- 本模块不 import fastapi / 不触网 / 不起服务——纯进程内函数。
"""
from __future__ import annotations

import dataclasses
import sqlite3
import time
from typing import Any, Optional

from agentmemhub.rag import memstore
from agentmemhub.rag.config import Settings, load_settings
from agentmemhub.rag.ingest import (
    ensure_bridge_schema,
    ensure_vec_table,
    open_index,
)
from agentmemhub.rag.memstore import (
    content_anchor,
    ensure_memstore_schema,
    put_feedback,
)
from agentmemhub.rag.runtime import get_active_embedder
from agentmemhub.rag.search import Hit, hybrid_search

_SETTINGS: Optional[Settings] = None


def _derive_settings() -> Settings:
    """以 hub 根 models.json 为模型锚，但索引/源库路径跟随 AgentMemHub data_dir。

    这样测试沙箱（AGENTMEM_HUB_DATA_DIR）与生产单一真源天然一致：
    index_db=<data_dir>/session_rag.db，source_db=config.db_path。
    """
    from agentmemhub import config
    base = load_settings()                      # 校验 models.json + 模型文件齐全
    cfg = config.config()
    data_dir = cfg.data_dir
    return dataclasses.replace(
        base,
        root=data_dir.parent if base.root != data_dir else base.root,
        source_db=cfg.db_path,
        index_db=data_dir / "session_rag.db",
        log_dir=_hub_log_dir(),
    )


def _hub_log_dir():
    try:
        from agentmemhub import logs
        return logs.log_dir()
    except Exception:
        import pathlib
        return pathlib.Path("logs")


def settings() -> Settings:
    global _SETTINGS
    if _SETTINGS is None:
        _SETTINGS = _derive_settings()
    return _SETTINGS


def configure(settings: Settings) -> None:
    """测试/启动期注入（tmp 沙箱隔离用），同时清空 embedder 缓存避免跨库串味。"""
    global _SETTINGS
    _SETTINGS = settings
    from agentmemhub.rag.runtime import reset_embedders
    reset_embedders()


def reset_settings() -> None:
    global _SETTINGS
    _SETTINGS = None


def _conn() -> sqlite3.Connection:
    conn = open_index(settings().index_db)
    conn.row_factory = sqlite3.Row
    ensure_bridge_schema(conn)
    ensure_memstore_schema(conn)
    return conn


# ── 标识与轮次视图 ─────────────────────────────────────────────────────

def resolve_unit_id(conn: sqlite3.Connection, ref: str) -> Optional[int]:
    """refId → unit id：legacy_id（mcp_*/trac_*）→ src_id → 数字 id。"""
    ref = str(ref or "").strip()
    if not ref:
        return None
    row = conn.execute(
        "SELECT id FROM units WHERE legacy_id=? OR src_id=? LIMIT 1",
        (ref, ref)).fetchone()
    if row:
        return int(row[0])
    if ref.startswith("unit:") and ref[5:].isdigit():
        row = conn.execute("SELECT id FROM units WHERE id=?",
                           (int(ref[5:]),)).fetchone()
        return int(row[0]) if row else None
    if ref.isdigit():
        row = conn.execute("SELECT id FROM units WHERE id=?", (int(ref),)).fetchone()
        return int(row[0]) if row else None
    return None


def _group_key(r: sqlite3.Row) -> str:
    if r["source"] == "memory":
        return f"unit:{r['id']}"          # 原子记忆一单元一轮
    if r["turn_key"]:
        return f"t:{r['source']}/{r['conversation_id']}/{r['turn_key']}"
    return f"unit:{r['id']}"


def _rows_to_traces(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    """units(+values) → MemOS TraceDTO 形状的轮次视图（ts 毫秒，对齐旧口径）。"""
    groups: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        groups.setdefault(_group_key(r), []).append(r)
    out: list[dict[str, Any]] = []
    for g in groups.values():
        g.sort(key=lambda r: (r["seq"] or 0))
        users = [r for r in g if r["role"] == "user"]
        agents = [r for r in g if r["role"] == "assistant"]
        anchor = users[0] if users else g[0]
        vals = [r["value"] for r in g if r["value"] is not None]
        manuals = [r["manual_value"] for r in g if r["manual_value"] is not None]
        out.append({
            "id": anchor["legacy_id"] or anchor["src_id"] or f"unit:{anchor['id']}",
            "ts": int(anchor["time"] or 0) * 1000,
            "episodeId": anchor["conversation_id"],
            "sessionId": anchor["conversation_id"],
            "userText": "\n".join(r["text"] for r in users),
            "agentText": "\n".join(r["text"] for r in agents),
            "summary": (anchor["title"] or "")[:200],
            # 手动加权优先于自动聚合值（与 ValueStore.values 的读取口径一致）
            "value": manuals[0] if manuals else (vals[0] if vals else 0.0),
            "manualValue": manuals[0] if manuals else None,
            "source": anchor["source"],
            "conversationId": anchor["conversation_id"],
        })
    out.sort(key=lambda t: t["ts"], reverse=True)
    return out


_SELECT_TRACE = (
    "SELECT u.id, u.source, u.conversation_id, u.seq, u.role, u.turn_key,"
    " u.time, u.title, u.src_id, u.legacy_id, u.text, v.value, v.manual_value"
    " FROM units u LEFT JOIN unit_values v ON v.unit_id = u.id")


def safe_cutoff_hits(hits: list[dict], *, max_keep: int = 5,
                     floor_ratio: float = 0.7) -> list[dict]:
    """机械终审（dict 版 ext.safe_cutoff 同规则）：≥0.7×top 且 ≤max_keep，至少保 1。"""
    if not hits:
        return []
    top = max(h["score"] for h in hits[:1]) or 0.0
    kept = [h for h in hits[:max_keep] if h["score"] >= floor_ratio * top]
    return kept or [hits[0]]


# ── 端点语义实现 ───────────────────────────────────────────────────────

#: 面板语义检索的默认返回上限。curate 只做「相关度截断」不做「硬砍到 5 条」——
#: 用户搜东西期望看到"有哪些相关"，5 条过少（R6 体验反馈）。
SEARCH_MAX_HITS = 20


def search(agent: str, query: str, *, k: int = SEARCH_MAX_HITS,
           curate: bool = True,
           exclude_session: tuple[str, str] | None = None) -> dict:
    """POST /api/v1/memory/search 的 rag 实现（hits 形状对齐 RetrievalResultDTO）。

    额外带回会话定位信息（source/conversationId/turnKey/title），
    让面板能把命中项点开跳回对应会话的对应轮次。
    """
    t0 = time.perf_counter()
    st = settings()
    vstore = memstore.ValueStore(st.index_db)
    hits = hybrid_search(
        st, query, k=k, candidate_k=max(k * 4, 30), expand_turns=False,
        exclude_session=exclude_session,
        value_provider=vstore,
        no_decay_ids=vstore.no_decay_ids,
        log=_log())
    refmap: dict = {}
    if hits:
        conn = _conn()
        try:
            marks = ",".join("?" * len(hits))
            for r in conn.execute(
                f"SELECT id, source, conversation_id, turn_key, title,"
                f" legacy_id, src_id FROM units WHERE id IN ({marks})",
                [h.unit_id for h in hits]):
                refmap[r["id"]] = {
                    "refId": r["legacy_id"] or r["src_id"] or f"unit:{r['id']}",
                    "source": r["source"],
                    "conversationId": r["conversation_id"],
                    "turnKey": r["turn_key"] or "",
                    "title": r["title"] or "",
                }
        finally:
            conn.close()
    dto_hits = []
    for h in hits:
        m = refmap.get(h.unit_id) or {}
        dto_hits.append({
            "tier": 2, "refKind": "trace",
            "refId": m.get("refId", f"unit:{h.unit_id}"),
            "score": round(h.score, 4),
            "snippet": (h.title + " | " if h.title else "") + h.text[:200],
            # 会话定位（面板点击跳转用）；原子记忆（source='memory'）无会话
            "source": m.get("source", ""),
            "conversationId": m.get("conversationId", ""),
            "turnKey": m.get("turnKey", ""),
            "title": m.get("title", ""),
            "atomic": m.get("source") == "memory",
        })
    if curate:
        # 相关度截断（≥0.7×top）但不再硬砍到 5 条，上限放宽到 SEARCH_MAX_HITS
        dto_hits = safe_cutoff_hits(dto_hits, max_keep=SEARCH_MAX_HITS)
    ctx = "\n".join(f"- {d['snippet'][:160]}" for d in dto_hits[:3])
    return {"hits": dto_hits, "injectedContext": ctx,
            "tierLatencyMs": {"rag": round((time.perf_counter() - t0) * 1000)}}


def traces_list(limit: int = 20, offset: int = 0) -> dict:
    """GET /api/v1/traces?groupByTurn=1 的 rag 实现（时间倒序轮次视图）。"""
    conn = _conn()
    try:
        total = conn.execute("SELECT COUNT(*) FROM units").fetchone()[0]
        rows = conn.execute(
            _SELECT_TRACE + " ORDER BY u.time DESC, u.id DESC LIMIT ? OFFSET ?",
            (limit * 3, offset * 3)).fetchall()
        traces = _rows_to_traces(rows)[offset:offset + limit]
        return {"traces": traces, "limit": limit, "offset": offset,
                "total": total, "nextOffset": offset + limit}
    finally:
        conn.close()


def import_bundle(traces: list[dict], *, embedder=None) -> dict:
    """POST /api/v1/import 的 rag 实现：按 legacy_id 幂等（与 MemOS 行为对齐）。

    处理的是「记忆型 trace DTO」（userText/agentText/summary + value 预置）；
    会话轨迹在 rag 后端下由 run_ingest 直采源库，不经此路径。
    """
    st = settings()
    conn = _conn()
    imported = skipped = 0
    try:
        emb = embedder or get_active_embedder(st)
        spec = st.active_spec
        ensure_vec_table(conn, spec)
        pend_texts: list[str] = []
        pend_dtos: list[dict] = []
        for t in traces or []:
            lid = str(t.get("id") or "").strip()
            text = (t.get("userText") or "").strip() or \
                   (t.get("agentText") or "").strip() or \
                   (t.get("summary") or "").strip()
            if not lid or not text:
                skipped += 1
                continue
            if conn.execute("SELECT 1 FROM units WHERE legacy_id=?",
                            (lid,)).fetchone():
                skipped += 1
                continue
            pend_texts.append(text)
            pend_dtos.append({**t, "_text": text})
        if pend_texts:
            vecs = emb.encode_passages(pend_texts)
            with conn:
                seq = conn.execute(
                    "SELECT COALESCE(MAX(seq),0) FROM units"
                    " WHERE source='memory'").fetchone()[0]
                for d, v in zip(pend_dtos, vecs):
                    seq += 1
                    ts = int((d.get("ts") or time.time() * 1000) // 1000)
                    cur = conn.execute(
                        "INSERT INTO units(source, conversation_id, seq, role,"
                        " turn_key, src_id, time, title, text, chars, legacy_id)"
                        " VALUES('memory','mcp',?,'user','mcp',?,?,?,?,?,?)",
                        (seq, content_anchor(d["_text"]), ts,
                         "记忆", d["_text"], len(d["_text"]), d["id"]))
                    conn.execute(
                        f"INSERT INTO {spec.vec_table}(rowid, embedding)"
                        " VALUES(?,?)",
                        (cur.lastrowid, bytes(v)))
                    value = float(d.get("value") or 0.0)
                    if value:
                        conn.execute(
                            "INSERT OR REPLACE INTO unit_values(unit_id, value,"
                            " r_human, priority, updated_at) VALUES(?,?,?,?,?)",
                            (cur.lastrowid, value, None,
                             float(d.get("priority") or abs(value)), int(time.time())))
                    imported += 1
        return {"imported": imported, "skipped": skipped}
    finally:
        conn.close()


def feedback(trace_id: str, polarity: str, *, magnitude: float = 1.0,
             channel: str = "explicit") -> dict:
    """POST /api/v1/feedback 的 rag 实现 + 会话级 r_task 滚动聚合。"""
    conn = _conn()
    try:
        uid = resolve_unit_id(conn, trace_id)
        if uid is None:
            raise KeyError(f"trace 不存在: {trace_id}")
        out = put_feedback(conn, uid, polarity, magnitude=magnitude,
                           channel=channel)
        src, cid = conn.execute(
            "SELECT source, conversation_id FROM units WHERE id=?",
            (uid,)).fetchone()
        if src != "memory":  # 会话轮次：滚动更新该会话均值分（原 episodes.r_task 语义位）
            r = conn.execute(
                "SELECT AVG(v.value) FROM units u JOIN unit_values v"
                " ON v.unit_id=u.id WHERE u.source=? AND u.conversation_id=?"
                " AND v.value<>0", (src, cid)).fetchone()[0]
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO conv_scores(source, conversation_id,"
                    " r_task, updated_at) VALUES(?,?,?,?)",
                    (src, cid, r, int(time.time())))
        out.update({"traceId": trace_id, "ok": True})
        return out
    finally:
        conn.close()


def rebuild_embeddings(mode: str = "repair", limit: int = 500,
                       offset: int = 0) -> dict:
    """POST /api/v1/embeddings/rebuild 的 rag 实现。

    rag 写入即嵌入：repair = 给 active 表缺向量的 units 补嵌；
    rebuild = 无需动作（全量重嵌是 reembed --model 的职责）。
    """
    st = settings()
    conn = _conn()
    try:
        spec = st.active_spec
        ensure_vec_table(conn, spec)
        missing = [r[0] for r in conn.execute(
            f"SELECT u.id FROM units u LEFT JOIN {spec.vec_table} v ON v.rowid=u.id"
            " WHERE v.rowid IS NULL")]
        updated = 0
        if mode == "repair" and missing:
            emb = get_active_embedder(spec)
            tables = [spec.vec_table]
            for chunk_i in range(0, len(missing), 64):
                ids = missing[chunk_i:chunk_i + 64]
                rows = conn.execute(
                    "SELECT id, text FROM units WHERE id IN (" +
                    ",".join("?" * len(ids)) + ") ORDER BY id", ids).fetchall()
                vecs = emb.encode_passages([r[1] for r in rows])
                with conn:
                    for (uid, _), v in zip(rows, vecs):
                        conn.execute(
                            f"INSERT OR REPLACE INTO {spec.vec_table}(rowid,"
                            " embedding) VALUES(?,?)", (uid, bytes(v)))
                        updated += 1
        total = conn.execute("SELECT COUNT(*) FROM units").fetchone()[0]
        return {"processed": total, "updated": updated, "failed": 0,
                "done": True, "nextOffset": 0,
                "statsAfter": {"total": total, "missing": len(missing) - updated}}
    finally:
        conn.close()


def overview() -> dict:
    """GET /api/v1/overview 的 rag 实现。"""
    st = settings()
    conn = _conn()
    try:
        units = conn.execute("SELECT COUNT(*) FROM units").fetchone()[0]
        convs = conn.execute(
            "SELECT COUNT(DISTINCT source||'/'||conversation_id) FROM units"
            " WHERE source<>'memory'").fetchone()[0]
        memos = conn.execute(
            "SELECT COUNT(*) FROM units WHERE source='memory'").fetchone()[0]
        vt = st.active_spec.vec_table
        has = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name=?", (vt,)).fetchone()
        vecs = conn.execute(f"SELECT COUNT(*) FROM {vt}").fetchone()[0] if has else 0
        return {"ok": True, "episodes": convs, "traces": units,
                "memory_units": memos, "skills": {"total": 0},
                "policies": 0, "worldModels": 0,
                "embedder": {"available": units == 0 or vecs > 0,
                             "model": st.active_model},
                "llm": {"available": _scoring_llm_available()},  # 探测 Hub 侧评分 LLM 配置
                "uptimeMs": 0,
                "rag": {"units": units, "vectors": vecs, "model": st.active_model,
                        "coverage": round(vecs / units, 4) if units else 1.0}}
    finally:
        conn.close()


_LLM_PROBE: tuple = (None, 0.0)


def _scoring_llm_available() -> bool:
    """评分 LLM 配置可用性（60s 缓存）：策略在 Hub 侧，stats 面板如实展示。"""
    global _LLM_PROBE
    now = time.time()
    if _LLM_PROBE[0] is not None and now - _LLM_PROBE[1] < 60:
        return _LLM_PROBE[0]
    try:
        from agentmemhub import scoring
        ok = bool(scoring.read_engine_llm())
    except Exception:
        ok = False
    _LLM_PROBE = (ok, now)
    return ok


def probe() -> dict:
    """引擎就绪探测（代替 auth/daemon 探活）：库可开、schema 就绪即在线。"""
    try:
        ov = overview()
        return {"online": bool(ov.get("ok")), "backend": "rag", "summary": ov}
    except Exception as e:
        return {"online": False, "backend": "rag", "error": str(e)}


def all_traces() -> list[dict]:
    """全量轮次视图（scoring list_all_traces 的 rag 等价物，ts 升序）。"""
    conn = _conn()
    try:
        rows = conn.execute(_SELECT_TRACE + " ORDER BY u.time ASC, u.id ASC").fetchall()
    finally:
        conn.close()
    out = _rows_to_traces(rows)
    out.sort(key=lambda t: t["ts"])
    return out


def turn_refs() -> list[str]:
    """全量轮次锚 id 列表（scoring list_trace_ids 的 rag 等价物）。"""
    return [t["id"] for t in all_traces()]


def sync_episode_r_task(trace_ids=None) -> dict:
    """兼容旧签名：rag 后端 conv_scores 在 feedback 时已滚动更新，此函数仅全量重建。"""
    return sync_conv_scores()


def unscored_traces(*, exclude_ids: set[str] | None = None,
                    limit: int | None = None) -> list[dict]:
    """评分枚举（取代 scoring 直连 memos.db）：无 value 的轮次视图，ts 升序。"""
    conn = _conn()
    try:
        rows = conn.execute(
            _SELECT_TRACE + " ORDER BY u.time ASC, u.id ASC").fetchall()
    finally:
        conn.close()
    traces = _rows_to_traces(rows)
    excl = exclude_ids or set()
    out = [t for t in traces if t["id"] not in excl and t["value"] == 0.0]
    return out[::-1][:limit] if limit else out[::-1]


def traces_by_ids(ids: set[str]) -> list[dict]:
    """定点读轮次视图（scoring 增量路径，取代 memos.db 直读）。

    每个 id 先解析到 unit，再展开其所在轮次的全部 units——
    非锚点 unit（如轮内 assistant 消息）被引用时同样能取回完整轮。
    """
    conn = _conn()
    traces: list[dict] = []
    seen: set[str] = set()
    try:
        for raw in sorted({str(i) for i in ids if i}):
            uid = resolve_unit_id(conn, raw)
            if uid is None:
                continue
            row = conn.execute(
                "SELECT source, conversation_id, turn_key FROM units WHERE id=?",
                (uid,)).fetchone()
            if row["turn_key"] and row["source"] != "memory":
                key = f"t:{row['source']}/{row['conversation_id']}/{row['turn_key']}"
                if key in seen:
                    continue
                seen.add(key)
                rows = conn.execute(
                    _SELECT_TRACE + " WHERE u.source=? AND u.conversation_id=?"
                    " AND u.turn_key=?",
                    (row["source"], row["conversation_id"], row["turn_key"])).fetchall()
            else:
                if f"unit:{uid}" in seen:
                    continue
                seen.add(f"unit:{uid}")
                rows = conn.execute(
                    _SELECT_TRACE + " WHERE u.id=?", (uid,)).fetchall()
            traces.extend(_rows_to_traces(rows))
    finally:
        conn.close()
    traces.sort(key=lambda t: t["ts"])
    return traces


def sync_conv_scores() -> dict:
    """全量重建 conv_scores（面板展示用；取代旧 sync_episode_r_task 全表 backfill）。"""
    conn = _conn()
    try:
        with conn:
            conn.execute("DELETE FROM conv_scores")
            conn.execute(
                "INSERT OR REPLACE INTO conv_scores(source, conversation_id, r_task,"
                " updated_at) SELECT u.source, u.conversation_id, AVG(v.value),"
                " ? FROM units u JOIN unit_values v ON v.unit_id=u.id"
                " WHERE v.value<>0 AND u.source<>'memory'"
                " GROUP BY u.source, u.conversation_id", (int(time.time()),))
        n = conn.execute("SELECT COUNT(*) FROM conv_scores").fetchone()[0]
        return {"conversations": n}
    finally:
        conn.close()


def _log():
    import logging
    return logging.getLogger("asrag.bridge")
