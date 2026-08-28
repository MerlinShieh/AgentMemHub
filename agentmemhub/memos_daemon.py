"""MemOS 记忆引擎 daemon 生命周期管理。

AgentMemHub 作为「管理器」：拉起/停止/巡检 MemOS Local Plugin 的 bridge daemon，
不介入引擎内部实现——只通过它的公开 HTTP API 交互。

路径约定（无硬编码）：
- 插件目录：环境变量 MEMOS_PLUGIN_DIR（= MemOS repo 的 apps/memos-local-plugin），
  未设置时探测少量常见位置；找不到则如实报告并提示设置。
- PID 文件：~/.agentmemhub/memos_daemon.pid（AGENTMEMHUB_DATA_DIR 可覆盖数据目录）
- 日志：数据目录下 memos_daemon.log

daemon 启动命令：npm run bridge:daemon -- --agent=<agent>（默认 hermes，
端口随之 18800；与 memos.base_url 的 MEMOS_BASE_URL 约定保持独立）。
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any, Optional

#: 常见插件目录探测列表（相对 HOME，均为惯例布局而非个人绝对路径）
_PLUGIN_DIR_CANDIDATES = (
    "MemOS-Local-Plugin/apps/memos-local-plugin",
    "Agent_Memory/MemOS-Local-Plugin/apps/memos-local-plugin",
    "memos-local-plugin",
    ".memos-local-plugin",
)

_DAEMON_PORT = 18800
_START_TIMEOUT_S = 30


def _data_dir() -> Path:
    d = Path(os.environ.get("AGENTMEM_HUB_DATA_DIR", Path.home() / ".agentmemhub"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _pid_file() -> Path:
    return _data_dir() / "memos_daemon.pid"


def _log_file() -> Path:
    return _data_dir() / "memos_daemon.log"


def base_url() -> str:
    return f"http://127.0.0.1:{_DAEMON_PORT}"


def find_plugin_dir(explicit: Optional[str] = None) -> Optional[Path]:
    """定位 MemOS 插件目录：显式参数 > 环境变量 > 数据目录 config.json > 常见位置探测。"""
    for cand in (explicit, os.environ.get("MEMOS_PLUGIN_DIR")):
        if cand and Path(cand).is_dir():
            return Path(cand)
    cfg = _data_dir() / "config.json"
    try:
        saved = (json.loads(cfg.read_text(encoding="utf-8")) or {}).get("memos_plugin_dir")
        if saved and Path(saved).is_dir():
            return Path(saved)
    except Exception:
        pass
    home = Path.home()
    for rel in _PLUGIN_DIR_CANDIDATES:
        p = home / rel
        if p.is_dir() and (p / "package.json").exists():
            return p
    return None


def save_plugin_dir(path: str | Path) -> Path:
    """把插件目录持久化到数据目录 config.json（环境变量优先级更高）。"""
    p = Path(path).expanduser().resolve()
    if not (p / "package.json").exists():
        raise FileNotFoundError(f"目录下没有 package.json: {p}")
    cfg_file = _data_dir() / "config.json"
    cfg: dict = {}
    try:
        cfg = json.loads(cfg_file.read_text(encoding="utf-8")) or {}
    except Exception:
        pass
    cfg["memos_plugin_dir"] = str(p)
    cfg_file.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _overview(timeout: float = 1.5) -> Optional[dict]:
    try:
        with urllib.request.urlopen(base_url() + "/api/v1/overview", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        code = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                              capture_output=True, text=True).returncode
        return code == 0 and str(pid) in (subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"],
            capture_output=True, text=True).stdout or "")
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def daemon_status() -> dict[str, Any]:
    """巡检：在线状态 + 归属（本工具启动 / 外部）+ 引擎摘要。"""
    ov = _overview()
    online = ov is not None
    pid: Optional[int] = None
    managed = False
    pf = _pid_file()
    if pf.exists():
        try:
            pid = int(pf.read_text(encoding="utf-8").strip())
        except Exception:
            pid = None
        if pid and online and _pid_alive(pid):
            managed = True
        elif not pid or not _pid_alive(pid):
            pf.unlink(missing_ok=True)   # 陈旧 PID 清理
            pid = None
    pdir_resolved = find_plugin_dir()
    result: dict[str, Any] = {
        "online": online,
        "pid": pid,
        "managed": managed,          # True=由 AgentMemHub 拉起（可 stop）
        "base_url": base_url(),
        "plugin_dir": str(pdir_resolved) if pdir_resolved else None,
    }
    if online and ov:
        emb = ov.get("embedder") or {}
        result["summary"] = {
            "episodes": ov.get("episodes"),
            "traces": ov.get("traces"),
            "embedding_ready": emb.get("available"),
            "embedding_model": emb.get("model"),
            "llm_available": (ov.get("llm") or {}).get("available"),
        }
    return result


def daemon_start(agent: str = "hermes",
                 plugin_dir: Optional[str] = None,
                 wait_s: int = _START_TIMEOUT_S) -> dict[str, Any]:
    """拉起 daemon（已在线则幂等返回）。返回 {started, online, ...}。"""
    if _overview() is not None:
        return {"started": False, "reason": "already-online", **daemon_status()}
    d = find_plugin_dir(plugin_dir)
    if d is None:
        return {"started": False, "reason": "plugin-dir-not-found",
                "hint": "设置环境变量 MEMOS_PLUGIN_DIR 指向 MemOS repo 的 apps/memos-local-plugin"}
    npm = "npm.cmd" if os.name == "nt" else "npm"
    log = open(_log_file(), "ab")
    flags = 0
    if os.name == "nt":
        # 脱离父进程：控制台退出后 daemon 继续存活
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    try:
        proc = subprocess.Popen(
            [npm, "run", "bridge:daemon", "--", f"--agent={agent}"],
            cwd=str(d), stdout=log, stderr=subprocess.STDOUT,
            creationflags=flags, close_fds=True,
        )
    except FileNotFoundError:
        return {"started": False, "reason": "npm-not-found",
                "hint": "未找到 npm，请先安装 Node.js >= 20"}
    _pid_file().write_text(str(proc.pid), encoding="utf-8")

    deadline = time.time() + wait_s
    while time.time() < deadline:
        if _overview(timeout=1.0) is not None:
            return {"started": True, "pid": proc.pid, **daemon_status()}
        if proc.poll() is not None:
            return {"started": False, "reason": "process-exited",
                    "pid": proc.pid, "returncode": proc.returncode,
                    "log": str(_log_file())}
        time.sleep(1.0)
    return {"started": False, "reason": "timeout", "pid": proc.pid,
            "log": str(_log_file())}


def daemon_stop() -> dict[str, Any]:
    """停止本工具拉起的 daemon（外部启动的不动，只报告）。"""
    st = daemon_status()
    if not st["online"]:
        return {"stopped": False, "reason": "not-online"}
    if not st["managed"] or not st["pid"]:
        return {"stopped": False, "reason": "external-process",
                "hint": f"daemon 非本工具启动，请自行关闭（端口 {_DAEMON_PORT}）"}
    pid = st["pid"]
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, text=True)
    else:
        import signal
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except OSError:
            os.kill(pid, signal.SIGTERM)
    # 等端口下线
    deadline = time.time() + 10
    while time.time() < deadline:
        if _overview(timeout=0.8) is None:
            _pid_file().unlink(missing_ok=True)
            return {"stopped": True, "pid": pid}
        time.sleep(0.5)
    return {"stopped": False, "reason": "still-online", "pid": pid}