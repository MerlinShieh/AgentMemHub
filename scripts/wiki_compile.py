"""LLM Wiki 编译原型：把一个会话的零散记忆归纳成结构化 wiki 文档。

**这一步是"碎片 → wiki"的核心，也是 Karpathy 文档没有覆盖的部分。**
他的 Ingest 假设输入是**有内在结构的素材**（一篇文章有标题/章节/逻辑流），
而我们的输入是**无结构的碎片**（一条条彼此独立的结论）。前者是*提炼*
（extraction），后者是*归纳*（induction）——后者难得多，也正是本脚本要验证的。

架构（实测踩坑后定型）：
    实测 45 条记忆一次性编译：prompt 15150 tokens、思维链 21483 tokens，
    输出到 32K 上限仍被截断（finish_reason=length，JSON 未闭合）。而且
    deepseek-v4-flash 是推理模型，**思维链也计入 max_tokens**，8K 时正文
    一个字都轮不到。加上 `topic` 几乎唯一（1543 条→1541 个）、`slice_key`
    是 merge/whole（会话级而非主题级），**没有现成的分组键**。

    因此改为 **map-reduce 两段式**（短会话仍走单次，省一次调用）：
        ① 分组（轻量）：编号 + 一句话摘要 → 分组方案
        ② 逐组编译：单组记忆全文 → 一个页面

本脚本刻意只做**验证**，不是正式实现：
    · 只读打开索引库（mode=ro），绝不写库、不建表、不改引擎；
    · 产出为独立目录下的 markdown 文件，不进入任何数据表。

用法：
    python scripts/wiki_compile.py --list
    python scripts/wiki_compile.py --source X --conversation-id Y --out DIR
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Windows 控制台默认 GBK，用量报告里的 ¥ 会抛 UnicodeEncodeError 中断流程——
# 而它出现在**写文件之前**，实测导致整个会话的产物丢失（跑完 4 页却没落盘）。
# 统一改成 UTF-8 并容错，绝不让打印问题影响产物。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

#: 记忆条数不超过此值就单次编译（省一次分组调用）
SINGLE_SHOT_MAX = 12

# ---------------------------------------------------------------------------
# 提示词 —— 整个方案成败在这里
#
# 逐条对应我们分析过的难点：
#   1. 先分主题：一个会话未必只有一个主题（实测 45 条的会话横跨 7 类内容）
#   2. 综合而非拼接：碎片是"记录"，页面是"叙述"
#   3. 保留溯源：论断级来源标注 —— 这是那两个项目做不到的
#   4. 矛盾标注：S3 只做去重（重复），不解决冲突（相反）
#   5. 时效择优：靠会话内的 created_at 序判断新旧取代
#   6. **允许不成页**：必须给 LLM 拒绝的权利，否则它会硬凑页面
# ---------------------------------------------------------------------------

_PAGE_RULES = """规则：
1. **综合而非拼接**：重组顺序、合并重复、剔除过程叙述。正文要读起来像人写的条目，
   而不是记忆的堆砌。
2. **正文里绝不要再写页面标题**：`body` 直接从内容开始，节标题用 `##` / `###`。
   页面标题由 `title` 字段单独渲染——重复写会产出两级相同标题（实测踩过）。
3. **保留溯源**：正文里每个论断后用 [n] 标注来源记忆编号（可多个，如 [3][7]）。
4. **标注矛盾**：互相冲突的记忆用 `> ⚠️ 矛盾：` 引用块，写明双方内容与来源编号。
5. **时效择优**：若后面的记忆取代了前面的结论，只写新结论，可注明"早期曾用 X（[n]）"。
6. **信息取舍**：具体行号、临时路径、一次性操作可以省略；保留有复用价值的结论与根因。
7. 正文用中文，markdown 格式（可用小标题、列表、表格、代码块）。专有名词保留原文。"""

SYSTEM_SINGLE = """你是知识库编辑。任务：把一段开发过程中沉淀的**零散记忆**编译成结构化的 wiki 文档。

输入不是一篇文章，而是若干条彼此独立、粒度不一的结论（决策 / 事实 / 偏好 / 踩坑教训）。
它们来自同一次会话，但**未必属于同一个主题**。"""

USER_SINGLE = """以下是同一个开发会话中沉淀的 {n} 条记忆：

