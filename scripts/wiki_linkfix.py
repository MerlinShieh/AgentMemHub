"""修复 wiki 产出里的 `[[…]]` 链接 —— **按 Obsidian 的真实解析规则**输出。

## 为什么必须按文件名，而不是标题

2026-09-26 用 Obsidian 官方 CLI 实测（`obsidian eval`）：

    app.metadataCache.getFirstLinkpathDest("RAG 与 LLM Wiki：互补架构…", "")   → null    ✗
    app.metadataCache.getFirstLinkpathDest("041-RAG-与-LLM-Wiki-…", "")      → 命中    ✓

即 **Obsidian 的 `[[X]]` 只按「文件名 / 路径」解析，`aliases` 根本不参与**。
本模块此前把链接重写成 `[[标题]]`，于是**全库 1136 条链接在 Obsidian 里全是死链**
（`obsidian unresolved` 报 228 个目标）——点击就**新建一个空白笔记**。

产物是给人看的（Obsidian 视图），所以链接必须**点得开**。本模块的输出形态固定为：

    [[<目标文件名>|<显示文本>]]

显示文本不变（还是原标题），目标唯一确定（文件名在 vault 内唯一）。

## 解析顺序（保守：只做能唯一确定的修复）

  1. 已经是文件名 / `文件名|显示` 形态且命中 → 保留
  2. 精确命中标题 / 别名 / 文件名
  3. 被合并页的旧标题 → 按 `from_titles` 重定向到合并后的页
  4. 归一化后唯一命中（去标点/空白/大小写；标题里可能自带 `/`，故**整串先试**）
  5. 其余 → **保留文字、去掉链接标记**（`[[X]]` → `X`），计入报告

**不猜**：有歧义的名字（如 `AgentMemHub` 对应二十多个标题）强行指向某页，会把
读者引到不相关的地方 —— 比留一个死链更糟。

用法
====
    python scripts/wiki_linkfix.py --dir 产出目录 --dry-run   # 先看报告
    python scripts/wiki_linkfix.py --dir 产出目录             # 落盘
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

LINK_RE = re.compile(r"\[\[([^\[\]]+)\]\]")
#: 标题里的噪音字符（中英文标点、空白、连接符）——比对时全部忽略
_NOISE = re.compile(r"[\s\-_·—–、，。：:；;！!？?（）()【】\[\]{}《》\"'“”‘’/\\|]+")


def norm(s: str) -> str:
    return _NOISE.sub("", s or "").lower()


class Index:
    """产出目录的页面索引：把任意写法（标题/别名/文件名/归一化）映到**唯一**页面。

    `lookup` 只保留**唯一**命中；有歧义的键进 `ambiguous`（供报告解释"为什么不去链"）。
    """

    def __init__(self) -> None:
        self.by_stem: dict[str, str] = {}          # 文件名 stem → stem
        self.lookup: dict[str, str] = {}           # 各种写法 → stem（唯一命中）
        self.ambiguous: set[str] = set()           # 命中多个页面的键
        self.title_of: dict[str, str] = {}         # stem → 页面标题（显示用）
        self.redirect: dict[str, str] = {}         # 旧标题 → 合并后页的 stem
        self._acc: dict[str, set[str]] = defaultdict(set)

    def add(self, key: str, stem: str) -> None:
        if key:
            self._acc[key].add(stem)

    def finalize(self) -> None:
        for k, v in self._acc.items():
            if len(v) == 1:
                self.lookup.setdefault(k, next(iter(v)))
            else:
                self.ambiguous.add(k)


def collect_pages(root: Path) -> tuple[Index, list[Path]]:
    """收集页面与索引。

    **两级产出的 frontmatter `title` 含义不同**：第二级是页面标题（一文件一页），
    第一级是"发起 / 会话 id"（一文件多页，页面标题在正文 `# ` 里）。
    所以两边的标题都要收，否则拿第一级产出跑会一个标题都收不到。
    """
    idx = Index()
    files: list[Path] = []
    for f in sorted(root.rglob("*.md")):
        if f.name == "index.md":
            continue
        files.append(f)
        stem = f.stem
        idx.by_stem[stem] = stem
        idx.add(stem, stem)
        idx.add(norm(stem), stem)
        t = f.read_text(encoding="utf-8")

        def _put(title: str, primary: bool = False) -> None:
            title = (title or "").strip()
            if not title:
                return
            if primary and stem not in idx.title_of:
                idx.title_of[stem] = title
            idx.add(title, stem)
            idx.add(norm(title), stem)

        m = re.search(r"^title:\s*(.+?)\s*$", t, re.M)
        if m:
            # frontmatter 的 title 可能是 YAML 列表/引号包裹，取首值即可
            _put(m.group(1).strip().strip("\"'"), primary=True)
        m = re.search(r"^aliases:\s*(\[.*\])\s*$", t, re.M)
        if m:
            for a in _parse_list(m.group(1)):
                _put(a)
        # 第一级产出：页面标题写在正文 `# ` 里（一文件多页）
        for h in re.findall(r"^# (.+)$", t, re.M):
            _put(h.strip(), primary=(stem not in idx.title_of))
    idx.finalize()
    return idx, files


def _parse_list(raw: str) -> list[str]:
    """解析 frontmatter 里的列表字面量（按 YAML 语义，处理 `\\"` 转义）。"""
    try:
        import yaml
        v = yaml.safe_load(raw)
        if isinstance(v, list):
            return [str(x) for x in v]
    except Exception:                       # noqa: BLE001
        pass
    return [x.strip().strip("\"'") for x in raw.strip("[]").split(",") if x.strip()]


def collect_redirects(root: Path) -> dict[str, str]:
    """建立 **被合并页标题 → 合并后页面的文件名** 的重定向表。

    这是第二级死链的主因：聚合会把第一级的多个页面合并成一页，于是正文里
    指向**第一级标题**的链接全部失效（实测第二级死链率 92.6%）。好在 json 的
    `from_titles` 记下了每个最终页合并了哪些源页，据此就能重定向过去。
    """
    out: dict[str, str] = {}
    for f in sorted(root.rglob("*.json")):
        if f.name.startswith("_"):
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:                   # noqa: BLE001
            continue
        # 目录里不只有"页面 json"：早期版本的产物可能是**数组**。实测
        # `'list' object has no attribute 'get'` 会让整条 linkfix 挂掉 ——
        # 于是链接修复与 Obsidian 验收**一起没跑**（而异常被上层当成 linkfix 失败
        # 记了下来，产物却已经写盘）。扫目录的代码必须有这类类型容错。
        if not isinstance(d, dict):
            continue
        title = (d.get("title") or "").strip()
        if not title:
            continue
        # 该 json 对应的页面文件名 = 同名 md 的 stem（md 由 wiki_aggregate 一并写出）
        stem = f.stem
        for t in (d.get("from_titles") or []):
            t = (t or "").strip()
            if t and t != title:
                out.setdefault(t, stem)
                out.setdefault(norm(t), stem)
    return out


def resolve(name: str, idx: Index, redirects: dict[str, str]) -> str | None:
    """把一个 `[[name]]` 解析成确定目标的 **文件名 stem**；不确定返回 None。"""
    name = (name or "").strip()
    if not name:
        return None
    # ① 已是文件名（或 `文件名|显示` 已拆过）→ 直接命中
    if name in idx.by_stem:
        return name
    # ② 精确命中标题 / 别名 / 归一化键
    if name in idx.lookup:
        return idx.lookup[name]
    # ③ 被合并掉的旧标题 → **必须确认目标页真的存在**（页面可能又被删了）
    r = redirects.get(name) or redirects.get(norm(name))
    if r and r in idx.by_stem:
        return r
    # ④ 路径形态（`域/标题`）→ 拿最后一段再试
    #    ⚠️ 必须放在精确/归一化之后：标题**自身可能含 `/`**（如 `视觉/OCR`、
    #    `feat/llm-wiki`），先拆就会把这类标题拆坏。
    if "/" in name:
        tail = name.rsplit("/", 1)[-1].strip()
        if tail in idx.by_stem:
            return tail
        if tail in idx.lookup:
            return idx.lookup[tail]
        if norm(tail) in idx.lookup:
            return idx.lookup[norm(tail)]
    if norm(name) in idx.lookup:
        return idx.lookup[norm(name)]
    return None


def run_fix(root: Path, dry_run: bool = False) -> dict:
    """执行链接修复（可编程入口 —— 服务层增量更新复用，不走 argparse）。

    输出形态：`[[<目标文件名>|<显示文本>]]`（Obsidian 可解析）。
    返回统计：total/ok/fixed/redir/dropped/files_changed/pages/titles/redirects + 样例。
    """
    idx, files = collect_pages(root)
    redirects = collect_redirects(root)
    idx.redirect = redirects
    for old, stem in redirects.items():         # 旧标题也进索引（让第 ③ 步可命中）
        if old and stem:
            idx.add(old, stem)
    idx.finalize()

    n_total = n_ok = n_fixed = n_dropped = n_redir = 0
    fixed_samples: list[tuple[str, str]] = []
    dropped_samples: list[str] = []
    n_files_changed = 0

    for f in files:
        text = f.read_text(encoding="utf-8")
        if "[[" not in text:
            continue
        changed = False

        def repl(m: re.Match) -> str:
            nonlocal n_total, n_ok, n_fixed, n_dropped, n_redir, changed
            n_total += 1
            raw = m.group(1)
            name, sep, disp = raw.partition("|")
            name, disp = name.strip(), (disp.strip() or name.strip())
            stem = resolve(name, idx, redirects)
            if stem is None:
                n_dropped += 1
                if len(dropped_samples) < 15:
                    dropped_samples.append(name)
                changed = True
                return disp                     # 保留文字、去掉链接标记
            want = "[[%s|%s]]" % (stem, disp)
            if want == m.group(0):
                n_ok += 1                       # 已经是正确形态
                return m.group(0)
            n_fixed += 1
            if redirects.get(name) == stem or redirects.get(norm(name)) == stem:
                n_redir += 1
            if len(fixed_samples) < 15:
                fixed_samples.append((m.group(0), want))
            changed = True
            return want

        new = LINK_RE.sub(repl, text)
        if changed and not dry_run:
            f.write_text(new, encoding="utf-8", newline="")
        if changed:
            n_files_changed += 1

    return {"total": n_total, "ok": n_ok, "fixed": n_fixed, "redir": n_redir,
            "dropped": n_dropped, "files_changed": n_files_changed,
            "pages": len(files), "titles": len(idx.title_of),
            "redirects": len(redirects),
            "fixed_samples": fixed_samples, "dropped_samples": dropped_samples}


def main() -> int:
    ap = argparse.ArgumentParser(
        description="修复 wiki 产出的 [[…]] 链接（输出 Obsidian 可解析的 文件名|显示 形态）")
    ap.add_argument("--dir", required=True, help="产出目录（页面 md 所在，即 Obsidian vault）")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不改文件")
    args = ap.parse_args()

    root = Path(args.dir)
    if not root.is_dir():
        print("目录不存在：%s" % root)
        return 2

    r = run_fix(root, dry_run=args.dry_run)
    print("页面 %d 个 / 标题 %d 个 / 合并重定向 %d 条"
          % (r["pages"], r["titles"], r["redirects"]))
    print()
    print("链接引用：%d 处（涉及 %d 个文件）" % (r["total"], r["files_changed"]))
    print("  原本就正确      ：%d" % r["ok"])
    print("  自动修复        ：%d（其中按合并关系重定向 %d）" % (r["fixed"], r["redir"]))
    print("  无法确定（去链）：%d（%.1f%%）"
          % (r["dropped"], 100.0 * r["dropped"] / max(r["total"], 1)))
    if r["fixed_samples"]:
        print()
        print("修复样例：")
        for a, b in r["fixed_samples"]:
            print("  %s  →  %s" % (a, b))
    if r["dropped_samples"]:
        print()
        print("去链样例（歧义或无对应页）：")
        for a in r["dropped_samples"]:
            print("  %s" % a)
    if args.dry_run:
        print()
        print("（--dry-run：未写回文件）")
    print()
    print("验收（Obsidian 权威口径）：python scripts/obsidian.py unresolved")
    return 0


if __name__ == "__main__":
    sys.exit(main())
