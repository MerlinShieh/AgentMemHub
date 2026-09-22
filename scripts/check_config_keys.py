# -*- coding: utf-8 -*-
"""配置键审计：找出「定义了却没有任何读取点」的僵尸键。

为什么需要它
============
本项目的配置读取有一类**沉默的失效**：键在 `agentmemhub.yaml.example` 与
`DEFAULT_*` 里都存在，代码却从不读它。它不抛异常、不让测试变红，唯一的表现是
"改了配置没反应" —— 而如果**硬编码的默认值恰好等于配置里的值**，连"改了没反应"
都发现不了。本项目已经踩过三次同一个坑：

    `wiki.single_shot_max`（脚本用硬编码常量，默认值恰好一致）
    `wiki.workers`（`compile_all` 的 `workers=4` + `wiki.py` 的 `workers or 4`）
    `wiki.l2.workers` / `min_pages` / `domain_max`（脚本只认 argparse）
    `wiki.enabled`（**假开关**，没有任何消费点）

所以这个脚本是**回归防线**：加配置键时跑一下，别让它悄悄失效。

判定方式（以及它的边界）
========================
对每个叶子键名，在 `agentmemhub/` 与 `scripts/` 的源码里找**字符串字面量**出现。

关键取舍 ①：`agentmemhub/config.py` **不能整个排除**（那样 `data_dir` / `db_path` /
`memos.home` 这类"读取点就在 Config 属性内部"的键会被误报成僵尸），也**不能整个
包含**（`DEFAULT_WIKI = {"single_shot_max": 12}` 这种**默认值定义**会让每个键都
"命中"）。正确做法是**只剔除 `DEFAULT_* = {...}` 定义块**，其余照常匹配。

关键取舍 ②：这是**启发式**，不是证明。短键名（`enabled` / `workers` / `llm`）可能
命中无关上下文造成假阳性，所以输出一律称「疑似」，由人复核。

用法
====
    uv run python scripts/check_config_keys.py            # 人读
    uv run python scripts/check_config_keys.py --json     # 机器读
    uv run python scripts/check_config_keys.py --strict   # 有僵尸键则退出码 1（接 CI）
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

#: 键的来源与"被检查对象"：这两个文件本身不含读取逻辑，必须排除。
#: 本脚本自身也要排除 —— 它的 docstring 里举了键名当例子，会自己命中自己。
EXAMPLE = "agentmemhub.yaml.example"
LOCAL_YAML = "agentmemhub.yaml"
SKIP = {EXAMPLE, LOCAL_YAML, "scripts/check_config_keys.py"}

#: 源码扫描范围
SRC_GLOBS = ("agentmemhub/**/*.py", "scripts/**/*.py")

#: `DEFAULT_XXX = {` —— 默认值定义块的起点（块内是"定义"，不是"读取证据"）
_DEFAULT_BLOCK = re.compile(r"^DEFAULT_[A-Z0-9_]+\s*=\s*\{", re.M)


def leaves(node: Any, prefix: str = "") -> list[str]:
    """把嵌套 dict 摊平成叶子键路径列表（`{"a": {"b": 1}}` → `["a.b"]`）。"""
    out: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            p = "%s.%s" % (prefix, k) if prefix else str(k)
            if isinstance(v, dict):
                out.extend(leaves(v, p))
            else:
                out.append(p)
    return out


def load_keys(path: Path | str) -> list[str]:
    """读 yaml 并摊平成叶子键路径；文件不存在则返回空列表。"""
    p = Path(path)
    if not p.exists():
        return []
    import yaml
    return leaves(yaml.safe_load(p.read_text(encoding="utf-8")) or {})


def strip_default_blocks(text: str) -> str:
    """删掉 `DEFAULT_XXX = {...}` 整块，只留"读取证据"部分。

    这里是本脚本**最容易被写错**的一处：若把 `config.py` 整体排除，那么读取点
    就在 `Config` 属性内部的键（`self._get("data_dir", "")`）会被误报；若整体
    包含，`DEFAULT_*` 里的 `"键": 默认值` 又会让**每一个**键都"命中"。
    所以按块删除 —— 需要按大括号计数，因为 `DEFAULT_LOGS` 这类是嵌套的。
    """
    kept: list[str] = []
    depth = 0
    skipping = False
    for ln in text.splitlines(keepends=True):
        if not skipping and _DEFAULT_BLOCK.match(ln):
            skipping = True
            depth = 0
        if skipping:
            depth += ln.count("{") - ln.count("}")
            if depth <= 0:
                skipping = False
            continue
        kept.append(ln)
    return "".join(kept)


def collect_sources(root: Path | str = ROOT) -> dict[str, str]:
    """收集扫描源码：{相对路径: 文本}；`config.py` 已剔除 `DEFAULT_*` 定义块。"""
    root = Path(root)
    out: dict[str, str] = {}
    for pat in SRC_GLOBS:
        for f in sorted(root.glob(pat)):
            rel = f.relative_to(root).as_posix()
            if rel in SKIP:
                continue
            text = f.read_text(encoding="utf-8", errors="replace")
            if rel == "agentmemhub/config.py":
                text = strip_default_blocks(text)
            out[rel] = text
    return out


def find_reads(leaf: str, sources: dict[str, str]) -> list[str]:
    """找出"以字符串字面量提到过该叶子键名"的文件（启发式）。"""
    pat = re.compile(r"""["']%s["']""" % re.escape(leaf))
    return sorted(rel for rel, text in sources.items() if pat.search(text))


def audit(root: Path | str = ROOT, *, extra_yaml: Path | str | None = None
          ) -> dict[str, Any]:
    """审计 example（支持的键）与本地 yaml（实际配置）。

    返回：{"<标签>": {"keys": [...], "dead": [...], "reads": {key: [文件]}}}
    """
    root = Path(root)
    sources = collect_sources(root)
    targets = [("example（支持的键）", root / EXAMPLE),
               ("本地 yaml（当前实际配置）", Path(extra_yaml) if extra_yaml
                else root / LOCAL_YAML)]
    report: dict[str, Any] = {}
    for label, path in targets:
        keys = load_keys(path)
        reads = {k: find_reads(k.split(".")[-1], sources) for k in keys}
        report[label] = {
            "path": str(path),
            "exists": Path(path).exists(),
            "keys": keys,
            "dead": [k for k in keys if not reads[k]],
            "reads": reads,
        }
    return report


def render(report: dict[str, Any]) -> str:
    """人读格式。"""
    lines: list[str] = []
    total_dead = 0
    for label, r in report.items():
        if not r["exists"]:
            lines.append("=== %s === （文件不存在，跳过：%s）" % (label, r["path"]))
            lines.append("")
            continue
        lines.append("=== %s === %d 个叶子键（%s）"
                     % (label, len(r["keys"]), r["path"]))
        for k in r["keys"]:
            hits = r["reads"][k]
            if hits:
                lines.append("  OK  %-36s ← %s" % (k, ", ".join(hits[:2])))
            else:
                lines.append("  ??  %-36s ← 【无任何读取点，疑似僵尸键】" % k)
        lines.append("  ---- 疑似僵尸键 %d 个：%s" % (len(r["dead"]), r["dead"]))
        lines.append("")
        total_dead += len(r["dead"])
    lines.append("合计疑似僵尸键：%d" % total_dead)
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="配置键审计：找出定义了却没有任何读取点的僵尸键")
    ap.add_argument("--root", default=str(ROOT), help="项目根（默认脚本上级目录）")
    ap.add_argument("--yaml", default="", help="额外检查一个 yaml（默认本地 agentmemhub.yaml）")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    ap.add_argument("--strict", action="store_true",
                    help="存在疑似僵尸键时退出码 1（供 CI 使用）")
    args = ap.parse_args()

    report = audit(args.root, extra_yaml=args.yaml or None)
    dead = [k for r in report.values() for k in r["dead"]]
    if args.json:
        print(json.dumps({"report": report, "dead": dead},
                         ensure_ascii=False, indent=2))
    else:
        print(render(report))
    return 1 if (args.strict and dead) else 0


if __name__ == "__main__":
    sys.exit(main())
