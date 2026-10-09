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
import os
import re
import shutil
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from wiki_compile import _usage_report  # noqa: E402  （复用用量/成本统计）
from agentmemhub.failures import FailureLog  # noqa: E402
from agentmemhub.llm_batch import (  # noqa: E402
    STATE_NAME,
    BatchJob,
    BatchRequest,
    PendingError,
    SyncRunner,
    load_state,
    save_state,
)

# Windows 控制台默认 GBK，用量报告里的 ¥ 会抛 UnicodeEncodeError。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

#: 第二级参数的兜底默认值（配置读不到时用，如脚本独立运行）。
L2_WORKERS = 6
L2_MIN_PAGES = 2
L2_DOMAIN_MAX = 60

#: 域内细分**单批成员上限**。超过就分批调用再合并。
#: 为什么必须分批：细分是**一次** LLM 调用，prompt 随域内页数线性增长。实测
#: 2026-09-25，114 个成员的域必定撞上游 HTTP 524（超时），而细分失败的兜底是
#: "退化为整域一页" —— 43 页的域因此被压成 1 页（两次事故的直接放大器）。
#: 与第一级的 `wiki_compile.PLAN_BATCH_MAX` 同一思路：**限制单批输入，
#: 批内编号再映射回全局编号**。
L2_PLAN_BATCH_MAX = 40

#: 写盘保护阈值（2026-09-25 丢页事故后加）。
#: 事故：定时触发的 L2 聚合在 LLM 上游**全程 HTTP 524** 时仍照写 —— 域内细分失败
#: 退化成"一域一页"，33 页的域被**整域替换**成 1 页，整轮 362 → 289 页、丢 73 页、
#: 334 处 `[[链接]]` 变悬空。产物虽是派生物，但"降级结果覆盖好结果"不可逆，
#: 所以宁可**不写**（保留上一轮产物 + 记日志/失败清单），也不要写下去。
#:   · COLLAPSE_MIN_OLD    ：旧页数达到该值才启用页数崩塌判据（小域本就 1~2 页，别误伤）
#:   · COLLAPSE_RATIO      ：新页数 < 旧页数 × 该比例 → 判崩塌，拒写该域
#:   · ABORT_DEGRADE_RATIO ：本轮 fail-open 降级的页占比 ≥ 该值 → **整轮不写盘**
COLLAPSE_MIN_OLD = 4
COLLAPSE_RATIO = 0.5
ABORT_DEGRADE_RATIO = 0.5

#: **全量重跑**的整轮内容覆盖底线：本轮要写进去的记忆里，盖住了上一轮产物的多少。
#: 低于它说明整轮都在降级（上游 LLM 大面积故障），宁可整轮不写盘。
#: 为什么全量要用"整轮覆盖"而不是逐域页数：全量会**重新划分域**，某个域变小往往
#: 只是内容被并到别的域去了（2026-10-04 实测：4 个域 61→5 / 109→28 / 98→16 /
#: 31→11，但整轮覆盖仍有约 97%）。逐域判会把正常的域重构误判成降级。
COVER_MIN_RATIO = 0.85


def _l2_cfg(key: str, default: int) -> int:
    """读 `wiki.l2.<key>`；读不到（脚本独立运行 / 键缺失 / 非法）回落常量。

    这几个键此前**从未被读取** —— 脚本只认命令行参数，而 argparse 的默认值又
    恰好等于配置里的值，所以"配了不生效"永远不报错（2026-09-22 配置审计发现）。
    优先级：命令行显式传值 > `wiki.l2.<key>` > 本模块常量。
    """
    try:
        from agentmemhub import config as hub_config
        v = int((hub_config.config().wiki_l2 or {}).get(key))
    except Exception:                       # noqa: BLE001 —— 脚本要能独立运行
        return default
    return v if v > 0 else default


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
    """构造 L2 的 LLM 客户端（第二级配置：顶层 `llm` + `wiki.l2.llm`）。

    ⚠️ **测试沙箱下一律抛异常**（`AGENTMEMHUB_TEST_SANDBOX=1`）。

    为什么熔断要加在**构造点**而不是"靠测试记得 mock"：这是本项目最后一条
    "缺省会自动构造真实外呼客户端"的路径。2026-10-08 实测教训 ——
    `tests/test_offline_e2e.py` 的「坏 JSON」用例没注入假 client，
    `wiki_batch._repair_client()` 就默认造了**真**客户端，于是真的发了 HTTPS 请求、
    卡了 900 秒超时才被 `faulthandler` 抓到；而测试把日志重定向到 tmp，
    **生产 `logs/agentmemhub.log` 里一行痕迹都没有**，从日志完全看不出发生过。
    "漏一个 mock 就真花钱"这种事靠自觉守不住（`test_wiki_service.py` 就有 3 处
    直接调 `wiki.update()`），所以必须在**唯一构造点**兜死。

    与 `wiki_triggers._targets()`（抛）、`batch_client`（抛 `BatchError`）同源同值。
    这里选**抛**而不是像 `wiki_batch._repair_client()` 那样返回 `None`：
    `make_client()` 没有"跳过"语义，返回 `None` 只会让调用方崩在更远的地方
    （`call_json(None, …)`），错误信息反而更难定位。

    测试里请 mock 本函数，或给 `assign_new_pages(client=…)` 注入假 client。
    """
    from agentmemhub.logs import SANDBOX_ENV
    if os.environ.get(SANDBOX_ENV):
        raise RuntimeError(
            "测试沙箱拒绝构造真实 LLM 客户端（%s 已置）：它会发真请求、真花钱。"
            "请 mock `wiki_aggregate.make_client` 或注入假 client。" % SANDBOX_ENV)
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

def plan_prompt(domain_name: str, members: list[dict]) -> str:
    """细分的用户提示（**纯函数**，分批与离线批处理都要复用它）。"""
    listing = "\n".join("[%d] %s :: %s" % (i, p["title"], p["summary"][:120])
                        for i, p in enumerate(members, 1))
    return USER_PLAN.format(domain=domain_name, n=len(members), listing=listing)


def _domain_key(name: str) -> str:
    """custom_id 里的域名段：`slug + 名称 crc32` 后缀。

    为什么不能只用 slug：不同域名完全可能 slug 成同一个串（只差标点/分隔符），
    而 custom_id 是**离线批处理的凭据** —— 撞了就是"把 A 域的结果填进 B 域"，
    既花错钱又写错页。加个短哈希把唯一性钉死。
    """
    import zlib
    return "%s-%08x" % (_slug(name), zlib.crc32(name.encode("utf-8")))


