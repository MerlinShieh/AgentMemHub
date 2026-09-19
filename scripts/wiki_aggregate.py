"""LLM Wiki 第二级：跨会话聚合（第一级的会话内页面 → 主题实体页）。

背景与定位
==========
第一级（`wiki_compile.py`）把每个会话的零散记忆编译成**会话内页面**：实测
224 个会话 → 217 份 md / 873 页，但压缩比只有 1.67:1。原因是大量页面其实是
"同一主题在不同会话里的碎片"（`AgentMemHub` 一个实体就散在 21 个会话、
40 个不同标题里）。第二级按**主题**把它们重新编译成实体页，这才是真正的收敛点。

第一级产出的特性（决定了本脚本的输入形态）
==========================================
· 正文里的 `[n]` 是**会话内序号**，`**来源**：[m7]` 是**全局记忆 id**，两者是
  同一映射的两面；而 md 只渲染了后者。所以重建映射必须**同时读同名 json 的
  `pages[].sources`**（原始序号数组，与 md 的 `**来源**` 同序、同长）。
· 重建后把正文的 `[n]` **提升为 `[m<id>]`**：全局唯一（聚合后不冲突），且直接
  可追溯回 `distilled_memories`。这顺带修好了第一级"只写裸 id、读者无法追溯"的缺陷。
· 第一级的 `[[相关]]` 死链率 15.5%（措辞不一致），所以**第二级不信任它**，
  只在提示词里作为"可能相关的主题名"参考，最终链接由第二级自己重写。

三个阶段（可单独跑，便于小规模验证）
====================================
  ① domains：873 个标题 → M 个主题域（一次调用，按域列成员，脚本校验覆盖完整性）
  ② plan   ：逐域把域内页面细分成"最终页"（域内页数 ≤ min_pages 时跳过，直接成一页）
  ③ compile：逐最终页把源页面正文重新编译成一页（真·蒸馏，不是拼接）

只读第一级产出，不碰数据库、不写任何表。

用法：
    python scripts/wiki_aggregate.py --src DIR --out DIR --stage domains
    python scripts/wiki_aggregate.py --src DIR --out DIR --stage all --workers 6
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from wiki_compile import _usage_report  # noqa: E402  （复用用量/成本统计）
from agentmemhub.failures import FailureLog  # noqa: E402

# Windows 控制台默认 GBK，用量报告里的 ¥ 会抛 UnicodeEncodeError。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ---------------------------------------------------------------------------
# 解析第一级产出
# ---------------------------------------------------------------------------

SRC_RE = re.compile(r"\[m(\d+)\]")
LINK_RE = re.compile(r"\[\[([^\[\]]+)\]\]")
BODY_REF = re.compile(r"(?<!\[)\[(\d+)\](?!\()")
SUM_RE = re.compile(r"^\*\*摘要\*\*：(.+)$", re.M)
SRC_LINE = re.compile(r"^\*\*来源\*\*：(.+)$", re.M)
REL_LINE = re.compile(r"^\*\*相关\*\*：(.+)$", re.M)
#: 页面头的唯一可靠特征：`# 标题` 之后紧跟 `*type: xxx*`。
#: **不能**用 `\n(?=# )` 分页 —— 正文代码块里的 `# 注释` 会被误判成页面边界，
#: 实测会让 47/217 个文件的页数与 json 对不上（最大差 5 页）。
PAGE_HEAD = re.compile(r"^# (.+)\n\n\*type: (.+?)\*$", re.M)
#: `**来源**` 行里的每个方括号 token（含越界时渲染出的 `?`，必须保留占位才能对齐）
SRC_TOKEN = re.compile(r"\[([^\]]*)\]")


def _front(text: str) -> dict:
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    out = {}
    for line in parts[1].splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def _promote_refs(body: str, smap: dict[int, str]) -> str:
    """把正文里的会话内序号 [n] 提升为全局 [m<id>]。

    smap 来自"json 的 sources 数组"与"md 的 **来源** 列表"按位置对齐 ——
    二者由 wiki_compile.render 用同一个 nums 顺序写出，顺序天然一致。
    """
    def rep(m):
        n = int(m.group(1))
        return "[%s]" % smap.get(n, "m?%d" % n)
    return BODY_REF.sub(rep, body)


def build_seq_map(db: Path) -> dict:
    """{（source, conversation_id）: {会话内序号: m-id}}。

    **这是权威映射**：第一级的 `src_map` 就是按 `created_at, id` 排序的会话内序号，
    与这里逐字一致。不能只靠 json 的 `sources` 重建 —— 实测 LLM 会在正文里引用
    `sources` 数组**没列全**的编号（250/5552 处），只靠产物会留下无法追溯的引用。
    """
    out: dict = {}
    conn = sqlite3.connect("file:%s?mode=ro" % Path(db).as_posix(), uri=True)
    try:
        for src, cid, mid in conn.execute(
                "SELECT source, conversation_id, id FROM distilled_memories"
                " WHERE status IN ('new','similar') ORDER BY source, conversation_id,"
                " created_at, id"):
            d = out.setdefault((src, cid), {})
            d[len(d) + 1] = "m%d" % mid
    finally:
        conn.close()
    return out


def load_pages(src_dir: Path, seq_map: dict | None = None) -> list[dict]:
    """读第一级产出目录，返回带全局溯源编号的页面列表。

    页面边界用 `PAGE_HEAD`（`# 标题` + `*type:*`）识别，**不是** `\\n# ` 切分：
    正文代码块里的 `# 注释` 会被后者误判成页面边界，实测 47/217 个文件因此错位。
    """
    pages: list[dict] = []
    n_files = n_mismatch = n_badref = 0
    for mf in sorted(Path(src_dir).glob("*.md")):
        jf = mf.with_suffix(".json")
        if not jf.exists():
            continue
        try:
            data = json.loads(jf.read_text(encoding="utf-8"))
        except Exception:
            continue
        jpages = data.get("pages") or []
        text = mf.read_text(encoding="utf-8")
        meta = _front(text)
        heads = list(PAGE_HEAD.finditer(text))
        n_files += 1
        if len(heads) != len(jpages):
            n_mismatch += 1
            print("  [警告] %s：md 识别 %d 页 != json %d 页"
                  % (mf.name, len(heads), len(jpages)))
        for i, h in enumerate(heads):
            end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
            block = text[h.start():end]
            jp = jpages[i] if i < len(jpages) else {}

            # 重建 会话内序号 -> 全局 m-id 映射：
            # json 的 sources 数组与 md 的 **来源** token 由同一个 nums 顺序写出，
            # 按位置对齐即可（越界项渲染成 `?`，占位保留才能对齐）。
            nums = [int(x) for x in (jp.get("sources") or [])
                    if str(x).lstrip("-").isdigit()]
            sl = SRC_LINE.search(block)
            tokens = SRC_TOKEN.findall(sl.group(1)) if sl else []
            # 权威映射来自数据库（会话内序号 → m-id）；产物内的对齐只作兜底，
            # 因为第一级写进 json 的 sources 数组可能没列全正文用到的编号。
            smap = dict((seq_map or {}).get(
                (meta.get("source", ""), meta.get("conversation_id", "")), {}))
            for n, tok in zip(nums, tokens):
                if tok[:1] == "m" and tok[1:].isdigit():
                    smap.setdefault(n, tok)
            if not smap:
                n_badref += 1

            body = re.sub(r"^\*\*来源\*\*：.*$", "", block, flags=re.M)
            body = re.sub(r"^\*\*相关\*\*：.*$", "", body, flags=re.M)
            body = re.sub(r"^# .+\n\n\*type: .+?\*\n", "", body, count=1, flags=re.M)
            pages.append({
                "file": mf.name,
                "source": meta.get("source", ""),
                "cid": meta.get("conversation_id", ""),
                "title": (jp.get("title") or h.group(1)).strip(),
                "type": jp.get("type") or h.group(2),
                "summary": (jp.get("summary") or "").strip(),
                "body": _promote_refs(body, smap),
                "mids": [t for t in tokens if t[:1] == "m" and t[1:].isdigit()],
                "related": [x.strip() for x in LINK_RE.findall(
                    REL_LINE.search(block).group(1))] if REL_LINE.search(block) else [],
            })
    n_lost = sum(len(re.findall(r"\[m\?\d+\]", p["body"])) for p in pages)
    print("  解析：%d 个文件 / %d 页，页数不匹配 %d，无可用映射 %d 页，未映射引用 %d 处"
          % (n_files, len(pages), n_mismatch, n_badref, n_lost))
    return pages


# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------

SYSTEM_DOMAIN = """你是知识库架构师。下面是一个个人开发知识库的**全部页面标题**（编号 + 来源项目）。

