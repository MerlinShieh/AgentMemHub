#!/usr/bin/env python
"""一次性探针：把历史 MCP 记忆按「项目」分组，产出可人工复核的报告。

## 为什么需要它

`memory_save` 的来源会话/项目字段是 2026-11-12 才加的。在那之前写入的 MCP
记忆既没有 `source_session_id`、也没有 cwd —— **项目归属在数据层面根本不存在**，
无法用规则迁移。所以用 LLM 做一次两阶段归类：

    阶段 1  给「id + 内容前 120 字」→ 让 LLM 归纳候选项目清单（命名统一）
    阶段 2  拿确定的清单分批（默认 40 条/批）归类 → id → 项目 + 理由

## 隔离保证

- **只读生产库**：SQLite 连接用 `mode=ro`，本脚本没有任何写库语句；
- 产物只落 `exports/project_grouping_report/`（已在 `.gitignore` 里）；
- 不触发 wiki 编译、不调蒸馏、不改任何表。

## 怎么用

    uv run python scripts/project_grouping_probe.py                # 全量
    uv run python scripts/project_grouping_probe.py --limit 30     # 小规模试跑
    uv run python scripts/project_grouping_probe.py --resume       # 复用已有中间结果

跑完打开 `exports/project_grouping_report/report.html` 复核；不满意就改
`assignments.json` 再重跑 `--render-only`（不重新花钱调 LLM）。
"""
from __future__ import annotations

import argparse
import html
import json
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

OUT_DIR = Path("exports") / "project_grouping_report"
PROJECTS_JSON = OUT_DIR / "projects.json"       # 阶段 1 产物
ASSIGNMENTS_JSON = OUT_DIR / "assignments.json"  # 阶段 2 产物
REPORT_HTML = OUT_DIR / "report.html"

#: 阶段 1 每条记忆喂给 LLM 的内容长度（够看出项目，又不至于烧 token）
STAGE1_HEAD = 120

#: 阶段 2 每批条数（40 条 × 747 字符 ≈ 30K 字符，单批请求可控）
STAGE2_BATCH = 40


# ══════════════════════════════════════════════════════════════════════
# 数据（只读）
# ══════════════════════════════════════════════════════════════════════

def load_memories(limit: int = 0) -> list[dict]:
    """读出发源会话为空、需要分组的 MCP 记忆。**只读**。"""
    from agentmemhub.rag.config import load_settings

    db = Path(load_settings().index_db)
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        sql = (
            "SELECT id, conversation_id, content, created_at, type"
            " FROM distilled_memories"
            " WHERE source='mcp' AND (source_session_id IS NULL"
            "       OR source_session_id='')"
            " ORDER BY id")
        rows = [dict(r) for r in conn.execute(sql)]
    finally:
        conn.close()
    if limit:
        rows = rows[:limit]
    return rows


# ══════════════════════════════════════════════════════════════════════
# LLM
# ══════════════════════════════════════════════════════════════════════

def make_client():
    """构造 LLM 客户端（复用 wiki L2 的配置——同一把钥匙，不另开配置键）。"""
    from agentmemhub import config as hub_config
    from agentmemhub.llm import client_from_config

    return client_from_config(hub_config.config().wiki_l2.get("llm"))


SYS_STAGE1 = """你是知识库架构师。下面是一位开发者用 AI 编码助手长期积累的记忆条目摘要（编号 + 内容开头）。

任务：归纳出这些记忆覆盖的**项目**清单。

判定口径（重要）：
- 一个「项目」= 一个独立的工程：代码仓库、产品、工具链、技能库等；
- 判断依据是内容里出现的仓库名、绝对路径、组件名、发布版本号、技术栈；
- **不要按技术主题分组**（"记忆蒸馏""召回调优"这类是主题域，不是项目）；
- 实在无法归类的记忆，统一归入兜底项目 `未分类`。

输出严格 JSON（不要 markdown 代码块）：
{"projects":[{"name":"项目短名","key":"--编码--","why":"一句话说明覆盖什么","examples":[编号,...]}]}

其中 `key` 用 DSH 的项目目录编码规则（把路径里的 / \\ : 合并成一个 -，其它字符
原样，前后加 --）。例如 D:\\data\\vibeCoding\\Agent_Memory\\AgentMemHub →
`--D-data-vibeCoding-Agent_Memory-AgentMemHub--`。若无法确定路径，key 写空串。"""

