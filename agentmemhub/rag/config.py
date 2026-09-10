"""配置单点：统一读 agentmemhub.yaml 的 rag 段（兼容旧 models.json）。

禁止在其他模块硬编码模型 id/维度/路径/批大小——一切经本模块的 Settings。
优先级：agentmemhub.yaml > models.json（旧格式回退）> 内置默认。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: 内置默认（yaml 缺失项时使用）
DEFAULT_EMBED = {
    "batch_size": 32,
    "bucketing": True,
    "bucket_caps": [64, 128, 256, 384],
    "intra_op_threads": 0,
}
DEFAULT_RETRIEVAL = {
    "models": [],                # 空 = 仅用 active
    "candidate_k": 30,
    "threshold_floor": 0.2,
    "max_per_conversation": 2,
    "search_max_hits": 20,
    "rrf_k": 60,
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


def load_settings(root: Path | str | None = None) -> Settings:
    """读配置（yaml 优先，models.json 兼容回退），校验激活模型文件齐全。"""
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

    return Settings(
        root=root,
        models=models,
        active_model=active,
        source_db=root / "database" / "agentmemhub.db",
        index_db=root / "database" / "session_rag.db",
        log_dir=root / "logs",
        embed=embed or dict(DEFAULT_EMBED),
        retrieval=retrieval or dict(DEFAULT_RETRIEVAL),
        write=write or dict(DEFAULT_WRITE),
    )