你的任务：归纳出这个知识库的**主题域**（domain）清单。一个域 = 一个大主题，
通常对应「一个项目 / 一套技术栈 / 一类方法论 / 一个持续关注的问题」。

**这一轮只需要给出域名，不要分配具体页面** —— 页面分配在下一步分批做。"""

USER_DOMAIN = """知识库共 {n} 个页面标题：

{listing}

请返回 JSON：
{{
  "domains": [{{"name": "域名称", "why": "覆盖什么"}}]
}}

要求：
1. 域的数量控制在 {dmin}–{dmax} 之间。宁可少而准，不要拆得过碎。
2. **`why` 必须控制在 20 字以内**（它只用于后续归类时判断边界，不需要展开）。
3. 域名要具体可辨认（如「DSH / Harness 架构」「记忆引擎与蒸馏」「Windows 桌面控制」），
   不要「其他」「杂项」这类兜底名——若某些页无处可放，为它们建立一个描述真实共同点的域。
4. 同一项目的不同侧面（架构 / 配置 / 踩坑）应属于同一个域，不要按页面类型拆。
5. 覆盖要全：清单里出现的项目名、技术栈、工具都应该有归属。
6. **每个域只出现一次**，不要重复。"""

SYSTEM_ASSIGN = """你是知识库架构师。下面给出一个**已确定的主题域清单**（编号 + 名称），
以及一批页面标题。

