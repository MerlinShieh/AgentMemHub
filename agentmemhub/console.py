"""AgentMemHub 交互式控制台（新用户入口）。

零依赖交互菜单：环境检测 → 提取入库 → 关键字检索 → 网页看板 → 记忆推送 → 状态总览。
复用 cli.py 的共享 helper（run_ingest / run_search_text / run_memos），不含业务逻辑。

去耦约定：
- 不出现任何绝对路径：数据位置走 Store 默认解析（HOME / 环境变量），
  MemOS 地址走 MEMOS_BASE_URL 环境变量（默认 http://127.0.0.1:18800），
  看板端口走 AGENTMEMHUB_PORT（默认 8086）。
- 不 import web 模块本身（看板用独立子进程启动，避免阻塞菜单）。

入口：`python -m agentmemhub`（无参数）或 start.bat。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from typing import Optional

from agentmemhub import adapters
from agentmemhub.store import Store


def _out(s: str) -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print(s)


def memos_base_url() -> str:
    from agentmemhub import config
    return config.config().memos_base_url


def dashboard_port() -> int:
    from agentmemhub import config
    return config.config().web_port


def memos_probe(base_url: Optional[str] = None, timeout: float = 1.5) -> Optional[dict]:
    """探测 MemOS 是否在线（GET /api/v1/overview）；离线返回 None。"""
    try:
        url = (base_url or memos_base_url()) + "/api/v1/overview"
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def _store_stats_safe() -> Optional[dict]:
    try:
        store = Store()
        stats = store.stats()
        store.close()
        return stats
    except Exception:
        return None


def env_snapshot() -> dict:
    """环境快照：各 Agent 数据源状态 + 本地库规模 + MemOS 引擎状态。"""
    from agentmemhub import memos_daemon
    return {
        "adapters": [a.describe() for a in adapters.all_adapters()],
        "stats": _store_stats_safe(),
        "memos": memos_probe() is not None,
        "memos_url": memos_base_url(),
        "engine": memos_daemon.daemon_status(),
    }


def _render_snapshot(s: dict) -> str:
    lines = ["  ── 环境 ─────────────────────────────"]
    found = [d for d in s["adapters"] if d["located"]]
    missing = [d for d in s["adapters"] if not d["located"]]
    lines.append("  数据源: " + (" ".join(f"{d['source']}✓" for d in found)
                                + ("  " + " ".join(f"{d['source']}✗" for d in missing) if missing else "")
                                or "（未发现任何 Agent 数据）"))
    st = s["stats"]
    if st:
        lines.append(f"  本地库: {st['conversations']} 会话 / {st['events']} 事件")
    else:
        lines.append("  本地库: （空 —— 建议先执行 [1] 提取入库）")
    eng = s.get("engine") or {}
    if eng.get("online"):
        managed = "，本工具托管" if eng.get("managed") else ""
        pid = f" PID {eng['pid']}" if eng.get("pid") else ""
        summ = eng.get("summary") or {}
        traces = summ.get("traces")
        extra = f"，{traces} 条记忆" if traces is not None else ""
        lines.append(f"  记忆引擎: 运行中{pid}{managed}{extra}（{s['memos_url']}）")
    else:
        lines.append(f"  记忆引擎: 已停止（[6] 启动；{s['memos_url']}）")
    return "\n".join(lines)


BANNER = r"""
   ╔══════════════════════════════════════════╗
   ║  AgentMemHub 控制台                        ║
   ║  统一提取 Agent 会话 → 本地库 → 记忆       ║
   ╚══════════════════════════════════════════╝"""

MENU = """
  [1] 提取所有 Agent 会话入库（可选拢单个 Agent）
  [2] 检索关键字（跨 Agent 全文搜索）
  [3] 启动网页看板（后台运行，菜单不阻塞）
  [4] 推送记忆到 MemOS（生成 bundle + 分批导入 + 补 embedding）
  [5] 状态总览（数据源 / 本地库 / 记忆引擎）
  [6] 启动记忆引擎（MemOS daemon，首次会提示插件目录）
  [7] 停止记忆引擎（仅限本工具启动的实例）
  [0] 退出
