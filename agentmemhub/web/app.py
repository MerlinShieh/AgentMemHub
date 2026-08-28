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
_SORTABLE = {
    "updated": "updated_at", "created": "created_at",
    "events": "event_count", "title": "title",
}


def _clip(v: Any, n: int) -> Optional[str]:
    if v is None:
        return None
    s = str(v)
    return s if len(s) <= n else s[:n] + "…"


def _workspace_of(cwd: Optional[str]) -> str:
    if not cwd:
        return "(unknown)"
    return cwd.rstrip("\\/").rsplit("\\", 1)[-1].rsplit("/", 1)[-1] or "(unknown)"


def _conv_to_dict(c: Any) -> dict[str, Any]:
    """conversations row → 前端 camelCase 契约字段。"""
    import json
    try:
        roles = json.loads(c["roles_json"]) if c["roles_json"] else []
    except Exception:
        roles = []
    return {
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


def _event_to_short(e: Any) -> dict[str, Any]:
    """Event → 前端短键压缩形状（键名与静态快照流水线一致）。"""
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
            convs = []
            for i, c in enumerate(store.list_conversations()):
                d = _conv_to_dict(c)
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
        items = []
        for c in convs:
            d = _conv_to_dict(c)
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
        shown = [_event_to_short(e) for e in page_events]
        return JSONResponse({
            "total": total,
            "offset": offset,
            "limit": limit,
            "capped": total > offset + limit,
            "events": shown,
        })

    @app.delete("/api/conversations/{source}/{cid}")
    def api_delete(source: str, cid: str):
        with _LOCK:
            try:
                n = store.delete_conversation(source, cid)
            except KeyError:
                raise HTTPException(status_code=404, detail="conversation not found")
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
    # 记忆引擎（MemOS）网关：不碰本地库，不持 _LOCK；引擎离线时透明降级
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

    @app.post("/api/memos/start")
    def api_memos_start():
        from agentmemhub import memos_daemon
        r = memos_daemon.daemon_start()
        if not (r.get("started") or r.get("online")):
            raise HTTPException(status_code=502, detail=r)
        return JSONResponse(r)

    @app.post("/api/memos/stop")
    def api_memos_stop():
        from agentmemhub import memos_daemon
        r = memos_daemon.daemon_stop()
        if not r.get("stopped") and r.get("reason") not in ("not-online",):
            raise HTTPException(status_code=502, detail=r)
        return JSONResponse(r)

    @app.get("/api/memos/search")
    def api_memos_search(q: str = Query(default="", min_length=1),
                         top: int = Query(default=8, ge=1, le=30)):
        """转发语义检索：返回 hits（tier/refKind/score/snippet）。"""
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
             "refId": h.get("refId")}
            for h in (res.get("hits") or [])[:top]
        ]
        return JSONResponse({"query": q, "hits": hits,
                             "injectedContext": res.get("injectedContext") or "",
                             "episodes": ov.get("episodes"),
                             "traces": ov.get("traces")})

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

    # ---- 静态页面（index.html 由 StaticFiles html=True 自动兜底 /）----
    static_dir = Path(__file__).resolve().parent / "static"
    if static_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

    return app