{listing}

请编译成 wiki 文档，返回如下 JSON：
{{
  "session_summary": "这个会话总体上在做什么（一两句话）",
  "pages": [
    {{"title": "页面标题", "type": "concept | entity | analysis | lesson",
      "summary": "一句话摘要，将用于 index.md", "sources": [1, 5],
      "body": "markdown 正文", "related": ["相关页面标题"]}}
  ],
  "unclassified": [{{"n": 12, "why": "为什么这条不值得单独成页"}}]
}}

除上述外，遵守：
1. **先分主题**：这些记忆可能横跨多个主题。同一主题归为一页，不同主题分页。
   宁可多分一页，也不要把不相关的信息糅在一起。
2. **标题要具体**，一眼能看出讲什么，不要"关于 XX 的讨论"这类空泛说法。
3. **允许不成页**：孤立、无复用价值或纯过程性的记忆放进 unclassified 并说明理由。
   **不要为了用尽输入而硬凑页面。**

{page_rules}"""

SYSTEM_PLAN = """你是知识库编辑。下面是一个开发会话中沉淀的若干条记忆（编号 + 一句话摘要）。

你的任务：**把它们按主题分组**，使每一组成为 wiki 文档的一个页面。"""

USER_PLAN = """会话共 {n} 条记忆：

{listing}

请返回分组方案 JSON：
{{
  "session_summary": "这个会话总体上在做什么（一两句话）",
  "groups": [
    {{"title": "页面标题", "type": "concept | entity | analysis | lesson",
      "members": [1, 5, 7], "why": "这组讲什么（一句话）"}}
  ],
  "unclassified": [{{"n": 12, "why": "为什么这条不值得单独成页"}}]
}}

规则：
1. 同一主题归一组，不同主题分开。**宁可多分几组，也不要把不相关的糅在一起。**
2. 每组 2–8 条为宜；单条若有独立价值（如一条重要的架构决策）也可独占一组。
3. 标题要具体，一眼看出讲什么，不要"关于 XX"这类空泛说法。
   - concept：机制 / 方案 / 设计
   - entity：项目 / 工具 / 产品 / 库
   - analysis：对比 / 权衡 / 结论
   - lesson：踩过的坑与根因
4. **允许不成页**：孤立、无复用价值或纯过程性的记忆放进 unclassified 并说明理由。
   **不要为了用尽输入而硬凑分组。**"""

SYSTEM_PAGE = """你是知识库编辑。下面是一个主题下的若干条零散记忆，请把它们编译成**一个** wiki 页面。

