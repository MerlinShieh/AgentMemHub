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

⚠️ **验收前必须先让它重载**（`verify()` 默认会做，见其 docstring）：某个 `[[…]]`
能否解析由 Obsidian 的**内存索引**决定，而 wiki 更新是**整域替换** —— 域一重构，
几百个文件名同时变。不重载就等于"拿旧文件名比新产物"，会报出一批**并不存在**的
死链（2026-10-04 实测：全量重写 21 个域后报 **499 个未解析目标**，`reload` 之后
立刻归零；当时差点据此去改 `wiki_linkfix` 的判据，而它报告"1197 处全部正确"
其实一直是对的）。

CLI 路径解析顺序：显式参数 → `OBSIDIAN_CLI` 环境变量 → `PATH`。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

#: 解析 CLI 可执行文件时的候选名（Windows 上官方用 .com 终端重定向器）
_CLI_NAMES = ("obsidian", "Obsidian.com", "obsidian.exe")

#: 重载后等 Obsidian 重建索引的秒数。产物是**整域替换**，几百个文件名会同时变；
#: 太小会拿到半旧的索引，报出并不存在的死链（2026-10-04 实测）。
RELOAD_SETTLE_S = 20


class ObsidianUnavailable(RuntimeError):
    """Obsidian CLI 不可用（没运行 / 没注册 / 没开开关）—— 调用方应旁路。"""


def find_cli(explicit: str = "") -> str:
    """定位 CLI：显式参数 → 配置 `wiki.obsidian_cli` → `OBSIDIAN_CLI` → PATH。

    **配置优先于环境变量**：进程环境是**启动时捕获**的 —— 实测把 Obsidian 目录
    加进用户 PATH 后重启 DSH，35 条用户 PATH 里 34 条继承了、**只有最后加的那条没**
    （DSH 从旧环境继承而来），验收因此静默退化成"未验收"。配置位与进程启动时机
    无关，才是稳的那个。
    """
    if explicit:
        return explicit
    try:
        from agentmemhub import config
        cfg = str(config.config().wiki.get("obsidian_cli") or "").strip()
        if cfg:
            return cfg
    except Exception:                       # noqa: BLE001 —— 配置读不到就继续往下找
        pass
    env = os.environ.get("OBSIDIAN_CLI", "").strip()
    if env:
        return env
    for name in _CLI_NAMES:
        p = shutil.which(name)
        if p:
            return p
    raise ObsidianUnavailable(
        "找不到 Obsidian CLI。三种配法任选：① 在 agentmemhub.yaml 写 "
        "`wiki.obsidian_cli`（推荐，与进程启动时机无关）；② 设 OBSIDIAN_CLI "
        "环境变量；③ 把 Obsidian 安装目录加进 PATH。另需 Obsidian 正在运行、"
        "且设置→通用 已开启「命令行界面」。")


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
    """未解析链接（Obsidian 权威口径）。返回 `[{link, count, sources}]`。

    ⚠️ **CLI 的 `unresolved` 只支持 `total` / `counts` 两个开关** —— 没有
    `verbose` / `format=json`（那是我从别的命令抄来的参数）。传错会得到
    `Error: Command "unresolved" not found. It may require a plugin`，于是**每一次
    验收都白报"命令不存在"**（2026-10-01 实测踩过，白排查了一轮，还差点去装插件）。
    `counts` 的输出是 TSV：`目标<TAB>计数`。

    ⚠️ CLI 输出**带 UTF-8 BOM**（实测）—— 不剥会让首行解析带上 `\ufeff`。
    没有未解析链接时返回的是**一句英文**（不是空数组），当成条目会误报
    （实测踩过：明明已 0，却报"1 个目标 / 0 条"）。
    """
    out = run("unresolved", "counts", cli=cli, vault=vault, timeout=timeout)
    out = out.lstrip("\ufeff").strip()
    if not out or out.lower().startswith("no unresolved"):
        return []
    low = out.lower()
    # **命令不可用**（插件没加载 / 版本变化）要当"没法验"，绝不能当成"有死链"：
    # 2026-10-01 实测就因此把验收误报成"不通过"，差点让人去修一批不存在的死链。
    if low.startswith("error") or "not found" in low or "no such command" in low:
        raise ObsidianUnavailable("Obsidian CLI 不认 `unresolved` 命令（没法验收）：%s"
                                  % out[:200])
    if out.startswith("["):                 # 兼容 JSON 形态（旧版 CLI / 别的实现）
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return [{"link": out[:200], "count": 0, "sources": "", "raw": True}]
        return [{"link": it.get("link", ""),
                 "count": int(it.get("count") or 0),
                 "sources": it.get("sources", "")}
                for it in data if isinstance(it, dict)]
    rows: list[dict] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.rsplit("\t", 1)
        link = parts[0].strip()
        try:
            cnt = int(parts[1]) if len(parts) > 1 else 0
        except (TypeError, ValueError):
            cnt = 0
        if link:
            rows.append({"link": link, "count": cnt, "sources": ""})
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
           require_running: bool = True, reload_first: bool = True,
           settle_s: "float | None" = None) -> dict:
    """产物链接验收（给流程调用的**不抛异常**版本）。

    返回 `{ok, available, total_targets, total_links, items, error}`：
      · `available=False` → Obsidian/CLI 不可用（调用方应记为"未验收"，不是失败）
      · `ok=True`         → 未解析为 0（**这是唯一的通过标准**）

    ⚠️ **必须先重载再验收**（`reload_first=True`，默认）：某个 `[[…]]` 能不能解析，
    由 Obsidian 的**内存索引**说了算，而 wiki 更新是**整域替换** —— 域一重构，
    几百个文件名同时变。不重载就等于"拿旧文件名去比新产物"，报出一大批**并不
    存在**的死链：2026-10-04 实测全量重写 21 个域后报 **499 个未解析目标**，
    `reload` 之后**立刻归零**，白排查了一轮（还差点去改 linkfix 的判据，
    而它报告"1197 处全部正确"其实一直是对的）。

    排查时可以传 `reload_first=False` 看重载前的状态，但**验收不许关**。

    `settle_s=None`（默认）取模块常量 `RELOAD_SETTLE_S`；**写成可选参数而不是
    直接当默认值**，是为了让测试能覆盖它 —— Python 的默认参数在**函数定义时**
    求值，改模块常量对已绑定的默认值无效（2026-10-07 实测踩到）。
    """
    if require_running and not app_running():
        return {"ok": False, "available": False, "total_targets": 0,
                "total_links": 0, "items": [],
                "error": "Obsidian 未在运行（不自动启动它）"}
    if reload_first:
        reload_app(cli=cli, vault=vault)
        wait = RELOAD_SETTLE_S if settle_s is None else settle_s
        if wait > 0:
            time.sleep(wait)
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
