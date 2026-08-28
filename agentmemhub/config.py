"""AgentMemHub 统一配置体系。

原则：所有路径/端口默认采用官方默认；用户可在配置文件里覆盖（优先最小化配置）。
优先级（高 → 低）：
    1. 环境变量（临时覆盖，命名 AGENTMEMHUB_* / MEMOS_*）
    2. 配置文件：AGENTMEMHUB_CONFIG 指定 > 项目根 agentmemhub.yaml > 数据目录 config.yaml
    3. 内置默认值

配置文件为标准 YAML（见项目根 agentmemhub.yaml.example）。所有相对路径
相对项目根解析，~ 展开为用户目录。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_yaml(path: Path) -> dict:
    import yaml
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


class Config:
    """配置视图：合并 内置默认 < 配置文件 < 环境变量。"""

    def __init__(self, path: Optional[Path] = None, env=None):
        self._env = env if env is not None else os.environ
        cfg_path = path
        if cfg_path is None:
            named = self._env.get("AGENTMEMHUB_CONFIG", "").strip()
            if named:
                cfg_path = Path(named).expanduser()
            else:
                for cand in (PROJECT_ROOT / "agentmemhub.yaml",
                             Path.home() / ".agentmemhub" / "config.yaml"):
                    if cand.exists():
                        cfg_path = cand
                        break
        self._file: dict = _load_yaml(cfg_path) if cfg_path and cfg_path.exists() else {}
        self._file_path = cfg_path

    # -- 基础目录 --------------------------------------------------------

    @property
    def data_dir(self) -> Path:
        v = self._env.get("AGENTMEM_HUB_DATA_DIR", "")
        if not v:
            v = str(self._get("data_dir", ""))
        if v:
            return self._resolve(v)
        return Path.home() / ".agentmemhub"

    @property
    def db_path(self) -> Path:
        v = self._env.get("AGENTMEMHUB_DB", "")
        if not v:
            v = str(self._get("db_path", ""))
        if v:
            return self._resolve(v)
        return self.data_dir / "agentmemhub.db"

    @property
    def web_port(self) -> int:
        try:
            return int(self._env.get("AGENTMEMHUB_PORT", self._get("web", {}).get("port", 8086)))
        except (TypeError, ValueError):
            return 8086

    # -- 各 Agent Harness 数据位置 ---------------------------------------

    def agent_path(self, source: str) -> Optional[Path]:
        """显式配置的 source 数据路径（未配置返回 None → adapter 走官方默认探测）。"""
        sec = self._get("agents", {}) or {}
        if not isinstance(sec, dict):
            return None
        raw = sec.get(source, "")
        if not raw:
            return None
        return self._resolve(str(raw))

    # -- MemOS 记忆引擎 ---------------------------------------------------

    @property
    def memos_base_url(self) -> str:
        return (self._env.get("MEMOS_BASE_URL")
                or str(self._get("memos", {}).get("base_url", ""))
                or "http://127.0.0.1:18800").rstrip("/")

    @property
    def memos_repo_dir(self) -> Path:
        """MemOS 项目根（含 apps/memos-local-plugin 的完整 repo）。

        默认 <项目根>/memOS —— 用户把 MemOS 平移到项目内后无需任何配置；
        想放到其他目录时在此指定（或 MEMOS_REPO_DIR 环境变量）。
        """
        v = self._env.get("MEMOS_REPO_DIR", "")
        if not v:
            v = str(self._get("memos", {}).get("repo_dir", ""))
        if v:
            return self._resolve(v)
        return PROJECT_ROOT / "memOS"

    @property
    def memos_plugin_dir(self) -> Optional[Path]:
        """apps/memos-local-plugin 目录：显式配置 > memos.repo_dir 推导。

        返回"应得路径"，存在性由调用方判断（find_plugin_dir 探测）。
        """
        v = self._env.get("MEMOS_PLUGIN_DIR", "")
        if not v:
            v = str(self._get("memos", {}).get("plugin_dir", ""))
        if v:
            return self._resolve(v)
        return self.memos_repo_dir / "apps" / "memos-local-plugin"

    @property
    def memos_home(self) -> Optional[Path]:
        """引擎 home（记忆库 / .auth.json / config.yaml 所在）。

        默认 <repo_dir>/home（MemOS 平移到项目内后自动跟随 repo）；未配置由引擎自行决定。
        """
        v = self._env.get("MEMOS_HOME", "")
        if not v:
            v = str(self._get("memos", {}).get("home", ""))
        if v:
            return self._resolve(v)
        return self.memos_repo_dir / "home"

    @property
    def memos_password(self) -> str:
        return (self._env.get("MEMOS_PASSWORD", "")
                or str(self._get("memos", {}).get("password", "")))

    @property
    def memos_lightweight(self) -> Optional[bool]:
        """true/false 强制托管轻量模式；None=交给引擎自身配置。"""
        v = self._get("memos", {}).get("lightweight")
        if v is None and not self._env.get("MEMOS_LIGHTWEIGHT", ""):
            return None
        if v is None:
            return str(self._env.get("MEMOS_LIGHTWEIGHT", "")).lower() in ("1", "true", "on")
        return bool(v)

    # -- 内部 -------------------------------------------------------------

    def _get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._file
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def _resolve(self, raw: str) -> Path:
        p = Path(os.path.expandvars(str(raw))).expanduser()
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        return p

    def __repr__(self) -> str:  # pragma: no cover
        return f"Config(file={self._file_path or '<default>'})"


_instance: Optional[Config] = None


def config() -> Config:
    """进程级单例（测试可传 env/path 重建）。"""
    global _instance
    if _instance is None:
        _instance = Config()
    return _instance


def reset() -> None:
    """重置单例（测试用）。"""
    global _instance
    _instance = None