SYS_STAGE2 = """你在给记忆条目做项目归类。

下面是**候选项目清单**（名称 + 说明）。请为每条记忆选择**最匹配的一个**项目。

规则：
- 只输出清单里已有的项目名，不要发明新项目；
- 判断依据是内容里的仓库名、路径、组件名、版本号、技术栈；
- 无法判断时选 `未分类`，并把 confidence 设为 low；
- confidence：high=内容明确提到该项目；medium=有较强线索；low=只能靠推测。

输出严格 JSON（不要 markdown 代码块）：
{"assignments":[{"id":4348,"project":"项目短名","confidence":"high|medium|low","reason":"不超过 30 字的依据"}]}"""


def stage1_projects(client, mems: list[dict]) -> list[dict]:
    """阶段 1：归纳候选项目清单。"""
    lines = [f"[{m['id']}] {(m['content'] or '')[:STAGE1_HEAD]}".replace("\n", " ")
             for m in mems]
    user = "共 %d 条记忆：\n\n%s" % (len(mems), "\n".join(lines))
    print(f"[阶段1] 提交 {len(mems)} 条摘要，约 {sum(len(x) for x in lines):,} 字符…")
    t0 = time.time()
    r = client.complete_json(SYS_STAGE1, user, max_tokens=8000)
    projects = r.get("projects") or []
    print(f"[阶段1] 完成，耗时 {time.time()-t0:.1f}s，得到 {len(projects)} 个项目")
    for p in projects:
        print(f"    - {p.get('name')}（{p.get('key') or '无编码'}）")
    return projects


