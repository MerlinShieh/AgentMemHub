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

#: LLM Wiki 编译（把零散记忆归纳成可读页面）。
#:
#: **为什么必须与 distillation 分开配 llm**：两条链路的推理需求是相反的——
#: 蒸馏 / 评分要"想清楚"（思考提升结论质量），而 wiki 编译只是把**已经想清楚的
#: 结论**归纳成页面，相对不需要思考。共用一份 llm 配置会让 wiki 白白付推理开销
#: （实测思维链占其输出的 66%，单次编译 105K → 326K 输出 token）。
#:
#: llm 子段留空 = 全部继承顶层 llm；只写需要覆盖的字段：
#:     llm: {thinking: "disabled"}       # DeepSeek 官方：彻底关闭推理（最省）
#:     llm: {reasoning_effort: "low"}    # 只能调强度的 provider（如 Command Code）
DEFAULT_WIKI = {
    "enabled": True,
    # 第一级是**长调用**：一个会话几十条记忆，分组与逐组编译的输入可达几万字符。
    # 默认 timeout 只有 60s —— 实测 101 条记忆的会话在**分组调用**上连续 3 次读
    # 超时（60s × 3 + 退避 ≈ 187 秒），`LLMTransientError` 抛出后整会话编译失败。
    # 第二级早已在 yaml 里放宽到 900s，第一级此前漏配；**默认值必须是"能跑完"
    # 的**，不能要求使用者先知道这个内部坑才会配。
    "llm": {"timeout": 900},
    "single_shot_max": 12,   # 记忆数 ≤ 此值时单次编译（省一次分组调用）
    # 分组调用的**单批记忆数上限**：分组是"输出规模随输入线性增长"的调用 ——
    # 101 条一次性分组实测会卡死（客户端 60s × 3 次超时；放宽到 900s 后 22 分钟
    # 仍无任何响应）。超出即分批分组，批内编号映射回全局编号
    # （见 `wiki_compile.plan_groups_batched`）。
    "plan_batch_max": 40,
    # 组级（页级）并发编译的并发度：各组之间无依赖，串行纯属浪费（实测 32 组
    # 串行约 48 分钟，并发 3 后约 12 分钟）。真实并发 = 会话级 workers × 本级，
    # 上游有 RPM/TPM 限制，撞 429 会走全局限流闸门退避（`agentmemhub/llm.py`）。
    "page_workers": 3,
    "workers": 4,            # 全量编译的并发数
    # 产出目录的正式配置位 —— 触发器/对齐审计/增量更新的默认目标。
    # 不配则这些功能需要显式传目录。
    "out_l1": "",
    "out_l2": "",
    # 增量更新触发器：检测便宜（align 毫秒级只读）、编译昂贵（LLM），
    # 按规则攒批触发；任一规则命中且有脏数据才真的跑 update。
    "update": {
        "enabled": True,
        "schedule": ["09:00", "12:00", "18:00"],  # 每天这些时点检测一次
        "dirty_memories": 10,   # 脏记忆（新增+变更）达到此数触发一次更新
        "first_write_daily": True,  # 每自然日第一次蒸馏写入后检测并更新
    },
    # 第二级：跨会话聚合（第一级的会话内页面 → 主题实体页）。
    # 它的 llm 子段**继承顶层 llm 而非 wiki.llm** —— 两级是独立的覆盖关系，
    # 级联继承会让第一级的 provider 选择牵连第二级（见 Config.wiki_l2）。
    "l2": {
        "llm": {},
        "workers": 6,        # 聚合阶段调用密集，并发可高于第一级
        "min_pages": 2,      # 一个分组至少这么多页才值得合并成新页
        "domain_max": 60,    # 主题域数量上限（控制归域调用的输出规模）
    },
}


#: 快照保留策略（`snapshot` 段）。
#:
#: 为什么值得单独配：快照是**不可重建产物**的最后一道后悔药——wiki 页面重编要
#: 花钱花时间且结果会漂移，unit_values 里的反馈演化值更是完全无法重建。保留份数
#: 是"磁盘 vs 安全感"的权衡：一份约 190 MB（索引库 176 MB + 两级 wiki 产物 5 MB），
#: 默认 **10 份约 1.8 GB**。磁盘紧就调小，想留更长历史就调大——不必改代码。
#:
#: 实测：整份 gzip 能压到 **67%**（176 MB → 119 MB，6.5 秒），10 份省约 0.5 GB。
#: 但快照的价值在"出事时能立刻恢复"，**可读、可直接打开**比省这点空间更重要
#: （压缩会把恢复路径从"拷回来"变成"先解压"，还多一层压缩文件损坏的风险）；
#: 库里 93% 是含向量 blob 的索引库，本来也压不动多少。故默认**不压缩**。
DEFAULT_SNAPSHOT = {
    "keep": 5,   # 最多保留几份（超出自动删最旧）
}

