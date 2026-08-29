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
  ── 数据流程（按顺序操作）────────────────────────
  [1] 提取所有 Agent 会话入库（可选单个 Agent）
  [2] 清洗数据（删除系统注入事件，先预览后确认）
  [3] 推送记忆到 MemOS（导入 + 幂等 + 自动补向量）
  [4] 补向量（embedding rebuild，导入后修复语义检索）
  [5] 自动评分（LLM 三轴批量补价值分，跳过已评）
  ── 日常查询与看板 ──────────────────────────────
  [6] 检索关键字（跨 Agent 全文搜索）
  [7] 启动网页看板（后台运行，菜单不阻塞）
  [8] 停止网页看板（结束占用看板端口的服务进程）
  [9] 状态总览（数据源 / 本地库 / 记忆引擎）
  ── 记忆引擎管理 ────────────────────────────────
  [10] 启动记忆引擎（MemOS daemon，首次会提示插件目录）
  [11] 停止记忆引擎（仅限本工具启动的实例）
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
    if _port_listening(port):
        _out(f"  看板已在运行（端口 {port} 被占用，跳过重复启动）")
        _out(f"  → 直接在浏览器打开 http://127.0.0.1:{port}/")
        return
    # 独立子进程跑 serve：菜单不阻塞；同样继承当前解释器（uv venv 生效）
    proc = subprocess.Popen(
        [sys.executable, "-m", "agentmemhub", "serve", "--no-open", "--port", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    _out(f"  看板启动中（PID {proc.pid}）→ http://127.0.0.1:{port}/")
    _out("  （在浏览器打开上面的地址；停止看板可关闭该进程或按 Ctrl+C 退出控制台）")


def _port_listening(port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _dashboard_pid(port: int) -> Optional[int]:
    """返回监听端口进程的 PID（Windows netstat；其他平台用 psutil 简化探测）。"""
    if os.name == "nt":
        out = subprocess.run(["netstat", "-ano"], capture_output=True,
                             text=True).stdout
        for line in out.splitlines():
            if "LISTENING" in line and f":{port}" in line:
                parts = line.split()
                if parts:
                    try:
                        return int(parts[-1])
                    except ValueError:
                        return None
    return None


def action_dashboard_stop() -> None:
    """停止网页看板：结束占用看板端口的服务进程（确认后执行）。"""
    import time as _t
    port = dashboard_port()
    if not _port_listening(port):
        _out(f"  看板未在运行（端口 {port} 空闲）")
        return
    pid = _dashboard_pid(port)
    if pid is None:
        _out(f"  端口 {port} 被占用但未能解析进程 PID，请手动关闭占用进程")
        return
    if not _confirm(f"  将停止看板进程（PID {pid}，端口 {port}）？"):
        _out("  （已取消）")
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                       capture_output=True, text=True)
    else:
        import signal as _sig
        try:
            os.kill(pid, _sig.SIGTERM)
        except OSError:
            _out("  ✗ 进程不存在（可能已退出）")
            return
    for _ in range(20):                      # 等端口释放（最多 10 秒）
        if not _port_listening(port):
            _out(f"  ✓ 网页看板已停止（PID {pid}，端口 {port} 已释放）")
            return
        _t.sleep(0.5)
    _out(f"  ⚠ 端口 {port} 仍在占用（进程可能未完全退出）")


def action_clean() -> None:
    """清洗数据：预览系统注入事件 → 确认后删除（重建 FTS 与计数）。"""
    from agentmemhub.store import Store
    store = Store()
    try:
        rows = store.system_event_counts()
        if not rows:
            _out("  （无系统注入事件——库已经干净）")
            return
        total = sum(r["n"] for r in rows)
        _out(f"  系统注入事件共 {total} 条：")
        for r in rows:
            _out(f"    [{r['source']}] {r['n']} 条（{r['convs']} 个会话）")
        if not _confirm(f"  删除后将重建 FTS 索引与会话计数，确认执行？"):
            _out("  （已取消）")
            return
        deleted, convs = store.delete_system_events()
        _out(f"  ✓ 已删除 {deleted} 条注入事件（{convs} 个会话受影响）")
        from agentmemhub.cli import _cli_log
        _cli_log(f"clean（控制台）→ 删除 {deleted} 条")
    finally:
        store.close()


def action_rebuild() -> None:
    """补向量：触发引擎 embedding rebuild（默认 repair，实时进度）。"""
    from agentmemhub import memos_daemon
    from agentmemhub.memos import rebuild_embeddings
    if memos_daemon.auth_state() is None:
        _out("  ⚠ 记忆引擎未在线（可用 [6] 启动）")
        if not _confirm("  仍要尝试补向量？"):
            _out("  （已取消）")
            return
    mode = _ask("  模式（repair=只补缺失向量[默认] / rebuild=全部重算）> ", "repair")
    mode = mode.strip().lower()
    if mode not in ("repair", "rebuild"):
        mode = "repair"
    _out(f"  补向量中（{mode}，本地计算可能耗时数分钟）…")
    r = rebuild_embeddings(base_url=memos_daemon.base_url(), mode=mode,
                           on_progress=lambda s: _out(f"    {s}"))
    _out(f"  ✓ 完成: {r}")
    from agentmemhub.cli import _cli_log
    _cli_log(f"rebuild（控制台，{mode}）→ {r}")


def action_score() -> None:
    """自动评分：LLM 三轴批量评估未评记忆并写入价值分（4 worker 并发）。"""
    from agentmemhub.cli import _cli_log
    from agentmemhub.scoring import run_score_all
    limit_raw = _ask("  最多评分条数（回车=全部）> ", "0")
    try:
        limit = max(0, int(limit_raw.strip() or "0"))
    except ValueError:
        limit = 0
    dry = _ask("  模式（回车=实际写入 / dry=只评估不写入）> ", "").strip().lower() in ("dry", "dry-run", "d")
    if not dry and not _confirm("  将对未评过的记忆评估并写入价值分（4 并发，可能耗时数分钟），确认？"):
        _out("  （已取消）")
        return
    _out(f"  评分中（{'dry-run，不写入' if dry else '实际写入'}）…")
    r = run_score_all(emit=lambda s: _out(f"    {s}"), limit=limit,
                      dry_run=dry, workers=4)
    _out(f"  ✓ 完成: evaluated={r['evaluated']} skipped={r['skipped']} "
         f"positive={r['positive']} neutral={r['neutral']} "
         f"negative={r['negative']} errors={r['errors']}"
         + ("（dry-run）" if r["dryRun"] else ""))
    _cli_log(f"score（控制台）→ {r}")


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
    "2": ("清洗数据", action_clean),
    "3": ("推送记忆到 MemOS", action_memos),
    "4": ("补向量", action_rebuild),
    "5": ("自动评分", action_score),
    "6": ("检索关键字", action_search),
    "7": ("启动网页看板", action_dashboard),
    "8": ("停止网页看板", action_dashboard_stop),
    "9": ("状态总览", action_status),
    "10": ("启动记忆引擎", action_engine_start),
    "11": ("停止记忆引擎", action_engine_stop),
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