你的任务：把**每一个页面**归入最合适的一个域。"""

USER_ASSIGN = """主题域清单：
{table}

本批共 {n} 个页面：
{listing}

请返回 JSON：
{{"assign": {{"1": 域编号, "2": 域编号, ...}}}}

其中键是**页面编号**，值是上面域清单里的**域编号**（数字，不是域名）。
每个页面都必须有归属，不能遗漏。"""

SYSTEM_PLAN = """你是知识库编辑。下面是一个**主题域**内的若干页面（编号 + 标题 + 摘要）。

你的任务：把它们划分成若干**最终页面**——同一件事的多个片段合成一页，不同的事分开。
第一级是按会话切的，所以同一主题常常散在好几页里，你的工作就是让它们合回去。"""

USER_PLAN = """主题域「{domain}」下共 {n} 个页面：

{listing}

请返回 JSON：
{{
  "pages": [
    {{"title": "最终页面标题", "members": [1, 4, 7], "why": "合并理由（一句话）"}}
  ]
}}

规则：
1. **每个编号恰好归入一个最终页**，不能遗漏、不能重复。
2. 合并的判据是「讲的是不是同一件事」：
   - 同一实体的不同侧面（架构 / 配置 / 踩坑）→ **合并成一页**
   - 不同实体、不同机制、不同决策 → **分开**
3. 单个页面若已自成一个完整主题，可以独占一页。
4. 最终页数量通常显著少于输入页数；若某几页内容高度重叠，必须合并。
5. 标题要具体，能一眼看出讲什么；合并后的标题可以比原标题更上位。"""

#: 单个最终页允许喂进去的源正文总量（字符）。超出就按序截断 ——
#: 一个域里可能有十几个页面被并成一页，不设上限会直接把上下文撑爆
#: （第一级实测过：45 条记忆一次性编译时思维链吃满 max_tokens、JSON 被截断）。
MAX_GROUP_CHARS = 60000

SYSTEM_COMPILE = """你是知识库编辑。下面同一主题下的若干页面来自**不同开发会话**，是同一件事在不同时间、不同场合留下的记录。

请把它们**重新编译成一个页面**：不是拼接，而是理解全部内容后用自己的话重写，
消除重复、统一术语、按时序理顺、保留所有有价值的结论与细节。

特别注意：
- 不同会话的记录可能**互相矛盾**或**新旧取代**，要按下面的规则处理。
- 正文里的 `[m123]` 是来源记忆的全局编号，**必须原样保留并正确使用**。"""

_PAGE_RULES = """规则：
1. **综合而非拼接**：重组结构，合并重复表述，剔除过程性叙述。读起来要像一篇完整条目。
2. **正文里不要再写页面标题**：`body` 直接从内容开始，节标题用 `##` / `###`。
3. **保留溯源**：正文里每个论断后标注来源编号 `[m123]`（可多个，如 `[m12][m45]`）。
   **编号必须来自上面给出的页面正文**，不允许自己发明编号。
4. **标注矛盾**：不同会话的记录互相冲突时，用 `> ⚠️ 矛盾：` 引用块写明双方内容与编号。
5. **时效择优**：若后来的记录取代了早期结论，只写新结论，可注明"早期曾用 X（[m12]）"。
6. **信息取舍**：具体行号、临时路径、一次性操作可省略；有复用价值的结论、根因、版本号、
   文件路径、参数值必须保留。