#: 日志滚动 / 归档策略（`logs` 段）。
#:
#: **规则**（用户确定）：
#:   · **作用域**：`rotate` 是**全局默认**，对所有日志生效；`files.<日志名>`
#:     覆盖它，单文件配置为空（或没写该项）= 回落全局。
#:   · **滚动规则二选一**（不是同时命中）：配了 `max_mb`（>0）→ 按**大小**滚动
#:     —— **这是最高优先级**；没有大小规则但 `daily` 为真 → 按**自然日首次写入**
#:     滚动；两者都没配 → 不滚动。
#:   · **归档**：`<日志目录>/<archive_dir>/<日志名>/<日志名>.<时间戳>` —— 先按
#:     日志名分文件夹，再把归档文件放进去。
#:   · **暂不压缩**：`compress: false` 时归档保留**原始文件数据**（配置位预留，
#:     将来打开即用 gzip）。
#:   · 时间维度的最低单位是**一个自然日（24 小时）**，没有小时级细粒度。
DEFAULT_LOGS = {
    "rotate": {
        "max_mb": 5,         # 超过多少 MB 滚动（0 = 不启用大小规则）
        "daily": False,      # 按自然日首次写入滚动（有大小规则时本项被忽略）
        "keep_days": 30,     # 归档保留天数，超出删最旧（0 = 不限）
        "compress": False,   # 归档时是否 gzip（当前保留原始文件）
        "archive_dir": "archive",   # 归档根目录（相对日志目录）
    },
    "files": {},             # 单文件覆盖，如 {wiki: {max_mb: 10}, tasks: {daily: true}}
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

    #: 可以从 `llm` 段原样透传的标量字段。
    #: **新增 provider 开关时务必加到这里** —— 本段是白名单过滤，漏加会让配置
    #: 静默失效（实测踩过：`thinking` / `reasoning_effort` 写进了 yaml 却读不到，
    #: 表现为"开关不生效"，排查成本很高）。
    _LLM_SCALAR_KEYS = ("timeout", "max_tokens", "temperature",
                        "thinking", "reasoning_effort", "repair_model")

    @property
    def llm(self) -> dict[str, Any]:
        """LLM 接入配置：{endpoint, api_key, model} + 可选的 headers/超参。

        优先级：env（AGENTMEMHUB_LLM_ENDPOINT / _API_KEY / _MODEL）> yaml `llm` 段。
        空值表示未配置，由调用方决定报错或降级。api_key 只读不打印。

        **使用方**：记忆蒸馏（distill.py）与批量评分（scoring.read_engine_llm）
        共用本段——换模型/换服务商只改这一处；蒸馏如需单独覆盖写 distillation.llm。
        headers：provider 特定请求头（部分服务商有硬性要求）。
        thinking / reasoning_effort：思考模式与强度（DeepSeek 系）。
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
        for k in self._LLM_SCALAR_KEYS:
            if sec.get(k) is not None:
                out[k] = sec[k]
        return out

    @classmethod
    def _merge_llm(cls, top: dict[str, Any], sub: dict[str, Any]) -> dict[str, Any]:
        """蒸馏的 llm 子段：标量留空继承顶层；headers 合并（子段优先）。"""
        out: dict[str, Any] = {
            k: (str(sub.get(k) or "") or str(top.get(k) or ""))
            for k in ("endpoint", "api_key", "model", "repair_model")
        }
        headers: dict[str, Any] = {}
        if isinstance(top.get("headers"), dict):
            headers.update(top["headers"])
        if isinstance(sub.get("headers"), dict):
            headers.update(sub["headers"])
        if headers:
            out["headers"] = headers
        for k in cls._LLM_SCALAR_KEYS:
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

    # -- LLM Wiki 编译 ----------------------------------------------------

    @property
    def wiki(self) -> dict[str, Any]:
        """LLM Wiki 编译配置（已合并默认值，调用方无需处理缺键）。

        `llm` 子段留空的字段继承顶层 `llm` —— 顶层配 provider / 密钥 / 请求头，
        这里只覆盖"要不要思考"这类差异项（见 DEFAULT_WIKI 的说明）。
        """
        merged = _deep_merge(DEFAULT_WIKI, self._get("wiki", {}) or {})
        merged["llm"] = self._merge_llm(self.llm, merged.get("llm") or {})
        return merged

    @property
    def wiki_l2(self) -> dict[str, Any]:
        """第二级（跨会话聚合）配置。

        `llm` 的继承链是 **顶层 llm → wiki.l2.llm**，刻意**跳过 wiki.llm**：
        `wiki.llm` 是"第一级用什么模型"的选择，级联下去会让第一级换 provider
        时连带改掉第二级（实测踩过：第一级用 DeepSeek 官方关推理，第二级
        因此丢掉了 Command Code 的凭据）。两级各自相对顶层覆盖，互不牵连。
        """
        merged = _deep_merge(DEFAULT_WIKI, self._get("wiki", {}) or {})
        l2 = dict(merged.get("l2") or {})
        l2["llm"] = self._merge_llm(self.llm, l2.get("llm") or {})
        return l2

    # -- 快照 -------------------------------------------------------------

    @property
    def snapshot(self) -> dict[str, Any]:
        """快照配置（已合并默认值，调用方无需处理缺键）。

        `keep` = 最多保留几份，超出自动删最旧。非法值由 snapshot.keep_count()
        统一兜底（回退内置默认），此处只负责把 yaml 值原样递过去。
        """
        return _deep_merge(DEFAULT_SNAPSHOT, self._get("snapshot", {}) or {})

    # -- 日志 -------------------------------------------------------------

    @property
    def logs(self) -> dict[str, Any]:
        """日志滚动/归档配置（已合并默认值）。

        `rotate` 是全局默认（对所有日志生效），`files.<日志名>` 覆盖它——
        解析成"某个日志的生效策略"由 `agentmemhub.logs.rotate_policy(name)` 负责
        （那里才知道日志名与文件路径的对应关系）。
        """
        return _deep_merge(DEFAULT_LOGS, self._get("logs", {}) or {})

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


# ---------------------------------------------------------------------------
# LLM 生效配置视图（只读）
# ---------------------------------------------------------------------------

#: 四个 LLM 使用点：显示名 → 取值函数（返回该调用方**合并后**的 llm 段）。
#:
#: 为什么要这个视图（2026-09-22）：LLM 的覆盖项是**就地**的（`wiki.llm` /
#: `distillation.llm` / `wiki.l2.llm`）—— 这与日志的 `logs.files.<name>`、
#: 召回档位的 `retrieval.callers.<name>` 那种**集中式覆盖**不同。于是"某个
#: 调用方到底用哪个模型、哪些参数是继承的"要翻好几处配置才能拼出来。
#: 本函数把结果拼好，并标注每一项的来源。
_LLM_USERS: tuple[tuple[str, str, str], ...] = (
    ("记忆蒸馏", "distillation.llm", "distill"),
    ("批量评分", "llm（顶层，无覆盖）", "score"),
    ("Wiki 第一级", "wiki.llm", "wiki_l1"),
    ("Wiki 第二级", "wiki.l2.llm", "wiki_l2"),
)


def _llm_fields(cfg: dict) -> dict[str, Any]:
    """从一份 llm 配置里取"看得见的字段"（api_key 只报存在性，绝不回显）。"""
    out: dict[str, Any] = {}
    for k in ("endpoint", "model", "headers", "timeout", "max_tokens",
              "temperature", "thinking", "reasoning_effort", "repair_model"):
        v = (cfg or {}).get(k)
        if v is None or v == "" or v == {}:
            continue
        out[k] = v
    key = str((cfg or {}).get("api_key") or "")
    out["api_key"] = "（已设置）" if key else "（未设置）"
    return out


def llm_effective(conf: "Config | None" = None) -> dict[str, Any]:
    """各 LLM 使用点**最终生效**的配置 + 每项的来源（继承 / 覆盖）。

    返回 `{"top": {...}, "users": [{"name", "path", "values", "overrides",
    "inherits"}, ...]}`。`overrides` 是与顶层**取值不同**的字段（即该调用方
    真正覆盖了什么），`inherits` 是沿用顶层的字段。
    """
    c = conf or config()
    top = c.llm
    top_fields = _llm_fields(top)
    # 四个调用方各自的合并结果（与生产链路读的是同一份属性，不会各说各话）
    raw: dict[str, dict] = {
        "distill": (c.distillation or {}).get("llm") or {},
        "score": top,
        "wiki_l1": (c.wiki or {}).get("llm") or {},
        "wiki_l2": (c.wiki_l2 or {}).get("llm") or {},
    }
    users = []
    for name, path, key in _LLM_USERS:
        vals = _llm_fields(raw[key])
        overrides, inherits = [], []
        for k, v in vals.items():
            if k == "api_key":
                continue
            if top_fields.get(k) == v:
                inherits.append(k)
            else:
                overrides.append(k)
        users.append({"name": name, "path": path, "values": vals,
                      "overrides": overrides, "inherits": inherits})
    return {"top": {"path": "llm（顶层）", "values": top_fields}, "users": users}