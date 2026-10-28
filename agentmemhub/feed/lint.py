# -*- coding: utf-8 -*-
"""投喂产物与登记的**确定性体检**（纯脚本、零 LLM、零外部依赖）。

为什么需要它（来自参考实现的教训）
==================================
`second-brain-skill` 的 `lint.py` 是它**唯一**的实质代码（352 行、零 LLM）：LLM 负责
产出，脚本负责**校验**。本项目此前只有 `obsidian.verify()`（依赖外部程序、且索引会过期）
—— 正因缺这一层，"顶层 index.md 的链接指向 `.json`"那个 bug 才能存活很久。

投喂链路尤其需要它，因为**两份产物必须严丝合缝**：登记表（materials.db）↔ 原件（raw/）
↔ 交付物（kb/）↔ 投影（units）。任何一处错位都是**静默的**：不报错、不让测试变红，
只是"搜不到"或"点开是空的"。

分级（照 `lint.py`）：**P0 = 数据不一致，必须修**；**P1 = 建议复核**。
CLI 在 P0 > 0 时返回**非零退出码**，可直接当提交/巡检门槛。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

#: 交付物 frontmatter 的必填字段（缺任一项 ⇒ 下游读不到关键信息）
REQUIRED_FM = ("title", "origin", "doc", "doc_time")
_LEVEL_ORDER = {"P0": 0, "P1": 1, "P2": 2}


def _fm_of(text: str) -> dict:
    """解析 kb 页面的 frontmatter（YAML 块）。解析不了返回空 dict。"""
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        import yaml
        d = yaml.safe_load(parts[1])
        return d if isinstance(d, dict) else {}
    except Exception:                       # noqa: BLE001 —— 坏值当缺失，由调用方报错
        return {}


def _kb_file_of(kb_dir: Path, doc_id: str) -> Path:
    """按 doc_id 找交付物（`<标题>-<id>.md` 与旧的纯 `<id>.md` 都认）。

    为什么不直接拼 `<doc_id>.md`：2026-10-28 起交付物文件名改成
    「自然语言标题 + 末尾 doc_id」，路径不能再由 doc_id 反推。
    """
    exact = kb_dir / ("%s.md" % doc_id)
    if exact.is_file():
        return exact
    for p in sorted(kb_dir.glob("*-%s.md" % doc_id)):
        if p.is_file():
            return p
    return exact


def run_lint(*, config=None, with_index: bool = True,
             index_db=None) -> dict[str, Any]:
    """跑全部检查。返回 `{"issues": [...], "counts": {...}, "p0": n, "ok": bool}`。

    `issues` 每条：`{"level", "kind", "target", "detail"}` —— 便于 JSON 消费与
    按 kind 分组（"哪一类问题最多"比"一共几条"更有用）。

    `index_db` 可注入（**测试用**）：不注入就读配置里的真实索引库 —— 那是只读的，
    但会让测试依赖真实库状态、结果不确定。
    """
    from .store import MaterialsDB, kb_doc_of

    if config is None:
        from agentmemhub.config import config as _cfg
        config = _cfg()
    paths = config.feed_paths()
    issues: list[dict[str, str]] = []

    def add(level: str, kind: str, target: str, detail: str) -> None:
        issues.append({"level": level, "kind": kind, "target": str(target),
                       "detail": detail})

    # ---- 1. 登记表 ↔ 原件 ↔ 交付物 --------------------------------------
    rows: list[dict] = []
    db_file = Path(paths["db"])
    if db_file.exists():
        db = MaterialsDB(db_file)
        try:
            rows = db.list()
        finally:
            db.close()
    by_doc = {r["doc_id"]: r for r in rows}
    store_root = Path(paths["store"])
    kb_dir = Path(paths["kb"])

    for r in rows:
        doc = r["doc_id"]
        # 原件必须在（它是不可再生的那一份）
        raw_rel = r.get("raw_path") or ""
        if raw_rel and not (store_root / raw_rel).is_file():
            add("P0", "raw_missing", doc,
                "登记的原件不在 raw/ 里：%s（原件不可再生，必须查明）" % raw_rel)
        if r.get("status") != "published":
            # **失败素材也要在体检里露头**：`needs_ocr` 不是数据坏了，而是能力还没接
            # （原件与页面镜像都已保住，接上 T4 通路可 `feed retry` 续跑）。不报的话
            # 这些素材会静默躺在库里 —— 而"静默"正是这个模块存在的理由。
            kind = str(r.get("error_kind") or "")
            if kind in ("needs_ocr", "needs_renderer"):
                add("P1", "awaiting_ocr", doc,
                    "扫描件待 OCR（%s）：原件与页面镜像已保住，接上 T4 通路后可 "
                    "`feed retry`；也可先 `feed add --stdin` 投喂 OCR 结果" % kind)
            elif str(r.get("status")) == "failed":
                add("P1", "failed_material", doc,
                    "转译失败（%s）：%s" % (kind or "-", (r.get("error") or "")[:100]))
            continue
        kb_file = _kb_file_of(kb_dir, doc)
        if not kb_file.is_file():
            add("P0", "kb_missing", doc,
                "登记为 published 但交付物不存在：%s" % kb_file)
            continue
        fm = _fm_of(kb_file.read_text(encoding="utf-8", errors="replace"))
        missing = [k for k in REQUIRED_FM if not fm.get(k)]
        if missing:
            add("P0", "fm_missing", doc,
                "交付物 frontmatter 缺字段：%s" % "、".join(missing))
        if str(fm.get("doc") or "") != doc:
            add("P0", "doc_mismatch", doc,
                "frontmatter 的 doc（%s）与登记不一致 ⇒ 溯源链会断"
                % fm.get("doc"))
        if str(fm.get("origin") or "") != "external":
            add("P0", "origin_wrong", doc,
                "投喂交付物的 origin 必须是 external（实际 %s）" % fm.get("origin"))
        if r.get("warnings") or float(r.get("quality") or 1.0) < 0.5:
            add("P1", "low_quality", doc,
                "质量 %.2f / 告警：%s" % (float(r.get("quality") or 0.0),
                                          "；".join(r.get("warnings") or []) or "无"))

    # ---- 2. 交付物 ↔ 登记（反向：kb 里有、登记里没有）-------------------
    if kb_dir.is_dir():
        seen: dict[str, list[str]] = {}
        for f in sorted(kb_dir.rglob("*.md")):
            doc = kb_doc_of(f.name)
            if not doc or doc not in by_doc:
                add("P0", "kb_unregistered", f.stem,
                    "交付物没有对应登记：%s（文件名末尾应是 16 位 <doc_id>.md）"
                    % f.name)
                continue
            seen.setdefault(doc, []).append(f.name)
        # 同一份素材留了两份交付物（改了标题但旧文件没删、或手工复制）⇒ 投影会
        # 把同内容算成两个页面，召回时自己跟自己做重复 —— 必须报出来。
        for doc, names in seen.items():
            if len(names) > 1:
                add("P0", "kb_duplicate", doc,
                    "同一份素材有多个交付物文件：%s" % "、".join(sorted(names)))

    # ---- 3. 投影（units）与登记的一致性 ---------------------------------
    if with_index:
        try:
            from agentmemhub.rag.ingest import open_index
            from agentmemhub.rag.config import load_settings
            idx = open_index(index_db or load_settings().index_db)
            try:
                rows_idx = idx.execute(
                    "SELECT doc_id, wiki_path, origin FROM units"
                    " WHERE source='wiki'").fetchall()
            finally:
                idx.close()
            for doc_id, wiki_path, origin in rows_idx:
                wp = str(wiki_path or "")
                if wp and not Path(wp).is_file():
                    add("P0", "projection_stale", doc_id or wp,
                        "投影指向的文件不存在：%s（跑 index-pages 收敛）" % wp)
                if doc_id and doc_id not in by_doc:
                    # 撤下/删除后残留的投影 —— 不报就"删了还能搜到"（本项目踩过）
                    add("P0", "projection_orphan", doc_id,
                        "投影里有这条，登记表里没有 ⇒ 撤下/删除没清干净")
                elif doc_id and (origin or "") != "external":
                    add("P0", "projection_origin", doc_id,
                        "投喂页面的投影 origin 必须是 external（实际 %s）" % origin)
        except Exception as e:              # noqa: BLE001 —— 索引库打不开不该让体检失败
            add("P1", "index_unavailable", "-",
                "索引库检查跳过：%s: %s" % (type(e).__name__, str(e)[:120]))

    # ---- 4. 配置级：页面根不能互相嵌套 ----------------------------------
    try:
        from agentmemhub import wiki_index
        roots = wiki_index.page_roots(config.wiki.get("out_l2"), config=config)
        rs = [str(Path(r).resolve()) for r in roots]
        for i, a in enumerate(rs):
            for b in rs[i + 1:]:
                if a in b or b in a:
                    add("P0", "roots_nested", a,
                        "页面根互相嵌套（%s / %s）⇒ 一方的整域替换会毁掉另一方" % (a, b))
    except Exception as e:                  # noqa: BLE001
        add("P1", "roots_unavailable", "-",
            "页面根检查跳过：%s: %s" % (type(e).__name__, str(e)[:120]))

    issues.sort(key=lambda x: (_LEVEL_ORDER.get(x["level"], 9), x["kind"], x["target"]))
    counts: dict[str, int] = {}
    for i in issues:
        counts[i["level"]] = counts.get(i["level"], 0) + 1
    return {"issues": issues, "counts": counts, "total": len(issues),
            "p0": counts.get("P0", 0), "ok": counts.get("P0", 0) == 0,
            "path": str(paths["kb"])}


def format_report(result: dict[str, Any], *, limit: int = 40) -> str:
    """人可读报告（CLI 用）。P0 在前，按 kind 分组。"""
    lines = ["投喂产物体检 —— %s" % result.get("path", "")]
    lines.append("共 %d 项：%s"
                 % (result.get("total", 0),
                    "、".join("%s %d" % (k, v)
                              for k, v in sorted((result.get("counts") or {}).items()))
                    or "无"))
    lines.append("")
    if not result.get("issues"):
        lines.append("✅ 未发现问题")
        return "\n".join(lines)
    for lv in ("P0", "P1", "P2"):
        items = [i for i in result["issues"] if i["level"] == lv]
        if not items:
            continue
        mark = {"P0": "🔴 P0 — 数据不一致，必须修",
                "P1": "🟡 P1 — 建议复核"}.get(lv, "🟢 %s" % lv)
        lines.append("%s（%d）" % (mark, len(items)))
        for i in items[:limit]:
            lines.append("  [%s] %s: %s" % (i["kind"], i["target"], i["detail"]))
        if len(items) > limit:
            lines.append("  …（其余 %d 项，用 --json 看全）" % (len(items) - limit))
        lines.append("")
    if result.get("p0"):
        lines.append("P0 不为 0 ⇒ 退出码 1（可直接当提交门槛）")
    return "\n".join(lines)