def stage2_assign(client, mems: list[dict], projects: list[dict]) -> list[dict]:
    """阶段 2：分批归类，返回 assignments 列表。"""
    catalog = "\n".join(
        "%d. %s —— %s" % (i + 1, p.get("name", "?"), p.get("why", ""))
        for i, p in enumerate(projects))
    out: list[dict] = []
    total = (len(mems) + STAGE2_BATCH - 1) // STAGE2_BATCH
    for bi in range(total):
        batch = mems[bi * STAGE2_BATCH:(bi + 1) * STAGE2_BATCH]
        body = "\n\n".join(
            "[%d] %s" % (m["id"], (m["content"] or "")[:900]) for m in batch)
        user = "候选项目：\n%s\n\n待归类记忆：\n%s" % (catalog, body)
        print(f"[阶段2] 第 {bi+1}/{total} 批，{len(batch)} 条…")
        t0 = time.time()
        try:
            r = client.complete_json(SYS_STAGE2, user, max_tokens=8000)
            got = r.get("assignments") or []
        except Exception as e:                       # noqa: BLE001
            print(f"    批失败：{e}（该批留空，稍后可单独重跑）")
            got = []
        print(f"    完成 {len(got)} 条，耗时 {time.time()-t0:.1f}s")
        out.extend(got)
        # 每批落盘一次：长任务断在中途不用从头再花钱
        ASSIGNMENTS_JSON.write_text(
            json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


# ══════════════════════════════════════════════════════════════════════
# 报告
# ══════════════════════════════════════════════════════════════════════

_PROJECT_HUES = [258, 210, 160, 20, 340, 280, 45, 190, 100, 320, 0, 130]


def _hue(i: int) -> int:
    return _PROJECT_HUES[i % len(_PROJECT_HUES)]


def render_html(mems: list[dict], projects: list[dict],
                assigns: list[dict]) -> str:
    """生成自包含 HTML 报告（无外部依赖）。"""
    pmap = {p.get("name"): p for p in projects}
    amap = {int(a.get("id", 0)): a for a in assigns if str(a.get("id", "")).isdigit()}
    order = [p.get("name", "?") for p in projects]
    counts = Counter(a.get("project") or "未分类" for a in assigns)
    # 漏掉没被分配的
    unassigned = [m for m in mems if m["id"] not in amap]

    def esc(s) -> str:
        return html.escape(str(s or ""))

    rows = []
    for m in mems:
        a = amap.get(m["id"]) or {}
        proj = a.get("project") or "（未分配）"
        conf = a.get("confidence") or ""
        idx = order.index(proj) if proj in order else 99
        rows.append((idx, m["id"], proj, conf, a.get("reason") or "",
                     (m["content"] or "").replace("\n", " ")))

    cards = []
    for i, p in enumerate(projects):
        name = p.get("name", "?")
        n = counts.get(name, 0)
        cards.append(
            f'<div class="card"><div class="pname" style="color:hsl({_hue(i)} 65% 42%)">'
            f'{esc(name)}</div><div class="pnum">{n}</div>'
            f'<div class="pkey">{esc(p.get("key") or "（未给编码）")}</div>'
            f'<div class="pwhy">{esc(p.get("why"))}</div></div>')
    if unassigned:
        cards.append(f'<div class="card"><div class="pname">（未分配）</div>'
                     f'<div class="pnum">{len(unassigned)}</div>'
                     f'<div class="pwhy">LLM 未返回该条结果</div></div>')

    trs = []
    for idx, mid, proj, conf, reason, content in rows:
        c = {"high": "hi", "medium": "mid", "low": "lo"}.get(conf, "")
        trs.append(
            f'<tr data-p="{esc(proj)}" data-c="{conf}">'
            f'<td class="mono">{mid}</td>'
            f'<td><span class="tag" style="--h:{_hue(idx)}">{esc(proj)}</span></td>'
            f'<td class="conf {c}">{esc(conf) or "—"}</td>'
            f'<td class="reason">{esc(reason)}</td>'
            f'<td class="content" title="{esc(content[:300])}">{esc(content[:110])}</td>'
            f'</tr>')

    opts = "".join(f'<option value="{esc(n)}">{esc(n)}（{counts.get(n,0)}）</option>'
                   for n in order)

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>MCP 记忆项目分组 · 探针报告</title>
<style>
:root{{--bg:#fbfbfd;--fg:#1c1c1e;--mut:#6b7280;--bd:#e5e7eb}}
*{{box-sizing:border-box}}
body{{margin:0;padding:28px 32px;background:var(--bg);color:var(--fg);
  font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}}
h1{{font-size:20px;margin:0 0 4px}}
.sub{{color:var(--mut);font-size:12.5px;margin-bottom:20px}}
.cards{{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:22px}}
.card{{background:#fff;border:1px solid var(--bd);border-radius:10px;
  padding:12px 14px;min-width:190px;max-width:260px;box-shadow:0 1px 2px #0000000a}}
.pname{{font-weight:600;font-size:13px}}
.pnum{{font-size:26px;font-weight:700;line-height:1.2;margin:2px 0}}
.pkey{{font:11px/1.4 ui-monospace,Consolas,monospace;color:var(--mut);
  word-break:break-all}}
.pwhy{{font-size:11.5px;color:var(--mut);margin-top:6px}}
.bar{{display:flex;gap:10px;align-items:center;margin-bottom:12px;flex-wrap:wrap}}
input,select{{padding:6px 10px;border:1px solid var(--bd);border-radius:7px;
  background:#fff;font:inherit;font-size:13px}}
input{{width:260px}}
table{{width:100%;border-collapse:collapse;background:#fff;
  border:1px solid var(--bd);border-radius:10px;overflow:hidden}}
th{{text-align:left;font-size:11.5px;text-transform:uppercase;letter-spacing:.04em;
  color:var(--mut);background:#f6f7f9;padding:9px 12px;border-bottom:1px solid var(--bd)}}
td{{padding:8px 12px;border-bottom:1px solid #f0f1f3;vertical-align:top}}
tr:last-child td{{border-bottom:none}}
.mono{{font-family:ui-monospace,Consolas,monospace;font-size:12px;color:var(--mut)}}
.tag{{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11.5px;
  font-weight:600;white-space:nowrap;
  background:hsl(var(--h) 70% 95%);color:hsl(var(--h) 60% 34%)}}
.conf{{font-size:11.5px;font-weight:600;white-space:nowrap}}
.conf.hi{{color:#059669}}.conf.mid{{color:#d97706}}.conf.lo{{color:#9ca3af}}
.reason{{font-size:12px;color:#4b5563;max-width:230px}}
.content{{font-size:12px;color:#374151;max-width:520px}}
.foot{{margin-top:16px;color:var(--mut);font-size:11.5px}}
</style></head><body>
<h1>MCP 记忆项目分组 · 探针报告</h1>
<div class="sub">共 <b>{len(mems)}</b> 条待分组记忆 · 归纳出 <b>{len(projects)}</b> 个项目 ·
只读生产库，未写入任何数据</div>
<div class="cards">{''.join(cards)}</div>
<div class="bar">
  <input id="q" placeholder="搜索内容 / 项目 / 理由…">
  <select id="pf"><option value="">全部项目</option>{opts}</select>
  <select id="cf"><option value="">全部置信度</option>
    <option value="high">high</option><option value="medium">medium</option>
    <option value="low">low</option></select>
  <span class="mono" id="cnt"></span>
</div>
<table><thead><tr>
  <th style="width:70px">id</th><th style="width:190px">项目</th>
  <th style="width:90px">置信度</th><th>判断依据</th><th>内容摘要</th>
</tr></thead><tbody>{''.join(trs)}</tbody></table>
<div class="foot">提示：这是 LLM 的一次性判断，不是事实。复核后如需迁移，
请先 <code>snapshot</code> 备份，再人工确认映射。</div>
<script>
const rows=[...document.querySelectorAll('tbody tr')];
const q=document.getElementById('q'),pf=document.getElementById('pf'),
      cf=document.getElementById('cf'),cnt=document.getElementById('cnt');
function apply(){{
  const kw=q.value.trim().toLowerCase(),p=pf.value,c=cf.value;let n=0;
  rows.forEach(r=>{{
    const okKw=!kw||r.textContent.toLowerCase().includes(kw);
    const okP=!p||r.dataset.p===p, okC=!c||r.dataset.c===c;
    const show=okKw&&okP&&okC; r.style.display=show?'':'none'; if(show)n++;
  }});
  cnt.textContent=`显示 ${{n}} / ${{rows.length}} 条`;
}}
q.oninput=apply;pf.onchange=apply;cf.onchange=apply;apply();
</script></body></html>"""


# ══════════════════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════════════════

def main() -> int:
    ap = argparse.ArgumentParser(description="历史 MCP 记忆的项目分组探针（只读）")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 条（试跑用）")
    ap.add_argument("--resume", action="store_true", help="复用已有中间结果")
    ap.add_argument("--render-only", action="store_true",
                    help="只重新渲染 HTML（不调 LLM，适合手工改完 assignments.json 后看）")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    mems = load_memories(args.limit)
    if not mems:
        print("没有需要分组的记忆（全部已有来源会话）")
        return 0
    print(f"待分组记忆：{len(mems)} 条，总 {sum(len(m['content'] or '') for m in mems):,} 字符")

    if args.render_only:
        projects = json.loads(PROJECTS_JSON.read_text(encoding="utf-8"))
        assigns = json.loads(ASSIGNMENTS_JSON.read_text(encoding="utf-8"))
    else:
        resumed = args.resume and PROJECTS_JSON.exists() and ASSIGNMENTS_JSON.exists()
        if resumed:
            projects = json.loads(PROJECTS_JSON.read_text(encoding="utf-8"))
            assigns = json.loads(ASSIGNMENTS_JSON.read_text(encoding="utf-8"))
            print(f"复用中间结果：{len(projects)} 个项目 / {len(assigns)} 条分配")
        else:
            client = make_client()
            projects = stage1_projects(client, mems)
            PROJECTS_JSON.write_text(
                json.dumps(projects, ensure_ascii=False, indent=1), encoding="utf-8")
            assigns = stage2_assign(client, mems, projects)

    REPORT_HTML.write_text(render_html(mems, projects, assigns), encoding="utf-8")

    # 终端汇总
    print("\n=== 分组结果 ===")
    c = Counter(a.get("project") or "未分类" for a in assigns)
    for name, n in c.most_common():
        print(f"  {name:<24} {n:>4} 条")
    miss = len(mems) - len(assigns)
    if miss:
        print(f"  ⚠️ 未拿到结果的条目：{miss} 条")
    print(f"\n报告：{REPORT_HTML.resolve()}")
    print(f"中间结果：{PROJECTS_JSON.resolve()} / {ASSIGNMENTS_JSON.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
