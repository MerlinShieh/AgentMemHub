# -*- coding: utf-8 -*-
"""前端 ↔ 后端接口对照审计：哪些 HTTP 接口面板**没有**调用。

为什么要这个脚本
================
面板前端是**单文件**（`agentmemhub/web/static/index.html`），后端却有 60 个路由 ——
"哪些已就绪、但前端还没接"是**每次做前端改造都会问的问题**，靠人肉对比必然漏。
2026-10-16 实测：60 个路由里面板只调了 18 个，未接的 42 个里有**三块完整能力**
（feed 12 个 · wiki 16 个 · snapshots 3 个），全都没接。

⚠️ **静态扫描一定有漏网，结论必须交叉验证**（这是实测踩到的）
==========================================================
1. URL 写在变量里（`` const u = `/api/…`; fetch(u) ``）—— 正则匹配不到；
2. 前端用**模板串**拼路径，且常嵌函数调用：
   `` `/api/conversations/${encodeURIComponent(s)}/${c}/events` ``。
   必须在抓取**之前**把整段 `${...}` 归一成 `{}` —— 否则会在 `(` 处截断，
   把"其实在用"的接口报成未调用（本脚本第一版就误报了会话详情/删除/改标题三个）。
所以：**关键接口再用关键字 grep 复核一次**，不要只信本脚本。

用法
====
    python scripts/audit_frontend_api.py           # 人读（按域分组）
    python scripts/audit_frontend_api.py --used    # 连"面板已用"一起列
    python scripts/audit_frontend_api.py --json    # 机器读（给看板/CI 用）
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "agentmemhub" / "web" / "app.py"
HTML = ROOT / "agentmemhub" / "web" / "static" / "index.html"

#: 前端抓路径用的字符集（模板串已归一成 `{}`，所以只留 `{}` 两个花括号）
_PATH_RE = re.compile(r"/api/[A-Za-z0-9_\-/{}.]{1,}")
_TEMPLATE_RE = re.compile(r"\$\{[^}]*\}")
_ROUTE_RE = re.compile(r"@app\.(get|post|put|delete|patch)\(\s*[\"']([^\"']+)")


def _segs(path: str) -> list[str]:
    """路径 → 段列表，`{param}` 归一成 `{}`（两边同规则才能逐段比对）。"""
    return ["{}" if s.startswith("{") else s for s in path.strip("/").split("/")]


def routes() -> list[tuple[str, str]]:
    """后端路由 `[(方法, 路径)]`（顺序即源码顺序）。"""
    text = APP.read_text(encoding="utf-8")
    return [(m.upper(), p) for m, p in _ROUTE_RE.findall(text)]


def front_paths() -> list[str]:
    """前端出现过的 `/api/...` 路径（模板串已归一）。"""
    html = HTML.read_text(encoding="utf-8")
    html = _TEMPLATE_RE.sub("{}", html)
    out = {c.rstrip("/") for c in _PATH_RE.findall(html) if len(c) > 4}
    return sorted(out)


def audit() -> dict:
    fsegs = [_segs(c) for c in front_paths()]

    def used(path: str) -> bool:
        want = _segs(path)
        return any(len(want) == len(got)
                   and all(a == b or b == "{}" for a, b in zip(want, got))
                   for got in fsegs)

    items = [{"method": m, "path": p,
              "group": "/".join(p.split("/")[:3]), "frontend": used(p)}
             for m, p in routes()]
    return {
        "total": len(items),
        "used": sum(1 for i in items if i["frontend"]),
        "unused": sum(1 for i in items if not i["frontend"]),
        "items": items,
        "frontendPaths": front_paths(),
    }


def render(rep: dict, *, show_used: bool = False) -> str:
    lines = ["后端 HTTP 路由 %d 个 · 面板已用 %d · **未用 %d**"
             % (rep["total"], rep["used"], rep["unused"]), ""]
    groups: dict[str, list] = {}
    for it in rep["items"]:
        groups.setdefault(it["group"], []).append(it)
    for g in sorted(groups):
        rows = [i for i in groups[g] if show_used or not i["frontend"]]
        if not rows:
            continue
        lines.append("## %s" % g)
        for i in rows:
            lines.append("   %-7s %-52s %s"
                         % (i["method"], i["path"],
                            "面板已用" if i["frontend"] else "面板未用"))
        lines.append("")
    lines.append("（--used 可连同已用的一起列；结论请交叉验证，见脚本 docstring）")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="前端 ↔ 后端接口对照审计")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    ap.add_argument("--used", action="store_true", help="连同面板已用的接口一起列")
    args = ap.parse_args()
    rep = audit()
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(render(rep, show_used=args.used))
    return 0


if __name__ == "__main__":
    sys.exit(main())
