"""记忆冲突检测（规则层）——发现「旧结论已被新结论取代」。

背景与设计（见 `docs/memory-lifecycle.md` §4）：LLM（Mem0 的
ADD/UPDATE/DELETE/NOOP 四操作）是**开放域**的通用解，但个人/团队知识库里
的"矛盾"绝大多数是**同一事实的版本演进**（v2.5→v2.6、端口 8086→9100），
而且人表达"更新"时几乎总会留下语言标记（"改为/废弃/不再"）。所以这里用
**零成本、可解释的规则层**，LLM 只留作兜底（L4，未实现）。

四层漏斗中的实现范围：
- L0 候选生成：向量近邻（**直接用库里已有的向量**做 KNN，不需要加载嵌入模型）
- L1 变更词规则：新记忆含"改为/废弃/不再"等标记 → 疑似取代
- L2 结构化槽位对比：同槽位不同值（端口/路径/模型名/数字+单位/版本号）
- L3/L4：未实现（本地 NLI 缓做、LLM 只兜底高价值项）

**只标记、不执行**：所有处置动作（归档/保留/忽略）由人工经 CLI 或面板确认，
归档复用 `memstore.set_manual_value(uid, 0.0)`（撤出召回面、可逆、零新表）。
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from typing import Any, Callable, Optional

#: 变更标记词：命中即说明这条记忆在描述"某事的改变"（新方通常是取代方）。
#: 中英文分开维护，词表可热改（与 feed 格式白名单同模式：从常量起步）。
CHANGE_MARKERS: tuple[str, ...] = (
    # 中文
    "改为", "改成", "改为用", "换成", "换用", "替换为", "取代", "替代为",
    "迁移到", "不再", "停止使用", "停用", "废弃", "弃用", "取消", "关闭",
    "已删除", "已移除", "回退到", "回滚到", "纠正", "更正", "其实不是",
    # 英文
    "deprecated", "removed", "no longer", "instead of", "replaced by",
    "reverted", "switched to", "migrated to",
)

#: 模型名（`owner/name`）的候选形态。**还要再过一道版本号筛**（见
#: `_MODEL_VERSION_RE`）：技术文本里 `owner/name` 满天飞——GitHub 仓库名
#: （`adbutils/pure-python-adb`）、分支名（`feat/memory-distillation`）、
#: 参数组合（`p1/p2`）都是这个形状，实测把它们当"模型"会让 evolution 类
#: 从 76 对暴涨到 1557 对且几乎全误报。只有 `owner/name` 且 name 带版本号
#: （`xiaomi/mimo-v2.6-flash`）才当作"模型"。
#: `(?<![\w/])` 另外防止把路径片段（`/etc/app.yaml`）误识别成模型名；
#: owner 段要求**字母开头**，否则 `0.873/0.659` 这种数值对被当成模型名
#: （真实库实测踩到：价值分对照表被判成"模型演进"）。
_MODEL_SHAPE_RE = re.compile(
    r"(?<![\w/])([a-z][\w.\-]*/[a-z0-9][\w.\-]*)\b", re.I)
_MODEL_VERSION_RE = re.compile(r"v?\d+\.\d+", re.I)

#: 带**主体名**的版本号：`Mobile_App_AutoTest v1.4.0` / `框架v3.0`。
#:
#: 为什么必须绑主体：裸版本号在不同主题的邻居之间毫无可比性。真实库实测
#: （2026-11-07，2173 条记忆）——只认裸版本号时 13 对里 12 对是误报：
#: `NextChat` vs `github-mcp-server`、`VoiceStudio v0.5.0` vs `NextChat`、
#: `v2.0.0 Release` vs `bge-small-zh-v1.5`，只是"都在聊某个版本号"而已。
#: 唯一正确的那对是 `Mobile_App_AutoTest v1.1.0` vs `... v1.4.0`——
#: **主体相同、版本不同**，这才是"同一事物的换代"。
_VERSIONED_EN_RE = re.compile(
    r"(?:(?<=[\s（(，,。:：、\"'`])|^)([A-Za-z][\w\-.]{1,40})\s+v(\d+\.\d+(?:\.\d+)*)",
    re.M)
_VERSIONED_ZH_RE = re.compile(r"([\u4e00-\u9fff]{2,8})\s*v(\d+\.\d+(?:\.\d+)*)")

#: **参数名词**：出现这些词说明文本在陈述"某个配置项的值"。
#:
#: 为什么需要它：光看"同单位数字不同"会把「M1 完成 106 条」vs「M3 完成 5 条」
#: 判成冲突。加了这个词表后，只有当**两条记忆都在说同一个参数**（都提到
#: "半衰期"/"端口"/"超时"…）才比较数值——这才是"同一属性的两个候选值"。
#:
#: ⚠️ 词表**只收"可量化且唯一"的配置参数**。真实库实测（2026-11-07）：
#: `预算`/`衰减`/`并发`/`批次`/`缓存`/`配额`/`频率` 这类词在技术笔记里
#: 几乎总指"某个统计口径"而不是"某个配置项"，把它们收进来会让
#: 「预算 8 次」vs「36/5 个片段」这种不同维度的数字被判成参数冲突。
#: 维护原则：**宁缺毋滥**——加词前先问"两条记忆都为这个词给过唯一数值吗"。
_PARAM_NOUNS: tuple[str, ...] = (
    "半衰期", "保鲜期", "端口", "维度", "超时", "阈值", "上限", "下限",
    "保留份数", "间隔", "权重上限", "重试次数",
)


def param_nouns(text: str) -> set[str]:
    """文本中出现的参数名词（用于判断两条记忆是否在说同一配置项）。"""
    return {n for n in _PARAM_NOUNS if n in (text or "")}


def _model_stem(value: str) -> str:
    """模型名的"词干"：剥掉版本片段，用于判断是否同一系列。

    `xiaomi/mimo-v2.5` 与 `xiaomi/mimo-v2.6-flash` → 都是 `xiaomi/mimo`
    （同一系列的两个版本 = 换代）；`z-ai/glm-5.2` 与 `qwen3.7/3.6` → 不同。
    """
    return re.sub(r"[-_.v]?\d+(?:\.\d+)*.*$", "", value).strip("/-._")


#: 中文主体黑名单：这些是**动词/泛名词**不是项目名。实测「AGENTS.md 记录 v1.3.2」
#: 会把"记录"抽成主体——不同主体之间不会互相比较（不会造成误报），但会污染
#: detail 证据，也会让"记录 v1.1" 与 "记录 v2.0" 这种巧合互相比较。
_VER_ZH_STOP = frozenset({
    "记录", "说明", "更新", "完成", "版本", "支持", "使用", "发布", "项目",
    "文档", "结构", "功能", "实现", "设计", "现在", "当前", "升级", "兼容",
})

#: 槽位抽取规则。**比 `search.extract_identifiers` 更细**：后者是为检索设计的
#: 高熵标识符（≥12 字符），而冲突检测要的是"同槽位的两个值"——端口只有 4 位、
#: 版本号 `2.6` 也只有 3 个字符，都会被它漏掉。
_SLOT_PATTERNS: dict[str, re.Pattern] = {
    # 不带主体名的裸版本号**不作为槽位**（见 _VERSIONED_EN_RE 的说明）
    # 端口：要吃掉中间的动词，否则「端口改为 9100」抽不到（实测踩到——
    # 这类表述恰恰是变更句最典型的形态）
    "port": re.compile(
        r"(?:端口|port)\s*(?:改为|改成|换成|为|是|[:=])?\s*(\d{2,5})", re.I),
    "num_unit": re.compile(
        r"(\d+(?:\.\d+)?)\s*(天|日|小时|分钟|秒|ms|条|个|维|%|MB|GB|KB|条/秒)",
        re.I),
    "path": re.compile(r"([A-Za-z]:\\[^\s，。；：、）)]+|/[\w.\-]+(?:/[\w.\-]+)+)"),
    "model": _MODEL_SHAPE_RE,
}

#: 硬冲突槽位：**只认端口**。端口是真正"一服务一值"的槽位。
#:
#: `path` 与数字类（"72.7 秒" vs "89 秒"）实测误报极高——它们在不同语境下
#: 天然不同，不构成矛盾。两者降级为 `detail` 里的证据，供人工判读。
#: `model` 同理不进硬冲突：换模型几乎总是"演进"（`mimo-v2.5` → `mimo-v2.6`
#: 是升级），把它当矛盾会让每条模型变更都淹没在"冲突"里。
_HARD_SLOTS = ("port",)

#: 硬冲突的**唯一值**要求：参与比较的槽位在两侧都必须只出现 1 个值。
#:
#: 2026-11-07 实测教训：真实库 2173 条记忆首扫出 1633 对 conflict，抽样
#: **全是误报**——长文本里罗列多个数字/路径（"3 分钟/11.6 条/4897 条"、
#: 多个目录），两侧集合必然不相交，于是被当成"同槽位不同值"。加这条后，
#: 只有"这条记忆在说**唯一一个**端口，那条说另一个端口"才进队列。
#: 代价：一条记忆提到两个端口时漏报——**宁可漏报不误报**，误报会让整个
#: 功能失去可信度（用户点开三条都是噪音就再也不用了）。
_UNIQUE_MAX = 1

#: 数字槽位的唯一值上限放宽到 2：真实记忆常写成"180 天半衰期 + 90 天保鲜期"
#: （一句话两个同类数字），按 1 会漏掉这类最重要的参数变更。配合"必须共享
#: 参数名词 + 新方含变更词"两个前置条件，误报风险可控。
_NUM_UNIQUE_MAX = 2

#: 默认只扫「记忆类」unit（与评分枚举、stale 报表同口径）。
_MEMORY_WHERE = "(u.role='distilled' OR u.source='memory')"


def ensure_conflicts_schema(conn: sqlite3.Connection) -> None:
    """建冲突表（幂等）。表放索引库：读写都走引擎连接，与 distill_hashes 同级。"""
    conn.executescript("""
