"""Obsidian 官方 CLI 的薄封装 —— wiki 产物的**权威验收工具**。

## 为什么需要它

wiki 产物最终是**给人看的**（Obsidian 视图），而 `[[…]]` 的解析规则只有 Obsidian
自己说了算。实测教训（2026-09-26）：我们按"别名应该能解析"的直觉判断，得出了
"1136 条链接 100% 可解析"的**错误结论**；用 Obsidian 自己的 CLI 一问，`unresolved`
报 **228 个目标** —— 全是死链，点击就新建空白笔记。

所以：**别再猜，直接问它。**

## 用法

    python scripts/obsidian.py unresolved            # 未解析链接清单（权威）
    python scripts/obsidian.py unresolved --json
    python scripts/obsidian.py check                 # 验收：未解析必须为 0，否则退出码 1
    python scripts/obsidian.py eval "app.vault.getFiles().length"
    python scripts/obsidian.py reload                # 让 Obsidian 重新索引产物

前置：Obsidian 必须**正在运行**，且在「设置 → 通用」里开启了命令行界面
（要求安装器 1.12.7+）。CLI 路径按 `OBSIDIAN_CLI` 环境变量 → `PATH` 顺序解析；
vault 默认取配置里的 `wiki.out_l2`。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

#: 解析 CLI 可执行文件时的候选名（Windows 上官方会用 .com 终端重定向器）
_CLI_NAMES = ("obsidian", "Obsidian.com", "obsidian.exe")


def find_cli(explicit: str = "") -> str:
    """定位 Obsidian CLI：显式参数 → `OBSIDIAN_CLI` 环境变量 → PATH。"""
    if explicit:
        return explicit
    env = os.environ.get("OBSIDIAN_CLI", "").strip()
    if env:
        return env
    for name in _CLI_NAMES:
        p = shutil.which(name)
        if p:
            return p
    raise FileNotFoundError(
        "找不到 Obsidian CLI。确认：① Obsidian 正在运行；② 设置→通用 已开启"
        "「命令行界面」；③ 已注册 PATH（或设 OBSIDIAN_CLI 指向可执行文件）")


def default_vault() -> str:
    """默认 vault 名 = 配置 `wiki.out_l2` 的**目录名**。

    ⚠️ CLI 的 `vault=` 要的是 **vault 名/id**，不是路径 —— 传路径会得到
    "Vault not found."（实测踩过）。Obsidian 注册 vault 时用的就是文件夹名，
    所以取 basename 即可。
    """
    try:
        from agentmemhub import config
        p = str(config.config().wiki.get("out_l2") or "")
        return Path(p).name if p else ""
    except Exception:                       # noqa: BLE001
        return ""


def as_vault_name(v: str) -> str:
    """把用户给的 vault（可能是路径）归一成名字。"""
    v = (v or "").strip()
    if not v:
        return ""
    return Path(v).name if ("/" in v or "\\" in v) else v


def run(*cmd: str, cli: str = "", vault: str = "", timeout: int = 120) -> str:
    """执行一条 Obsidian CLI 命令，返回 stdout（失败抛 RuntimeError 带 stderr）。"""
    exe = find_cli(cli)
    args = [exe]
    if vault:
        args.append("vault=%s" % vault)
    args.extend(cmd)
    try:
        p = subprocess.run(args, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Obsidian CLI 超时（%ds）：%s" % (timeout, " ".join(cmd)))
    if p.returncode != 0:
        raise RuntimeError("Obsidian CLI 退出码 %d：%s\n%s"
                           % (p.returncode, " ".join(cmd), (p.stderr or "").strip()))
    return (p.stdout or "").strip()


def strip_arrow(out: str) -> str:
    """`eval` 的输出带 `=> ` 前缀，去掉它。"""
    return out[3:].strip() if out.startswith("=>") else out


# ---------------------------------------------------------------------------
# 常用动作
# ---------------------------------------------------------------------------

def unresolved(*, cli: str = "", vault: str = "") -> list[dict]:
    """未解析链接（Obsidian 权威口径）。返回 [{link, count, sources}]。

    ⚠️ CLI 的输出**带 UTF-8 BOM**（实测）—— 直接 `json.loads` 会失败，
    于是静默走到兜底分支、把 `count` 变成非数字，后面求和就炸。这里显式剥 BOM。
    """
    out = run("unresolved", "verbose", "format=json", cli=cli, vault=vault)
    out = out.lstrip("\ufeff").strip()
    if not out:
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return [{"link": out[:200], "count": 0, "sources": "", "raw": True}]
    out_list = []
    for it in data:
        try:
            cnt = int(it.get("count") or 0)
        except (TypeError, ValueError):
            cnt = 0
        out_list.append({
            "link": it.get("link", ""),
            "count": cnt,
            "sources": it.get("sources", ""),
        })
    return out_list


def eval_js(code: str, *, cli: str = "", vault: str = "") -> str:
    """在 Obsidian 里执行 JS 并返回结果（排查解析规则的最强手段）。"""
    return strip_arrow(run("eval", "code=%s" % code, cli=cli, vault=vault))


def reload_app(*, cli: str = "", vault: str = "") -> None:
    """重载 Obsidian 窗口（产物整目录换过之后，让它重新索引）。"""
    try:
        run("reload", cli=cli, vault=vault, timeout=30)
    except Exception:                       # noqa: BLE001 —— reload 偶尔无输出
        pass


def check(*, cli: str = "", vault: str = "", limit: int = 15) -> int:
    """验收：未解析链接必须为 **0**。返回退出码（0=通过，1=不通过）。"""
    try:
        bad = unresolved(cli=cli, vault=vault)
    except Exception as e:                  # noqa: BLE001
        print("⚠️ 无法查询 Obsidian（它没运行 / CLI 没注册？）：%s" % e)
        return 2
    total = sum(x["count"] for x in bad)
    if not bad:
        print("✓ wiki 链接验收通过：Obsidian 未解析链接 = 0")
        return 0
    print("✗ wiki 链接验收不通过：%d 个未解析目标 / %d 条链接"
          % (len(bad), total))
    print("  （这些链接点击会新建空白笔记 —— 产物是给人看的，必须为 0）")
    for x in sorted(bad, key=lambda y: -y["count"])[:limit]:
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

    vault = as_vault_name(args.vault or default_vault())
    try:
        if args.action == "check":
            return check(cli=args.cli, vault=vault)
        if args.action == "unresolved":
            bad = unresolved(cli=args.cli, vault=vault)
            if args.json:
                print(json.dumps(bad, ensure_ascii=False, indent=1))
            else:
                total = sum(x["count"] for x in bad)
                print("未解析链接：%d 个目标 / %d 条" % (len(bad), total))
                for x in sorted(bad, key=lambda y: -y["count"]):
                    print("  x%-3d %s" % (x["count"], x["link"][:70]))
            return 0
        if args.action == "eval":
            if not args.rest:
                print("eval 需要一段 JS：python scripts/obsidian.py eval \"app.vault.getFiles().length\"")
                return 2
            print(eval_js(args.rest[0], cli=args.cli, vault=vault))
            return 0
        if args.action == "reload":
            reload_app(cli=args.cli, vault=vault)
            print("已请求 Obsidian 重载")
            return 0
        print(run(args.action, *args.rest, cli=args.cli, vault=vault))
        return 0
    except Exception as e:                   # noqa: BLE001
        print("%s" % e)
        return 2


if __name__ == "__main__":
    sys.exit(main())