def plan_requests(domain_key: str, domain_name: str, members: list[dict],
                  size: int) -> list[BatchRequest]:
    """把域内成员切成若干批，每批**一条请求**。

    custom_id 自带定位信息（阶段｜域｜第几批）而不是裸序号 —— 离线批处理里
    「上传」与「回读」可能隔几小时甚至换机器，凭据必须能独立定位。
    """
    n_batch = max(1, (len(members) + size - 1) // size)
    out: list[BatchRequest] = []
    for bi in range(n_batch):
        chunk = members[bi * size:(bi + 1) * size]
        out.append(BatchRequest(
            custom_id="l2plan|%s|%d" % (domain_key, bi),
            system=SYSTEM_PLAN, user=plan_prompt(domain_name, chunk),
            max_tokens=16000, tag="细分[%d/%d] " % (bi + 1, n_batch)))
    return out


def stage_plan(client, domain: dict, members: list[dict], *,
               batch: int = 0, runner=None, log=print) -> list[dict]:
    """域内细分；成员多时**分批**调用再合并（理由见 `L2_PLAN_BATCH_MAX`）。

    **批内编号是「批内 1 起」，必须映射回全局编号** —— 与第一级的
    `wiki_compile.plan_groups_batched` 是同一个坑：不映射会取错成员，且越靠后的
    批次错得越离谱。

    `runner` 是**离线批处理的唯一接缝**：默认 `SyncRunner`（当场调用），
    换成离线运行器即可走 JSONL（见 `agentmemhub/llm_batch.py`）。单条失败由
    runner 记成空结果 → 该批成员**各自成页**，绝不丢内容、也不会塌成一页。
    """
    size = batch or L2_PLAN_BATCH_MAX
    if size <= 0:
        size = L2_PLAN_BATCH_MAX
    reqs = plan_requests(domain.get("key") or _domain_key(domain.get("name", "")),
                         domain.get("name", ""), members, size)
    if runner is None:
        runner = SyncRunner(
            lambda s, u, mt, tag: call_json(client, s, u, max_tokens=mt, tag=tag),
            on_error=lambda r, e: log("     %s失败（该批成员各自成页）：%s"
                                      % (r.tag, str(e)[:70])))
    results = runner.run(reqs)

    seen: set[int] = set()
    out: list[dict] = []
    for bi, r in enumerate(results):
        base = bi * size
        for pl in ((r or {}).get("pages") or []):
            idx = []
            for x in (pl.get("members") or []):
                if not str(x).isdigit():
                    continue
                g = base + int(x)          # 批内编号 → 全局编号
                if 1 <= g <= len(members) and g not in seen:
                    idx.append(g)
            if not idx:
                continue
            seen.update(idx)
            out.append({"title": pl.get("title") or "(无标题)", "idx": idx})
    for i in range(1, len(members) + 1):   # 细分遗漏的页面各自成页，绝不丢内容
        if i not in seen:
            out.append({"title": members[i - 1]["title"], "idx": [i]})
    return out


# ---------------------------------------------------------------------------
# 阶段 ③：编译最终页
# ---------------------------------------------------------------------------

def compile_prompt(title_hint: str, group: list[dict],
                   all_titles: list[str]) -> tuple[str, str, int]:
    """构造编译最终页的 `(system, user, dropped)`。

    **抽出来是为了两条路共用同一份提示词**：实时路当场调它，离线路把同一份编进
    JSONL。提示词一旦分叉，两条路的产物就会漂移 —— 这是本项目反复踩过的教训
    （离线路若绕过清洗、少发开关，产物看着像但实质不同）。
    """
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
    return SYSTEM_COMPILE, user, dropped


def stage_compile(client, title_hint: str, group: list[dict],
                  all_titles: list[str]) -> dict:
    system, user, dropped = compile_prompt(title_hint, group, all_titles)
    r = call_json(client, system, user, max_tokens=32000, tag="编译 ")
    # SRC_RE.findall 返回的是**捕获组**（纯数字），必须补回 `m` 前缀，
    # 否则会渲染出 `**来源**：[3669]` 这种缺前缀的编号、失去可追溯性（实测踩过）。
    nums = sorted({"m" + x for p in group for x in SRC_RE.findall(p["body"])})
    return _compile_page(r, title_hint, group, nums, dropped)


def _compile_page(r: dict, title_hint: str, group: list[dict],
                  nums: list[str], dropped: int) -> dict:
    """把 LLM 的返回（或离线回读到的同一形状）补成落盘用的页面对象。

    两条路共用：`sources` / `from_files` / `from_titles` 这些**派生字段**必须由
    这里统一算，否则离线路会漏掉它们（页面就失去可追溯性）。
    """
    return {"title": r.get("title") or title_hint,
            "type": r.get("type") or "concept",
            "summary": (r.get("summary") or "").strip(),
            "body": (r.get("body") or "").strip(),
            "related": [x for x in (r.get("related") or []) if x],
            "sources": nums,
            "from_files": sorted({p["file"] for p in group}),
            "from_titles": [p["title"] for p in group],
            "truncated": dropped}


def merge_plan_results(items: dict[str, dict], members: dict[str, list[dict]],
                       results: dict[str, dict],
                       plan_batch: int) -> list[tuple[str, list[dict]]]:
    """把回读到的**细分结果**合并成「域 → 页面分组」（与实时路径同一套编号映射）。

    编号映射是这里唯一容易错的地方：细分的 `members` 下标是**批内 1 起**，必须
    按 `批号 × plan_batch` 偏移回**全局编号**才能取对成员（越靠后的批次错得越离谱）。

    · 某批**失败或没回来** → 该批成员**各自成页**（与实时路同一兜底：绝不丢内容、
      也不会塌成一页）；
    · 细分的页面都没覆盖到的成员 → 同样各自成页。
    """
    by_domain: dict[str, list[tuple[int, dict]]] = {}
    for cid, meta in (items or {}).items():
        r = (results or {}).get(cid) or {}
        if not r.get("ok"):
            continue                        # 失败/未到的批，交给下面的"各自成页"
        domain = str((meta or {}).get("domain") or "")
        if not domain:
            continue
        by_domain.setdefault(domain, []).append(
            (int((meta or {}).get("batch_no") or 0), r.get("data") or {}))

    out: list[tuple[str, list[dict]]] = []
    for name, mem in (members or {}).items():
        groups: list[dict] = []
        seen: set[int] = set()
        for bi, data in sorted(by_domain.get(name, []), key=lambda x: x[0]):
            base = bi * int(plan_batch)
            for pl in ((data or {}).get("pages") or []):
                idx = []
                for x in (pl.get("members") or []):
                    if not str(x).isdigit():
                        continue
                    g = base + int(x)
                    if 1 <= g <= len(mem) and g not in seen:
                        idx.append(g)
                if not idx:
                    continue
                seen.update(idx)
                groups.append({"title": pl.get("title") or "(无标题)",
                               "group": [mem[i - 1] for i in idx]})
        for i in range(1, len(mem) + 1):     # 遗漏成员各自成页 —— 绝不丢内容
            if i not in seen:
                groups.append({"title": mem[i - 1].get("title") or "(无标题)",
                               "group": [mem[i - 1]]})
        out.append((name, groups))
    return out


def compile_requests(domain_plans: list[tuple[str, list[dict]]],
                     all_titles: list[str], *,
                     recompile_single: bool = False) -> tuple[
                         list[BatchRequest], dict[str, dict], dict[str, list[dict]]]:
    """把「域 → 页面分组」转成**编译阶段**的批量请求 + 现场。

    只把**需要 LLM 的页**放进去：单成员页在实时路也是直接沿用源页内容（省一次调用），
    离线路同样本地生成 —— 不占批量额度、不花钱。

    现场（`members`）存的是**每个页组的编译输入**：落盘轮是**另一次运行**，那时
    L1 页面早已不在内存里，没有这份快照就没法编译、也没法给单成员页取正文。
    连 `dropped`（合并时截断了几个来源页）也一并存 —— 它是 `compile_prompt()`
    当场算出来的，落盘轮重算既浪费又可能与当时不一致。
    """
    reqs: list[BatchRequest] = []
    items: dict[str, dict] = {}
    members: dict[str, list[dict]] = {}
    for name, groups in domain_plans:
        key = _domain_key(name)
        snap: list[dict] = []
        for ti, pl in enumerate(groups, 1):
            group = list(pl.get("group") or [])
            needs_llm = bool(recompile_single or len(group) > 1)
            entry: dict = {"ti": ti, "title": pl.get("title") or "(无标题)",
                           "group": group, "needs_llm": needs_llm, "dropped": 0}
            if needs_llm:
                system, user, dropped = compile_prompt(pl.get("title") or "",
                                                       group, all_titles)
                entry["dropped"] = dropped
                cid = "l2page|%s|%d" % (key, ti)
                reqs.append(BatchRequest(custom_id=cid, system=system, user=user,
                                         max_tokens=32000, tag="编译[%d] " % ti))
                items[cid] = {"domain": name, "page_no": ti}
            snap.append(entry)
        members[name] = snap
    return reqs, items, members


def assemble_pages(members: dict[str, list[dict]],
                   results: dict[str, dict]) -> tuple[dict[str, list[dict]], list[str]]:
    """把回读到的**编译结果**组装成「域 → 页面列表」（落盘轮用）。

    三条规则与实时路的 `compile_task()` **逐条对应** —— 这是"两条路产物一致"的落点：

    · **单成员页**（`needs_llm=False`）→ 直接沿用源页内容，不调 LLM（实时路也这么省）；
    · **有结果且成功** → 走 `_compile_page()` 补派生字段（`sources` / `from_*` / `truncated`）；
    · **结果缺失或失败** → **fail-open 退化为直接拼接**（绝不丢内容），并计入 `degraded`
      —— 后者供整轮降级闸判断，与实时路的 fail-open 语义相同。

    返回 `(by_domain_pages, degraded_titles)`。
    """
    by_domain: dict[str, list[dict]] = {}
    degraded: list[str] = []
    for name, snap in (members or {}).items():
        pages_out: list[dict] = []
        for item in snap or []:
            group = list(item.get("group") or [])
            if not group:
                continue
            ti = int(item.get("ti") or 0)
            title = item.get("title") or "(无标题)"
            nums = sorted({"m" + x for p in group
                           for x in SRC_RE.findall(p.get("body") or "")})
            if not item.get("needs_llm"):
                src = group[0]
                page = {"title": src.get("title") or title,
                        "type": src.get("type") or "concept",
                        "summary": src.get("summary") or "",
                        "body": src.get("body") or "",
                        "related": list(src.get("related") or []),
                        "sources": sorted(set(src.get("mids") or [])),
                        "from_files": [src.get("file") or ""],
                        "from_titles": [src.get("title") or ""],
                        "truncated": 0}
            else:
                cid = "l2page|%s|%d" % (_domain_key(name), ti)
                r = (results or {}).get(cid) or {}
                if r.get("ok") and isinstance(r.get("data"), dict):
                    page = _compile_page(r["data"], title, group, nums,
                                         int(item.get("dropped") or 0))
                else:
                    degraded.append(title)
                    page = {"title": title,
                            "type": group[0].get("type") or "concept",
                            "summary": group[0].get("summary") or "",
                            "body": "\n\n---\n\n".join(
                                (p.get("body") or "").strip() for p in group),
                            "related": [], "truncated": 0, "sources": nums,
                            "from_files": sorted({p.get("file") or "" for p in group
                                                  if p.get("file")}),
                            "from_titles": [p.get("title") or "" for p in group]}
            page["_file"] = "%03d-%s.md" % (ti, _slug(page["title"]))
            pages_out.append(page)
        if pages_out:
            by_domain[name] = pages_out
    return by_domain, degraded


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


def _yaml_flow_scalar(s: object) -> str:
    """把任意文本安全地放进 YAML **流式序列**（`[a, b]`）里的单个标量。

    title 来自 LLM，不能假设它干净：裸标量以 `[`/`{`/`,`/`#`/`&`/`*` 等开头、
    或含 `: ` / ` #` 时在 YAML 里都有特殊含义。统一用双引号包住 + 转义 `\\` 与 `"`
    最省心（实测当前 362 个标题都没问题，但那是运气，不是保证）。
    """
    return '"%s"' % str(s).replace("\\", "\\\\").replace('"', '\\"')


def render_page(page: dict, domain: str) -> str:
    L = ["---", "title: %s" % page["title"], "domain: %s" % domain,
         # aliases：**这一行是给 Obsidian 用的**。Obsidian 的 `[[X]]` 按
         # **文件名或 frontmatter aliases** 解析，**不读 title**；而页面里
         # `**相关**：[[标题]]` 全是**标题**形态 —— 缺 aliases 时这些链接会
         # 全部悬空，点一下还会让编辑器在 vault 根**自动新建一个空文件**
         # （实测踩过：3 个空文件差点被当成知识页投影进召回面，见
         # `wiki_index.project_pages` 的空页面闸）。让 title 兼作 alias，
         # 零额外成本把图谱的链接真正接上。
         "aliases: [%s]" % _yaml_flow_scalar(page["title"]),
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


def _esc_links(s: str) -> str:
    """把索引行里的 `[[…]]` 转义掉 —— 否则 Obsidian 会当成真链接。

    `index.md` 的摘要/标题是**正文的浓缩**，而正文里可能**描述** wikilink 语法
    （实测踩过：一条摘要里举例写了 `[[链接]]`）→ 渲染进索引后就成了死链，
    **点击即新建一个空白笔记**。产物的链接验收（`obsidian check`）要求未解析为 0，
    所以索引文本必须先转义。
    """
    return re.sub(r"\[\[([^\[\]]*)\]\]", r"\\[\\[\1\\]\\]", s or "")


def render_domain_index(domain: str, pages: list[dict]) -> str:
    L = ["# %s" % domain, "", "共 %d 页。%s" % (len(pages), ""), ""]
    for p in pages:
        L.append("- [%s](%s) — %s" % (_esc_links(p["title"]), p["_file"],
                                      _esc_links(p["summary"])))
    return "\n".join(L)


def render_index(domains: list[tuple[str, list[dict]]]) -> str:
    total = sum(len(p) for _, p in domains)
    L = ["# 知识库索引", "",
         "共 %d 个主题域 / %d 页。" % (len(domains), total), ""]
    for name, pages in domains:
        L += ["## %s" % name, ""]
        for p in pages:
            L.append("- [%s](%s) — %s"
                     % (_esc_links(p["title"]), "%s/%s" % (p["_dir"], p["_file"]),
                        _esc_links(p["summary"])))
        L.append("")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# 增量模式辅助
# ---------------------------------------------------------------------------

_FM_TITLE_RE = re.compile(r"^title:\s*(.+)$", re.M)
_MD_SUMMARY_RE = re.compile(r"^\*\*摘要\*\*：(.+)$", re.M)


def _page_meta_from_md(path: Path) -> tuple[str, str]:
    """从页面 md 自身取 (title, summary)，作为 json 副产物缺失时的回落。

    只解析本项目自己写出去的固定格式（frontmatter 的 `title:` 行与正文的
    `**摘要**：` 行），不引入通用 markdown/frontmatter 解析依赖。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return path.stem, ""
    t = _FM_TITLE_RE.search(text)
    s = _MD_SUMMARY_RE.search(text)
    return ((t.group(1).strip() if t else "") or path.stem,
            s.group(1).strip() if s else "")


def render_index_from_disk(out: Path, include: list[tuple[str, list[dict]]],
                           skip_dirs: set[str]) -> str:
    """增量模式重建顶层索引：本轮域用内存结果，其余域从磁盘读。

    域名读各域 index.md 的首行（render_domain_index 写成 `# 域名`）；
    页的 title/summary 优先读各页 json 产物、缺失时回落 md 自身。
    **页面的枚举以 `.md` 为准** —— 索引里的链接必须指向知识页本身。
    磁盘是唯一真相 —— 不在内存里的域不能靠猜。
    """
    rows: list[tuple[str, list[tuple[str, str, str, str]]]] = []
    for name, pages_out in include:
        rows.append((name, [(p["_dir"], p["_file"], p["title"], p["summary"])
                            for p in pages_out]))
    for d in sorted(out.iterdir()):
        if not d.is_dir() or d.name in skip_dirs or not d.name[:1].isdigit():
            continue
        name = d.name
        idx = d / "index.md"
        if idx.exists():
            try:
                first = idx.read_text(encoding="utf-8").splitlines()[0]
                if first.startswith("# "):
                    name = first[2:].strip()
            except Exception:
                pass
        pages: list[tuple[str, str, str, str]] = []
        # 以 **.md 为准**枚举页面。曾经这里是 `glob("*.json")` 并把 **json 文件名**
        # 拼进链接，于是增量一跑顶层 index.md 就坏掉 —— 实测 362 条链接里 286 条
        # 指向 .json（点开是 JSON 原文）；域级 index 走 render_domain_index 另一条
        # 路径，所以没被污染，坏链因此潜伏了很久（2026-09-22 修复）。
        # json 仍是 title/summary 的首选来源（它是 LLM 的原始结构化产物），
        # 缺失或损坏时回落解析 md 自身。
        for mf in sorted(d.glob("*.md")):
            if mf.name == "index.md":
                continue
            title = summary = ""
            jf = mf.with_suffix(".json")
            if jf.exists():
                try:
                    j = json.loads(jf.read_text(encoding="utf-8"))
                    title = (j.get("title") or "").strip()
                    summary = (j.get("summary") or "").strip()
                except Exception:
                    pass
            if not title or not summary:
                md_title, md_summary = _page_meta_from_md(mf)
                title = title or md_title
                summary = summary or md_summary
            pages.append((d.name, mf.name, title or "(无标题)", summary))
        if pages:
            rows.append((name, pages))
    total = sum(len(p) for _, p in rows)
    L = ["# 知识库索引", "",
         "共 %d 个主题域 / %d 页。" % (len(rows), total), ""]
    for name, pages in rows:
        L += ["## %s" % name, ""]
        for dname, fname, title, summary in pages:
            L.append("- [%s](%s/%s) — %s"
                     % (_esc_links(title), dname, fname, _esc_links(summary)))
        L.append("")
    return "\n".join(L)


def _prune_stale_domain_dirs(out: Path, keep: set[str], *, partial: bool) -> int:
    """全量模式清掉**不属于本轮**的旧域目录，返回清掉的个数。

    为什么必须清：重跑时域划分/命名一变就生成新目录，旧目录原地不动，两轮产出
    混在一起（实测踩过：227 页的产出目录里躺着 449 个 md，核验与 linkfix 全被
    污染）。产物是纯派生物，清掉随时可重建。

    为什么**只清域目录**：产物目录可能被用户直接当 Obsidian vault 打开，里面会有
    `.obsidian/`（编辑器配置）这类**不是本脚本产出**的东西。原实现是"凡是不在
    本轮 keep 里的目录一律 rmtree"，会把用户的 vault 配置一起删掉 —— 不可逆的
    数据丢失（2026-09-22 实测发现）。现在两道闸：**跳过隐藏目录**（`.` 开头）
    且**只清 `%02d-` 数字前缀的域目录**。

    增量模式（partial）**绝不清目录**：本轮只编了少数域，其它域的产物都在磁盘上，
    清了就是把整个知识库删掉；目录名沿用现有映射，不让域集合编号重排波及未重编的域。
    """
    if partial:
        return 0
    if not keep:
        # ⚠️ **`keep` 为空 = 本轮一个域都没写出来**（归域/编译整体失败，比如 LLM 挂了、
        # 网络断了）。这时"清掉不属于本轮的目录"在字面上等于**清空整个知识库** ——
        # 绝不是任何调用方想要的。
        # 而且降级闸拦不住这种情况：`_is_degraded_run(0, 0)` 因为 `n_tasks > 0` 不成立
        # 而返回 False（它判的是"降级页占比"，一页都没有时无从计算）。
        # 产物虽然可由快照恢复，但"可恢复"不等于"该发生"。宁可留着旧产物、
        # 如实报"本轮没写出任何域"。（2026-10-04 体检发现）
        return 0
    removed = 0
    for d in list(out.iterdir()):
        if not d.is_dir() or d.name.startswith(".") or d.name in keep:
            continue
        if not d.name[:1].isdigit():      # 只认 `%02d-域名` 形态的域目录
            continue
        shutil.rmtree(d, ignore_errors=True)
        removed += 1
    return removed


def _is_degraded_run(n_degraded: int, n_tasks: int) -> bool:
    """本轮是否"降级到不该写盘"（fail-open 降级页占比 ≥ 阈值）。

    这一闸比逐域的页数崩塌更早：LLM 整体不可用时，**整轮都不该落盘**。
    """
    return n_tasks > 0 and n_degraded / n_tasks >= ABORT_DEGRADE_RATIO


def _existing_domain_dirs(out: Path) -> list[str]:
    """磁盘上已有的域目录名（`%02d-…` 形态）。

    判据与 `_prune_stale_domain_dirs` 保持一致：跳过隐藏目录（用户的
    `.obsidian/`）与非数字前缀目录（用户自建），否则会把"用户的东西"当成
    "上一轮产物"。
    """
    if not out.is_dir():
        return []
    return sorted(d.name for d in out.iterdir()
                  if d.is_dir() and not d.name.startswith(".") and d.name[:1].isdigit())


def _stale_domain_names(out: Path, keep: set[str], *, partial: bool) -> list[str]:
    """本轮**将被剪掉**的旧域**域名**（判据与 `_prune_stale_domain_dirs()` 完全一致）。

    为什么单独一个函数：`_prune_stale_domain_dirs()` 只回报"删了几个"，而
    "给消失的域销账"需要知道**删掉的是哪个域**。改它的返回值会波及既有调用点与
    测试，所以这里共用判据来源 `_existing_domain_dirs()` —— 两处各写一份必然漂移。

    为什么要反查域名：失败清单里的 `target` 是**域名**，不是目录名。反查靠
    `_domain_dir_map()`，而它对"对不上 `_domains.json` 的目录**直接不收**" ——
    那类孤儿目录被剪时**故意漏销**（宁可留一条待办让人看见，也不能因为
    "认不出它是谁"就误销一条真问题）。

    ⚠️ 必须在 `_prune_stale_domain_dirs()` **之前**调用：剪完目录名就反查不出来了。
    """
    if partial or not keep:
        return []
    dir_to_name = {d: n for n, d in _domain_dir_map(out).items()}
    return [dir_to_name[d] for d in _existing_domain_dirs(out)
            if d not in keep and d in dir_to_name]


def _domain_missing_abort(planned: set[str], produced: set[str], out: Path) -> bool:
    """全量写盘前问一句：本轮该不该因为"整域没产出"而整个中止？

    成立条件（三者同时）：本轮**计划里有的域**、**一页都没产出**、且磁盘上
    **已有旧产物**。

    为什么不能"跳过那个域、把其余域写了"：全量模式的目录名按**成功域**重新编号
    （`sorted(by_domain_pages)` 带序号），少一个域后面整体前移 —— 旧目录
    （`01-旧域`）与新目录（`01-新域`）并存，索引与互链全乱；而且
    `_prune_stale_domain_dirs` 会把没产出域的旧目录当"上一轮残留"删掉，
    本轮却没有替代产物 ⇒ **净损失一整域**。

    为什么"磁盘上没有旧产物"时放行：首次构建本就无物可毁，能写多少写多少，
    为了一个失败的域让整轮白跑没有意义。

    与降级闸同源：**宁可本轮什么都不做，也不要写出一个半成品知识库。**
    （2026-10-04 体检发现：`keep` 为空那道闸只覆盖了"全失败"，整域失败漏在外面。）
    """
    if not (planned - produced):
        return False
    return bool(_existing_domain_dirs(out))


def _is_page_collapse(old_n: int, new_n: int, *, strict: bool = True) -> bool:
    """页数是否"崩塌"到不该写的程度（判据见 COLLAPSE_* 的说明）。

    抽成函数是为了可测：这个判据是**防止降级产物覆盖好产物**的最后一道闸。

    `strict=False`（**全量重跑**）时**不按页数判** —— 全量会重新划分域，某个域
    变小往往只是内容被并到别的域去了，逐域看页数必然"崩塌"。那种情况改由
    `_run_coverage()` 的**整轮内容覆盖**闸把关（见 `finalize_outputs`）。
    """
    if not strict:
        return False
    return old_n >= COLLAPSE_MIN_OLD and new_n < old_n * COLLAPSE_RATIO


def _mids_of_pages(pages: list[dict]) -> set[int]:
    """一组**内存页**引用的记忆 id（`body` 里的 `[mNNN]` 加上 `sources`）。"""
    out: set[int] = set()
    for p in pages:
        out |= {int(x) for x in SRC_RE.findall(p.get("body") or "")}
        for t in (p.get("sources") or []):
            s = str(t)
            if s[:1] == "m" and s[1:].isdigit():
                out.add(int(s[1:]))
    return out


def _mids_of_dir(d: Path) -> set[int]:
    """某域目录**现有产物**引用的记忆 id（读页面 json；坏 json 跳过）。"""
    out: set[int] = set()
    if not d.is_dir():
        return out
    for j in d.glob("*.json"):
        if j.name == "index.json":
            continue
        try:
            p = json.loads(j.read_text(encoding="utf-8"))
        except Exception:                   # noqa: BLE001 —— 坏 json 不该绊住整轮
            continue
        if isinstance(p, dict):
            out |= _mids_of_pages([p])
    return out


def _run_coverage(out: Path, by_domain_pages: dict[str, list[dict]],
                  dir_of: dict[str, str]) -> dict[str, Any]:
    """**整轮内容覆盖**：本轮要写的内容，盖住了上一轮产物的多少。

    为什么必须用整轮口径：全量重跑会**重新划分域**，某个域变小往往只是内容被并到
    别的域去了 —— 逐域看页数必然"崩塌"（2026-10-04 实测：4 个域 61→5 / 109→28 /
    98→16 / 31→11，但整轮覆盖仍有约 97%）。逐域判不但把正常的域重构误判成降级，
    还会让这些域**永久卡住**：下轮比较基准仍是那批旧产物，同样"崩塌"，死循环。

    为什么不能干脆在全量模式下删掉这道闸：全量同样是"整域替换"，上游 LLM 大面积
    故障时写下去就不可逆。整轮口径两者兼顾 —— 放行正常的域重构，拦住真降级。
    """
    old: set[int] = set()
    for dirname in set(dir_of.values()):
        old |= _mids_of_dir(out / dirname)
    new: set[int] = set()
    for pages in by_domain_pages.values():
        new |= _mids_of_pages(pages)
    cover = (len(old & new) / len(old)) if old else 1.0
    return {"old": len(old), "new": len(new), "cover": cover,
            "degraded": bool(old) and cover < COVER_MIN_RATIO}


def _count_domain_pages(ddir: Path) -> int:
    """某域目录里**现有**的知识页数（不含 index.md）。目录不存在算 0。"""
    if not ddir.is_dir():
        return 0
    return sum(1 for f in ddir.glob("*.md") if f.name != "index.md")


def _swap_domain_dir(out: Path, ddir: Path, files: dict[str, str]) -> None:
    """把一个域的产物**整体换入**：先全量写进临时目录，成功后再与目标目录交换。

    为什么不能原地"删旧写新"：中途失败（磁盘满 / 进程被杀）会留下"旧页已删、新页
    没写"的半成品；而且**降级结果一旦写下就不可逆**（见 COLLAPSE_* 的说明）。

    临时目录与备份目录都用 `.` 前缀，`_prune_stale_domain_dirs` 不会碰它们；
    无论成功还是失败，`finally` 都会把两个临时目录收干净，不留垃圾。

    **失败仍然抛**（还原旧产物后抛给上层）——上层 `_write_domain()` 把它降级成
    "跳过这个域"，而不是让整轮中止。
    """
    tmp = out / (".tmp_" + ddir.name)
    bak = out / (".bak_" + ddir.name)
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    for fname, text in files.items():
        (tmp / fname).write_text(text, encoding="utf-8")
    shutil.rmtree(bak, ignore_errors=True)
    try:
        if ddir.exists():
            ddir.rename(bak)
        tmp.rename(ddir)
    except Exception:
        if not ddir.exists() and bak.exists():
            bak.rename(ddir)              # 换入失败 → 尽力把旧目录还原回去
        raise
    finally:
        # 失败路径也要收干净：`.tmp_<域>` 留在产物目录里既是视觉垃圾（它就在
        # Obsidian vault 里），又会被下一次换入开头 rmtree 掉（白写一遍）。
        # 实测 2026-09-28：换入失败留下 153 个文件的 `.tmp_04-…`。
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(bak, ignore_errors=True)


def _write_domain(out: Path, ddir: Path, files: dict[str, str]) -> str:
    """把单个域换入磁盘；**成功返回空串，失败返回可读原因（不抛）**。

    为什么不在这里抛：一次换入失败不该让整轮 L2 白跑。Windows 上最常见的成因
    是**该域目录里有文件被别的程序占着** —— 2026-09-28 实测：用户在 Obsidian 里
    打开着 `04-AgentMemHub-记忆系统` 的两个笔记，`ddir.rename(bak)` 报
    `PermissionError [WinError 5]`，于是 88 分钟的一轮里 L1 编完、**L2 一个域都没写**。
    """
    try:
        _swap_domain_dir(out, ddir, files)
        return ""
    except Exception as e:                      # noqa: BLE001 —— 任何失败都只跳过该域
        return "%s: %s" % (type(e).__name__, str(e)[:160])


def _write_domain_outputs(out: Path, by_domain_pages: dict[str, list[dict]],
                          dir_of: dict[str, str], fl: FailureLog, *,
                          strict_collapse: bool = True
                          ) -> tuple[list[tuple[str, list[dict]]], list[str],
                                     list[str]]:
    """逐域写盘（整域原子换入），返回 `(成功域, 被跳过的域, 跳过原因)`。

    两条跳过路径**必须走同一个出口**（都进 `skipped_domains`）：

    · **页数崩塌**（`_is_page_collapse`）—— 降级产物不可逆，保留旧页；
    · **换入失败**（`_write_domain`）—— 例如该域的文件被 Obsidian 占着。

    上层（`wiki.py`）靠 `skipped_domains` 把"**没真正写出来**"的域剔出新基线，
    它们因此保持"脏"、下轮自然重试 —— **跳过一次绝不等于永久不更新**。

    **成功也要销账**（与两条跳过路径配对的第三条出口）：某域本轮真写进去了，
    就 `fl.resolve("l2-write", name)` 销掉它的历史失败。失败清单是**待办**，
    磁盘上既然已是最新一轮产物，待办就该消 —— 否则攒出"幽灵失败"。
    2026-10-08 实测：10-04 14:32 增量轮跳过的 4 个域，在当晚 17:14 全量轮
    **完整写入 396 页、零跳过**之后仍然一条都没销，一直挂到 4 天后人工清账，
    根因就是销账只发生在 `--retry-failed` 路径。
    """
    results: list[tuple[str, list[dict]]] = []
    skipped: list[str] = []
    details: list[str] = []
    for name in sorted(by_domain_pages):
        pages_out = sorted(by_domain_pages[name], key=lambda p: p["_file"])
        ddir = out / dir_of[name]
        old_n = _count_domain_pages(ddir)
        new_n = len(pages_out)
        # 页数崩塌保护：写盘是**整域替换**，降级结果一旦写下就不可逆。
        # 实测 2026-09-25：LLM 上游全程 524 → 域内细分退化成"一域一页"，
        # 33 页的域被写成 1 页，整轮丢 73 页、334 处链接悬空。
        if _is_page_collapse(old_n, new_n, strict=strict_collapse):
            skipped.append(name)
            details.append("%s %d→%d 页（疑似降级）" % (name, old_n, new_n))
            print("  ⚠️ 跳过写盘：%-24s %d → %d 页（疑似降级，保留旧产物）"
                  % (name[:24], old_n, new_n), flush=True)
            _wlog(event="domain_write_skipped", domain=name, reason="page_collapse",
                  old_pages=old_n, new_pages=new_n)
            fl.record(stage="l2-write", target=name,
                      error="页数崩塌 %d→%d，已保留旧产物" % (old_n, new_n))
            continue
        # 先全量渲染到内存，再整体换入 —— 不再原地"删旧写新"
        files: dict[str, str] = {}
        for p in pages_out:
            p["_dir"] = ddir.name
            files[p["_file"]] = render_page(p, name)
            files[p["_file"].replace(".md", ".json")] = json.dumps(
                p, ensure_ascii=False, indent=2)
        files["index.md"] = render_domain_index(name, pages_out)
        err = _write_domain(out, ddir, files)
        if err:
            skipped.append(name)
            details.append("%s 换入失败（%s）" % (name, err))
            print("  ⚠️ 跳过写盘：%-24s 目录换入失败，保留旧产物：%s"
                  % (name[:24], err), flush=True)
            _wlog(event="domain_write_skipped", domain=name, reason="swap_failed",
                  error=err)
            fl.record(stage="l2-write", target=name,
                      error="域目录换入失败：%s" % err)
            continue
        # 本轮真写进去了 → 销掉该域的历史失败。这是"跳过"的**反面出口**：
        # 两条跳过路径记 / 留失败，这条成功路径清失败，三者必须配对，否则清单只进不出。
        # `resolve()` 是追加 tombstone（不改历史行），跨进程并发安全；只影响
        # (stage="l2-write", target=name) 这一个域，别的域不受牵连。
        fl.resolve("l2-write", name)
        results.append((name, pages_out))
    return results, skipped, details


def finalize_outputs(out: Path, by_domain_pages: dict[str, list[dict]], *,
                     dir_of: dict[str, str], partial: bool, fl: FailureLog,
                     dom_file: "Path | None" = None, args=None,
                     pages: "list[dict] | None" = None,
                     status: "dict[str, Any] | None" = None
                     ) -> list[tuple[str, list[dict]]]:
    """**落盘收尾**：清旧域目录 → 逐域原子换入 → 顶层索引 → 域成员刷新。

    实时路与离线**落盘轮**共用这一段 —— 域级原子写、页数崩塌闸、跳过通路、
    顶层索引的两种写法（全量 / 增量）全在这里；两条路各写一份必然漂移。

    `dir_of` 由调用方给：实时路按本轮域集合重新编号，落盘轮必须**沿用提交轮当时
    的目录名**，否则域目录会重排（互链与索引跟着乱）。
    """
    # ---- 整轮内容覆盖闸（**仅全量**）----
    # 全量会重新划分域，逐域页数必然"崩塌"且判据不自愈（见 `_run_coverage`）；
    # 所以全量改看整轮覆盖，逐域页数闸只留给增量（域结构稳定时才成立）。
    if not partial:
        cov = _run_coverage(out, by_domain_pages, dir_of)
        if cov["degraded"]:
            print("\n⚠️ 整轮内容覆盖仅 %.1f%%（上轮 %d 条 / 本轮 %d 条）—— **本轮不写盘**，"
                  "保留上一轮产物；请等 LLM 恢复后重跑。"
                  % (cov["cover"] * 100, cov["old"], cov["new"]), flush=True)
            _wlog(event="run_aborted", script="wiki_aggregate", stage="l2",
                  reason="run_coverage", old_inputs=cov["old"],
                  new_inputs=cov["new"], cover=round(cov["cover"], 4))
            if status is not None:
                status["aborted"] = "run_coverage"
                status["coverage"] = cov
            return []
    keep = set(dir_of.values())
    # 必须在剪之前算：剪完目录名就反查不出域名了
    stale_names = _stale_domain_names(out, keep, partial=partial)
    removed = _prune_stale_domain_dirs(out, keep, partial=partial)
    if removed:
        print("  清理上一轮的 %d 个旧域目录（避免两轮产出混在一起）" % removed)
    # 域在重划中消失（产物侧已无此域）⇒ 针对它的 `l2-write` 待办失效，销账。
    # 不销就成了**无人能销**的幽灵失败：下一轮的 `by_domain_pages` 里连它的名字
    # 都不会出现，`_write_domain_outputs()` 那条成功销账路径永远够不着它。
    resolved = [n for n in stale_names if fl.resolve("l2-write", n)]
    if resolved:
        print("  销账 %d 条随域消失而失效的失败记录：%s"
              % (len(resolved), "、".join(resolved[:5])))
    results, skipped_names, _details = _write_domain_outputs(
        out, by_domain_pages, dir_of, fl, strict_collapse=partial)
    if status is not None:
        status["written_domains"] = [n for n, _ in results]
        status["pages_by_domain"] = {n: len(ps) for n, ps in results}
        status["skipped_domains"] = list(skipped_names)
    if skipped_names:
        print("  共跳过 %d 个域（保持上一轮产物、下轮自动重试）：%s"
              % (len(skipped_names), "、".join(skipped_names[:5])))
    if partial:
        # 增量：顶层索引 = 本轮域（内存）+ 其余域（磁盘，域名读各域 index.md 首行）
        (out / "index.md").write_text(
            render_index_from_disk(out, results, keep), encoding="utf-8")
        # 域成员重写为最新平铺序号 —— L1 页数变化会让旧 members 序号漂移失效
        if dom_file is not None and args is not None and pages is not None:
            _refresh_domains_file(dom_file, args, pages)
    else:
        (out / "index.md").write_text(render_index(results), encoding="utf-8")
    return results


def _domain_dir_map(out: Path) -> dict[str, str]:
    """「**权威域名** → 磁盘目录名」。

    域名**只认 `_domains.json`**（产物侧的域表），再用 slug 反查目录名 ——
    **绝不读各域 `index.md` 的首行**：老产物（09-20/09-21 那批）的首行写的是
    **目录名**（`# 01-ADBController-与-Android-自动化`），照它定域名会造出一批
    "假域名"、写进 manifest；下一轮 `domain_map` 会把它当真域，于是又新建一套
    重复目录（2026-10-01 实测：manifest 从 18 个域涨到 21 个，产物里出现
    `20-04-AgentMemHub-记忆系统` 这种**双重前缀**目录）。

    对不上域表的目录**直接不收** —— 宁缺勿滥：假域名的危害远大于少认一个目录。
    """
    m: dict[str, str] = {}
    if not out.is_dir():
        return m
    by_slug: dict[str, str] = {}
    doms_file = out / "_domains.json"
    if doms_file.exists():
        try:
            for d in (json.loads(doms_file.read_text(encoding="utf-8")) or []):
                nm = str((d or {}).get("name") or "").strip()
                if nm:
                    by_slug.setdefault(_slug(nm), nm)
        except Exception:                   # noqa: BLE001 —— 域表坏了就当没有
            pass
    for d in sorted(out.iterdir()):
        if not d.is_dir() or d.name.startswith(".") or not d.name[:1].isdigit():
            continue
        slug = d.name.split("-", 1)[-1] if "-" in d.name else d.name
        name = by_slug.get(slug)
        if name and name not in m:
            m[name] = d.name
    return m


def write_offline_stage(out: Path, members: dict[str, list[dict]],
                        results: dict[str, dict], fl: FailureLog, *,
                        partial: bool = True) -> dict[str, Any]:
    """离线**落盘轮**：把回读到的编译结果组装成页面并写盘。

    与实时路的差别**只有一处**：页面不是当场编译的，而是从批量结果组装
    （`assemble_pages`）。写盘本身完全共用 `finalize_outputs()` —— 域级原子写、
    页数崩塌闸、跳过通路、顶层索引全部一致。
    """
    by_domain, degraded = assemble_pages(members, results)
    status: dict[str, Any] = {"aborted": "", "written_domains": [],
                              "skipped_domains": [], "pages": 0,
                              "pages_by_domain": {}, "offline_submitted": {},
                              "degraded": len(degraded)}
    if not by_domain:
        print("\n没有可落盘的页面（编译结果为空）")
        return status
    dir_of = _domain_dir_map(out)
    # 新域（域表里没有、或磁盘上还没目录）按现有最大编号往后补，沿用 `%02d-<slug>`
    # 形态。**已存在的域一律复用它的目录名**（`_domain_dir_map` 已经用 slug 反查过，
    # 所以老产物的"首行是目录名"也不会再骗到我们）。
    next_no = 1 + max([int(n[:2]) for n in dir_of.values() if n[:2].isdigit()] or [0])
    for name in sorted(by_domain):
        if name not in dir_of:
            dir_of[name] = "%02d-%s" % (next_no, _slug(name))
            next_no += 1
    written = finalize_outputs(out, by_domain, dir_of=dir_of, partial=partial,
                               fl=fl, status=status)
    status["pages"] = sum(len(p) for _, p in written)
    _wlog(event="offline_written", domains=len(written), pages=status["pages"],
          degraded=len(degraded))
    print("\n离线落盘完成：%d 域 / %d 页（其中降级拼接 %d 页）"
          % (len(written), status["pages"], len(degraded)))
    return status


def assign_new_pages(client, table: list[dict], new_pages: list[dict],
                     rep_pages: dict[str, list[str]] | None = None) -> dict[str, list[dict]]:
    """把新增 L1 页归入**现有**域表（一次调用，便宜）。

    增量模式刻意不重跑全量归域：域结构稳定比全局最优更重要（域名/目录不变，
    互链和索引才稳定）。实在无处的页归入「待整理」。

    rep_pages：{域名: [该域现有页的代表性标题]}。归类视野若只有域名+一句话
    why，新页容易被塞进语义不准确的域、被聚合进错误主题页且要到下次全量才
    修正 —— 给每域几个真实页标题，让 LLM 看到域内内容"长什么样"。
    返回 {域名: [页,...]}。
    """
    if not new_pages:
        return {}
    table = list(table) + [{"name": "待整理", "why": "归类时遗漏的页面"}]
    rep = rep_pages or {}
    tl = "\n".join(
        "[%d] %s —— %s%s" % (i, d["name"], d.get("why") or "",
                             ("（现有页如：%s）" % "、".join(rep.get(d["name"], [])[:3]))
                             if rep.get(d["name"]) else "")
        for i, d in enumerate(table, 1))
    lines = "\n".join("[%d] (%s) %s" % (i, p["source"], p["title"])
                      for i, p in enumerate(new_pages, 1))
    r = call_json(client, SYSTEM_ASSIGN,
                  USER_ASSIGN.format(n=len(new_pages), listing=lines, table=tl)
                  # 增量归类专用的宽容出口：新内容可能不属于任何现有域（外部
                  # 投喂、全新主题）。不放开这个口子，LLM 会按模板里"必须有
                  # 归属"的要求硬塞进最接近的域 —— 错误聚合比待整理更难修正。
                  + "\n\n注意：若某页与任何现有域都不相关或你没有把握，"
                    "将其归入「待整理」域，**不要强行归类**。",
                  max_tokens=24000, tag="增量归类 ")
    out: dict[str, list[dict]] = {}
    for k, v in (r.get("assign") or {}).items():
        try:
            i, dv = int(k), int(v)
        except (TypeError, ValueError):
            continue
        if 1 <= i <= len(new_pages) and 1 <= dv <= len(table):
            out.setdefault(table[dv - 1]["name"], []).append(new_pages[i - 1])
    return out


def _refresh_domains_file(dom_file: Path, args, pages: list[dict]) -> None:
    """增量收尾：把 _domains.json 的 members 重写为最新平铺序号。

    args.domain_map 是服务层给的全量归属 {域name: [L1文件名,...]} —— 必须覆盖
    **全部**域（含本轮没重编的），否则缺的域会被写丢。
    """
    dmap = getattr(args, "domain_map", None) or {}
    if not dmap:
        return
    whys: dict[str, str] = {}
    if dom_file.exists():
        try:
            for d in json.loads(dom_file.read_text(encoding="utf-8")):
                whys[d.get("name") or ""] = d.get("why") or ""
        except Exception:
            pass
    doms = []
    for name, files in sorted(dmap.items()):
        if not files:
            continue
        fs = set(files)
        doms.append({"name": name, "why": whys.get(name, ""),
                     "members": [i for i, p in enumerate(pages, 1)
                                 if p["file"] in fs]})
    dom_file.write_text(json.dumps(doms, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print("  域成员已按最新页序重写 → %s" % dom_file)


def _submit_stage(out: Path, runner, reqs: list[BatchRequest],
                  items: dict[str, dict[str, Any]],
                  members: dict[str, list[dict]], *, stage: str,
                  label: str = "离线提交") -> dict[str, Any]:
    """**提交内核**：落现场 → 交出请求 → 如实回报。细分段与编译段共用。

    它只关心"把已经准备好的请求交出去"，不关心请求怎么来的 —— 所以细分的
    `plan_requests()` 与编译的 `compile_requests()` 都能接在它后面。

    返回的 status 带 `offline_submitted`（**不是失败**）：上层据此保留现场、
    不刷 manifest、不跑 linkfix —— 结果几小时后才有，下轮再来回读。
    """
    status: dict[str, Any] = {"aborted": "", "written_domains": [],
                              "skipped_domains": [], "pages": 0,
                              "pages_by_domain": {}, "offline_submitted": {}}
    if not reqs:
        print("\n没有可提交的请求（%s），本轮不做任何事" % stage)
        return status

    state_path = out / STATE_NAME

    def _persist(info: dict[str, Any]) -> None:
        """把「已交给厂商」落盘。**由运行器在抛异常之前调用**（顺序见 OfflineRunner）。"""
        jobs = [j for j in load_state(state_path) if not j.voided]
        jobs.append(BatchJob(
            batch_id=str(info.get("batch_id") or ""), provider="mimo",
            stage=str(info.get("stage") or stage),
            submitted_at=float(info.get("submitted_at") or 0.0),
            items=dict(items), members=dict(members),
            input_file_id=str(info.get("input_file_id") or ""),
            provider_status=str(info.get("provider_status") or ""),
            expire_at=float(info.get("expire_at") or 0.0)))
        save_state(state_path, jobs)
        print("  现场已落盘：%s（%d 个域 / %d 条请求）"
              % (state_path, len(members), len(reqs)))

    runner.stage = stage
    runner.on_submitted = _persist
    n_domains = len(members)
    print("\n② %s：%d 个域 / %d 条请求 → 一次上传 + 一个任务"
          % (label, n_domains, len(reqs)), flush=True)
    t0 = time.time()
    try:
        runner.run(reqs)
    except PendingError as e:
        subs = list(getattr(runner, "submissions", []) or [])
        sub = {"batch_id": str(subs[-1].get("batch_id") or "") if subs else "",
               "stage": stage, "n_requests": len(reqs), "n_domains": n_domains,
               "domains": sorted(members), "pending_ids": list(e.pending_ids)}
        status["offline_submitted"] = sub
        _wlog(event="offline_submitted", stage=stage, batch_id=sub["batch_id"],
              n_requests=len(reqs), n_domains=n_domains,
              seconds=round(time.time() - t0, 1))
        print("  离线任务已提交（结果未回）：batch=%s，%d 个域等待回读"
              % (sub["batch_id"] or "(未知)", n_domains))
        # **提交成功要通知人**：离线是异步的（实测等 18 分钟 ~ 24 小时），提交完就该
        # 知道"活交出去了、接下来是等"。推不推微信由 `notify.push_when` 决定
        # （`wiki_trigger` 默认开 ⇒ 默认会推）。
        try:
            from agentmemhub import notify as _notify
            _notify.info("batch", "已提交离线批次",
                    "批次 %s（%s）：%d 个域 / %d 条请求，等厂商跑完后会自动回读落盘。"
                    % (sub["batch_id"] or "(未知)", stage, n_domains, len(reqs)),
                    meta={"batch_id": sub["batch_id"], "stage": stage,
                          "n_requests": len(reqs), "n_domains": n_domains,
                          "domains": sorted(members)})
        except Exception:                   # noqa: BLE001 —— 旁路
            pass
        return status

    # 运行器正常返回 = 它没把"结果未回"表达出来（实现有问题）。
    # **不猜、不假装提交成功**，如实记一条 error 让运维看见。
    _wlog(event="offline_submitted", stage=stage, ok=False,
          error="离线运行器没有抛 PendingError —— 本轮未提交任何任务")
    print("  ⚠️ 离线运行器没有抛 PendingError：本轮未提交任务")
    return status


def _submit_offline(out: Path, runner, dom_list: list[tuple[str, list[dict]]],
                    plan_batch: int) -> dict[str, Any]:
    """**细分提交轮**：把整个域集合的请求**一次**交给厂商，然后如实回报。

    与实时路径的关系：请求怎么切分、批内编号怎么映射回全局编号，与实时路是
    **同一段代码**（`plan_requests`）；分叉只在"谁执行、什么时候执行"。

    为什么**整体一次**提交：逐域提交会变成 N 次上传 + N 个任务（实测生产有 19 个
    域），对账与管理成本随域数线性膨胀。一次提交只有一份 JSONL、一个 `batch_id`。
    """
    reqs: list[BatchRequest] = []
    items: dict[str, dict[str, Any]] = {}
    members: dict[str, list[dict]] = {}
    for name, mem in dom_list:
        batch_reqs = plan_requests(_domain_key(name), name, mem, plan_batch)
        for bi, r in enumerate(batch_reqs):
            reqs.append(r)
            items[r.custom_id] = {"domain": name, "batch_no": bi}
        members[name] = list(mem)
    return _submit_stage(out, runner, reqs, items, members, stage="l2plan")


def submit_offline_compile(out: Path, runner, plan_job: "BatchJob",
                           results: dict[str, dict],
                           all_titles: list[str], *,
                           plan_batch: int = L2_PLAN_BATCH_MAX,
                           recompile_single: bool = False) -> dict[str, Any]:
    """**回读轮的第二步**：消费细分结果 → 生成编译请求 → 再交出去。

    `results` 是回读到的细分结果（`{custom_id: {ok, data|error}}`），`plan_job` 是
    运行 1 落盘的那个任务（`items` 给 custom_id→域映射、`members` 给成员快照）。

    产出的是**编译阶段**的现场：`域 → 每个页组的 {ti, title, group, needs_llm}`。
    落盘轮靠它编译多成员页、或给单成员页取正文。
    """
    domain_plans = merge_plan_results(plan_job.items, plan_job.members,
                                      results, plan_batch)
    reqs, items, members = compile_requests(domain_plans, all_titles,
                                            recompile_single=recompile_single)
    n_groups = sum(len(g) for _, g in domain_plans)
    print("  细分结果已合并：%d 个域 / %d 个页组（其中 %d 页需要 LLM）"
          % (len(domain_plans), n_groups, len(reqs)), flush=True)
    status = _submit_stage(out, runner, reqs, items, members, stage="l2compile",
                           label="离线编译提交")
    # 把编译现场带回去：**一条请求都没交出去**时（全是单成员页，本地就能生成），
    # 调用方要能直接落盘 —— 而不是"提交一个不存在的任务然后空等"。
    status["compile_members"] = members
    return status


def load_jobs(out: Path) -> list["BatchJob"]:
    """读产物目录里的在飞任务（`batch_state.json`）。"""
    return load_state(out / STATE_NAME)


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
    # 增量模式（服务层经 preset_domains 传入域成员）跳过归域：域结构保持稳定，
    # 成员由服务层按 manifest 的 file→域 映射组装。不能增量复用 stage_domains ——
    # 它的归类缓存按批次序号键控，L1 页数一变序号整体漂移，缓存全错位。
    dom_file = out / "_domains.json"
    preset = getattr(args, "preset_domains", None) or None
    partial = bool(getattr(args, "partial", False))
    if preset:
        dom_list = [(name, members) for name, _dn, members in preset]
        preset_dirs = {name: dn for name, dn, _ in preset}
        print("\n① 归域：增量模式 —— 使用给定的 %d 个域，跳过归域" % len(preset))
    else:
        if args.stage in ("domains", "all") and not (args.resume and dom_file.exists()):
            print("\n① 归域…")
            t0 = time.time()
            domains = stage_domains(client, pages, args.dmin,
                                    args.dmax or _l2_cfg("domain_max", L2_DOMAIN_MAX),
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
    min_pages = int(args.min_pages) or _l2_cfg("min_pages", L2_MIN_PAGES)
    cfg_workers = args.workers or _l2_cfg("workers", L2_WORKERS)
    plan_batch = _l2_cfg("plan_batch_max", L2_PLAN_BATCH_MAX)
    t0 = time.time()

    # ---- 离线模式：整个域集合**一次交出去**，本轮到此为止 ----
    # 刻意不走下面的逐域并发（那会变成"一个域一次上传 + 一个任务"）。运行器由
    # 调用方注入（`args.offline_runner`）—— 这里不关心"用哪家、凭据是什么"，
    # 只知道"把请求交给它，它会抛 PendingError 表示结果未回"。
    offline_runner = getattr(args, "offline_runner", None)
    if offline_runner is not None:
        return _submit_offline(out, offline_runner, dom_list, plan_batch)

    def plan_domain(item):
        """把一个域细分成若干"最终页"。

        **兜底从"整域一页"改成"成员各自成页"**：前者虽然不丢内容，却会把几十页
        的域压成 1 页（2026-09-25 两次丢页事故的放大器）；后者保持粒度、同样不丢
        内容，配合写盘侧的页数崩塌闸一起，把"降级"的破坏力限制在质量层面。
        """
        name, members = item
        if len(members) <= min_pages:
            return name, [{"title": (members[0]["title"] if len(members) == 1 else name),
                           "group": list(members)}]
        try:
            plans = stage_plan(client, {"name": name}, members, batch=plan_batch,
                               log=lambda m: print(m, flush=True))
        except PendingError:
            raise                       # 离线：结果未回来，交给上层保留现场
        except Exception as e:
            print("     细分 %s 失败（成员各自成页）：%s"
                  % (name[:24], str(e)[:70]), flush=True)
            return name, [{"title": m["title"], "group": [m]} for m in members]
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

    # ---- ③ 的离线分支：**只把编译段交出去**（细分已经在本轮实时跑完了）----
    # 为什么这样切分（2026-10-01 实测后的决定）：
    #   · **细分**只有「域数 × 批数」条请求（实测 3 个域 = 11 条），实时跑 1 分钟级；
    #     离线省下的绝对值很小，却要**多付一整轮排队延迟**（实测细分段排了 24 小时）。
    #   · **编译**是「每个最终页一条」（实测 113 条），正是离线划算的场景。
    #   两段都离线 ⇒ 48 小时；只离线编译 ⇒ 24 小时。少一段等待、成本只多几分钱。
    compile_runner = getattr(args, "offline_compile_runner", None)
    if compile_runner is not None:
        dplans = [(name, [{"title": pl["title"], "group": pl["group"]}
                          for pl in plans]) for name, plans in domain_plans]
        reqs, items, members = compile_requests(
            dplans, all_titles, recompile_single=bool(args.recompile_single))
        print("\n③ 离线编译提交（%d 个最终页 → %d 条请求）"
              % (len(tasks), len(reqs)), flush=True)
        return _submit_stage(out, compile_runner, reqs, items, members,
                             stage="l2compile", label="离线编译提交")

    print("\n③ 重新编译（%d 个最终页，并发 %d）…" % (len(tasks), cfg_workers), flush=True)

    # fail-open 降级过的页标题（LLM 失败 / 解析失败的代理信号）。线程安全：
    # 只做 append，GIL 下足够；读完在写盘前，此时并发已结束。
    degraded: list[str] = []

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
                degraded.append(pl["title"])
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

    # 本轮写盘的**如实回报**：谁进来了、谁被跳过、有没有整轮中止、写了多少页。
    # 上层（`wiki.py`）必须靠它决定"新基线该记哪些输入" —— 少了它，被跳过/被中止
    # 的那批输入仍会被记成"已编译"，下轮再也不会重试（静默丢失）。
    status: dict[str, Any] = {"aborted": "", "written_domains": [],
                              "skipped_domains": [], "pages": 0,
                              "pages_by_domain": {}}

    # ---- 整轮降级闸：**必须在一切破坏性动作之前** ----
    # 关键在"之前"：下面的 `_prune_stale_domain_dirs` 会删掉不属于本轮的旧域目录，
    # 那也是写盘的一部分 —— 先删了目录再中止，等于"声称中止了、磁盘却已经动了"。
    # fail-open 能保住内容，但质量会崩塌；而写盘是**整域替换**，把降级产物写下去
    # 就是用差结果占掉好结果的位置。宁可本轮什么都不做。
    if _is_degraded_run(len(degraded), len(tasks)):
        print("\n⚠️ 降级页占比 %.0f%%（%d/%d）≥ 阈值 %.0f%% —— **本轮不写盘**，"
              "保留上一轮产物；请等 LLM 恢复后重跑。"
              % (len(degraded) / len(tasks) * 100, len(degraded), len(tasks),
                 ABORT_DEGRADE_RATIO * 100), flush=True)
        _wlog(event="run_aborted", script="wiki_aggregate", stage="l2",
              reason="degrade_ratio", degraded=len(degraded), tasks=len(tasks))
        status["aborted"] = "degrade_ratio"
        return status

    # ---- 整域失败闸：同样是写盘前的破坏性动作，必须排在 `finalize_outputs` 之前 ----
    # 判据与缘由见 `_domain_missing_abort()`：全量重建少一个域 ⇒ 目录编号整体
    # 错位 + 没产出域的旧产物被当残留清掉，而本轮无替代品。宁可整轮不写。
    planned_domains = {name for name, _i, _pl in tasks}
    if not partial and _domain_missing_abort(planned_domains, set(by_domain_pages), out):
        missing = sorted(planned_domains - set(by_domain_pages))
        print("\n⚠️ 有 %d 个域整域没产出（%s）—— **本轮不写盘**，保留上一轮产物；"
              "请等 LLM 恢复后重跑。"
              % (len(missing), "、".join(missing[:3])), flush=True)
        _wlog(event="run_aborted", script="wiki_aggregate", stage="l2",
              reason="domain_missing", missing=len(missing),
              domains="、".join(missing[:5]))
        status["aborted"] = "domain_missing"
        return status

    # ---- 写盘 ----
    # 全量模式：先算出本轮的域目录名，再清掉**不属于本轮**的旧域目录。
    # 不清的后果：重跑时域划分/命名一变就生成新目录，旧目录原地不动，
    # 两轮产出混在一起（实测踩过：227 页的产出目录里躺着 449 个 md，
    # 核验与 linkfix 全都被污染）。产物是纯派生物，清掉随时可重建。
    # 增量模式（partial）：**绝不清目录** —— 本轮只编了少数域，其它域的
    # 产物都在磁盘上，清了就是把整个知识库删掉；目录名沿用现有映射，
    # 不让域集合的编号重排波及未重编的域。
    plan = [(name, (preset_dirs.get(name) if preset
                    else "%02d-%s" % (i, _slug(name))))
            for i, name in enumerate(sorted(by_domain_pages), 1)]
    dir_of = {name: dirname for name, dirname in plan}
    # 逐域写盘 + 顶层索引 —— 与离线**落盘轮**共用 `finalize_outputs()`。
    # **单个域写不进去绝不中止整轮**：换入失败（文件被别的程序占着）与页数崩塌走
    # 同一条跳过通路，该域保留旧产物、不进新基线 → 下轮自动重试。
    results = finalize_outputs(out, by_domain_pages, dir_of=dir_of,
                               partial=partial, fl=fl, dom_file=dom_file,
                               args=args, pages=pages, status=status)
    n_pages = sum(len(p) for _, p in results)
    print("\n完成：%d 域 / %d 页，用时 %.0fs" % (len(results), n_pages, time.time() - t0))
    print(_usage_report("第二级"))
    print()
    print(fl.summary())
    from agentmemhub.llm import usage_snapshot
    _u = usage_snapshot()
    _wlog(event="run_end", script="wiki_aggregate", domains=len(results),
          pages=n_pages,
          # seconds = 墙钟（机器休眠会把它撑大，实测一次显示 9.2 小时而实际
          # 约 1 小时）；call_seconds = 累计调用耗时，反映真实工作量
          seconds=round(time.time() - t_all, 1),
          call_seconds=round(_u.get("seconds", 0.0), 1), usage=_u)

    # 编译清单：与第一级同口径（全库输入快照），另附 域 → L1源文件/记忆 的映射，
    # 供对齐审计把"哪些记忆变了"映射成"哪些主题域需要重编"。
    # 没有索引库就没有可靠的 m-id，清单失去意义 —— 此时明确跳过而不是写空壳。
    if db_path and Path(db_path).exists():
        try:
            from agentmemhub import wiki_manifest as wm
            dom_map = {}
            for name, pages_out in results:
                mids: set[int] = set()
                files: set[str] = set()
                for p in pages_out:
                    files.add(p.get("file") or "")
                    mids.update(int(x) for x in
                                re.findall(r"\[m(\d+)\]", p.get("body") or ""))
                    mids.update(int(t[1:]) for t in (p.get("mids") or [])
                                if t[1:].isdigit())
                dom_map[name] = {
                    "dir": (pages_out[0].get("_dir") if pages_out else ""),
                    "pages": len(pages_out),
                    "l1_files": sorted(f for f in files if f),
                    "mids": sorted(mids),
                }
            mf = wm.build_manifest("l2", db_path, extra={
                "src_dir": str(src), "n_pages": n_pages, "domains": dom_map})
            wm.write_manifest(wm.manifest_path(out, "l2"), mf)
            print("编译清单已写：%s（输入 %d 条 / %d 域）"
                  % (wm.manifest_path(out, "l2"), mf["n_inputs"], len(dom_map)))
            _wlog(event="manifest", script="wiki_aggregate", stage="l2",
                  n_inputs=mf["n_inputs"], domains=len(dom_map))
        except Exception as e:                  # 旁路：清单失败不碰编译产物
            print("⚠️ 编译清单写入失败（不影响产物）：%s: %s" % (type(e).__name__, e))
    else:
        print("⚠️ 未提供索引库，跳过编译清单（对齐审计将没有 diff 锚点）")
    status["pages"] = n_pages
    return status


def main() -> None:
    ap = argparse.ArgumentParser(description="LLM Wiki 第二级：跨会话聚合")
    ap.add_argument("--src", required=True, help="第一级产出目录")
    ap.add_argument("--out", required=True, help="第二级输出目录")
    ap.add_argument("--db", default="",
                    help="索引库路径（只读）——用于把正文 [n] 补全为全局 m-id，强烈建议提供")
    ap.add_argument("--stage", default="all",
                    choices=["domains", "all"], help="domains=只归域（便宜，先看质量）")
    ap.add_argument("--workers", type=int, default=0,
                    help="并发数（0=用配置 wiki.l2.workers，默认 6）")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 页（试跑）")
    ap.add_argument("--domain", default="", help="只跑名称匹配该串的域（试跑）")
    ap.add_argument("--retry-failed", action="store_true", dest="retry_failed",
                    help="只重跑失败清单里未解决的域（读 --out 下的 failures.jsonl）")
    ap.add_argument("--dmin", type=int, default=20, help="主题域数量下限")
    ap.add_argument("--dmax", type=int, default=0,
                    help="主题域数量上限（0=用配置 wiki.l2.domain_max，默认 60）")
    ap.add_argument("--batch", type=int, default=120,
                    help="归类时每批的页面数（越小越不容易被思维链吃满 max_tokens）")
    ap.add_argument("--min-pages", type=int, default=0,
                    help="域内页数 ≤ 此值时不再细分（0=用配置 wiki.l2.min_pages，默认 2）")
    ap.add_argument("--recompile-single", action="store_true",
                    help="单页组成的最终页也走 LLM 重编译（默认沿用原文，省调用）")
    ap.add_argument("--thinking", default="", help="覆盖思考开关（enabled/disabled）")
    ap.add_argument("--no-resume", action="store_true", help="忽略已有的 _domains.json")
    args = ap.parse_args()
    args.resume = not args.no_resume
    run(args)


if __name__ == "__main__":
    main()