CREATE TABLE IF NOT EXISTS memory_conflicts(
  id INTEGER PRIMARY KEY,
  unit_new  INTEGER NOT NULL,
  unit_old  INTEGER NOT NULL,
  kind      TEXT NOT NULL,
  rule_hit  TEXT NOT NULL,
  similarity REAL,
  status    TEXT NOT NULL DEFAULT 'open',
  detail_json TEXT,
  detected_at INTEGER,
  resolved_at INTEGER,
  resolved_by TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_mc_pair
  ON memory_conflicts(unit_new, unit_old);
CREATE INDEX IF NOT EXISTS idx_mc_status ON memory_conflicts(status);
""")


def slots(text: str) -> dict[str, set[str]]:
    """抽取可比较的槽位值（每个槽位一个值集合，便于"交集为空"判定）。

    ⚠️ **`num_unit` 按单位分桶**（键形如 `num:天` / `num:条`）：不分桶时
    "3 分钟" 与 "4897 条" 会被当成同一槽位的两个值 → 实测在真实库上产生
    1633 对全误报（不同量纲的数字本来就没有可比性）。
    """
    out: dict[str, set[str]] = {}
    if not text:
        return out
    for key, pat in _SLOT_PATTERNS.items():
        for m in pat.finditer(text):
            val = m.group(1).strip().lower()
            if not val:
                continue
            if key == "num_unit":
                unit = (m.group(2) or "").strip().lower()
                out.setdefault(f"num:{unit}", set()).add(val)
            elif key == "model":
                # 只有 name 部分带版本号才算模型（排除仓库名/分支名/参数对）
                name = val.split("/", 1)[-1]
                if _MODEL_VERSION_RE.search(name):
                    out.setdefault("model", set()).add(val)
            else:
                out.setdefault(key, set()).add(val)
    # 带主体的版本号：槽位键带主体名（`ver:mobile_app_autotest`），
    # 只有主体相同才可比——这是精度最高的一类信号。
    for m in _VERSIONED_EN_RE.finditer(text):
        out.setdefault(f"ver:{m.group(1).lower()}", set()).add(m.group(2).lower())
    for m in _VERSIONED_ZH_RE.finditer(text):
        name = m.group(1).lower()
        if name in _VER_ZH_STOP:
            continue                      # 动词/泛名词不是主体
        out.setdefault(f"ver:{name}", set()).add(m.group(2).lower())
    return out


def change_markers_hit(text: str) -> list[str]:
    """返回命中的变更标记词（保序去重）。"""
    low = (text or "").lower()
    hits: list[str] = []
    for m in CHANGE_MARKERS:
        if m.lower() in low and m not in hits:
            hits.append(m)
    return hits


def classify(new_text: str, old_text: str, *,
             similarity: float = 1.0) -> Optional[tuple[str, str, dict]]:
    """判定一对记忆的关系。返回 `(kind, rule_hit, detail)` 或 None。

    kind：`conflict`（同一配置项两个值，硬证据）/ `evolution`（版本/模型演进）。

    ⚠️ **判定纪律：宁可漏报，不误报**（2026-11-07 两轮真实库实测定案）：

    1. **`L1` 变更词不单独触发**。技术记忆几乎每条都在描述变更
       （"2026-10-07 从 X 改为 Y"），把"含变更词"当取代证据，实测一次扫描
       产出 1877 对几乎全是噪音。变更词只作为 `detail` 里的补充证据供人工判读。
    2. **只有"同一配置项的唯一值"才算硬冲突**，且只认 `port`（端口是真正
       一服务一值的槽位）。`path` / 数字类差异在长文本里太常见（"72.7 秒"
       vs "89 秒" 是不同场景的耗时，不是矛盾），实测误报率极高，一并降级
       为 detail 证据。
    3. **必须在高相似度区比对**（默认 ≥0.90）：冲突检测要找的是"几乎同一
       条结论、但关键值不同"，泛语义邻居之间比槽位没有意义。
    4. **文本相同直接不判**（调用方负责跳过）：相似度 1.0 且正文一致是
       重复投影/重复蒸馏，不是冲突。

    版本号/模型不同 → `evolution`：这是"同一主题后来换了"，值得提示但不是矛盾。
    """
    a, b = slots(old_text), slots(new_text)
    detail: dict[str, Any] = {"similarity": round(float(similarity), 4)}
    marks = change_markers_hit(new_text)
    if marks:
        detail["markers"] = marks          # 仅作证据，不单独触发
    if a or b:
        detail["slots_old"] = {k: sorted(v) for k, v in a.items()}
        detail["slots_new"] = {k: sorted(v) for k, v in b.items()}

    # ── 硬冲突：唯一端口的两个不同值 ──────────────────────────────────
    for key in _HARD_SLOTS:
        va, vb = a.get(key), b.get(key)
        if not va or not vb:
            continue
        if len(va) > _UNIQUE_MAX or len(vb) > _UNIQUE_MAX:
            continue
        if not (va & vb):
            return "conflict", f"l2:{key} {sorted(va)} ≠ {sorted(vb)}", detail

    # ── 数字参数变更：**必须共享参数名词**才算（"半衰期 30 天" vs "半衰期
    #    改为 180 天"）。这是知识库最有价值的一类冲突（配置/参数被改），
    #    但只在"两条都在说同一个参数"时才判——否则「M1 完成 106 条」vs
    #    「M3 完成 5 条」也会被算成冲突（实测 76 对全误报就是这么来的）。
    shared_nouns = param_nouns(old_text) & param_nouns(new_text)
    if shared_nouns and marks:
        for key in sorted(set(a) & set(b)):
            if not key.startswith("num:"):
                continue
            va, vb = a[key], b[key]
            if len(va) > _NUM_UNIQUE_MAX or len(vb) > _NUM_UNIQUE_MAX:
                continue
            if not (va & vb):
                detail["param_nouns"] = sorted(shared_nouns)
                return ("conflict",
                        f"l2:{'/'.join(sorted(shared_nouns))} "
                        f"{sorted(va)} ≠ {sorted(vb)}", detail)

    # ── 演进：同一主体的版本换代 ────────────────────────────────────
    # `ver:<主体>` 的键已绑定主体名，直接比较即可（裸版本号不参与）。
    for key in sorted(set(a) & set(b)):
        if not key.startswith("ver:"):
            continue
        va, vb = a[key], b[key]
        if not (va & vb):
            return "evolution", f"l2:{key} {sorted(va)} ≠ {sorted(vb)}", detail
    # `model` 差异只在**同一系列**内算换代（`mimo-v2.5` → `mimo-v2.6`）。
    # 跨系列（`glm-5.2` vs `qwen3.7/3.6`）不判——实测那类多是"推荐哪个模型"
    # 与"某通道用哪个模型"并存，不是换代。
    ma, mb = a.get("model"), b.get("model")
    if ma and mb and not (ma & mb):
        stems_a = {_model_stem(v) for v in ma}
        stems_b = {_model_stem(v) for v in mb}
        same = {x for x in stems_a if x and x in stems_b}
        if same:
            detail["model_old"] = sorted(ma)
            detail["model_new"] = sorted(mb)
            return ("evolution",
                    f"l2:model 同系列换代 {sorted(ma)} → {sorted(mb)}", detail)
    return None


def _knn_pairs(conn: sqlite3.Connection, vec_table: str, unit_id: int,
               k: int = 10) -> list[tuple[int, float]]:
    """用该 unit **库里已有的向量**做 KNN（不加载嵌入模型），返回 [(id, cosine)]。

    cosine 由 L2 距离换算：向量已归一化，d² = 2 - 2s ⇒ s = 1 - d²/2。
    """
    row = conn.execute(f"SELECT embedding FROM {vec_table} WHERE rowid=?",
                       (unit_id,)).fetchone()
    if not row or not row[0]:
        return []
    try:
        hits = conn.execute(
            f"SELECT rowid, distance FROM {vec_table}"
            f" WHERE embedding MATCH ? AND k = ?", (row[0], int(k) + 1)).fetchall()
    except sqlite3.OperationalError:
        return []
    out: list[tuple[int, float]] = []
    for rid, dist in hits:
        rid = int(rid)
        if rid == unit_id:
            continue
        sim = 1.0 - float(dist) * float(dist) / 2.0
        out.append((rid, sim))
    return out


def _split_src(src_id: str | None) -> tuple[str, str]:
    """拆 src_id 前缀与内容标识：`dst_<h>` → ("dst", "<h>")。"""
    s = src_id or ""
    if "_" not in s:
        return "", s
    head, _, tail = s.partition("_")
    return head, tail


def _dual_projection(src_a: str | None, src_b: str | None) -> bool:
    """`mcp_<h>` 与 `dst_<h>` 是**同一内容的两份投影**，不是冲突。

    实测（2026-11-07 真实库）：Agent 直写记忆同时落 `mcp_` 与 `dst_` 两个
    unit，近邻检索里必然互相命中、相似度 1.000，且正文含"改为/废弃"等词就
    被判成演进——首扫 75 对全是这种噪音，而且 sim=1.0 让它们排在列表最前，
    用户第一眼看到的全是没有信息量的重复行。
    """
    pa, ha = _split_src(src_a)
    pb, hb = _split_src(src_b)
    if not (pa and pb and ha and ha == hb):
        return False
    return {pa, pb} <= {"mcp", "dst"} and pa != pb


def purge_dual_projections(conn: sqlite3.Connection) -> int:
    """清掉已记录的「双投影误报」（幂等自愈，规则收紧后可重修存量）。"""
    ensure_conflicts_schema(conn)
    rows = conn.execute(
        """SELECT c.id, un.src_id, uo.src_id FROM memory_conflicts c
             LEFT JOIN units un ON un.id=c.unit_new
             LEFT JOIN units uo ON uo.id=c.unit_old""").fetchall()
    dead = [r[0] for r in rows if _dual_projection(r[1], r[2])]
    if dead:
        marks = ",".join("?" * len(dead))
        conn.execute(f"DELETE FROM memory_conflicts WHERE id IN ({marks})", dead)
        conn.commit()
    return len(dead)


def reset(conn: sqlite3.Connection) -> int:
    """清空全部冲突记录（规则调整后重扫用；已处置记录也会清掉）。"""
    ensure_conflicts_schema(conn)
    n = conn.execute("SELECT COUNT(*) FROM memory_conflicts").fetchone()[0]
    conn.execute("DELETE FROM memory_conflicts")
    conn.commit()
    return int(n)


def detect_for_unit(conn: sqlite3.Connection, unit_id: int, *, vec_table: str,
                    k: int = 10, min_similarity: float = 0.90,
                    commit: bool = True) -> list[dict]:
    """对单个 unit 检候选冲突（新 unit 落库后调用）。返回新增的冲突记录。"""
    ensure_conflicts_schema(conn)
    me = conn.execute(
        f"SELECT id, text, time, role, source, src_id FROM units WHERE id=?",
        (unit_id,)).fetchone()
    if me is None or not me[1]:
        return []
    my_text, my_time, my_src = me[1], int(me[2] or 0), me[5]
    found: list[dict] = []
    for rid, sim in _knn_pairs(conn, vec_table, unit_id, k):
        if sim < min_similarity:
            continue
        other = conn.execute(
            "SELECT id, text, time, role, source, src_id FROM units WHERE id=?",
            (rid,)).fetchone()
        if other is None or not other[1]:
            continue
        if not (other[3] == "distilled" or other[4] == "memory"):
            continue                      # 只跟记忆类比（消息层不是"结论"）
        if _dual_projection(my_src, other[5]):
            continue                      # 同内容双投影：不是冲突
        if (my_text or "").strip() == (other[1] or "").strip():
            continue                      # 正文完全一致：重复而非冲突
        o_time = int(other[2] or 0)
        # 方向：时间新的作为 unit_new；同时间按 id 大者为新
        if (o_time, rid) > (my_time, unit_id):
            new_id, new_text, old_id, old_text = rid, other[1], unit_id, my_text
        else:
            new_id, new_text, old_id, old_text = unit_id, my_text, rid, other[1]
        verdict = classify(new_text, old_text, similarity=sim)
        if verdict is None:
            continue
        kind, rule_hit, detail = verdict
        found.append(_insert_conflict(conn, unit_new=new_id, unit_old=old_id,
                                      kind=kind, rule_hit=rule_hit,
                                      similarity=sim, detail=detail))
    if commit and found:
        conn.commit()
    return [f for f in found if f.get("created")]


def _insert_conflict(conn: sqlite3.Connection, *, unit_new: int, unit_old: int,
                     kind: str, rule_hit: str, similarity: float,
                     detail: dict) -> dict:
    """写入一条冲突（pair 唯一：已存在则不重复插，返回 created=False）。"""
    now = int(time.time())
    cur = conn.execute(
        "INSERT OR IGNORE INTO memory_conflicts(unit_new, unit_old, kind,"
        " rule_hit, similarity, status, detail_json, detected_at)"
        " VALUES(?,?,?,?,?,'open',?,?)",
        (unit_new, unit_old, kind, rule_hit, float(similarity),
         json.dumps(detail, ensure_ascii=False), now))
    created = bool(cur.rowcount)
    rid = cur.lastrowid if created else None
    return {"id": rid, "unit_new": unit_new, "unit_old": unit_old,
            "kind": kind, "rule_hit": rule_hit, "similarity": similarity,
            "created": created}


def scan(conn: sqlite3.Connection, *, vec_table: str, k: int = 10,
         min_similarity: float = 0.90, limit: int = 0,
         progress: Optional[Callable[[int, int], None]] = None) -> dict:
    """存量扫描：遍历记忆类 unit，逐条 KNN 找疑似冲突对。

    成本：每条一次 ANN 查询（毫秒级），2 万条约分钟级——纯 SQL，不加载模型。
    扫描前先做两件自愈：清掉双投影误报、跳过同 hash 投影对（见 `_dual_projection`）。
    """
    ensure_conflicts_schema(conn)
    purged = purge_dual_projections(conn)
    rows = conn.execute(
        f"SELECT u.id FROM units u WHERE {_MEMORY_WHERE} AND u.text IS NOT NULL"
        " ORDER BY u.id").fetchall()
    ids = [int(r[0]) for r in rows]
    if limit:
        ids = ids[:limit]
    total = len(ids)
    stats = {"scanned": 0, "pairs": 0, "conflict": 0, "evolution": 0,
             "errors": 0, "purged_dual": purged}
    for i, uid in enumerate(ids, 1):
        try:
            found = detect_for_unit(conn, uid, vec_table=vec_table, k=k,
                                    min_similarity=min_similarity, commit=False)
        except Exception:                 # noqa: BLE001 —— 单条失败不中断全扫
            stats["errors"] += 1
            found = []
        stats["scanned"] += 1
        stats["pairs"] += len(found)
        for f in found:
            stats[f["kind"]] = stats.get(f["kind"], 0) + 1
        if progress is not None and (i % 200 == 0 or i == total):
            try:
                progress(i, total)
            except Exception:
                pass
    conn.commit()
    return stats


_COLS = ("c.id, c.unit_new, c.unit_old, c.kind, c.rule_hit, c.similarity,"
         " c.status, c.detail_json, c.detected_at, c.resolved_at, c.resolved_by")


def list_conflicts(conn: sqlite3.Connection, *, status: str = "open",
                   limit: int = 50) -> list[dict]:
    """列出冲突（带两侧正文摘要，面板/CLI 一眼可判）。"""
    ensure_conflicts_schema(conn)
    where, args = ("WHERE c.status=?", [status]) if status else ("", [])
    cur = conn.execute(
        f"""SELECT {_COLS},
                   un.src_id AS new_src, substr(un.text,1,200) AS new_text,
                   un.time AS new_time, uo.src_id AS old_src,
                   substr(uo.text,1,200) AS old_text, uo.time AS old_time
              FROM memory_conflicts c
              LEFT JOIN units un ON un.id=c.unit_new
              LEFT JOIN units uo ON uo.id=c.unit_old
              {where}
             ORDER BY c.similarity DESC, c.id DESC
             LIMIT ?""", args + [max(1, int(limit))])
    # 不依赖调用方的 row_factory（索引库连接由引擎维护，改它会有副作用）
    cols = [d[0] for d in cur.description]
    out: list[dict] = []
    for r in cur.fetchall():
        d = dict(zip(cols, r))
        try:
            d["detail"] = json.loads(d.pop("detail_json") or "{}")
        except Exception:
            d["detail"] = {}
            d.pop("detail_json", None)
        out.append(d)
    return out


def resolve(conn: sqlite3.Connection, conflict_id: int, action: str, *,
            reason: str = "", by: str = "cli") -> dict:
    """处置一条冲突。

    - `superseded`：**归档旧方**（`manual_value=0` → 撤出召回面、可逆）。
      旧方若已有其他手动加权，拒绝自动覆盖并要求人工处理（用户意志优先）。
    - `kept_both`：两者都保留（标记已确认，不再报告）。
    - `dismissed`：误报（记原因，同对不再重复报告）。
    """
    from agentmemhub.rag.memstore import ensure_memstore_schema, set_manual_value

    ensure_conflicts_schema(conn)
    ensure_memstore_schema(conn)      # 空库首次处置时 unit_values 可能还不存在
    row = conn.execute(
        "SELECT id, unit_new, unit_old, status FROM memory_conflicts WHERE id=?",
        (conflict_id,)).fetchone()
    if row is None:
        raise KeyError(f"冲突不存在: {conflict_id}")
    _id, unit_new, unit_old, status = row
    if status != "open":
        return {"id": conflict_id, "status": status, "changed": False,
                "note": "该冲突已处置过"}
    if action not in ("superseded", "kept_both", "dismissed"):
        raise ValueError(f"非法 action: {action!r}")

    if action == "superseded":
        old_manual = conn.execute(
            "SELECT manual_value FROM unit_values WHERE unit_id=?",
            (unit_old,)).fetchone()
        if old_manual and old_manual[0] not in (None, 0, 0.0):
            return {"id": conflict_id, "status": "open", "changed": False,
                    "note": f"旧方 #{unit_old} 已手动加权为 {old_manual[0]}，"
                            "归档会覆盖用户意志；请先清除加权或改选 kept_both"}
        set_manual_value(conn, unit_old, 0.0)
    now = int(time.time())
    conn.execute(
        "UPDATE memory_conflicts SET status=?, resolved_at=?, resolved_by=?,"
        " rule_hit=rule_hit||? WHERE id=?",
        (action, now, by, (f" | resolved:{action}" + (f" ({reason})" if reason else "")),
         conflict_id))
    conn.commit()
    return {"id": conflict_id, "status": action, "changed": True,
            "unit_new": unit_new, "unit_old": unit_old,
            "old_archived": action == "superseded"}


def stats(conn: sqlite3.Connection) -> dict:
    """冲突总量与分状态统计（面板/CLI 概览）。"""
    ensure_conflicts_schema(conn)
    rows = conn.execute(
        "SELECT status, COUNT(*) FROM memory_conflicts GROUP BY status").fetchall()
    by_status = {r[0]: int(r[1]) for r in rows}
    return {"total": sum(by_status.values()), "by_status": by_status}
