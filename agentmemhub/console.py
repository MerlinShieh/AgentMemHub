"""AgentMemHub 交互式控制台（新用户入口）。

零依赖交互菜单：环境检测 → 提取入库 → 关键字检索 → 网页看板 → 写入记忆 → 状态总览。
复用 cli.py 的共享 helper（run_ingest / run_search_text / _vectorize_stage），不含业务逻辑；
action 收尾日志统一走本模块 _action_log（延迟导入 cli._cli_log，未导入即静默）。

去耦约定：
- 不出现任何绝对路径：数据位置走 Store 默认解析（HOME / 环境变量），
  看板端口走 AGENTMEMHUB_PORT（默认 8086）。
- 引擎状态一律经 memos_daemon.daemon_status() 获取（rag 后端为进程内直调，
  不发 HTTP、无守护进程可启停）。
- 不 import web 模块本身（看板用独立子进程启动，避免阻塞菜单）。

入口：`python -m agentmemhub`（无参数）或 start.bat。
"""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Optional

from agentmemhub import adapters
from agentmemhub.store import Store


def _out(s: str) -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print(s)


def _action_log(msg: str) -> None:
    """控制台操作落盘（logs/cli.log，复用 cli 实现，延迟导入防循环依赖）。

    日志绝不影响交互：导入失败/写盘失败都静默跳过（cli._cli_log 内部亦已
    兜异常）。各 action 必须经此记录，不得直接裸调 cli._cli_log。
    """
    try:
        from agentmemhub.cli import _cli_log
    except Exception:
        return
    _cli_log(msg)


def dashboard_port() -> int:
    from agentmemhub import config
    return config.config().web_port


def _store_stats_safe() -> Optional[dict]:
    try:
        store = Store()
        stats = store.stats()
        store.close()
        return stats
    except Exception:
        return None


