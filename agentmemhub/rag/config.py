"""配置单点：统一读 agentmemhub.yaml 的 rag 段（兼容旧 models.json）。

禁止在其他模块硬编码模型 id/维度/路径/批大小——一切经本模块的 Settings。
优先级：agentmemhub.yaml > models.json（旧格式回退）> 内置默认。
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# 检索调用方（决定用哪一档召回严格度）
# ---------------------------------------------------------------------------

#: 当前检索的**调用方**（`mcp` / `panel` / `cli` / `agent` …；空 = 未标记）。
#:
#: **为什么用 contextvar 而不是层层传参**：档位在 `Settings` 里被多处读取
#: （`recall_level` → `recall_profile` → `page_policy`，而 `page_policy` 又被
#: `hybrid_search` 内部使用）—— 传参要改一整条链路的签名。contextvar 让配置层
#: 直接读到当前调用方，`hybrid_search` 一行都不用动。
#: 与 `logs._MEMORY_PATH`（记忆操作来源）是**同一模式**。
_RETRIEVAL_CALLER: contextvars.ContextVar[str] = contextvars.ContextVar(
    "agentmemhub_retrieval_caller", default="")


@contextlib.contextmanager
def retrieval_caller(name: str):
    """标记其内检索的调用方：`with retrieval_caller("mcp"): ...`。"""
    token = _RETRIEVAL_CALLER.set(name)
    try:
        yield
    finally:
        _RETRIEVAL_CALLER.reset(token)


def current_retrieval_caller() -> str:
    """当前上下文里的检索调用方（未标记时为空串）。"""
    return _RETRIEVAL_CALLER.get()


#: 内置默认（yaml 缺失项时使用）
DEFAULT_EMBED = {
    "batch_size": 32,
    "bucketing": True,
    "bucket_caps": [64, 128, 256, 384],
    "intra_op_threads": 0,
}
DEFAULT_RETRIEVAL = {
    "models": [],                # 空 = 仅用 active
    #: 召回严格度档位（1 最严格 … 5 最宽松）——统一控制候选宽度/阈值/页面席位，
    #: 见 RECALL_LEVELS。默认 **3（均衡）**：实测 1~2 档要求字面证据 + 终审
    #: 0.85~0.90，会让 windowsctrol/AI助手/deskflow 这类查询**各只返回 1 条**，
    #: 接近"查了没结果"；3 档起才是"严格但可用"（每查询 2~4 条、页面 1~3 条）。
    #: 要更严或更松，只改这一个数字。
    "recall_level": 3,
    "candidate_k": 30,
    "threshold_floor": 0.2,
    "max_per_conversation": 2,
    "search_max_hits": 20,
    "rrf_k": 60,
}

#: 召回严格度档位表：**1 最严格 … 5 最宽松**（`rag.retrieval.recall_level`）。
#:
#: 为什么要有档位：调优过程中攒下了三类互相牵制的旋钮，散着配很容易配出
#: 自相矛盾的组合（比如"页面只给 1 席"配"必须无证据才入选"）。档位把它们
#: 收成一条单调的刻度，调用方只回答一个问题："我现在要少而准，还是全而杂？"
#:
#: 三类参数：
#:   · candidate_k       各通道取多少候选（越大越容易捞到长尾）
#:   · threshold_floor   **非页面**条目的相对阈值（越高越严）
#:   · curate_floor      终审截断的相对阈值（桥接层 safe_cutoff）
#:   · page.*            页面层席位与证据要求（见 DEFAULT_PAGE_POLICY）
#:
#: 档位与"证据"的关系（实测教训的固化，见 search.apply_page_policy）：
#:   **证据只对低分页面生效**（`literal_required_below` 之下必须有字面证据），
#:   高分页面一律只看分数——所以档位表里**不设** `min_evidence` 硬门槛：
#:   实测档 3 曾用 min_evidence="vector"，结果把「网络环境确认与连接故障排查」
#:   （仅池内信号、但分数 0.668 是候选里最高的页面）挡在门外，而 0.334/0.197
#:   两条低分字面页入选——**正好与"高分语义优先"相反**。
#:   各档的"严/松"由 `literal_required_below`（多少分以上可只看分数）与席位表达。
#:
#: **默认 3（均衡）**：1~2 档实测过于极端（三个查询各只返回 1 条），
#: 3 档是"严格但可用"的下沿；需要极限精度时手动下调，需要宽召回时上调。
DEFAULT_RECALL_LEVEL = 3

RECALL_LEVELS = {
    1: {"name": "最严格", "candidate_k": 20, "threshold_floor": 0.35,
        "curate_floor": 0.90, "nonpage_literal_seats": 1,
        "page": {"max_in_results": 1, "literal_seats": 1,
                 "literal_required_below": 0.95, "floor_ratio": 0.5}},
    2: {"name": "严格", "candidate_k": 25, "threshold_floor": 0.30,
        "curate_floor": 0.85, "nonpage_literal_seats": 2,
        "page": {"max_in_results": 2, "literal_seats": 1,
                 "literal_required_below": 0.85, "floor_ratio": 0.3}},
    3: {"name": "均衡", "candidate_k": 30, "threshold_floor": 0.20,
        "curate_floor": 0.80, "nonpage_literal_seats": 2,
        "page": {"max_in_results": 3, "literal_seats": 1,
                 "literal_required_below": 0.70, "floor_ratio": 0.1}},
    4: {"name": "宽松", "candidate_k": 40, "threshold_floor": 0.15,
        "curate_floor": 0.75, "nonpage_literal_seats": 3,
        "page": {"max_in_results": 4, "literal_seats": 2,
                 "literal_required_below": 0.50, "floor_ratio": 0.0}},
    5: {"name": "最宽松", "candidate_k": 60, "threshold_floor": 0.10,
        "curate_floor": 0.70, "nonpage_literal_seats": 4,
        "page": {"max_in_results": 5, "literal_seats": 3,
                 "literal_required_below": 0.30, "floor_ratio": 0.0}},
}

#: `nonpage_literal_seats`：**非页面层的字面兜底席位**（记忆/消息）。
#:
#: 为什么需要（2026-09-22 实测）：终审原先对非页面只套一条乘性相对门限
#: （`score ≥ curate_floor × top`），而 **top 本身可能是噪声** —— 拼错查询
#: `windowsctrol` 的 top1 是完全无关的字面命中(1.238)，门限被抬到 0.990，
#: 24 条候选只剩 4 条，真相关的「WindowsControl 项目架构与技术栈」(0.701，
#: 命中 fts+vec) 被白白切掉；而**同一批结果里的页面**因为有字面兜底席，
#: 连 0.195 的真相关页都进了结果 —— **这个不对称本身就是缺陷**。
#:
#: 与页面的 `literal_seats` 同构：席位只留给"**低分但命中字面通道**
#: （fts/ident/phrase）"的条目 —— 低分且只有向量分的仍然切掉，否则等于取消门限。
#: 档位越高（越宽松）给得越多，与 `page.literal_seats` 同方向。

#: 页面层（L2 知识页）准入策略——见 search.apply_page_policy 的实测依据。
#:
#: 为什么需要单独一套策略：页面是**长文本聚合产物**，池子只有几百条，
#: "页面池内 KNN top-k"对任何查询都成立（哪怕查询与知识库毫不相关），
#: 于是页面在结果里刷屏（实测 12/12 查询出现页面、平均 4.2 条、最差全页面）。
DEFAULT_PAGE_POLICY = {
    #: 页面独立通道的候选数（向量 + 全文各取这么多）
    "channel_k": 8,
    #: 最终结果里页面最多几条（配额——页面不能无限占位）。
    #:
    #: 配额是**唯一**的数量约束，所以它直接决定"会不会漏"。此处默认值与
    #: 默认档（recall_level=3）一致；生产中总由档位覆盖。
    "max_in_results": 3,
    #: 其中**保底留给"低分但有字面证据"页面**的席位数（"低分模糊匹配"的兜底）。
    #:
    #: 为什么必须保底：中分的池内页（如拼错查询里的「Clink」0.627）会按分数把
    #: 低分字面页（0.014~0.026）全部挤出配额——实测那样 `windowsctrol` 的 3 条
    #: 真相关页只剩 0 条。保底席把它们救回来，同时不挤掉高分语义页。
    "literal_seats": 1,
    #: **低于此分数的页面必须命中字面证据（page_fts/fts/ident）才准入**；高于此分
    #: 分数本身就作数（视为"高分语义相关"）。
    #:
    #: 这条规则取代了早先的"证据等级绝对优先"——后者实测会让**低分字面匹配挤掉
    #: 高分语义相关**：查询 "github网络失败" 时，「GitHub 文件抓取方法」0.334 与
    #: 「GitHub账户认证」0.197 占满了 3 个席位，而真正对症的「网络环境确认与连接
    #: 故障排查」0.848、「opencode 卡在网络故障」0.841 因"没有字面证据"落选。
    #:
    #: 而字面兜底仍然必要：拼错/近义表达（"windowsctrol"）时，真相关页面往往只有
    #: 0.014~0.026 的低分（靠 page_fts 残缺匹配），不认字面证据就会被高分噪声
    #: （「Clink」0.627）全挤出局。所以规则是——**高分看分数，低分看字面证据**。
    "literal_required_below": 0.7,
    #: **不要用分数卡页面**：实测 231 页 × 12 查询，页面融合分与相关性甚至
    #: 反相关——相关页落在 0.21~1.02×top，噪声页稳定在 0.52~0.68×top
    #: （"LLM Wiki 工程化"页词面命中第 1 名却只有 0.21×top，而完全无关的
    #: "Mobile_App_AutoTest 发布记录"有 0.63×top）。原因是页面作为长文本聚合
    #: 产物，融合分被"短条目天然高分 + 价值加权"系统性压制，与记忆/消息
    #: **不可比**。故默认 0 = 不设门限；需要时可收紧做二次过滤。
    "floor_ratio": 0.0,
    #: 页面的**最低证据等级**（any / vector / literal）。默认 any = 不设限，
    #: 证据只用于**排序分层**，不剔除任何页面。
    #:
    #: 为什么默认不设限：实测查询 "windowsctrol"（拼错）时，语义最相关的
    #: 「Windows Control Core 窗口控制内核」**只有最弱的池内信号**，而噪声
    #: 「Clink」反而被全局向量池命中——把证据当门槛会**同时做错两件事**：
    #: 把真相关的判死、把噪声放进来。拼错、换词、近义表达这些场景下，"真相关"
    #: 恰恰最缺字面证据。要的是"相关的不遗漏 + 排得靠前"，不是"只留有证据的"。
    "min_evidence": "any",
    #: 准入页面保底占位：有证据的页面融合分天然低（0.21×top），纯按分数排
    #: 会被记忆挤出最终 k 条——而页面通道的立身之本正是"异质候选源各自成路"。
    "reserve_seats": True,
}
DEFAULT_WRITE = {
    "order": [],                 # 空 = 仅 active
    "fast_first": True,
    "background_hint": "高精度模型正在后台向量化，稍后自动生效",
}


@dataclass(frozen=True)
class ModelSpec:
    id: str
    path: Path
    dim: int
    pooling: str          # "cls" | "mean"
    normalize: bool
    quantized: bool
    max_tokens: int
    language: str
    query_prefix: str = ""     # 查询侧前缀（E5 用 "query: "）
    passage_prefix: str = ""   # 文档侧前缀（E5 用 "passage: "）
    family: str = ""           # 家族标签（异族融合判断用）

    @property
    def onnx_file(self) -> Path:
        # 量化文件名不统一：Xenova 用 model_quantized.onnx，
        # text2vec 等仓库自带的是 model_qint8_*.onnx —— 依次探测
        onnx_dir = self.path / "onnx"
        candidates = (
            ["model_quantized.onnx"] if self.quantized else []
        ) + [
            "model_qint8_avx512_vnni.onnx",
            "model_qint8_avx512.onnx",
            "model_quantized.onnx",
            "model.onnx",
        ]
        for name in candidates:
            p = onnx_dir / name
            if p.exists():
                return p
        return onnx_dir / ("model_quantized.onnx" if self.quantized else "model.onnx")

    @property
    def tokenizer_file(self) -> Path:
        return self.path / "tokenizer.json"

    @property
    def vec_table(self) -> str:
        """每模型独立向量表名（模型切换契约：新老共存）。"""
        return "vec_" + re.sub(r"[^0-9a-zA-Z]+", "_", self.id).lower()


@dataclass(frozen=True)
class Settings:
    root: Path
    models: dict[str, ModelSpec]
    active_model: str
    source_db: Path
    index_db: Path
    log_dir: Path
    embed: dict = field(default_factory=lambda: dict(DEFAULT_EMBED))
    retrieval: dict = field(default_factory=lambda: dict(DEFAULT_RETRIEVAL))
    write: dict = field(default_factory=lambda: dict(DEFAULT_WRITE))

    @property
    def active_spec(self) -> ModelSpec:
        return self.model(self.active_model)

    def model(self, model_id: str) -> ModelSpec:
        try:
            return self.models[model_id]
        except KeyError:
            raise KeyError(
                f"未注册模型 {model_id!r}，已注册：{sorted(self.models)}"
            ) from None

    @property
    def write_order(self) -> list[str]:
        """向量化写入顺序（先跑的先可用）；空则仅 active。"""
        order = [m for m in (self.write.get("order") or [])
                 if m in self.models]
        return order or [self.active_model]

    @property
    def retrieval_models(self) -> list[str]:
        """参与召回的多路模型；空则仅 active。"""
        ms = [m for m in (self.retrieval.get("models") or [])
              if m in self.models]
        return ms or [self.active_model]

    @property
    def global_recall_level(self) -> int:
        """**全局**召回严格度档位（`retrieval.recall_level`，不按调用方）。

        非法值（非整数 / 越界）一律回退默认档——它决定的是"给不给结果"，
        配置写错时宁可回到最保守的一档，也不要静默放宽。
        """
        raw = self.retrieval.get("recall_level", DEFAULT_RECALL_LEVEL)
        try:
            lv = int(raw)
        except (TypeError, ValueError):
            return DEFAULT_RECALL_LEVEL
        return lv if lv in RECALL_LEVELS else DEFAULT_RECALL_LEVEL

    def recall_level_for(self, caller: str = "") -> int:
        """某调用方的**生效**档位：全局 `recall_level` < `retrieval.callers.<caller>`。

        与日志的滚动策略**同一模式**（全局 `logs.rotate` < `logs.files.<name>`）：
        默认走全局，配了细分就用细分的。

        细分配错时的回落目标是**全局档位**而不是默认档 —— 一个写错的覆盖项
        不该比全局更严（那会让"想调松"的意图反向生效）。
        """
        if not caller:
            return self.global_recall_level
        raw = (self.retrieval.get("callers") or {}).get(caller)
        if raw is None:
            return self.global_recall_level
        try:
            lv = int(raw)
        except (TypeError, ValueError):
            return self.global_recall_level
        return lv if lv in RECALL_LEVELS else self.global_recall_level

    @property
    def recall_level(self) -> int:
        """**当前调用方**的生效档位（读 contextvar；未标记时 = 全局档位）。"""
        return self.recall_level_for(current_retrieval_caller())

    @property
    def recall_profile(self) -> dict:
        """当前调用方档位展开后的召回参数（带 level / name，便于日志与界面）。"""
        lv = self.recall_level
        return {"level": lv, **RECALL_LEVELS[lv]}

    @property
    def page_policy(self) -> dict:
        """页面层策略：内置默认 < **档位** < yaml 显式 `retrieval.page.*`。

        优先级这样排：档位是"整体旋钮"，而用户临时只想改某一项时（例如单独
        把页面席位调大），yaml 里的显式配置应当能覆盖档位、不必换档。
        `retrieval` 段本身只做浅合并，嵌套的 `page` 需要在这里再合一次。
        """
        raw = self.retrieval.get("page") or {}
        if not isinstance(raw, dict):
            raw = {}
        prof = self.recall_profile.get("page") or {}
        return {**DEFAULT_PAGE_POLICY, **prof, **raw}


def _load_rag_section(root: Path) -> tuple[dict, dict, dict, dict, str]:
    """读 agentmemhub.yaml 的 rag 段。返回 (models, embed, retrieval, write, active)。"""
    yml = root / "agentmemhub.yaml"
    if yml.exists():
        try:
            import yaml
            cfg = yaml.safe_load(yml.read_text(encoding="utf-8")) or {}
            rag = cfg.get("rag") or {}
            if rag.get("models"):
                return (rag.get("models") or {},
                        {**DEFAULT_EMBED, **(rag.get("embed") or {})},
                        {**DEFAULT_RETRIEVAL, **(rag.get("retrieval") or {})},
                        {**DEFAULT_WRITE, **(rag.get("write") or {})},
                        str(rag.get("active") or ""))
        except Exception:
            pass          # yaml 不可用/解析失败 → 回退 models.json
    return {}, {}, {}, {}, ""


def _load_models_json(root: Path) -> tuple[dict, str]:
    """旧格式回退：models.json。"""
    f = root / "models.json"
    if not f.exists():
        return {}, ""
    raw = json.loads(f.read_text(encoding="utf-8"))
    return raw.get("models", {}), str(raw.get("active") or "")


def _data_dir_override() -> Path | None:
    """AGENTMEM_HUB_DATA_DIR 覆盖（与顶层 agentmemhub/config.py 同源语义）。

    沙箱隔离：一个环境变量同时切换采集库与索引库，使实验（如记忆蒸馏）
    完全不碰生产数据。日志目录**不跟随**——既定语义是统一 <程序根>/logs
    （见 agentmemhub/logs.py），沙箱实验日志仍可在同一处追溯。
    """
    v = os.environ.get("AGENTMEM_HUB_DATA_DIR", "").strip()
    return Path(v) if v else None


def load_settings(root: Path | str | None = None) -> Settings:
    """读配置（yaml 优先，models.json 兼容回退），校验激活模型文件齐全。

    数据目录解析（优先级）：
    - **显式传入 root** → `root/database`（调用方指定根，测试隔离用，不受环境变量干扰）；
    - 无参调用 → `AGENTMEM_HUB_DATA_DIR` 环境变量优先（沙箱/测试隔离），
      否则项目内 `PROJECT_ROOT/database`。

    注：`rag_bridge.settings()` 会以自己的 hub 配置真源再覆盖一次 source_db/
    index_db（见 rag_bridge._derive_settings），两条路径结果一致。
    """
    explicit_root = root is not None
    root = Path(root) if root else PROJECT_ROOT
    models_raw, embed, retrieval, write, active = _load_rag_section(root)
    if not models_raw:
        models_raw, active = _load_models_json(root)
    if not models_raw and not active:
        # 完全无配置（既无 yaml.rag.models 也无 models.json）→ 明确报错
        (root / "models.json")  # noqa: B018  (仅为可读性)
        if not (root / "models.json").exists() and not (root / "agentmemhub.yaml").exists():
            raise FileNotFoundError(
                f"未找到模型配置：请在 {root / 'agentmemhub.yaml'} 配置 "
                f"rag.models，或提供旧格式 {root / 'models.json'}")
        # 有配置但 models 为空 → 交由下方 active 校验报"未注册"
        models_raw = {k: v for k, v in models_raw.items()}

    models: dict[str, ModelSpec] = {}
    for mid, m in models_raw.items():
        models[mid] = ModelSpec(
            id=mid,
            path=(root / m["path"]).resolve(),
            dim=int(m["dim"]),
            pooling=m.get("pooling", "cls"),
            normalize=bool(m.get("normalize", True)),
            quantized=bool(m.get("quantized", True)),
            max_tokens=int(m.get("maxTokens", 512)),
            language=m.get("language", "zh"),
            query_prefix=m.get("queryPrefix", ""),
            passage_prefix=m.get("passagePrefix", ""),
            family=m.get("family", ""),
        )

    if active not in models:
        raise ValueError(
            f"active={active!r} 未注册，可用：{sorted(models)}")
    spec = models[active]
    for f in (spec.onnx_file, spec.tokenizer_file, spec.path / "config.json"):
        if not f.exists():
            raise FileNotFoundError(
                f"激活模型 {active!r} 缺少文件：{f}"
                f"（量化版文件名会被自动探测）")

    # 数据目录：显式 root 优先；无参调用时环境变量优先（沙箱隔离）
    data_dir = (root / "database") if explicit_root else (
        _data_dir_override() or (root / "database"))
    return Settings(
        root=root,
        models=models,
        active_model=active,
        source_db=data_dir / "agentmemhub.db",
        index_db=data_dir / "session_rag.db",
        log_dir=root / "logs",
        embed=embed or dict(DEFAULT_EMBED),
        retrieval=retrieval or dict(DEFAULT_RETRIEVAL),
        write=write or dict(DEFAULT_WRITE),
    )