7. 正文用中文，markdown 格式（可用小标题、列表、表格、代码块）。专有名词保留原文。
8. `related` 填**本文档中真实存在的其他页面标题**（见下方"已有页面标题"清单），
   不要发明标题；没有合适的就留空数组。"""


# ---------------------------------------------------------------------------
# LLM 客户端（第二级配置：顶层 llm + wiki.l2.llm）
# ---------------------------------------------------------------------------

def make_client(thinking: str = ""):
    from agentmemhub import config as hub_config
    from agentmemhub.llm import client_from_config
    c = client_from_config(hub_config.config().wiki_l2.get("llm"))
    if thinking:
        c.cfg.thinking = thinking
    return c


def _wlog(**kw) -> None:
    """写一条 wiki 编译事件到 `logs/wiki.log`（JSONL）。

    旁路：写失败绝不影响编译。长任务必须能从日志回答四件事——跑到哪了、
    哪些失败了、花了多少、上次断在哪；控制台输出一关就什么都没了。
    """
    try:
        from agentmemhub.logs import audit_wiki
        kw.setdefault("ts", time.time())
        audit_wiki(kw)
    except Exception:
        pass


def _usage_delta(before: dict, after: dict) -> dict:
    return {k: after.get(k, 0) - before.get(k, 0) for k in after}


def call_json(client, system: str, user: str, max_tokens: int,
              tries: int = 3, tag: str = ""):
    """带脚本级重试的 JSON 调用，并把每次调用落进 `logs/wiki.log`。

    client 内部的重试只覆盖 HTTP 层瞬态错误；实测 provider 会**偶发返回空 content**
    （`ValueError: 模型返回空内容`），它不算瞬态错误、不会触发内部重试，却会让
    整批归域白跑。这层把它兜住——重试通常立刻就好。
    """
    from agentmemhub.llm import usage_snapshot
    last = None
    for i in range(tries):
        before = usage_snapshot()
        t0 = time.time()
        try:
            r = client.complete_json(system, user, max_tokens=max_tokens,
                                     temperature=0.2)
            _wlog(event="call", tag=tag, ok=True, attempt=i + 1,
                  seconds=round(time.time() - t0, 1),
                  usage=_usage_delta(before, usage_snapshot()))
            return r
        except Exception as e:
            last = e
            print("     %s调用失败（第 %d/%d 次）：%s"
                  % (tag, i + 1, tries, str(e)[:90]), flush=True)
            _wlog(event="call", tag=tag, ok=False, attempt=i + 1,
                  seconds=round(time.time() - t0, 1),
                  error="%s: %s" % (type(e).__name__, str(e)[:200]),
                  usage=_usage_delta(before, usage_snapshot()))
            if i + 1 < tries:
                time.sleep(2.0 * (i + 1))
    raise last


# ---------------------------------------------------------------------------
# 阶段 ①：归域
# ---------------------------------------------------------------------------

def stage_domains(client, pages: list[dict], dmin: int, dmax: int,
                  batch: int = 120,
                  cache_file: Path | None = None) -> list[dict]:
    """归域：两段式 + 分批 + 断点续。

    一次调用处理 870 个标题并要求当场分组会超时（默认 60s 超时下必然失败）；
    拆开后每次调用要么"输入大、输出小"、要么"输入小、输出小"。

    批次还要**小到 120 左右**：这类重推理模型即使 `reasoning_effort=low`，
    思维链仍占输出的九成以上（实测 94%），任务一大就会把 `max_tokens` 全烧在
    思考上、返回空 content —— 实测 200 页/批时第 2 批连续 3 次失败，整轮归域作废。

    每批结果即时落盘：跑 5 批时最后一批失败，不该让前 4 批白跑。
    """
    cache: dict = {}
    if cache_file and Path(cache_file).exists():
        try:
            cache = json.loads(Path(cache_file).read_text(encoding="utf-8"))
        except Exception:
            cache = {}

    def save():
        if cache_file:
            Path(cache_file).write_text(json.dumps(cache, ensure_ascii=False),
                                        encoding="utf-8")

    if cache.get("table"):
        table = cache["table"]
        print("  ① 域表：复用缓存（%d 个域）" % len(table), flush=True)
    else:
        listing = "\n".join("[%d] (%s) %s" % (i, p["source"], p["title"])
                            for i, p in enumerate(pages, 1))
        print("  ① 归纳域表（%d 个标题）…" % len(pages), flush=True)
        r = call_json(client, SYSTEM_DOMAIN,
                      USER_DOMAIN.format(n=len(pages), listing=listing,
                                         dmin=dmin, dmax=dmax),
                      max_tokens=16000, tag="域表 ")
        seen_name, table = set(), []
        for d in (r.get("domains") or []):
            nm = (d.get("name") or "").strip()
            if nm and nm not in seen_name:   # 去重：模型偶尔会重复列出同一个域
                seen_name.add(nm)
                table.append({"name": nm, "why": (d.get("why") or "").strip()})
        cache["table"] = table
        save()
    print("     域表：%d 个域" % len(table), flush=True)
    if not table:
        return []
    tl = "\n".join("[%d] %s —— %s" % (i, d["name"], d.get("why") or "")
                   for i, d in enumerate(table, 1))

    by_domain: dict[int, list[int]] = {}
    n_batches = (len(pages) + batch - 1) // batch
    for bi in range(n_batches):
        key = "assign_%d" % bi
        if key in cache:
            amap = {int(k): int(v) for k, v in cache[key].items()}
            print("  ② 归类 [%d/%d]：复用缓存" % (bi + 1, n_batches), flush=True)
        else:
            chunk = pages[bi * batch:(bi + 1) * batch]
            base = bi * batch
            lines = "\n".join("[%d] (%s) %s" % (base + i + 1, p["source"], p["title"])
                              for i, p in enumerate(chunk))
            print("  ② 归类 [%d/%d]（%d 页）…" % (bi + 1, n_batches, len(chunk)),
                  flush=True)
            rr = call_json(client, SYSTEM_ASSIGN,
                           USER_ASSIGN.format(n=len(chunk), listing=lines, table=tl),
                           max_tokens=24000,
                           tag="归类[%d/%d] " % (bi + 1, n_batches))
            amap = {}
            for k, v in (rr.get("assign") or {}).items():
                try:
                    amap[int(k)] = int(v)
                except (TypeError, ValueError):
                    continue
            cache[key] = {str(k): v for k, v in amap.items()}
            save()
        for k, v in amap.items():
            if 1 <= v <= len(table) and 1 <= k <= len(pages):
                by_domain.setdefault(v, []).append(k)

    covered = {x for v in by_domain.values() for x in v}
    miss = [i for i in range(1, len(pages) + 1) if i not in covered]
    domains = [{"name": table[v - 1]["name"], "why": table[v - 1].get("why") or "",
                "members": sorted(by_domain.get(v, []))}
               for v in sorted(by_domain) if by_domain.get(v)]
    if miss:
        print("  未归属 %d 页 → 归入『待整理』" % len(miss))
        domains.append({"name": "待整理", "why": "归类时遗漏的页面", "members": miss})
    print("  归域完成：%d 个域，覆盖 %d/%d 页（未归属 %d）"
          % (len(domains), len(covered), len(pages), len(miss)))
    return domains


# ---------------------------------------------------------------------------
# 阶段 ②：域内细分
# ---------------------------------------------------------------------------

def stage_plan(client, domain: dict, members: list[dict]) -> list[dict]:
    listing = "\n".join("[%d] %s :: %s" % (i, p["title"], p["summary"][:120])
                        for i, p in enumerate(members, 1))
    user = USER_PLAN.format(domain=domain.get("name", ""), n=len(members),
                            listing=listing)
    r = call_json(client, SYSTEM_PLAN, user, max_tokens=16000, tag="细分 ")
    plans = r.get("pages") or []
    seen, out = set(), []
    for pl in plans:
        idx = [int(x) for x in (pl.get("members") or [])
               if str(x).isdigit() and 1 <= int(x) <= len(members) and int(x) not in seen]
        if not idx:
            continue
        seen.update(idx)
        out.append({"title": pl.get("title") or "(无标题)", "idx": idx})
    left = [i for i in range(1, len(members) + 1) if i not in seen]
    for i in left:                      # 细分遗漏的页面各自成页，绝不丢内容
        out.append({"title": members[i - 1]["title"], "idx": [i]})
    return out


# ---------------------------------------------------------------------------
# 阶段 ③：编译最终页
# ---------------------------------------------------------------------------

def stage_compile(client, title_hint: str, group: list[dict],
                  all_titles: list[str]) -> dict:
    budget = MAX_GROUP_CHARS
    parts, dropped = [], 0
    for p in group:
        body = p["body"].strip()
        if len(body) > budget:
            body = body[:max(budget, 0)] + "\n\n…（本页过长，已截断）"
            dropped += 1
        budget -= len(body)
        parts.append("### 来源页面：%s\n（来自 %s / %s）\n\n%s"
                     % (p["title"], p["source"], p["cid"][:40], body))
        if budget <= 0:
            dropped += len(group) - len(parts)
            break
    listing = "\n".join("- %s" % t for t in all_titles[:400])
    user = ("目标页面标题（可沿用，也可按内容修正）：%s\n\n"
            "以下是 %d 个来源页面：\n\n%s\n\n"
            "已有页面标题清单（`related` 只能从这里选，选不到就留空）：\n%s\n\n"
            "请返回 JSON：\n"
            "{\n"
            '  "title": "最终页面标题",\n'
            '  "type": "concept | entity | analysis | lesson",\n'
            '  "summary": "一句话摘要，将用于 index.md",\n'
            '  "body": "markdown 正文",\n'
            '  "related": ["相关页面标题"]\n'
            "}\n\n%s" % (title_hint, len(group), "\n\n".join(parts), listing,
                         _PAGE_RULES))
    r = call_json(client, SYSTEM_COMPILE, user, max_tokens=32000, tag="编译 ")
    # SRC_RE.findall 返回的是**捕获组**（纯数字），必须补回 `m` 前缀，
    # 否则会渲染出 `**来源**：[3669]` 这种缺前缀的编号、失去可追溯性（实测踩过）。
    nums = sorted({"m" + x for p in group for x in SRC_RE.findall(p["body"])})
    return {"title": r.get("title") or title_hint,
            "type": r.get("type") or "concept",
            "summary": (r.get("summary") or "").strip(),
            "body": (r.get("body") or "").strip(),
            "related": [x for x in (r.get("related") or []) if x],
            "sources": nums,
            "from_files": sorted({p["file"] for p in group}),
            "from_titles": [p["title"] for p in group],
            "truncated": dropped}


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def _slug(s: str, n: int = 40) -> str:
    """标题 → 文件名片段。

    **只保留字母 / 数字 / 中文 / 连字符**，其余（Windows 非法字符、中英文标点、
    空白）统一折叠成 `-`。`\\w` 在 Python 3 默认就是 Unicode 语义，中文会被保留。

    这里踩过两次：① 中文标点（`（`/`：`/`、`）混进文件名后 markdown 链接难以解析；
    ② 按字符硬截断正好落在括号中间，产出 `02-...settings.json（.md` 这种断名。
    """
    s = re.sub(r"[^\w\-]+", "-", (s or "").strip())
    s = re.sub(r"-+", "-", s).strip("-")
    return (s[:n] or "page").rstrip("-")


def render_page(page: dict, domain: str) -> str:
    L = ["---", "title: %s" % page["title"], "domain: %s" % domain,
         "type: %s" % page["type"],
         "compiled_at: %s" % time.strftime("%Y-%m-%d %H:%M:%S"),
         "sources: %d" % len(page["sources"]),
         "merged_from: %d" % len(page["from_titles"]),
         "generator: wiki_aggregate prototype", "---", ""]
    # 页面自身的标题必须由渲染器写出（`_PAGE_RULES` 约束的是 body **内部**
    # 不要再重复写标题，避免两级同名标题 —— 两者是不同的事，别一起漏掉）。
    L += ["# %s" % page["title"], ""]
    if page.get("truncated"):
        L += ["> ⚠️ 本页合并时因长度限制截断了 %d 个来源页面。" % page["truncated"], ""]
    if page["summary"]:
        L += ["**摘要**：%s" % page["summary"], ""]
    L += [page["body"], ""]
    if page["related"]:
        L += ["**相关**：" + " · ".join("[[%s]]" % r for r in page["related"]), ""]
    if page["sources"]:
        L += ["**来源**：" + " ".join("[%s]" % s for s in page["sources"]), ""]
    L += ["<details><summary>合并自 %d 个会话内页面</summary>" % len(page["from_titles"]), ""]
    for t in page["from_titles"]:
        L.append("- %s" % t)
    for f in page["from_files"]:
        L.append("  - `%s`" % f)
    L += ["", "</details>", ""]
    return "\n".join(L)


def render_domain_index(domain: str, pages: list[dict]) -> str:
    L = ["# %s" % domain, "", "共 %d 页。%s" % (len(pages), ""), ""]
    for p in pages:
        L.append("- [%s](%s) — %s" % (p["title"], p["_file"], p["summary"]))
    return "\n".join(L)


def render_index(domains: list[tuple[str, list[dict]]]) -> str:
    total = sum(len(p) for _, p in domains)
    L = ["# 知识库索引", "",
         "共 %d 个主题域 / %d 页。" % (len(domains), total), ""]
    for name, pages in domains:
        L += ["## %s" % name, ""]
        for p in pages:
            L.append("- [%s](%s) — %s"
                     % (p["title"], "%s/%s" % (p["_dir"], p["_file"]), p["summary"]))
        L.append("")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run(args) -> None:
    src = Path(args.src)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    t_all = time.time()
    fl = FailureLog(out / "failures.jsonl")
    _wlog(event="run_start", script="wiki_aggregate", stage=args.stage,
          src=str(src), out=str(out), db=(args.db or "(配置默认)"),
          limit=args.limit, domain=args.domain, batch=args.batch)

    # 索引库是**可选但强烈建议**的：正文里的 `[n]` 是会话内序号，只有库能算出
    # 它对应哪条记忆；缺了它就只能靠产物 json 的 sources 对齐，而那个数组并不全
    # （实测 250/5552 处引用会失去可追溯性）。所以不显式给就回退到项目配置，
    # 与第一级 `wiki_compile.py --index-db` 的行为保持一致。
    db_path = Path(args.db) if args.db else None
    if db_path is None:
        try:
            from agentmemhub.rag.config import load_settings
            db_path = Path(load_settings().index_db)
        except Exception:
            db_path = None
    seq_map: dict = {}
    if db_path and db_path.exists():
        print("读索引库（只读）建立序号映射：%s" % db_path)
        seq_map = build_seq_map(db_path)
        print("  覆盖 %d 个会话" % len(seq_map))
    else:
        print("⚠️ 未指定 --db 且找不到索引库：正文 [n] 只能靠产物对齐，"
              "会有一批引用失去可追溯性")
    print("读第一级产出：%s" % src)
    pages = load_pages(src, seq_map)
    print("页面总数：%d" % len(pages))
    if args.limit:
        pages = pages[:args.limit]
        print("（试跑：只取前 %d 页）" % len(pages))
    if not pages:
        raise SystemExit("没有读到页面")

    client = make_client(args.thinking)

    # ---- ① 归域 ----
    dom_file = out / "_domains.json"
    if args.stage in ("domains", "all") and not (args.resume and dom_file.exists()):
        print("\n① 归域…")
        t0 = time.time()
        domains = stage_domains(client, pages, args.dmin, args.dmax,
                                batch=args.batch,
                                cache_file=out / "_assign_cache.json")
        dom_file.write_text(json.dumps(domains, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        print("  用时 %.0fs → %s" % (time.time() - t0, dom_file))
        print(_usage_report("归域"))
    else:
        domains = json.loads(dom_file.read_text(encoding="utf-8"))
        print("\n① 归域：复用 %s（%d 个域）" % (dom_file, len(domains)))

    if args.stage == "domains":
        for d in domains:
            print("   %-30s %d 页  %s" % (d.get("name", "")[:30],
                                          len(d.get("members") or []),
                                          (d.get("why") or "")[:40]))
        return

    # ---- ② 逐域细分（域级并发）----
    assigned = set()
    dom_list = []
    for d in domains:
        idx = [int(x) for x in (d.get("members") or [])
               if str(x).isdigit() and 1 <= int(x) <= len(pages) and int(x) not in assigned]
        assigned.update(idx)
        if idx:
            dom_list.append((d.get("name") or "未命名", [pages[i - 1] for i in idx]))
    if args.domain:
        dom_list = [(n, m) for n, m in dom_list if args.domain.lower() in n.lower()]
        print("\n（只跑匹配 %r 的 %d 个域）" % (args.domain, len(dom_list)))
    if args.retry_failed:
        bad = set(fl.targets("l2-plan")) | set(fl.targets("l2-compile"))
        if not bad:
            print("\n没有待重跑的失败域（%s）" % (out / "failures.jsonl"))
            return
        dom_list = [(n, m) for n, m in dom_list if n in bad]
        print("\n定向补跑 %d 个失败域（不全量重来）：%s"
              % (len(dom_list), [n[:18] for n, _ in dom_list][:5]))

    all_titles = [p["title"] for p in pages]
    min_pages = int(args.min_pages)
    cfg_workers = args.workers or 6
    t0 = time.time()

    def plan_domain(item):
        """把一个域细分成若干"最终页"。失败时退化为整域一页，绝不丢内容。"""
        name, members = item
        if len(members) <= min_pages:
            return name, [{"title": (members[0]["title"] if len(members) == 1 else name),
                           "group": list(members)}]
        try:
            plans = stage_plan(client, {"name": name}, members)
        except Exception as e:
            print("     细分 %s 失败（退化为整域一页）：%s"
                  % (name[:24], str(e)[:70]), flush=True)
            return name, [{"title": name, "group": list(members)}]
        return name, [{"title": pl["title"], "group": [members[i - 1] for i in pl["idx"]]}
                      for pl in plans if pl.get("idx")]

    print("\n② 域内细分（%d 个域，并发 %d）…" % (len(dom_list), cfg_workers), flush=True)
    domain_plans: list[tuple[str, list[dict]]] = []
    with ThreadPoolExecutor(max_workers=cfg_workers) as ex:
        futs = {ex.submit(plan_domain, it): it[0] for it in dom_list}
        for i, fut in enumerate(as_completed(futs), 1):
            nm = futs[fut]
            try:
                name, plans = fut.result()
            except Exception as e:
                print("[%3d/%3d] 细分 %-26s 失败：%s: %s"
                      % (i, len(dom_list), nm[:26], type(e).__name__, str(e)[:80]),
                      flush=True)
                fl.record(stage="l2-plan", target=nm,
                          error="%s: %s" % (type(e).__name__, str(e)[:200]))
                continue
            domain_plans.append((name, plans))
            print("[%3d/%3d] 细分 %-26s → %d 页"
                  % (i, len(dom_list), nm[:26], len(plans)), flush=True)
            _wlog(event="domain_planned", name=name, pages=len(plans))
            fl.resolve("l2-plan", name)

    # ---- ③ 逐最终页编译（**按最终页并发**）----
    # 不能按域并发：一个域可能有上百个最终页（实测最大的域 213 页），
    # 串行跑光这一个域就要一小时。
    tasks = [(name, ti, pl) for name, plans in domain_plans
             for ti, pl in enumerate(plans, 1)]
    print("\n③ 重新编译（%d 个最终页，并发 %d）…" % (len(tasks), cfg_workers), flush=True)

    def compile_task(t):
        name, ti, pl = t
        group = pl["group"]
        if len(group) == 1 and not args.recompile_single:
            src_p = group[0]
            page = {"title": src_p["title"], "type": src_p["type"],
                    "summary": src_p["summary"], "body": src_p["body"],
                    "related": src_p["related"],
                    "sources": sorted(set(src_p["mids"])),
                    "from_files": [src_p["file"]],
                    "from_titles": [src_p["title"]], "truncated": 0}
        else:
            try:
                page = stage_compile(client, pl["title"], group, all_titles)
            except Exception as e:
                # fail-open：编译失败就退化为"拼起来"，总比丢内容强
                print("     编译失败，退化为直接拼接：%s" % str(e)[:70], flush=True)
                page = {"title": pl["title"], "type": group[0]["type"],
                        "summary": group[0]["summary"],
                        "body": "\n\n---\n\n".join(p["body"].strip() for p in group),
                        "related": [], "truncated": 0,
                        "sources": sorted({m for p in group for m in p["mids"]}),
                        "from_files": sorted({p["file"] for p in group}),
                        "from_titles": [p["title"] for p in group]}
        page["_file"] = "%03d-%s.md" % (ti, _slug(page["title"]))
        return name, page

    by_domain_pages: dict[str, list[dict]] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=cfg_workers) as ex:
        futs = {ex.submit(compile_task, t): t for t in tasks}
        for fut in as_completed(futs):
            name, ti, pl = futs[fut]
            done += 1
            try:
                name, page = fut.result()
            except Exception as e:
                print("[%3d/%3d] 编译 %-30s 失败：%s: %s"
                      % (done, len(tasks), pl["title"][:30], type(e).__name__,
                         str(e)[:80]), flush=True)
                _wlog(event="page_fail", domain=name, title=pl["title"],
                      error="%s: %s" % (type(e).__name__, str(e)[:200]))
                fl.record(stage="l2-compile", target=name,
                          error="%s: %s" % (type(e).__name__, str(e)[:200]),
                          title=pl["title"])
                continue
            by_domain_pages.setdefault(name, []).append(page)
            _wlog(event="page_done", domain=name, title=page["title"],
                  merged=len(page["from_titles"]),
                  truncated=page.get("truncated", 0))
            print("[%3d/%3d] %-24s %-38s 并 %d 源页"
                  % (done, len(tasks), name[:24], page["title"][:38],
                     len(page["from_titles"])), flush=True)

    # ---- 写盘 ----
    # 先算出本轮的域目录名，再清掉**不属于本轮**的旧域目录。
    # 不清的后果：重跑时域划分/命名一变就生成新目录，旧目录原地不动，
    # 两轮产出混在一起（实测踩过：227 页的产出目录里躺着 449 个 md，
    # 核验与 linkfix 全都被污染）。产物是纯派生物，清掉随时可重建。
    plan = [(name, "%02d-%s" % (i, _slug(name)))
            for i, name in enumerate(sorted(by_domain_pages), 1)]
    keep = {dirname for _, dirname in plan}
    removed = 0
    for d in list(out.iterdir()):
        if d.is_dir() and d.name not in keep:
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    if removed:
        print("  清理上一轮的 %d 个旧域目录（避免两轮产出混在一起）" % removed)

    results: list[tuple[str, list[dict]]] = []
    for di, name in enumerate(sorted(by_domain_pages), 1):
        pages_out = sorted(by_domain_pages[name], key=lambda p: p["_file"])
        ddir = out / ("%02d-%s" % (di, _slug(name)))
        ddir.mkdir(parents=True, exist_ok=True)
        for p in pages_out:
            p["_dir"] = ddir.name
            (ddir / p["_file"]).write_text(render_page(p, name), encoding="utf-8")
            (ddir / p["_file"].replace(".md", ".json")).write_text(
                json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")
        (ddir / "index.md").write_text(render_domain_index(name, pages_out),
                                       encoding="utf-8")
        results.append((name, pages_out))

    (out / "index.md").write_text(render_index(results), encoding="utf-8")
    n_pages = sum(len(p) for _, p in results)
    print("\n完成：%d 域 / %d 页，用时 %.0fs" % (len(results), n_pages, time.time() - t0))
    print(_usage_report("第二级"))
    print()
    print(fl.summary())
    from agentmemhub.llm import usage_snapshot
    _wlog(event="run_end", script="wiki_aggregate", domains=len(results),
          pages=n_pages, seconds=round(time.time() - t_all, 1),
          usage=usage_snapshot())


def main() -> None:
    ap = argparse.ArgumentParser(description="LLM Wiki 第二级：跨会话聚合")
    ap.add_argument("--src", required=True, help="第一级产出目录")
    ap.add_argument("--out", required=True, help="第二级输出目录")
    ap.add_argument("--db", default="",
                    help="索引库路径（只读）——用于把正文 [n] 补全为全局 m-id，强烈建议提供")
    ap.add_argument("--stage", default="all",
                    choices=["domains", "all"], help="domains=只归域（便宜，先看质量）")
    ap.add_argument("--workers", type=int, default=0, help="并发数（0=用配置）")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 页（试跑）")
    ap.add_argument("--domain", default="", help="只跑名称匹配该串的域（试跑）")
    ap.add_argument("--retry-failed", action="store_true", dest="retry_failed",
                    help="只重跑失败清单里未解决的域（读 --out 下的 failures.jsonl）")
    ap.add_argument("--dmin", type=int, default=20, help="主题域数量下限")
    ap.add_argument("--dmax", type=int, default=60, help="主题域数量上限")
    ap.add_argument("--batch", type=int, default=120,
                    help="归类时每批的页面数（越小越不容易被思维链吃满 max_tokens）")
    ap.add_argument("--min-pages", type=int, default=2,
                    help="域内页数 ≤ 此值时不再细分（直接合成一页）")
    ap.add_argument("--recompile-single", action="store_true",
                    help="单页组成的最终页也走 LLM 重编译（默认沿用原文，省调用）")
    ap.add_argument("--thinking", default="", help="覆盖思考开关（enabled/disabled）")
    ap.add_argument("--no-resume", action="store_true", help="忽略已有的 _domains.json")
    args = ap.parse_args()
    args.resume = not args.no_resume
    run(args)


if __name__ == "__main__":
    main()