def env_snapshot() -> dict:
    """环境快照：各 Agent 数据源状态 + 本地库规模 + 记忆索引状态。

    引擎状态只取 daemon_status() 一处真相（rag 后端下它是进程内直调，
    不探 18800）；不再额外发 HTTP 二次探测。
    """
    from agentmemhub import memos_daemon
    return {
        "adapters": [a.describe() for a in adapters.all_adapters()],
        "stats": _store_stats_safe(),
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
    summ = eng.get("summary") or {}
    if eng.get("backend") == "rag":
        # 内置引擎：随进程启动，无守护/无端口；就绪即可 [3] 写入
        if eng.get("online"):
            traces = summ.get("traces")
            model = summ.get("embedding_model")
            cov = summ.get("coverage")
            detail = "，".join(x for x in (
                f"{traces} 条记忆" if traces is not None else "",
                f"模型 {model}" if model else "",
                f"向量覆盖 {(cov * 100):.0f}%" if cov is not None else "",
            ) if x)
            lines.append("  记忆索引: 就绪（内置引擎）" + (f" · {detail}" if detail else ""))
        else:
            lines.append("  记忆索引: 不可用 —— 检查 database/session_rag.db 与 models/")
        return "\n".join(lines)
    if eng.get("online"):
        managed = "，本工具托管" if eng.get("managed") else ""
        pid = f" PID {eng['pid']}" if eng.get("pid") else ""
        traces = summ.get("traces")
        extra = f"，{traces} 条记忆" if traces is not None else ""
        lines.append(f"  记忆引擎: 运行中{pid}{managed}{extra}（{eng.get('base_url') or ''}）")
    else:
        lines.append("  记忆引擎: 已停止（backend=memos 回退模式，需外部引擎）")
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
  [3] 蒸馏记忆（LLM 提炼原始会话为结构化记忆）
  [4] 写入记忆（向量化采集库会话到记忆索引）
  ── 日常查询与看板 ──────────────────────────────
  [5] 检索关键字（跨 Agent 全文搜索）
  [6] 启动网页看板（后台运行，菜单不阻塞）
  [7] 停止网页看板（结束占用看板端口的服务进程）
  [8] 状态总览（数据源 / 本地库 / 记忆索引）
  [0] 退出

  提示：自动评分（LLM 三轴补价值分）入口已隐藏——蒸馏的置信度标注与
  面板 👍/👎 已覆盖质量把关；确需批量评分用 CLI：python -m agentmemhub score
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
        _action_log(f"clean（控制台）→ 删除 {deleted} 条")
    finally:
        store.close()


def action_score() -> None:
    """自动评分：LLM 三轴评估记忆并写入价值分（增量优先，4 worker 并发）。

    **入口已从菜单隐藏**（蒸馏置信度 + 面板 👍/👎 已覆盖质量把关，且批量
    评分实测多为 neutral/全跳过）；函数保留供 CLI `score` 与将来恢复使用。
    """
    from agentmemhub.scoring import run_score_incremental
    limit_raw = _ask("  最多评分条数（回车=全部）> ", "0")
    try:
        limit = max(0, int(limit_raw.strip() or "0"))
    except ValueError:
        limit = 0
    dry = _ask("  模式（回车=实际写入 / dry=只评估不写入）> ", "").strip().lower() in ("dry", "dry-run", "d")
    if not dry and not _confirm("  将评估并写入价值分（增量优先：先评 sync 推送的新记忆；4 并发，可能耗时数分钟），确认？"):
        _out("  （已取消）")
        return
    _out(f"  评分中（{'dry-run，不写入' if dry else '实际写入'}）…")
    r = run_score_incremental(emit=lambda s: _out(f"    {s}"), limit=limit,
                              dry_run=dry, workers=4)
    _out(f"  ✓ 完成[{r.get('mode', '?')}]: evaluated={r['evaluated']} skipped={r['skipped']} "
         f"positive={r['positive']} neutral={r['neutral']} "
         f"negative={r['negative']} errors={r['errors']}"
         + ("（dry-run）" if r["dryRun"] else ""))
    _action_log(f"score（控制台）→ {r}")


def action_distill() -> None:
    """蒸馏记忆：LLM 把原始会话提炼为结构化记忆（幂等，可反复执行）。"""
    from agentmemhub import rag_bridge
    from agentmemhub.distill import run_distill
    if not _confirm("  调用 LLM 蒸馏原始会话为结构化记忆？"
                    "（幂等：已蒸馏且内容未变的切片自动跳过）"):
        _out("  （已取消）")
        return
    _out("  蒸馏中（可能耗时较长）…")
    try:
        r = run_distill(rag_bridge.settings(), on_progress=lambda m: _out("  " + m))
    except Exception as e:
        _out(f"  ✗ 蒸馏失败: {e}")
        return
    if r.get("error"):
        _out(f"  ✗ 蒸馏未执行：{r['error']}")
        return
    _out(f"  ✓ 会话 {r['conversations']} · 切片 {r['slices']}"
         f"（跳过 {r['skipped_done']} / 失败 {r['failed']}）")
    _out(f"    产出记忆 {r['memories_new']} 条 · 合并 {r['merged']} 会话 · "
         f"投影 {r['projected']}（判重 {r['duplicate']}）")
    _out(f"    耗时 {r['seconds']}s · 模型 {r['model']}")
    _action_log(f"蒸馏记忆（控制台）→ {r}")


def action_memos() -> None:
    """写入记忆：把采集库的会话向量化写入记忆索引（小模型先跑完即可检索）。"""
    from agentmemhub.cli import _vectorize_stage
    if not _confirm("  开始向量化写入记忆索引？"):
        _out("  （已取消）")
        return
    r = _vectorize_stage(stdout=lambda m: _out("  " + str(m)))
    if r.get("failed"):
        _out("  ✗ 写入失败（详见日志）")
    else:
        _out(f"  ✓ 已写入可检索（{r.get('embedded', 0)} 条新嵌入）")
        if r.get("background"):
            _out(f"    提示：{r.get('background_hint', '')}")
            _out(f"    后台继续：{', '.join(r['background'])}")
    _action_log(f"写入记忆（控制台）→ {r.get('embedded')} 条")


def action_status() -> None:
    """状态总览：主循环已在下一次迭代重探并渲染，这里不再重复打印。"""
    return


ACTIONS = {
    "1": ("提取会话入库", action_ingest),
    "2": ("清洗数据", action_clean),
    "3": ("蒸馏记忆", action_distill),
    "4": ("写入记忆", action_memos),
    # 「自动评分」入口已隐藏（action_score 保留，供 CLI score 与将来恢复）
    "5": ("检索关键字", action_search),
    "6": ("启动网页看板", action_dashboard),
    "7": ("停止网页看板", action_dashboard_stop),
    "8": ("状态总览", action_status),
}


def run_console() -> None:
    _out(BANNER)
    snap: Optional[dict] = None
    while True:
        # 首屏渲染一次；此后只在需要时重探（[8] 复用同一次快照，不重复打印）
        if snap is None:
            try:
                snap = env_snapshot()
            except Exception:
                snap = {"adapters": [], "stats": None, "engine": {}}
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
        snap = None                          # 操作可能改变库/引擎状态，下轮重探