"""


def _choose_source() -> str:
    """选择来源：直接回车 = 全部；输入 source 名 = 单个。"""
    srcs = [a.source for a in adapters.all_adapters()]
    _out(f"  可选 source: {', '.join(srcs)}（回车 = 全部）")
    raw = input("  source> ").strip().lower()
    return raw if raw in srcs else ""


def _ask(prompt: str, default: str = "") -> str:
    raw = input(f"{prompt}").strip()
    return raw or default


def _confirm(prompt: str) -> bool:
    return input(f"{prompt} [y/N]> ").strip().lower() in ("y", "yes")


def action_ingest() -> None:
    from agentmemhub.cli import run_ingest
    source = _choose_source()
    sources = [source] if source else [a.source for a in adapters.all_adapters()]
    _out("  提取中（可能需要数十秒）…")
    total_conv, total_ev = run_ingest(sources)
    _out(f"  完成: {total_conv} 会话, {total_ev} 事件")


def action_search() -> None:
    from agentmemhub.cli import run_search_text
    query = _ask("  关键字> ")
    if not query:
        _out("  （未输入关键字）")
        return
    source = _choose_source()
    role = _ask("  角色（user/assistant/tool/reasoning，回车=全部）> ").lower()
    run_search_text(query, source=source, role=role, limit=20)


def action_dashboard() -> None:
    port = dashboard_port()
    # 独立子进程跑 serve：菜单不阻塞；同样继承当前解释器（uv venv 生效）
    proc = subprocess.Popen(
        [sys.executable, "-m", "agentmemhub", "serve", "--no-open", "--port", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    _out(f"  看板启动中（PID {proc.pid}）→ http://127.0.0.1:{port}/")
    _out("  （在浏览器打开上面的地址；停止看板可关闭该进程或按 Ctrl+C 退出控制台）")


def action_memos() -> None:
    from agentmemhub.cli import run_memos
    base = _ask(f"  MemOS 地址（回车 = {memos_base_url()}）> ", memos_base_url())
    probe = memos_probe(base)
    if probe is None:
        if not _confirm("  ⚠ 记忆引擎未在线（可用 [6] 启动）。仍要生成 bundle 并尝试推送吗？"):
            _out("  （已取消）")
            return
    else:
        if not _confirm("  MemOS 在线，开始推送（导入后自动补 embedding）？"):
            return
    run_memos(push=base)


def action_status() -> None:
    _out(_render_snapshot(env_snapshot()))


def action_engine_start() -> None:
    from agentmemhub import memos_daemon
    if memos_daemon.find_plugin_dir() is None:
        _out("  未找到 MemOS 插件目录（MemOS repo 的 apps/memos-local-plugin）。")
        raw = input("  输入路径（回车取消）> ").strip()
        if not raw:
            _out("  （已取消）")
            return
        try:
            p = memos_daemon.save_plugin_dir(raw)
            _out(f"  已记住插件目录: {p}")
        except Exception as e:
            _out(f"  ✗ 保存失败: {e}")
            return
    _out("  启动记忆引擎中（最多等 30 秒）…")
    r = memos_daemon.daemon_start()
    if r.get("online") or r.get("started"):
        st = r.get("summary") or {}
        _out(f"  ✓ 记忆引擎在线（PID {r.get('pid')}，{st.get('traces')} 条记忆）")
        if r.get("auth_required"):
            raw = input("  引擎已设密码，输入以保存（回车跳过）> ").strip()
            if raw:
                memos_daemon.save_password(raw)
                ok = memos_daemon._login()
                _out("  ✓ 密码已保存并登录" if ok else "  ⚠ 密码已保存但登录未通过，请核对后重设")
    else:
        _out(f"  ✗ 启动失败: {r.get('reason')}")
        if r.get("hint"):
            _out(f"    提示: {r['hint']}")
        if r.get("log"):
            _out(f"    日志: {r['log']}")


def action_engine_stop() -> None:
    from agentmemhub import memos_daemon
    r = memos_daemon.daemon_stop()
    if r.get("stopped"):
        _out(f"  ✓ 记忆引擎已停止（PID {r.get('pid')}）")
    else:
        _out(f"  - 未停止: {r.get('reason')}")
        if r.get("hint"):
            _out(f"    提示: {r['hint']}")


ACTIONS = {
    "1": ("提取会话入库", action_ingest),
    "2": ("检索关键字", action_search),
    "3": ("启动网页看板", action_dashboard),
    "4": ("推送记忆到 MemOS", action_memos),
    "5": ("状态总览", action_status),
    "6": ("启动记忆引擎", action_engine_start),
    "7": ("停止记忆引擎", action_engine_stop),
}


def run_console() -> None:
    _out(BANNER)
    while True:
        try:
            snap = env_snapshot()
        except Exception:
            snap = {"adapters": [], "stats": None, "memos": False,
                    "memos_url": memos_base_url()}
        _out(_render_snapshot(snap))
        _out(MENU)
        try:
            choice = input("  选择> ").strip()
        except (EOFError, KeyboardInterrupt):
            _out("\n  再见！")
            return
        if choice in ("0", "q", "quit", "exit"):
            _out("  再见！")
            return
        action = ACTIONS.get(choice)
        if action is None:
            _out("  （无效选择，请输入菜单编号）")
            continue
        try:
            action[1]()
        except KeyboardInterrupt:
            _out("\n  （已中断，返回菜单）")
        except Exception as e:
            _out(f"  ⚠ 执行出错: {e}")
        _out("")