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
    from agentmemhub import config
    return config.config().data_dir


def _pid_file() -> Path:
    return _data_dir() / "memos_daemon.pid"


def _log_file() -> Path:
    return _data_dir() / "memos_daemon.log"


def base_url() -> str:
    from agentmemhub import config
    return config.config().memos_base_url


# ---------------------------------------------------------------------------
# 引擎鉴权：viewer 可设密码（.auth.json）；AgentMemHub 保管密码并自动登录
# ---------------------------------------------------------------------------

class EngineAuthError(Exception):
    """引擎在线但鉴权失败（未保存密码或密码错误）。"""


_COOKIE_CACHE: set[str] = set()   # "name=value" 片段（进程内缓存，401 时重登）


def _config_file() -> Path:
    return _data_dir() / "config.json"


def save_password(password: str) -> None:
    """保存 MemOS viewer 密码到本机 config.json（不进仓库）。"""
    cfg: dict = {}
    try:
        cfg = json.loads(_config_file().read_text(encoding="utf-8")) or {}
    except Exception:
        pass
    cfg["memos_password"] = password
    _config_file().write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# 轻量模式管控：统一配置 memos.lightweight 优先；个别配置合并写引擎 config.yaml
# （引擎本体零改动，config.yaml 是官方运行时配置入口）
# ---------------------------------------------------------------------------

def engine_home() -> Optional[Path]:
    """引擎 home（记忆库 / .auth.json / config.yaml 所在）。

    优先统一配置 memos.home（MemOS 平移进项目后指向 <项目根>/memOS/home），
    否则按 Windows Hermes 官方默认回退探测（含 marker 目录）。
    """
    from agentmemhub import config
    cfg_home = config.config().memos_home
    if cfg_home and cfg_home.is_dir():
        return cfg_home
    if os.name == "nt":
        local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        for cand in (local / "hermes" / "memos-plugin",
                     Path.home() / ".hermes" / "memos-plugin"):
            if cand.is_dir():
                return cand
    for cand in (Path.home() / ".hermes" / "memos-plugin",
                 Path.home() / ".openclaw" / "memos-plugin",
                 Path.home() / ".dsh" / "memos-plugin"):
        if cand.is_dir():
            return cand
    return None


def engine_config_path() -> Optional[Path]:
    """引擎 config.yaml 路径（home 内）；无 home 返回 None。"""
    home = engine_home()
    return (home / "config.yaml") if home else None


def set_lightweight(enabled: bool) -> Optional[Path]:
    """开/关轻量记忆模式：合并写入引擎 config.yaml（保留 viewer 已存的其他配置）。

    false=完整进化链；需重启引擎生效。返回写入的配置文件路径。
    """
    import yaml
    p = engine_config_path()
    if p is None:
        raise RuntimeError("未找到引擎 home——先配置 memos.home 或确认引擎默认位置")
    cfg: dict = {}
    if p.exists():
        cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    algo = cfg.setdefault("algorithm", {}) or {}
    algo.setdefault("lightweightMemory", {})["enabled"] = bool(enabled)
    p.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return p


def lightweight_config() -> Optional[bool]:
    """读取链：统一配置 memos.lightweight > 引擎 config.yaml 的当前值。"""
    from agentmemhub import config
    forced = config.config().memos_lightweight
    if forced is not None:
        return forced
    p = engine_config_path()
    if p and p.exists():
        try:
            import yaml
            return bool((yaml.safe_load(p.read_text(encoding="utf-8")) or {})
                        .get("algorithm", {}).get("lightweightMemory", {}).get("enabled"))
        except Exception:
            return None
    return None


def _password() -> str:
    """读取链：统一配置(memos.password / MEMOS_PASSWORD) > 本机 config.json（旧）。"""
    from agentmemhub import config
    v = config.config().memos_password
    if v:
        return v
    try:
        return (json.loads(_config_file().read_text(encoding="utf-8")) or {}).get(
            "memos_password", "")
    except Exception:
        return ""


