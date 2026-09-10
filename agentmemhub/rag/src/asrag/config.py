"""配置单点：models.json 注册表 + 项目路径。禁止在其他模块硬编码模型 id/维度/路径。"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


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
    query_prefix: str = ""

    @property
    def onnx_file(self) -> Path:
        name = "model_quantized.onnx" if self.quantized else "model.onnx"
        return self.path / "onnx" / name

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

    @property
    def active_spec(self) -> ModelSpec:
        return self.model(self.active_model)

    def model(self, model_id: str) -> ModelSpec:
        try:
            return self.models[model_id]
        except KeyError:
            raise KeyError(
                f"models.json 中未注册模型 {model_id!r}，"
                f"已注册：{sorted(self.models)}"
            ) from None


def load_settings(root: Path | str | None = None) -> Settings:
    """读取 models.json 注册表并校验文件存在性。root 可注入（测试隔离用）。"""
    root = Path(root) if root else PROJECT_ROOT
    registry_file = root / "models.json"
    if not registry_file.exists():
        raise FileNotFoundError(f"缺少注册表文件：{registry_file}")
    raw = json.loads(registry_file.read_text(encoding="utf-8"))

    models: dict[str, ModelSpec] = {}
    for mid, m in raw.get("models", {}).items():
        spec = ModelSpec(
            id=mid,
            path=(root / m["path"]).resolve(),
            dim=int(m["dim"]),
            pooling=m.get("pooling", "cls"),
            normalize=bool(m.get("normalize", True)),
            quantized=bool(m.get("quantized", True)),
            max_tokens=int(m.get("maxTokens", 512)),
            language=m.get("language", "zh"),
            query_prefix=m.get("queryPrefix", ""),
        )
        models[mid] = spec

    active = raw.get("active", "")
    if active not in models:
        raise ValueError(
            f"models.json active={active!r} 未注册，可用：{sorted(models)}"
        )
    spec = models[active]
    for f in (spec.onnx_file, spec.tokenizer_file, spec.path / "config.json"):
        if not f.exists():
            raise FileNotFoundError(
                f"激活模型 {active!r} 缺少文件：{f}"
                f"（注意量化版仅含 model_quantized.onnx）"
            )

    return Settings(
        root=root,
        models=models,
        active_model=active,
        source_db=root / "database" / "agentmemhub.db",
        index_db=root / "database" / "session_rag.db",
        log_dir=root / "logs",
    )
