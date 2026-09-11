"""AgentMemHub Web — FastAPI 应用。

只读为主的管理面：统计/筛选/分页列表/单会话事件流按需加载；
管理操作仅限删除会话与改标题（显式接口，事务级联）。

启动：python -m agentmemhub serve --port 8086 --open
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel

from agentmemhub.store import Store

# ---- 截断规则（与静态快照流水线口径一致，防止巨大 payload）----
_CAP_CONTENT = 320
_CAP_OUTPUT = 200
_CAP_REASONING = 200
_CAP_DIFF = 400
_MAX_EVENTS = 100          # 单会话最多下发的事件条数
_HEAD_EVENTS = 88          # 截断时保留前 N 条

# 注意：必须定义在模块顶层 —— app.py 启用了 `from __future__ import annotations`，
# FastAPI 从模块全局命名空间解析类型注解；嵌套在工厂函数内的类会解析失败，
# 导致该 body 参数被当作必需的查询参数（422 missing body）。


class TitleIn(BaseModel):
    title: str


class ExclusionIn(BaseModel):
    """R6 记忆排除：turn_key 省略/空 = 整会话排除。

    sync_peers=True 时，同一 turn_key 在其他源/会话的副本一并排除
    （同一段对话常被多个 Agent 各自采集，避免漏网副本仍被召回）。
    """
    turn_key: str = ""
    note: str = ""
    sync_peers: bool = False


class ExclusionItem(BaseModel):
    source: str
    id: str


class ExclusionBatchIn(BaseModel):
    """批量排除：items=[{source,id}]；turn_key 空 = 整会话。"""
    items: list[ExclusionItem] = []
    turn_key: str = ""


_SORTABLE = {
    "updated": "updated_at", "created": "created_at",
    "events": "event_count", "title": "title",
}


def _exclusions_map(store) -> dict:
    """全库排除态映射：(source, id) → {"whole": bool, "turns": int}。

    列表页据此给会话行打「整会话排除（红）/ 部分轮次排除（橙）」标记。
    """
    m: dict = {}
    try:
        for r in store.list_exclusions():
            k = (r["source"], r["conversation_id"])
            e = m.setdefault(k, {"whole": False, "turns": 0})
            if r["turn_key"]:
                e["turns"] += 1
            else:
                e["whole"] = True
    except Exception:
        return {}
    return m


def _index_counts(source: str, cid: str,
                  turn_keys: Optional[list[str]] = None,
                  whole: bool = False) -> tuple[int, int]:
    """(该会话当前已入库单元数, 被排除轮次影响过的单元数)。

    用于面板展示「已写入 N 条 / 已排除影响 M 条」；memos 后端返回 (0, 0)。
    turn_keys/whole 由调用方从采集库的排除表读出后传入（排除表在采集库，
    索引库里没有它）。
    """
    try:
        from agentmemhub import memos_daemon
        if not memos_daemon._backend_is_rag():
            return 0, 0
        import sqlite3

        from agentmemhub import rag_bridge
        db = rag_bridge.settings().index_db
        conn = sqlite3.connect(f"file:{Path(db).as_posix()}?mode=ro", uri=True)
        try:
            indexed = conn.execute(
                "SELECT COUNT(*) FROM units WHERE source=? AND conversation_id=?",
                (source, cid)).fetchone()[0]
            if whole:
                return indexed, indexed          # 整会话排除：全部已移除
            turns = [t for t in (turn_keys or []) if t]
            if not turns:
                return indexed, 0
            marks = ",".join("?" * len(turns))
            removed = conn.execute(
                f"SELECT COUNT(*) FROM units WHERE source=? AND conversation_id=?"
                f" AND turn_key IN ({marks})", [source, cid] + turns).fetchone()[0]
            return indexed, removed
        finally:
            conn.close()
    except Exception:
        return 0, 0


def _purge_index(source: str, cid: str, turn_key: str | None = None) -> int:
    """索引库级联清理（R6）：会话删除/排除后同步移除派生记忆。

    rag 后端下直接删 units+vec+FTS；memos 回退后端无本地索引，返回 0。
    失败不抛（面板操作不该因索引异常而整体失败），仅记日志。
    """
    try:
        from agentmemhub import memos_daemon
        if not memos_daemon._backend_is_rag():
            return 0
        from agentmemhub import rag_bridge
        from agentmemhub.rag.ingest import (
            delete_units_for_conversation, delete_units_for_turn, open_index)

        conn = open_index(rag_bridge.settings().index_db)
        try:
            if turn_key:
                return delete_units_for_turn(conn, source, cid, turn_key)
            return delete_units_for_conversation(conn, source, cid)
        finally:
            conn.close()
    except Exception:
        try:
            from agentmemhub import logs
            logs.record(f"索引清理失败 {source}/{cid} turn={turn_key or '-'}",
                        level="error", actor="web")
        except Exception:
            pass
        return 0


def _clip(v: Any, n: int) -> Optional[str]:
    if v is None:
        return None
    s = str(v)
    return s if len(s) <= n else s[:n] + "…"


def _workspace_of(cwd: Optional[str]) -> str:
    if not cwd:
        return "(unknown)"
    return cwd.rstrip("\\/").rsplit("\\", 1)[-1].rsplit("/", 1)[-1] or "(unknown)"


class _DualWriter:
    """同时把输出写入 StringIO（终态文本）与 emit 回调（实时逐行，前端/日志可见）。"""

    def __init__(self, emit):
        import io
        self._sink = io.StringIO()
        self._emit = emit
        self._buf = ""

    def write(self, s: str) -> int:
        self._sink.write(s)
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            try:
                self._emit(line)
            except Exception:
                pass
        return len(s)

    def flush(self) -> None:
        # 无换行收尾的残留输出也作为一行发出（否则 tasks output 缺尾部）
        if self._buf:
            try:
                self._emit(self._buf.rstrip("\n"))
            except Exception:
                pass
            self._buf = ""

    def getvalue(self) -> str:
        return self._sink.getvalue()


def _emit_task(fn) -> Any:
    """把同步任务包装成 fn(emit, meta)（tasks.submit 契约）：实时流 + 终态文本。"""
    from contextlib import redirect_stdout

    def _do(emit, meta) -> str:
        w = _DualWriter(emit)
        with redirect_stdout(w):
            fn()
        return w.getvalue()
    return _do


def _run_ingest_fn(cli, adapters, source: str, signature: str):
    """看板「提取入库」后台动作：全部/指定 source 提取入库。"""
    def _run() -> None:
        sources = [source] if source else [a.source for a in adapters.all_adapters()]
        cli.run_ingest(sources, signature=signature)
    return _emit_task(_run)


def _run_push_fn(cli, source: str):
    """看板「推送记忆」后台动作：按 source 幂等推送 + 补向量（不 ingest）。"""
    from agentmemhub import adapters, memos_daemon
    from agentmemhub.store import Store

    def _run() -> None:
        if memos_daemon._backend_is_rag():
            # rag 后端：会话轨迹由向量化直采源库（push 语义 = 向量化；
            # 走 bundle 会把 trac_ 轨迹二次落成 memory 单元造成重复）
            return cli._vectorize_stage(stdout=print)
        store = Store()
        try:
            batches = ([source] if source
                       else [a.source for a in adapters.all_adapters() if a.locate()])
            # stdout=print：_DualWriter 实时捕获每行（推送批/rebuild 进度）→ 面板逐行显示
            cli.push_to_memos(store, sources=batches,
                              base_url=memos_daemon.base_url(), stdout=print)
        finally:
            store.close()
    return _emit_task(_run)


def _run_score_fn(cli, limit: int, dry_run: bool):
    """看板「自动评分」后台动作（tasks.submit 的 fn(emit, meta) 契约）。

    增量优先（run_score_incremental）：pending_score 队列非空只评队列（定点读，
    零全量枚举）；队列空则全量扫描未评（先筛 id 再定点读，无逐条跳过刷屏）。
    面板不逐条刷评估行（进度条/百分比展示）：逐条进度经 on_progress 结构化
    写入 job.progress（前端进度条），output 只留结束汇总。
    """
    from contextlib import redirect_stdout

    from agentmemhub.scoring import run_score_incremental
    from agentmemhub.web import tasks

    def _do(emit, meta) -> str:
        w = _DualWriter(emit)
        last_done = [0]

        def on_progress(done: int, total: int) -> None:
            # 每条都更新（前端自算百分比平滑推进；终态 100% 由下方 set 兜底）
            if done == last_done[0]:
                return
            last_done[0] = done
            tasks.set_progress(meta["id"], {
                "done": done, "total": total,
                "pct": int(done * 100 / total) if total else 0,
                "label": "评分"})

        with redirect_stdout(w):
            r = run_score_incremental(base_url="", limit=limit, dry_run=dry_run,
                                      workers=4, on_progress=on_progress)
            tasks.set_progress(meta["id"], {
                "done": r["evaluated"], "total": r["evaluated"],
                "pct": 100 if not r["errors"] else 99, "label": "评分"})
            print(f"评分完成[{r.get('mode', '?')}]: evaluated={r['evaluated']} "
                  f"skipped={r['skipped']} "
                  f"positive={r['positive']} neutral={r['neutral']} "
                  f"negative={r['negative']} errors={r['errors']}"
                  + ("（dry-run，未写入）" if r["dryRun"] else ""))
        return w.getvalue()
    return _do


def _run_distill_fn(source: str, limit: int, dry_run: bool):
    """看板「蒸馏记忆」后台动作：切片 → LLM 提炼 → 同会话合并 → 去重投影。

    run_distill 的 on_progress 本就是逐行文本，直接接到 emit（无需 redirect_stdout）。
    """
    def _do(emit, meta) -> str:
        from agentmemhub import rag_bridge
        from agentmemhub.distill import run_distill
        r = run_distill(rag_bridge.settings(), source=source, limit=limit,
                        dry_run=dry_run, on_progress=emit)
        if r.get("error"):
            msg = f"蒸馏未执行：{r['error']}"
        else:
            msg = (
                f"会话 {r['conversations']} 个 · 切片 {r['slices']} 片"
                f"（幂等跳过 {r['skipped_done']} / 失败 {r['failed']}）\n"
                f"产出记忆 {r['memories_new']} 条 · 合并沉淀 {r['merged']} 个会话"
                f"（失败 {r['merge_failed']}）\n"
                f"投影 {r['projected']} 条 · 相似打标 {r['similar']} · "
                f"判重丢弃 {r['duplicate']}\n"
                f"脱敏 {r['sanitized']} 条 · 丢弃空壳 {r['dropped']} 条\n"
                f"耗时 {r['seconds']}s · 模型 {r['model']}"
                + ("（dry-run：未落库）" if r.get("dry_run") else ""))
        emit("\n" + msg)          # tasks.submit 不使用返回值，汇总必须走 emit
        return msg
    return _do


def _run_exclude_fn(items, turn_key: str = ""):
    """看板「批量排除记忆」后台动作：逐会话落标记 + 清索引（幂等）。"""
    from agentmemhub.store import Store

    def _run() -> None:
        st = Store()
        try:
            total_removed = 0
            for it in items:
                src, cid = it.source, it.id
                created = st.add_exclusion(src, cid, turn_key)
                removed = _purge_index(src, cid, turn_key or None)
                total_removed += removed
                print(f"[{src}] {cid} → 排除"
                      f"{'（整会话）' if not turn_key else '（轮次）'} "
                      f"{'新增' if created else '已存在'}，清理索引 {removed} 条")
            print(f"完成：{len(items)} 个会话，共清理索引 {total_removed} 条")
        finally:
            st.close()
    return _emit_task(_run)


def _run_clean_fn(cli, source: str):
    """看板「清洗数据」后台动作：先打印各源统计再执行删除（重建 FTS/计数）。"""
    from agentmemhub.store import Store

    def _run() -> None:
        store = Store()
        try:
            cli.run_clean(store, source=source or None, apply=True)
        finally:
            store.close()
    return _emit_task(_run)


def _run_rebuild_fn(mode: str):
    """看板「补向量」后台动作：引擎 embedding rebuild（逐轮进度）。"""
    from agentmemhub import memos_daemon
    from agentmemhub.memos import rebuild_embeddings

    def _run() -> None:
        r = rebuild_embeddings(base_url=memos_daemon.base_url(),
                               mode=mode,
                               on_progress=lambda s: print(s))
        print(f"补向量完成({mode}): {r}")
    return _emit_task(_run)


def _logged_task(name: str, fn) -> Any:
    """任务包装：开始/完成/失败写统一操作日志（web.log）；完整输出逐行落盘
    <data_dir>/tasks/<job_id>.log（页面关掉/进程中断也可追溯）。"""
    from agentmemhub import logs

    def _do(emit, meta) -> str:
        logs.record(f"开始：{name}（id={meta['id']}）")
        def emit_full(line: str) -> None:
            emit(line)                                  # 前端实时
            logs.append_task_line(meta["id"], line)     # 完整落盘
        try:
            text = fn(emit_full, meta)
            logs.record(f"完成：{name}（id={meta['id']}）\n"
                        + (text or "").strip()[-600:])
            return text
        except Exception as e:
            logs.record(f"失败：{name}（id={meta['id']}）→ {e}", level="error")
            raise
    return _do


def _conv_to_dict(c: Any, *, excl_map: Optional[dict] = None) -> dict[str, Any]:
    """conversations row → 前端 camelCase 契约字段。

    excl_map：(source, id) → {"whole": bool, "turns": int}——R6 记忆排除态，
    供列表页渲染「整会话排除（红）/ 部分轮次排除（橙）」双色阶标记。
    不传则不带该字段（保持既有调用方形状不变）。
    """
    import json
    try:
        roles = json.loads(c["roles_json"]) if c["roles_json"] else []
    except Exception:
        roles = []
    d = {
        "source": c["source"],
        "id": c["id"],
        "title": c["title"] or "",
        "cwd": c["cwd"] or "",
        "workspace": _workspace_of(c["cwd"]),
        "model": c["model"] or "",
        "createdAt": c["created_at"] or 0,
        "updatedAt": c["updated_at"] or 0,
        "eventCount": c["event_count"] or 0,
        "roles": roles,
    }
    if excl_map:
        st = excl_map.get((c["source"], c["id"]))
        if st and st.get("whole"):
            d["excl"] = "whole"
        elif st and st.get("turns"):
            d["excl"] = "partial"
            d["exclTurns"] = st["turns"]
    return d


def _event_to_short(e: Any, *, whole: bool = False,
                    excluded_turns: Optional[set] = None) -> dict[str, Any]:
    """Event → 前端短键压缩形状（键名与静态快照流水线一致）。

    whole/excluded_turns：R6 记忆排除态——前端据此渲染勾选与灰化。
    """
    d: dict[str, Any] = {"s": e.seq, "r": e.role}
    if e.time:
        d["t"] = int(e.time)
    if e.content:
        d["c"] = _clip(e.content, _CAP_CONTENT)
    if e.tool_name:
        d["tn"] = e.tool_name
    if e.tool_input is not None:
        import json as _json
        try:
            d["ti"] = _json.dumps(e.tool_input, ensure_ascii=False)
        except Exception:
            d["ti"] = str(e.tool_input)
    if e.tool_output:
        d["to"] = _clip(e.tool_output, _CAP_OUTPUT)
    if e.tool_status:
        d["ts"] = e.tool_status
    if e.reasoning:
        d["rs"] = _clip(e.reasoning, _CAP_REASONING)
    if e.patch_file:
        d["pf"] = e.patch_file
    if e.patch_diff:
        d["pd"] = _clip(e.patch_diff, _CAP_DIFF)
    if getattr(e, "shell_cwd", None):
        d["sw"] = e.shell_cwd
    if e.model:
        d["m"] = e.model
    # shell/tool_output 复用：role=shell 时 c 里已含命令文本（models.py 渲染层处理）
    if e.role == "shell" and getattr(e, "shell_cmd", None):
        d["sc"] = _clip(e.shell_cmd, _CAP_OUTPUT)
    if e.role == "shell" and getattr(e, "shell_output", None):
        d["so"] = _clip(e.shell_output, _CAP_OUTPUT)
    if e.parent_id:
        d["pid"] = e.parent_id
    # 记忆锚（前端轮次分组 / 注入标记 / 稳定 id）
    if getattr(e, "turn_key", None):
        d["tk"] = e.turn_key
    if getattr(e, "src_id", None):
        d["si"] = e.src_id
    if getattr(e, "is_system", None):
        d["sys"] = True
    # R6 记忆排除态（该事件所属轮次是否被排除；整会话排除时为 True）
    if whole:
        d["ex"] = True
    elif excluded_turns and getattr(e, "turn_key", None) in excluded_turns:
        d["ex"] = True
    return d


def create_app(db_path: Path | None = None):
    """创建 FastAPI 应用（独立于核心功能，serve 子命令调用）。"""
    import threading

    from fastapi import FastAPI, HTTPException, Query
    from fastapi.middleware.gzip import GZipMiddleware
    from fastapi.responses import JSONResponse
    from fastapi.staticfiles import StaticFiles

    from agentmemhub.web.aggregates import Aggregates

    # 本地单机管理面：一把全局锁串行化所有 DB 访问（跨线程连接 + 锁）
    _LOCK = threading.RLock()
    store = (Store(Path(db_path), check_same_thread=False) if db_path
             else Store(check_same_thread=False))
    agg = Aggregates(store)

    app = FastAPI(
        title="AgentMemHub Web",
        description="统一 Agent 会话库的查询与管理 API（本地只读+有限管理）",
        version="0.1.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    def _invalidate():
        agg.invalidate()

    @app.get("/api/stats")
    def api_stats():
        with _LOCK:
            return JSONResponse(agg.stats_bundle())

    @app.get("/api/bootstrap")
    def api_bootstrap():
        """前端一次性引导数据（不含事件流——事件按需加载）。形状与静态快照 window.__DASH__ 对齐。"""
        with _LOCK:
            bundle = agg.stats_bundle()
            source_colors = {s["source"]: s["color"] for s in bundle["stats"]["sources"]}
            role_colors = {r["role"]: r["color"] for r in bundle["stats"]["roles"]}
            models_map = agg.conv_models()   # P2: 从 events 反查补全缺失的 model
            excl_map = _exclusions_map(store)   # R6: 排除态（列表行双色阶标记）
            convs = []
            for i, c in enumerate(store.list_conversations()):
                d = _conv_to_dict(c, excl_map=excl_map)
                if not d["model"]:
                    d["model"] = models_map.get((d["source"], d["id"]), "") or ""
                d["idx"] = i
                d["searchText"] = ""     # 服务端搜索接管；保留字段兼容前端
                convs.append(d)
        return JSONResponse({
            "meta": {**bundle["meta"], "dbPath": str(store.db_path)},
            "stats": bundle["stats"],
            "conversations": convs,
            "sourceColors": source_colors,
            "roleColors": role_colors,
            "eventsByConv": {},      # v2: 事件改为抽屉打开时按需拉取
        })

    @app.get("/api/facets")
    def api_facets():
        with _LOCK:
            f = agg.facets()
            # facets 面向前端筛选器：workspaces 返回文件夹名（与 conversations.workspace 一致）
            f["workspaces"] = sorted({_workspace_of(w) for w in f["workspaces"]})
            return JSONResponse(f)

    @app.get("/api/folders")
    def api_folders(source: Optional[str] = Query(default="")):
        with _LOCK:
            return JSONResponse({"folders": agg.folders(source or None)})

    @app.get("/api/conversations")
    def api_conversations(
        sources: Optional[str] = Query(default="", description="逗号分隔"),
        workspace: Optional[str] = Query(default="", description="逗号分隔多选（文件夹名）"),
        q: Optional[str] = Query(default=""),
        days: int = Query(default=0, ge=0, description="近 N 天（0=不限，按 createdAt）"),
        dateFrom: Optional[float] = Query(default=None, description="精确起始时间戳（Unix 秒）"),
        dateTo: Optional[float] = Query(default=None, description="精确截止时间戳（Unix 秒）"),
        all_: bool = Query(default=False, alias="all", description="返回全部匹配项（不分页）"),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, ge=1, le=100),
        sort: str = Query(default="updated"),
        order: str = Query(default="desc"),
    ):
        src_filter = [s.strip() for s in (sources or "").split(",") if s.strip()]
        ws_filter = [w.strip() for w in (workspace or "").split(",") if w.strip()]
        with _LOCK:
            convs = store.list_conversations(src_filter[0] if len(src_filter) == 1 else None)
            models_map = agg.conv_models()   # P2: 从 events 反查补全缺失的 model
            excl_map = _exclusions_map(store)   # R6: 排除态
        items = []
        for c in convs:
            d = _conv_to_dict(c, excl_map=excl_map)
            if not d["model"]:
                d["model"] = models_map.get((d["source"], d["id"]), "") or ""
            items.append(d)

        if len(src_filter) > 1:
            items = [i for i in items if i["source"] in src_filter]
        if ws_filter:
            items = [i for i in items if i["workspace"] in ws_filter]
        # 时间段：精确区间（dateFrom/dateTo）优先；未传时回退近 N 天（days）
        if dateFrom is not None or dateTo is not None:
            lo = float(dateFrom) if dateFrom is not None else 0
            hi = float(dateTo) if dateTo is not None else float("inf")
            items = [i for i in items if i["createdAt"] and lo <= i["createdAt"] <= hi]
        elif days > 0:
            import time as _t
            cutoff = _t.time() - days * 86400
            items = [i for i in items if i["createdAt"] and i["createdAt"] >= cutoff]

        qtext = (q or "").strip()
        if qtext:
            try:
                with _LOCK:
                    hits = store.search(qtext, limit=5000)
                hit_ids = {h["conversation_id"] for h in hits}
            except Exception:
                hit_ids = set()
            low = qtext.lower()
            items = [
                i for i in items
                if i["id"] in hit_ids
                or low in i["title"].lower()
                or low in i["cwd"].lower()
            ]

        colmap = {"updated": "updatedAt", "created": "createdAt",
                  "events": "eventCount", "title": "title"}
        keyname = colmap.get(sort, "updatedAt")
        reverse = order.lower() != "asc"
        items.sort(key=lambda x: (x[keyname] is None, x[keyname]), reverse=reverse)

        if all_:
            return JSONResponse({"total": len(items), "items": items})
        total = len(items)
        start = (page - 1) * page_size
        page_items = items[start:start + page_size]
        return JSONResponse({
            "total": total,
            "page": page,
            "pageSize": page_size,
            "items": page_items,
        })

    @app.get("/api/conversations/{source}/{cid}/events")
    def api_events(source: str, cid: str,
                   offset: int = Query(default=0, ge=0),
                   limit: int = Query(default=_MAX_EVENTS, ge=1, le=500)):
        """标准分页返回该会话事件流（按 seq 升序；前端倒序显示）。"""
        with _LOCK:
            conv = store.get_conversation(source, cid)
            if conv is None:
                raise HTTPException(status_code=404, detail="conversation not found")
            events = store.get_events(source, cid)
        total = len(events)
        page_events = events[offset:offset + limit]
        whole, turns = store.exclusion_state(source, cid)   # R6 排除态
        shown = [_event_to_short(e, whole=whole, excluded_turns=turns)
                 for e in page_events]
        return JSONResponse({
            "total": total,
            "offset": offset,
            "limit": limit,
            "capped": total > offset + limit,
            "events": shown,
            "exclusion": {"whole": whole, "turns": sorted(turns)},
        })

    @app.delete("/api/conversations/{source}/{cid}")
    def api_delete(source: str, cid: str):
        with _LOCK:
            try:
                n = store.delete_conversation(source, cid)
            except KeyError:
                raise HTTPException(status_code=404, detail="conversation not found")
            # R6 修复：索引库同步级联清理，否则已删会话仍可被记忆召回
            _purge_index(source, cid)
            _invalidate()
        return {"deleted": True, "source": source, "id": cid, "eventsRemoved": n}

    @app.patch("/api/conversations/{source}/{cid}/title")
    def api_rename(source: str, cid: str, body: TitleIn):
        with _LOCK:
            ok = store.update_title(source, cid, body.title)
            if not ok:
                raise HTTPException(status_code=404, detail="conversation not found")
            _invalidate()
        return {"updated": True}

    # ------------------------------------------------------------------
    # R6 记忆排除：控制会话/轮次不写入记忆（意图存采集库，索引即时收敛）
    # ------------------------------------------------------------------

    @app.get("/api/exclusions/summary")
    def api_exclusions_summary():
        """全库排除统计（面板顶部展示「已排除 N 项」）。"""
        with _LOCK:
            rows = store.list_exclusions()
        return {
            "count": len(rows),
            "conversations": len({(r["source"], r["conversation_id"]) for r in rows}),
            "turns": sum(1 for r in rows if r["turn_key"]),
        }

    @app.get("/api/conversations/{source}/{cid}/memory-exclusion")
    def api_exclusion_get(source: str, cid: str):
        """该会话的排除清单 + 已入库记忆规模（抽屉渲染勾选态用）。"""
        with _LOCK:
            if store.get_conversation(source, cid) is None:
                raise HTTPException(status_code=404, detail="conversation not found")
            rows = store.list_exclusions(source, cid)
            whole = any(not r["turn_key"] for r in rows)
            turns = [r["turn_key"] for r in rows if r["turn_key"]]
            indexed, removed = _index_counts(source, cid, turns, whole)
            # 每个已排除轮次的跨源副本（提示用户可一并排除）
            peer_map = {t: [{"source": s2, "id": c2}
                            for s2, c2 in store.peers_with_turn(source, cid, t)]
                        for t in turns}
        return {
            "whole": whole,
            "turns": turns,
            "indexedUnits": indexed,
            "removedUnits": removed,
            "peerTurns": peer_map,
        }

    @app.post("/api/conversations/{source}/{cid}/memory-exclusion")
    def api_exclusion_add(source: str, cid: str, body: ExclusionIn):
        """标记不写入：落意图 + 立即从索引移除（历史记忆即刻不再召回）。

        sync_peers：轮次排除时把同一 turn_key 的其他源副本一并排除。
        """
        with _LOCK:
            if store.get_conversation(source, cid) is None:
                raise HTTPException(status_code=404, detail="conversation not found")
            created = store.add_exclusion(source, cid, body.turn_key, body.note)
            removed = _purge_index(source, cid, body.turn_key or None)
            peers: list[dict] = []
            if body.sync_peers and body.turn_key:
                for psrc, pcid in store.peers_with_turn(source, cid, body.turn_key):
                    store.add_exclusion(psrc, pcid, body.turn_key, body.note)
                    n = _purge_index(psrc, pcid, body.turn_key)
                    removed += n
                    peers.append({"source": psrc, "id": pcid, "unitsRemoved": n})
            _invalidate()
        return {"excluded": True, "created": created, "turnKey": body.turn_key,
                "unitsRemoved": removed, "peers": peers}

    @app.delete("/api/conversations/{source}/{cid}/memory-exclusion")
    def api_exclusion_remove(source: str, cid: str, turn_key: str = ""):
        """取消标记。被移除的记忆需下次 sync 重新嵌入才会回到索引。"""
        with _LOCK:
            if store.get_conversation(source, cid) is None:
                raise HTTPException(status_code=404, detail="conversation not found")
            ok = store.remove_exclusion(source, cid, turn_key)
            _invalidate()
        return {"excluded": False, "removed": ok, "turnKey": turn_key,
                "hint": "下次 sync（向量化）后该内容恢复召回"}

    # ------------------------------------------------------------------
    # 记忆索引网关：rag 后端为进程内直调（经 engine_request 派发），
    # 不碰本地采集库、不持 _LOCK；不可用时透明降级并回 503
    # ------------------------------------------------------------------

    @app.get("/api/memos/status")
    def api_memos_status():
        from agentmemhub import memos_daemon
        return JSONResponse(memos_daemon.daemon_status())

    def _require_engine() -> dict:
        """引擎在线且鉴权可过则返回 overview；否则 503（detail 含原因）。"""
        from agentmemhub import memos_daemon
        try:
            return memos_daemon.engine_request("GET", "/api/v1/overview", timeout=15)
        except memos_daemon.EngineAuthError as e:
            raise HTTPException(status_code=503, detail=str(e))
        except Exception:
            raise HTTPException(status_code=503, detail="memory engine offline")

    @app.get("/api/memos/search")
    def api_memos_search(q: str = Query(default="", min_length=1),
                         top: int = Query(default=20, ge=1, le=50)):
        """语义检索：返回 hits（含会话定位信息，前端可点击跳转）。"""
        import json as _json
        from agentmemhub import memos_daemon
        ov = _require_engine()
        try:
            res = memos_daemon.engine_request(
                "POST", "/api/v1/memory/search",
                body={"agent": "hermes", "query": q}, timeout=30)
        except memos_daemon.EngineAuthError as e:
            raise HTTPException(status_code=503, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"engine query failed: {e}")
        hits = [
            {"tier": h.get("tier"), "refKind": h.get("refKind"),
             "score": h.get("score"), "snippet": h.get("snippet") or "",
             "refId": h.get("refId"),
             # 会话定位（点击跳转到对应会话/轮次）
             "source": h.get("source") or "",
             "conversationId": h.get("conversationId") or "",
             "turnKey": h.get("turnKey") or "",
             "title": h.get("title") or "",
             "atomic": bool(h.get("atomic"))}
            for h in (res.get("hits") or [])[:top]
        ]
        return JSONResponse({"query": q, "hits": hits,
                             "injectedContext": res.get("injectedContext") or "",
                             "episodes": ov.get("episodes"),
                             "traces": ov.get("traces")})

    @app.post("/api/memos/feedback")
    def api_memos_feedback(traceId: str = Query(...),
                           polarity: str = Query(..., pattern="^(positive|negative|neutral)$"),
                           magnitude: float = Query(default=1.0),
                           channel: str = Query(default="explicit")):
        """记忆打分（正/负反馈）。

        转发引擎 POST /api/v1/feedback 并带 traceId——引擎会立即按反馈
        极性/幅度重算该条记忆的 value/rHuman/priority（无需 LLM）。
        channel 必须是引擎约束的 explicit|implicit（面板人工评分=explicit）。
        """
        from agentmemhub import logs, memos_daemon
        try:
            res = memos_daemon.engine_request(
                "POST", "/api/v1/feedback",
                body={"channel": channel, "polarity": polarity,
                      "magnitude": magnitude, "traceId": traceId},
                timeout=15)
        except memos_daemon.EngineAuthError as e:
            raise HTTPException(status_code=503, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"engine feedback failed: {e}")
        # 手动打分成功 → 记入已评清单（重跑批量评分时跳过，不覆盖手动分）
        try:
            from agentmemhub.scoring import mark_scored
            mark_scored(traceId)
        except Exception:
            pass
        logs.record(f"记忆打分：trace={traceId} {polarity}（幅度 {magnitude}）")
        return JSONResponse({"ok": True, "traceId": traceId, "feedback": res})

    @app.get("/api/memos/traces")
    def api_memos_traces(limit: int = Query(default=8, ge=1, le=50),
                         offset: int = Query(default=0, ge=0)):
        """转发最近记忆列表（时间线，纯 SQL 侧）。"""
        from agentmemhub import memos_daemon
        _require_engine()
        try:
            res = memos_daemon.engine_request(
                "GET", f"/api/v1/traces?limit={limit}&offset={offset}&groupByTurn=1",
                timeout=15)
        except memos_daemon.EngineAuthError as e:
            raise HTTPException(status_code=503, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"engine query failed: {e}")
        traces = [
            {"id": t.get("id"), "ts": t.get("ts"),
             "userText": (t.get("userText") or "")[:200],
             "agentText": (t.get("agentText") or "")[:200],
             "value": t.get("value"), "episodeId": t.get("episodeId")}
            for t in (res.get("traces") or [])
        ]
        return JSONResponse({"total": res.get("total"), "offset": offset,
                             "traces": traces})

    # ------------------------------------------------------------------
    # 数据操作后台任务（ingest / 写入记忆 / 评分 / 清洗 / 补向量）：耗时
    # 操作放后台线程，前端轮询状态；同一时刻只允许一个任务（避免并发写库）
    # ------------------------------------------------------------------

    @app.post("/api/admin/ingest")
    def api_admin_ingest(source: str = Query(default=""),
                         signature: str = Query(default="")):
        from agentmemhub import adapters, cli
        from agentmemhub import logs
        from agentmemhub.web import tasks
        name = f"提取会话入库{'（' + source + '）' if source else ''}"
        job = tasks.submit(name, _logged_task(
            name, _run_ingest_fn(cli, adapters, source, signature)))
        if job is None:
            raise HTTPException(status_code=409, detail="已有任务在运行，请等待完成")
        logs.record(f"提交任务：{name}（id={job['id']}）")
        return JSONResponse({"job": job})

    @app.post("/api/admin/push")
    def api_admin_push(source: str = Query(default="")):
        """写入记忆：把采集库会话向量化写入记忆索引（后台任务）。"""
        from agentmemhub import cli, memos_daemon
        from agentmemhub import logs
        from agentmemhub.web import tasks
        if memos_daemon.auth_state() is None:
            raise HTTPException(status_code=503, detail="记忆索引不可用（检查 database/session_rag.db 与 models/）")
        name = f"写入记忆索引{'（' + source + '）' if source else ''}"
        job = tasks.submit(name, _logged_task(name, _run_push_fn(cli, source)))
        if job is None:
            raise HTTPException(status_code=409, detail="已有任务在运行，请等待完成")
        logs.record(f"提交任务：{name}（id={job['id']}）")
        return JSONResponse({"job": job})

    @app.get("/api/admin/job")
    def api_admin_job():
        from agentmemhub.web import tasks
        return JSONResponse({"job": tasks.status()})

    @app.post("/api/admin/score")
    def api_admin_score(limit: int = Query(default=0, ge=0),
                        dryRun: bool = Query(default=False)):
        """LLM 批量自动评分历史记忆（后台任务，实时进度）。需记忆索引可用且已配置 LLM。"""
        from agentmemhub import cli, logs, memos_daemon
        from agentmemhub.web import tasks
        if memos_daemon.auth_state() is None:
            raise HTTPException(status_code=503, detail="记忆索引不可用（检查 database/session_rag.db 与 models/）")
        name = f"自动评分历史记忆{'（上限 ' + str(limit) + ' 条）' if limit else ''}"
        job = tasks.submit(name, _logged_task(
            name, _run_score_fn(cli, limit, dryRun)))
        if job is None:
            raise HTTPException(status_code=409, detail="已有任务在运行，请等待完成")
        logs.record(f"提交任务：{name}（id={job['id']}）")
        return JSONResponse({"job": job})

    @app.post("/api/admin/clean")
    def api_admin_clean(source: str = Query(default="")):
        """清洗数据：删除系统注入事件（后台任务，先打印统计再执行）。"""
        from agentmemhub import cli
        from agentmemhub import logs
        from agentmemhub.web import tasks
        name = f"清洗注入数据{'（' + source + '）' if source else ''}"
        job = tasks.submit(name, _logged_task(
            name, _run_clean_fn(cli, source)))
        if job is None:
            raise HTTPException(status_code=409, detail="已有任务在运行，请等待完成")
        logs.record(f"提交任务：{name}（id={job['id']}）")
        return JSONResponse({"job": job})

    @app.post("/api/admin/exclude")
    def api_admin_exclude(body: "ExclusionBatchIn"):
        """批量设为不写入记忆（后台任务：落标记 + 逐会话清索引）。"""
        from agentmemhub import logs
        from agentmemhub.web import tasks
        name = f"批量排除记忆（{len(body.items)} 个会话）"
        job = tasks.submit(name, _logged_task(
            name, _run_exclude_fn(body.items, body.turn_key)))
        if job is None:
            raise HTTPException(status_code=409, detail="已有任务在运行，请等待完成")
        logs.record(f"提交任务：{name}（id={job['id']}）")
        return JSONResponse({"job": job})

    @app.post("/api/admin/distill")
    def api_admin_distill(source: str = Query(default=""),
                          limit: int = Query(default=0, ge=0),
                          dryRun: bool = Query(default=False)):
        """记忆蒸馏：LLM 把原始会话提炼为结构化记忆（后台任务，实时进度）。

        需 llm 段已配置；幂等（重跑跳过已完成切片），失败切片不登记、重跑可补。
        """
        from agentmemhub import logs, memos_daemon
        from agentmemhub.web import tasks
        if memos_daemon.auth_state() is None:
            raise HTTPException(
                status_code=503,
                detail="记忆索引不可用（检查 database/session_rag.db 与 models/）")
        name = ("记忆蒸馏" + ("（预览）" if dryRun else "")
                + (f"（{source}）" if source else ""))
        job = tasks.submit(name, _logged_task(
            name, _run_distill_fn(source, limit, dryRun)))
        if job is None:
            raise HTTPException(status_code=409, detail="已有任务在运行，请等待完成")
        logs.record(f"提交任务：{name}（id={job['id']}）")
        return JSONResponse({"job": job})

    @app.post("/api/admin/rebuild")
    def api_admin_rebuild(mode: str = Query(default="repair",
                                            pattern="^(repair|rebuild)$")):
        """补向量：给缺向量的记忆补嵌（后台任务，逐轮进度）。"""
        from agentmemhub import logs, memos_daemon
        from agentmemhub.web import tasks
        if memos_daemon.auth_state() is None:
            raise HTTPException(status_code=503, detail="记忆索引不可用（检查 database/session_rag.db 与 models/）")
        name = f"补向量（{mode}）"
        job = tasks.submit(name, _logged_task(name, _run_rebuild_fn(mode)))
        if job is None:
            raise HTTPException(status_code=409, detail="已有任务在运行，请等待完成")
        logs.record(f"提交任务：{name}（id={job['id']}）")
        return JSONResponse({"job": job})

    @app.get("/api/logs")
    def api_logs(limit: int = Query(default=100, ge=1, le=500)):
        """统一操作日志（面板控制/任务执行的最近记录，内存 + JSONL 留痕）。"""
        from agentmemhub import logs
        return JSONResponse({"logs": logs.recent(limit)})

    # ---- 静态页面（index.html 由 StaticFiles html=True 自动兜底 /）----
    static_dir = Path(__file__).resolve().parent / "static"
    if static_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

    return app