def _login() -> bool:
    """用保存的密码登录引擎，缓存 session cookie。成功 True。"""
    pw = _password()
    if not pw:
        return False
    req = urllib.request.Request(
        base_url() + "/api/v1/auth/login",
        data=json.dumps({"password": pw}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            set_cookies = r.headers.get_all("Set-Cookie") or []
    except urllib.error.HTTPError as e:
        if e.code in (400, 401):
            _COOKIE_CACHE.clear()   # 密码被改/失效，清缓存
            return False
        raise
    for sc in set_cookies:
        first = sc.split(";")[0].strip()
        if "=" in first:
            _COOKIE_CACHE.add(first)
    return bool(_COOKIE_CACHE)


def engine_request(method: str, path: str, body: Optional[dict] = None,
                   timeout: float = 30, retries: int = 1) -> dict:
    """带自动登录的引擎 HTTP 请求（AgentMemHub 网关统一出口）。"""
    url = base_url() + path
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    if _COOKIE_CACHE:
        headers["Cookie"] = "; ".join(sorted(_COOKIE_CACHE))
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text = r.read().decode("utf-8")
            return json.loads(text) if text.strip() else {}
    except urllib.error.HTTPError as e:
        if e.code == 401 and retries > 0 and _login():
            return engine_request(method, path, body, timeout, retries - 1)
        if e.code == 401:
            raise EngineAuthError(
                "引擎已设密码：运行 agentmemhub memos-daemon --set-password <密码> 保存后重试")
        raise


def auth_state() -> Optional[dict]:
    """引擎鉴权状态（/api/v1/auth/status 是公开端点）。离线返回 None。"""
    try:
        with urllib.request.urlopen(base_url() + "/api/v1/auth/status", timeout=2.5) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def find_plugin_dir(explicit: Optional[str] = None) -> Optional[Path]:
    """定位 MemOS 插件目录：
    显式参数 > 统一配置(plugin_dir / repo_dir 推导 / MEMOS_PLUGIN_DIR)
    > 数据目录 config.json > 常见位置探测。"""
    from agentmemhub import config
    cfg = config.config()
    for cand in (explicit, cfg.memos_plugin_dir):
        if cand and cand.is_dir():
            return cand
    cfg2 = _data_dir() / "config.json"
    try:
        saved = (json.loads(cfg2.read_text(encoding="utf-8")) or {}).get("memos_plugin_dir")
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
    """引擎 overview；在线但鉴权不过也返回 None（用 auth_state 区分）。"""
    try:
        return engine_request("GET", "/api/v1/overview", timeout=timeout)
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
    """巡检：在线状态 + 鉴权状态 + 归属（本工具启动 / 外部）+ 引擎摘要。"""
    ast = auth_state()
    online = ast is not None          # 公开端点可达即在线（不依赖鉴权）
    ov = _overview() if online else None
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
    from agentmemhub import config as _cfg
    _c = _cfg.config()
    result: dict[str, Any] = {
        "online": online,
        "auth": ast,                 # {enabled, needsSetup, authenticated} | None
        "auth_required": bool(online and ov is None and ast
                              and ast.get("enabled") and not ast.get("authenticated")),
        "pid": pid,
        "managed": managed,          # True=由 AgentMemHub 拉起（可 stop）
        "base_url": base_url(),
        "plugin_dir": str(pdir_resolved) if pdir_resolved else None,
        "repo_dir": str(_c.memos_repo_dir),
        "engine_home": str(engine_home()) if engine_home() else None,
        "lightweight": lightweight_config(),
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
    if auth_state() is not None:
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
    env = dict(os.environ)
    # 统一配置指定引擎 home（仅当目录真实存在——平移进项目内后让引擎用新位置）
    from agentmemhub import config
    mh = config.config().memos_home
    if mh and mh.is_dir():
        env["MEMOS_HOME"] = str(mh)
    try:
        proc = subprocess.Popen(
            [npm, "run", "bridge:daemon", "--", f"--agent={agent}"],
            cwd=str(d), stdout=log, stderr=subprocess.STDOUT,
            creationflags=flags, close_fds=True, env=env,
        )
    except FileNotFoundError:
        return {"started": False, "reason": "npm-not-found",
                "hint": "未找到 npm，请先安装 Node.js >= 20"}
    _pid_file().write_text(str(proc.pid), encoding="utf-8")

    deadline = time.time() + wait_s
    while time.time() < deadline:
        if auth_state() is not None:
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
                "hint": f"daemon 非本工具启动，请自行关闭（端口 {base_url()}）"}
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
    # 等端口下线（auth/status 是公开端点，可达即视为仍在线）
    deadline = time.time() + 10
    while time.time() < deadline:
        if auth_state() is None:
            _pid_file().unlink(missing_ok=True)
            return {"stopped": True, "pid": pid}
        time.sleep(0.5)
    return {"stopped": False, "reason": "still-online", "pid": pid}