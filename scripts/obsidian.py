"""Obsidian 官方 CLI 的命令行入口（**逻辑在 `agentmemhub/obsidian.py`**）。

wiki 产物是给人看的（Obsidian 视图），而 `[[…]]` 能不能解析只有 Obsidian 说了算
—— 所以**别再猜**，直接问它。实测教训见 `agentmemhub/obsidian.py` 的模块注释。

用法
====
    python scripts/obsidian.py unresolved            # 未解析链接清单（权威）
    python scripts/obsidian.py check                 # 验收：未解析必须为 0，否则退出码 1
    python scripts/obsidian.py eval "app.vault.getFiles().length"
    python scripts/obsidian.py reload                # 让 Obsidian 重新索引产物
    python scripts/obsidian.py files                 # 透传任意 CLI 子命令
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                       # noqa: BLE001
        pass

from agentmemhub import obsidian  # noqa: E402


def check(*, cli: str, vault: str, limit: int = 15) -> int:
    """验收：未解析链接必须为 **0**。返回退出码（0=通过，1=不通过，2=查不了）。"""
    r = obsidian.verify(cli=cli, vault=vault)
    if not r["available"]:
        print("⚠️ 无法查询 Obsidian（没运行 / CLI 没注册）：%s" % r["error"])
        return 2
    if r["ok"]:
        print("✓ wiki 链接验收通过：Obsidian 未解析链接 = 0")
        return 0
    print("✗ wiki 链接验收不通过：%d 个未解析目标 / %d 条链接"
          % (r["total_targets"], r["total_links"]))
    print("  （这些链接点击会新建空白笔记 —— 产物是给人看的，必须为 0）")
    for x in r["items"][:limit]:
        print("    x%-3d %s" % (x["count"], x["link"][:70]))
    print()
    print("  修法：python scripts/wiki_linkfix.py --dir <产出目录>")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Obsidian 官方 CLI 封装（wiki 产物验收）")
    ap.add_argument("action",
                    choices=["unresolved", "check", "eval", "reload", "files",
                             "vaults", "links", "backlinks", "aliases"],
                    help="check=验收（未解析必须为 0）；unresolved=列未解析链接")
    ap.add_argument("rest", nargs="*", help="透传给 Obsidian CLI 的其余参数")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出")
    ap.add_argument("--cli", default="", help="Obsidian CLI 可执行文件（默认查环境/PATH）")
    ap.add_argument("--vault", default="", help="vault 名（默认取配置 wiki.out_l2）")
    args = ap.parse_args()

    vault = obsidian.as_vault_name(args.vault or obsidian.default_vault())
    try:
        if args.action == "check":
            return check(cli=args.cli, vault=vault)
        if args.action == "unresolved":
            bad = obsidian.unresolved(cli=args.cli, vault=vault)
            if args.json:
                print(json.dumps(bad, ensure_ascii=False, indent=1))
            else:
                print("未解析链接：%d 个目标 / %d 条"
                      % (len(bad), sum(x["count"] for x in bad)))
                for x in sorted(bad, key=lambda y: -y["count"]):
                    print("  x%-3d %s" % (x["count"], x["link"][:70]))
            return 0
        if args.action == "eval":
            if not args.rest:
                print('eval 需要一段 JS：python scripts/obsidian.py eval '
                      '"app.vault.getFiles().length"')
                return 2
            print(obsidian.eval_js(args.rest[0], cli=args.cli, vault=vault))
            return 0
        if args.action == "reload":
            obsidian.reload_app(cli=args.cli, vault=vault)
            print("已请求 Obsidian 重载")
            return 0
        print(obsidian.run(args.action, *args.rest, cli=args.cli, vault=vault))
        return 0
    except Exception as e:                   # noqa: BLE001
        print("%s" % e)
        return 2


if __name__ == "__main__":
    sys.exit(main())
