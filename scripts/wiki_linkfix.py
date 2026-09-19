"""修复 wiki 产出里的 `[[标题]]` 死链（第二级收尾）。

问题
====
第一级实测死链率 15.5%：LLM 在 `related` 里写的是**实体名**（如 `AgentMemHub`），
而实际页面标题是「AgentMemHub 数据库概览与表清单」。第二级虽然被要求在
"已有页面标题清单"里选，仍会写出措辞不一致的名字。

策略（保守——只做**能唯一确定目标**的修复）
==========================================
  1. 精确命中            → 保留
  2. 规范化后唯一命中    → 替换（去标点 / 空白 / 大小写差异）
  3. 一方包含另一方且唯一 → 替换
  4. 其余                → **保留文字、去掉链接标记**（`[[X]]` → `X`），
                          并计入报告

**不猜**：一个有歧义的名字（比如 `AgentMemHub` 对应二十多个标题）如果强行
指向某个页面，会把读者引到不相关的地方——比留一个死链更糟。

用法
====
    python scripts/wiki_linkfix.py --dir 产出目录 --dry-run   # 先看报告
    python scripts/wiki_linkfix.py --dir 产出目录
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
REL_LINE = re.compile(r"^\*\*相关\*\*：(.+)$", re.M)
#: 标题里的噪音字符（中英文标点、空白、连接符）——比对时全部忽略
_NOISE = re.compile(r"[\s\-_·—–、，。：:；;！!？?（）()【】\[\]{}《》\"'“”‘’/\\|]+")


def norm(s: str) -> str:
    return _NOISE.sub("", s or "").lower()


def collect_titles(root: Path) -> tuple[set[str], list[Path]]:
    """收集产出目录里的所有页面标题与页面文件。

    **两级产出的 frontmatter `title` 含义不同**：第二级是页面标题（一文件一页），
    第一级是"发起 / 会话 id"（一文件多页，页面标题在正文 `# ` 里）。
    所以两边都要收，否则拿第一级产出跑会一个页面标题都收不到。
    """
    titles: set[str] = set()
    files: list[Path] = []
    for f in sorted(root.rglob("*.md")):
        if f.name == "index.md":
            continue
        files.append(f)
        t = f.read_text(encoding="utf-8")
        m = re.search(r"^title:\s*(.+)$", t, re.M)
        if m:
            titles.add(m.group(1).strip())
        for h in re.findall(r"^# (.+)$", t, re.M):
            titles.add(h.strip())
    return titles, files


def collect_redirects(root: Path) -> dict[str, str]:
    """建立 **被合并页标题 → 合并后页标题** 的重定向表。

    这是第二级死链的主因：聚合会把第一级的多个页面合并成一页，于是正文里
    指向**第一级标题**的链接全部失效。第一级实测死链率 15.5%，第二级反而
    飙到 92.6%（864/933）——不是模型写得差，而是它引用的页面在聚合后已经
    不存在了。好在 json 的 `from_titles` 记下了每个最终页合并了哪些源页，
    据此就能把链接重定向过去。
    """
    out: dict[str, str] = {}
    for f in sorted(root.rglob("*.json")):
        if f.name.startswith("_"):
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        title = (d.get("title") or "").strip()
        if not title:
            continue
        for t in (d.get("from_titles") or []):
            t = (t or "").strip()
            if t and t != title:
                out.setdefault(t, title)
    return out


def build_index(titles: set[str]) -> dict[str, set[str]]:
    idx: dict[str, set[str]] = defaultdict(set)
    for t in titles:
        idx[norm(t)].add(t)
    return idx


def resolve(name: str, titles: set[str], idx: dict[str, set[str]],
            redirects: dict[str, str] | None = None) -> str | None:
    """把一个 `[[name]]` 解析成确定的目标标题；不确定返回 None。"""
    if name in titles:
        return name
    # 被合并掉的旧页标题：重定向到合并后的页面
    r = (redirects or {}).get(name)
    if r and r in titles:
        return r
    n = norm(name)
    if not n:
        return None
    hit = idx.get(n)
    if hit and len(hit) == 1:
        return next(iter(hit))
    # 一方包含另一方，且候选唯一
    if len(n) >= 4:
        cand = {t for t in titles if n in norm(t) or norm(t) in n}
        if len(cand) == 1:
            return next(iter(cand))
    return None


def run_fix(root: Path, dry_run: bool = False) -> dict:
    """执行链接修复（可编程入口 —— 服务层增量更新复用，不走 argparse）。

    返回统计：total/ok/fixed/redir/dropped/files_changed + 样例。
    """
    titles, files = collect_titles(root)
    idx = build_index(titles)
    redirects = collect_redirects(root)

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
            name = m.group(1).strip()
            tgt = resolve(name, titles, idx, redirects)
            if tgt is None:
                n_dropped += 1
                if len(dropped_samples) < 15:
                    dropped_samples.append(name)
                changed = True
                return name                  # 保留文字、去掉链接标记
            if tgt == name:
                n_ok += 1
                return m.group(0)
            n_fixed += 1
            if redirects.get(name) == tgt:
                n_redir += 1
            if len(fixed_samples) < 15:
                fixed_samples.append((name, tgt))
            changed = True
            return "[[%s]]" % tgt

        new = LINK_RE.sub(repl, text)
        if changed and not dry_run:
            f.write_text(new, encoding="utf-8")
        if changed:
            n_files_changed += 1

    return {"total": n_total, "ok": n_ok, "fixed": n_fixed, "redir": n_redir,
            "dropped": n_dropped, "files_changed": n_files_changed,
            "pages": len(files), "titles": len(titles),
            "redirects": len(redirects),
            "fixed_samples": fixed_samples, "dropped_samples": dropped_samples}


def main() -> int:
    ap = argparse.ArgumentParser(description="修复 wiki 产出里的 [[标题]] 死链")
    ap.add_argument("--dir", required=True, help="产出目录（页面 md 所在）")
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
    print("相关链接引用：%d 处（涉及 %d 个文件）" % (r["total"], r["files_changed"]))
    print("  原本就有效      ：%d" % r["ok"])
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