不是罗列，而是理解之后用自己的话重写。"""


# ---------------------------------------------------------------------------

def open_ro(db: Path) -> sqlite3.Connection:
    """只读打开索引库 —— 本脚本对数据的唯一态度。"""
    if not db.exists():
        raise SystemExit(f"索引库不存在：{db}")
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def _wlog(**kw) -> None:
    """写一条编译事件到 `logs/wiki.log`（旁路，写失败绝不影响编译）。

    全量编译动辄十几分钟到几十分钟，控制台输出一关就没了；失败清单、进度、
    用量都必须落盘才能事后追。
    """
    try:
        from agentmemhub.logs import audit_wiki
        kw.setdefault("ts", time.time())
        audit_wiki(kw)
    except Exception:
        pass


def list_sessions(conn: sqlite3.Connection, *, min_n: int = 0,
                  limit: int = 20) -> list[tuple]:
    """列候选会话（按有效蒸馏记忆数降序）。

    标题取自 `units` 投影 —— `distilled_memories` 本身不存会话标题。
    """
    return conn.execute(
        "SELECT m.source, m.conversation_id, COUNT(*) n, MAX(u.title)"
        " FROM distilled_memories m"
        " LEFT JOIN units u ON u.src_id = 'dst_' || m.content_hash"
        " WHERE m.status IN ('new','similar')"
        " GROUP BY m.source, m.conversation_id HAVING n >= ?"
        " ORDER BY n DESC LIMIT ?", (min_n, limit)).fetchall()


def load_memories(conn: sqlite3.Connection, source: str,
                  conversation_id: str) -> list[dict]:
    """读某会话的有效蒸馏记忆。

    刻意读 `distilled_memories`（真相源）而非 `units`（召回投影）—— 前者带
    type / confidence / created_at，后者只是检索用的文本层。
    """
    rows = conn.execute(
        "SELECT id, type, confidence, content, created_at, topic"
        " FROM distilled_memories"
        " WHERE source=? AND conversation_id=? AND status IN ('new','similar')"
        " ORDER BY created_at, id", (source, conversation_id)).fetchall()
    return [{"id": r[0], "type": r[1], "confidence": r[2], "content": r[3],
             "created_at": r[4], "topic": r[5]} for r in rows]


def _clip(s: str, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[:n] + "…"


def build_listing(memories: list[dict], cap: int = 0) -> str:
    """记忆清单，编号即溯源锚点（1 起）。cap>0 时截断正文（分组阶段用）。"""
    out = []
    for i, m in enumerate(memories, 1):
        body = _clip(m["content"], cap) if cap else " ".join((m["content"] or "").split())
        out.append("[%d] (%s/%s) %s" % (i, m["type"], m["confidence"], body))
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 两阶段编译
# ---------------------------------------------------------------------------

def plan_groups(client, memories: list[dict]) -> dict:
    """① 轻量分组：只喂摘要（cap=200），输出分组方案。"""
    listing = build_listing(memories, cap=200)
    user = USER_PLAN.format(n=len(memories), listing=listing)
    return client.complete_json(SYSTEM_PLAN, user, max_tokens=16000,
                                temperature=0.2)


def compile_page(client, memories: list[dict], group: dict) -> dict:
    """② 逐组编译：只喂该组成员的全文。"""
    idx = [n for n in (group.get("members") or []) if 1 <= n <= len(memories)]
    members = [(n, memories[n - 1]) for n in idx]
    if not members:
        return {}
    listing = "\n".join(
        "[%d] (%s/%s) %s" % (n, m["type"], m["confidence"],
                            " ".join((m["content"] or "").split()))
        for n, m in members)
    user = ("本组主题（由分组阶段给出）：%s\n\n以下是该主题下的 %d 条记忆：\n\n%s\n\n"
            "请编译成一个 wiki 页面，返回 JSON：\n"
            "{\n"
            '  "title": "页面标题（可用分组阶段的标题，也可按内容修正）",\n'
            '  "type": "concept | entity | analysis | lesson",\n'
            '  "summary": "一句话摘要，将用于 index.md",\n'
            '  "sources": [%s],\n'
            '  "body": "markdown 正文",\n'
            '  "related": ["相关页面标题"]\n'
            "}\n\n%s" % (group.get("why") or group.get("title") or "",
                         len(members), listing,
                         ", ".join(str(n) for n, _ in members), _PAGE_RULES))
    r = client.complete_json(SYSTEM_PAGE, user, max_tokens=16000, temperature=0.2)
    r.setdefault("title", group.get("title"))
    r.setdefault("type", group.get("type"))
    r["_members"] = [n for n, _ in members]
    return r


def compile_session(client, memories: list[dict], log=print) -> dict:
    """短会话单次编译；长会话走「分组 → 逐组编译」。"""
    if len(memories) <= SINGLE_SHOT_MAX:
        log("  单次编译（%d 条 ≤ 阈值 %d）" % (len(memories), SINGLE_SHOT_MAX))
        r = client.complete_json(
            SYSTEM_SINGLE,
            USER_SINGLE.format(n=len(memories), listing=build_listing(memories),
                               page_rules=_PAGE_RULES),
            max_tokens=16000, temperature=0.2)
        return {"session_summary": r.get("session_summary", ""),
                "pages": r.get("pages") or [],
                "unclassified": r.get("unclassified") or []}

    log("  两段式：先分组（%d 条）…" % len(memories))
    plan = plan_groups(client, memories)
    groups = plan.get("groups") or []
    log("  分组结果：%d 组" % len(groups))
    pages = []
    for i, g in enumerate(groups, 1):
        log("   [%d/%d] %s（成员 %s）" % (i, len(groups), g.get("title"),
                                          g.get("members")))
        try:
            pages.append(compile_page(client, memories, g))
        except Exception as e:      # fail-open：单组失败不影响其它组
            log("       失败：%s: %s" % (type(e).__name__, str(e)[:120]))
    return {"session_summary": plan.get("session_summary", ""),
            "pages": [p for p in pages if p],
            "unclassified": plan.get("unclassified") or []}


def render(result: dict, meta: dict) -> str:
    """JSON → markdown。页面标题用 `#`，正文内标题由 LLM 用 `##`，避免层级冲突。"""
    pages = result.get("pages") or []
    L = ["---",
         "title: %s" % meta["title"],
         "source: %s" % meta["source"],
         "conversation_id: %s" % meta["conversation_id"],
         "compiled_at: %s" % time.strftime("%Y-%m-%d %H:%M:%S"),
         "memories_in: %d" % meta["n_in"],
         "pages_out: %d" % len(pages),
         "generator: wiki_compile prototype",
         "---", ""]
    ss = (result.get("session_summary") or "").strip()
    if ss:
        L += ["> " + ss, ""]

    src_map = meta["source_map"]
    for p in pages:
        L.append("# %s" % (p.get("title") or "(无标题)"))
        L.append("")
        L.append("*type: %s*" % (p.get("type") or "?"))
        if p.get("summary"):
            L += ["", "**摘要**：%s" % p["summary"]]
        L += ["", (p.get("body") or "").strip(), ""]
        rel = p.get("related") or []
        if rel:
            L += ["**相关**：" + " · ".join("[[%s]]" % r for r in rel), ""]
        nums = p.get("sources") or p.get("_members") or []
        if nums:
            L += ["**来源**：" + " ".join("[%s]" % src_map.get(n, "?") for n in nums), ""]
        L += ["---", ""]

    unc = result.get("unclassified") or []
    if unc:
        L += ["# 未归类（不成页）", ""]
        for u in unc:
            L.append("- [%s] %s" % (src_map.get(u.get("n"), u.get("n")),
                                    u.get("why", "")))
        L.append("")
    return "\n".join(L)


def _make_client(thinking: str = ""):
    """按 **wiki 段**的 llm 配置建客户端。

    刻意不读 `distillation.llm`：两条链路的推理需求相反（蒸馏要思考、wiki 编译
    不需要），分开配才能让 wiki 不白付推理开销。见 `config.DEFAULT_WIKI`。
    """
    from agentmemhub import config as hub_config
    from agentmemhub.llm import client_from_config
    c = client_from_config(hub_config.config().wiki.get("llm"))
    if thinking:
        c.cfg.thinking = thinking
    return c


def _usage_report(tag: str = "") -> str:
    """格式化累计用量与花费估算（成本可见性 —— 批量跑之前就该知道要花多少）。"""
    from agentmemhub.llm import estimate_cost, usage_snapshot
    u = usage_snapshot()
    cost = estimate_cost(u)
    return ("--- 用量统计%s ---\n"
            "调用次数   : %d\n"
            "输入 tokens: %s（缓存命中 %s / 未命中 %s）\n"
            "输出 tokens: %s（其中思维链 %s，占输出 %.0f%%）\n"
            "估算花费   : ¥%.2f（DeepSeek 高峰价：输入未命中 ¥2/M、命中 ¥0.04/M、输出 ¥8/M；\n"
            "             空闲时段为半价）"
            % (" " + tag if tag else "", u["calls"], f"{u['prompt']:,}",
               f"{u['cached_hit']:,}", f"{u['cached_miss']:,}",
               f"{u['completion']:,}", f"{u['reasoning']:,}",
               (100.0 * u["reasoning"] / u["completion"]) if u["completion"] else 0.0,
               cost))


def _out_stem(source: str, cid: str) -> str:
    return "%s__%s" % (source, re.sub(r"[^\w\-]+", "_", cid)[:60])


def compile_one(db: Path, source: str, cid: str, out_dir: Path,
                write: bool = True, thinking: str = "") -> dict:
    """编译单个会话并落盘。

    每次独立开只读连接（线程安全）；单会话失败只返回 error，不抛出——
    全量跑 200+ 个会话时，一个失败不该中断整批。
    """
    conn = open_ro(db)
    try:
        mems = load_memories(conn, source, cid)
    finally:
        conn.close()
    if not mems:
        return {"source": source, "cid": cid, "n_in": 0, "pages": 0,
                "skipped": True, "seconds": 0.0}

    client = _make_client(thinking)

    t0 = time.time()
    try:
        result = compile_session(client, mems, log=lambda *a, **k: None)
    except Exception as e:
        return {"source": source, "cid": cid, "n_in": len(mems), "pages": 0,
                "error": "%s: %s" % (type(e).__name__, str(e)[:180]),
                "seconds": round(time.time() - t0, 1)}

    if write:
        meta = {"title": "%s / %s" % (source, cid), "source": source,
                "conversation_id": cid, "n_in": len(mems),
                "source_map": {i: ("m%d" % m["id"]) for i, m in enumerate(mems, 1)}}
        stem = _out_stem(source, cid)
        (out_dir / (stem + ".md")).write_text(render(result, meta), encoding="utf-8")
        (out_dir / (stem + ".json")).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"source": source, "cid": cid, "n_in": len(mems),
            "pages": len(result.get("pages") or []),
            "seconds": round(time.time() - t0, 1)}


def compile_all(db: Path, out_dir: Path, *, workers: int = 4, limit: int = 0,
                resume: bool = True, min_n: int = 1,
                thinking: str = "") -> list[dict]:
    """全量编译（并发 + 断点续跑）。

    resume=True 时跳过已有产出的会话 —— 200+ 个会话跑一小时，中途失败必须能续。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    conn = open_ro(db)
    sessions = list_sessions(conn, min_n=min_n, limit=limit or 10 ** 9)
    conn.close()

    todo = []
    skipped = 0
    for s, cid, n, _t in sessions:
        if resume and (out_dir / (_out_stem(s, cid) + ".md")).exists():
            skipped += 1
            continue
        todo.append((s, cid, n))

    print("会话总数 %d，已有产出跳过 %d，本次待编译 %d（并发 %d）"
          % (len(sessions), skipped, len(todo), workers))
    if not todo:
        return []

    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    t0 = time.time()
    _wlog(event="run_start", script="wiki_compile", out=str(out_dir),
          sessions=len(sessions), skipped=skipped, todo=len(todo),
          workers=workers, thinking=(thinking or "(配置默认)"))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(compile_one, db, s, cid, out_dir, True, thinking): (s, cid, n)
                for s, cid, n in todo}
        for i, fut in enumerate(as_completed(futs), 1):
            s, cid, n = futs[fut]
            try:
                r = fut.result()
            except Exception as e:              # 兜底：绝不让单会话炸掉整批
                r = {"source": s, "cid": cid, "n_in": n, "pages": 0,
                     "error": "%s: %s" % (type(e).__name__, str(e)[:180])}
            results.append(r)
            if r.get("error"):
                _wlog(event="session_fail", script="wiki_compile", source=s,
                      cid=cid, memories=r.get("n_in"), error=r["error"])
            elif not r.get("skipped"):
                _wlog(event="session_done", script="wiki_compile", source=s,
                      cid=cid, memories=r.get("n_in"), pages=r.get("pages"),
                      seconds=r.get("seconds"))
            tag = ("失败 " + r["error"][:60]) if r.get("error") else (
                "跳过" if r.get("skipped") else "%d 页 %.0fs"
                % (r.get("pages", 0), r.get("seconds", 0)))
            print("[%3d/%3d] %-9s %-42s %s"
                  % (i, len(todo), s, cid[:42], tag), flush=True)

    ok = [r for r in results if not r.get("error") and not r.get("skipped")]
    bad = [r for r in results if r.get("error")]
    print("\n完成：成功 %d，失败 %d，总耗时 %.0fs（%.1f 分钟）"
          % (len(ok), len(bad), time.time() - t0, (time.time() - t0) / 60))
    print("页面合计：%d 页（输入 %d 条记忆）"
          % (sum(r.get("pages", 0) for r in ok), sum(r.get("n_in", 0) for r in ok)))
    print()
    print(_usage_report("（本次全量）"))
    if bad:
        print("\n失败清单（可重跑，脚本会自动跳过已成功的）：")
        for r in bad:
            print("  %s/%s  %s" % (r["source"], r["cid"][:40], r.get("error", "")[:100]))
    from agentmemhub.llm import usage_snapshot
    _wlog(event="run_end", script="wiki_compile", ok=len(ok), failed=len(bad),
          pages=sum(r.get("pages", 0) for r in ok), skipped=skipped,
          seconds=round(time.time() - t0, 1), usage=usage_snapshot())
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description="LLM Wiki 编译原型（只读验证）")
    ap.add_argument("--list", action="store_true", help="列出候选会话")
    ap.add_argument("--min", type=int, default=0, help="--list 时的最小记忆数")
    ap.add_argument("--source", default="")
    ap.add_argument("--conversation-id", default="", dest="cid")
    ap.add_argument("--out", default="", help="输出目录（默认打印到终端）")
    ap.add_argument("--index-db", default="",
                    help="索引库路径（默认读配置；验证时建议显式指向副本）")
    ap.add_argument("--all", action="store_true", help="全量编译所有会话")
    ap.add_argument("--workers", type=int, default=4, help="并发数（默认 4）")
    ap.add_argument("--limit", type=int, default=0, help="最多编译多少个会话")
    ap.add_argument("--no-resume", action="store_true",
                    help="不跳过已有产出（默认跳过，便于断点续跑）")
    ap.add_argument("--thinking", default="", choices=["", "enabled", "disabled"],
                    help="覆盖思考模式（默认用配置）；disabled 可显著省 token")
    args = ap.parse_args()

    if args.index_db:
        db = Path(args.index_db)
    else:
        from agentmemhub.rag.config import load_settings
        db = Path(load_settings().index_db)
    conn = open_ro(db)

    if args.list:
        print("索引库：%s\n" % db)
        print("%-9s %-44s %5s  %s" % ("source", "conversation_id", "条数", "标题"))
        for s, cid, n, t in list_sessions(conn, min_n=args.min):
            print("%-9s %-44s %5d  %s" % (s, cid[:44], n, (t or "")[:32]))
        conn.close()
        return 0

    if args.all:
        if not args.out:
            print("--all 需要 --out 指定输出目录")
            conn.close()
            return 2
        conn.close()
        compile_all(db, Path(args.out), workers=args.workers,
                    limit=args.limit, resume=not args.no_resume, min_n=args.min,
                    thinking=args.thinking)
        return 0

    if not (args.source and args.cid):
        print("需要 --source 与 --conversation-id（或用 --list 查看候选）")
        return 2

    mems = load_memories(conn, args.source, args.cid)
    conn.close()
    if not mems:
        print("该会话没有有效蒸馏记忆")
        return 1

    print("会话 %s/%s：%d 条记忆" % (args.source, args.cid, len(mems)))

    from agentmemhub.llm import usage_reset
    usage_reset()
    client = _make_client(args.thinking)
    if not client.cfg.complete():
        print("LLM 未配置完整：%s" % client.cfg.missing_hint())
        return 1

    t0 = time.time()
    result = compile_session(client, mems)
    print("总耗时 %.1fs，产出 %d 页" % (time.time() - t0, len(result["pages"])))
    _wlog(event="session_done", script="wiki_compile", source=args.source,
          cid=args.cid, memories=len(mems), pages=len(result["pages"]),
          seconds=round(time.time() - t0, 1))
    print(_usage_report("（本会话）"))

    meta = {"title": "%s / %s" % (args.source, args.cid),
            "source": args.source, "conversation_id": args.cid,
            "n_in": len(mems),
            "source_map": {i: ("m%d" % m["id"]) for i, m in enumerate(mems, 1)}}
    md = render(result, meta)

    if args.out:
        d = Path(args.out)
        d.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^\w\-]+", "_", args.cid)[:60]
        p = d / ("%s__%s.md" % (args.source, safe))
        p.write_text(md, encoding="utf-8")
        (d / ("%s__%s.json" % (args.source, safe))).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print("已写出：%s" % p)
    else:
        print()
        print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
