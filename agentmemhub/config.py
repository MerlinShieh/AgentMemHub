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

#: 记忆蒸馏默认配置（yaml distillation 段逐键覆盖；嵌套子段各自合并）
DEFAULT_DISTILL = {
    "enabled": True,
    # prompt_ver：留空（None）= 跟随代码里的 PROMPT_VER 常量（提示词一改就自动
    # 触发重蒸）；显式给整数则覆盖之（用于强制重蒸，如临时排查）。
    # 不设默认数字：否则改提示词易忘记同步，旧结果永不更新。
    "prompt_ver": None,
    "slice": {
        "max_chars": 24000,        # 单片字符预算（上下文保险）
        "max_turns": 16,           # 单片轮数预算；单轮巨会话走字符硬切
        "topic_boundary": True,    # 用关键词重叠度细化话题边界
        "boundary_window": 4,      # 粗切点 ±N 轮内找最低重叠处
        "per_message_cap": 2000,   # 单条消息入 prompt 时的截断
    },
    "merge": {
        "enabled": True,           # 同会话多片合并沉淀
        # 单次合并调用的输入字符预算。**必须远小于切片预算**：合并的输出是
        # "合并后的完整条目列表"，输入越大输出越大——实测 24000 字符（155 条）
        # 时推理模型把 8192 token 全耗在 reasoning 上，正文为空（确定性失败）。
        # 调小到 8000（约 40-60 条/批）后输出规模可控。
        "max_chars": 8000,
        # 层级收敛最多轮数：批次变小 → 批数变多，收敛需要的轮数也更多
        # （433 条 → 40/批 → 11 批 → 逐轮减半，约 5 轮收敛）
        "max_rounds": 6,
    },
    "dedup": {
        "cosine_duplicate": 0.92,  # ≥ 判重复（丢弃 + 指向已有）
        "cosine_similar": 0.80,    # ≥ 判相似（入库 + 打标互链）
    },
    "sanitize": {"enabled": True},  # 正则脱敏兜底
    "runtime": {
        "max_concurrent": 4,
        "timeout": 60,
        "dry_run": False,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """深合并（嵌套 dict 递归合并，其余类型直接覆盖）。"""
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


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
                             PROJECT_ROOT / "database" / "config.yaml",
                             Path.home() / ".agentmemhub" / "config.yaml"):
                    if cand.exists():
                        cfg_path = cand
                        break
        self._file: dict = _load_yaml(cfg_path) if cfg_path and cfg_path.exists() else {}
        self._file_path = cfg_path

    # -- 基础目录 --------------------------------------------------------

    @property
    def data_dir(self) -> Path:
        """可写数据根目录（db / watermarks / 评分状态 / 托管 pid）。

        默认项目内 <项目根>/database/（随项目走，便于备份与整机迁移）；
        仍可用 AGENTMEM_HUB_DATA_DIR 环境变量或 yaml data_dir 覆盖（测试隔离用）。
        """
        v = self._env.get("AGENTMEM_HUB_DATA_DIR", "")
        if not v:
            v = str(self._get("data_dir", ""))
        if v:
            return self._resolve(v)
        return PROJECT_ROOT / "database"

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

    # -- 记忆后端选择（R3 重构开关）--------------------------------------

    @property
    def memory_backend(self) -> str:
        """'rag'（内置 agentmemhub.rag 引擎，默认）| 'memos'（vendored MemOS，回退用）。

        优先级：env AGENTMEMHUB_BACKEND > yaml backend.backend > 默认 'rag'。
        改回 'memos' 即整体回退旧 HTTP 引擎路径（memOS/ 目录未删，一行回滚）。
        """
        raw = (self._env.get("AGENTMEMHUB_BACKEND", "")
               or str((self._get("backend", {}) or {}).get("backend", "")))
        v = raw.strip().lower()
        return v if v in ("rag", "memos") else "rag"

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

    # -- LLM（蒸馏 / 评分共用）-------------------------------------------

    @property
    def llm(self) -> dict[str, Any]:
        """LLM 接入配置：{endpoint, api_key, model} + 可选的 headers/超参。

        优先级：env（AGENTMEMHUB_LLM_ENDPOINT / _API_KEY / _MODEL）> yaml `llm` 段。
        空值表示未配置，由调用方决定报错或降级。api_key 只读不打印。

        **使用方**：记忆蒸馏（distill.py）与批量评分（scoring.read_engine_llm）
        共用本段——换模型/换服务商只改这一处；蒸馏如需单独覆盖写 distillation.llm。
        headers：provider 特定请求头（部分服务商有硬性要求）。
        """
        sec = self._get("llm", {}) or {}
        if not isinstance(sec, dict):
            sec = {}
        out: dict[str, Any] = {
            "endpoint": str(self._env.get("AGENTMEMHUB_LLM_ENDPOINT", "")
                            or sec.get("endpoint", "") or ""),
            "api_key": str(self._env.get("AGENTMEMHUB_LLM_API_KEY", "")
                           or sec.get("api_key", "") or ""),
            "model": str(self._env.get("AGENTMEMHUB_LLM_MODEL", "")
                         or sec.get("model", "") or ""),
        }
        headers = sec.get("headers")
        if isinstance(headers, dict) and headers:
            out["headers"] = {str(k): str(v) for k, v in headers.items()}
        for k in ("timeout", "max_tokens", "temperature"):
            if sec.get(k) is not None:
                out[k] = sec[k]
        return out

    @staticmethod
    def _merge_llm(top: dict[str, Any], sub: dict[str, Any]) -> dict[str, Any]:
        """蒸馏的 llm 子段：标量留空继承顶层；headers 合并（子段优先）。"""
        out: dict[str, Any] = {
            k: (str(sub.get(k) or "") or str(top.get(k) or ""))
            for k in ("endpoint", "api_key", "model")
        }
        headers: dict[str, Any] = {}
        if isinstance(top.get("headers"), dict):
            headers.update(top["headers"])
        if isinstance(sub.get("headers"), dict):
            headers.update(sub["headers"])
        if headers:
            out["headers"] = headers
        for k in ("timeout", "max_tokens", "temperature"):
            v = sub.get(k) if sub.get(k) is not None else top.get(k)
            if v is not None:
                out[k] = v
        return out

    # -- 记忆蒸馏 ---------------------------------------------------------

    @property
    def distillation(self) -> dict[str, Any]:
        """记忆蒸馏配置（已合并默认值，调用方无需处理缺键）。

        `llm` 子段留空的字段继承顶层 `llm` —— 默认只配一处即可两处共用；
        需要蒸馏走不同模型时再单独覆盖（如更便宜/更长上下文的模型）。
        """
        merged = _deep_merge(DEFAULT_DISTILL, self._get("distillation", {}) or {})
        merged["llm"] = self._merge_llm(self.llm, merged.get("llm") or {})
        return merged

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