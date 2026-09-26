"""Obsidian 官方 CLI 的薄封装 —— wiki 产物的**权威验收**通道。

## 为什么需要它

wiki 产物最终是**给人看的**（Obsidian 视图），而 `[[…]]` 到底能不能解析，
只有 Obsidian 自己说了算。实测教训（2026-09-26）：我们按"别名应该能解析"的
直觉判断，得出"1136 条链接 100% 可解析"的**错误结论**；用 Obsidian 自己的 CLI
一问，`unresolved` 报 **228 个目标** —— 全是死链，**点击就新建一个空白笔记**。

所以：**别再猜，直接问它**。产物流程收尾会调 `check()`，不通过就在更新结果里
报警（见 `agentmemhub/wiki.py`）。

## 前置条件与降级

需要：Obsidian **正在运行** + 安装器 1.12.7+ + 设置→通用 已开启「命令行界面」。
任何一步不满足都会抛 `ObsidianUnavailable` —— 调用方**必须旁路处理**
（验收是旁路，绝不能因为它把编译结果搞挂）。

CLI 路径解析顺序：显式参数 → `OBSIDIAN_CLI` 环境变量 → `PATH`。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

#: 解析 CLI 可执行文件时的候选名（Windows 上官方用 .com 终端重定向器）
_CLI_NAMES = ("obsidian", "Obsidian.com", "obsidian.exe")


class ObsidianUnavailable(RuntimeError):
    """Obsidian CLI 不可用（没运行 / 没注册 / 没开开关）—— 调用方应旁路。"""


def find_cli(explicit: str = "") -> str:
    """定位 Obsidian CLI：显式参数 → `OBSIDIAN_CLI` → PATH。"""
    if explicit:
        return explicit
    env = os.environ.get("OBSIDIAN_CLI", "").strip()
    if env:
        return env
    for name in _CLI_NAMES:
        p = shutil.which(name)
        if p:
            return p
    raise ObsidianUnavailable(
        "找不到 Obsidian CLI。确认：① Obsidian 正在运行；② 设置→通用 已开启"
        "「命令行界面」；③ 已注册 PATH（或设 OBSIDIAN_CLI 指向可执行文件）")


def default_vault() -> str:
    """默认 vault 名 = 配置 `wiki.out_l2` 的**目录名**。

    ⚠️ CLI 的 `vault=` 要的是 **vault 名/id**，不是路径 —— 传路径会得到
    "Vault not found."（实测踩过）。Obsidian 注册 vault 时用的就是文件夹名。
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
    """执行一条 Obsidian CLI 命令，返回 stdout。"""
    exe = find_cli(cli)
    args = [exe]
    if vault:
        args.append("vault=%s" % vault)
    args.extend(cmd)
    try:
        p = subprocess.run(args, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except FileNotFoundError as e:
        raise ObsidianUnavailable("无法执行 Obsidian CLI：%s" % e)
    except subprocess.TimeoutExpired:
        raise ObsidianUnavailable("Obsidian CLI 超时（%ds）：%s"
                                  % (timeout, " ".join(cmd)))
    if p.returncode != 0:
        raise ObsidianUnavailable("Obsidian CLI 退出码 %d：%s\n%s"
                                  % (p.returncode, " ".join(cmd),
                                     (p.stderr or "").strip()))
    return (p.stdout or "").strip()


def strip_arrow(out: str) -> str:
    """`eval` 的输出带 `=> ` 前缀，去掉它。"""
    return out[3:].strip() if out.startswith("=>") else out


def unresolved(*, cli: str = "", vault: str = "", timeout: int = 120) -> list[dict]:
    """未解析链接（Obsidian 权威口径）。返回 [{link, count, sources}]。

    ⚠️ CLI 的输出**带 UTF-8 BOM**（实测）—— 直接 `json.loads` 会失败，于是静默
    走到兜底分支、把 `count` 变成非数字。这里显式剥 BOM。
    """
    out = run("unresolved", "verbose", "format=json", cli=cli, vault=vault,
              timeout=timeout)
    out = out.lstrip("\ufeff").strip()
    # 没有未解析链接时 CLI 返回的是**一句英文**（不是 `[]`）——
    # 当成失败会误报（实测踩过：明明已 0，却报"1 个目标 / 0 条"）
    if not out or out.lower().startswith("no unresolved"):
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return [{"link": out[:200], "count": 0, "sources": "", "raw": True}]
    rows = []
    for it in data:
        try:
            cnt = int(it.get("count") or 0)
        except (TypeError, ValueError):
            cnt = 0
        rows.append({"link": it.get("link", ""), "count": cnt,
                     "sources": it.get("sources", "")})
    return rows


def eval_js(code: str, *, cli: str = "", vault: str = "") -> str:
    """在 Obsidian 里执行 JS 并返回结果（排查解析规则的最强手段）。"""
    return strip_arrow(run("eval", "code=%s" % code, cli=cli, vault=vault))


def reload_app(*, cli: str = "", vault: str = "") -> None:
    """重载 Obsidian 窗口（产物整目录换过之后，让它重新索引）。"""
    try:
        run("reload", cli=cli, vault=vault, timeout=30)
    except Exception:                       # noqa: BLE001 —— reload 偶尔无输出
        pass


def app_running(timeout: int = 20) -> bool:
    """Obsidian 桌面端是否**已在运行**。

    为什么要问：官方 CLI 在 Obsidian 未运行时**会把它启动起来**（官方文档明说
    "If Obsidian is not running, the first command you run launches Obsidian"）。
    而产物流程是**自动**跑的 —— 不能让一次编译把编辑器窗口弹出来。所以自动验收
    只在它本来开着时才做；没开就记"未验收"（**不是失败**）。
    """
    try:
        if os.name == "nt":
            p = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Obsidian.exe"],
                               capture_output=True, text=True, timeout=timeout)
            return "Obsidian.exe" in (p.stdout or "")
        p = subprocess.run(["pgrep", "-x", "obsidian"], capture_output=True,
                           text=True, timeout=timeout)
        return p.returncode == 0
    except Exception:                       # noqa: BLE001 —— 探测不了就别拦着
        return True


def verify(*, cli: str = "", vault: str = "", timeout: int = 180,
           require_running: bool = True) -> dict:
    """产物链接验收（给流程调用的**不抛异常**版本）。

    返回 `{ok, available, total_targets, total_links, items, error}`：
      · `available=False` → Obsidian/CLI 不可用（调用方应记为"未验收"，不是失败）
      · `ok=True`         → 未解析为 0（**这是唯一的通过标准**）
    """
    if require_running and not app_running():
        return {"ok": False, "available": False, "total_targets": 0,
                "total_links": 0, "items": [],
                "error": "Obsidian 未在运行（不自动启动它）"}
    try:
        bad = unresolved(cli=cli, vault=vault, timeout=timeout)
    except ObsidianUnavailable as e:
        return {"ok": False, "available": False, "total_targets": 0,
                "total_links": 0, "items": [], "error": str(e)[:200]}
    total = sum(x["count"] for x in bad)
    return {"ok": not bad, "available": True, "total_targets": len(bad),
            "total_links": total,
            "items": [{"link": x["link"], "count": x["count"]}
                      for x in sorted(bad, key=lambda y: -y["count"])[:10]],
            "error": ""}
