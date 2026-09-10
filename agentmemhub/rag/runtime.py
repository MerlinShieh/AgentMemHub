"""进程级单例：OnnxEmbedder 缓存（模型加载 ~150ms、内存驻留百 MB，绝不每调用重建）。

调用方（rag_bridge / cli / 面板任务线程）统一经 get_embedder 取用；
线程安全：初始化加锁，实例本身（ORT session + tokenizer）并发推理安全。
"""
from __future__ import annotations

import logging
import threading

from .config import ModelSpec, Settings
from .embedder import OnnxEmbedder

_LOCK = threading.Lock()
_CACHE: dict[tuple, OnnxEmbedder] = {}


def get_embedder(spec: ModelSpec, *, batch_size: int | None = None,
                 settings: Settings | None = None) -> OnnxEmbedder:
    """取（并缓存）模型 embedder。

    批大小/分桶参数优先取 settings.embed（配置文件），未传则用内置默认。
    """
    emb_cfg = (settings.embed if settings else None) or {}
    bs = int(batch_size or emb_cfg.get("batch_size") or 32)
    bucketing = bool(emb_cfg.get("bucketing", True))
    caps = emb_cfg.get("bucket_caps") or [64, 128, 256, 384]
    threads = int(emb_cfg.get("intra_op_threads") or 0)
    key = (spec.id, spec.dim, spec.quantized, bs, bucketing, tuple(caps), threads)
    with _LOCK:
        emb = _CACHE.get(key)
        if emb is None:
            emb = OnnxEmbedder(
                spec, batch_size=bs, bucketing=bucketing, bucket_caps=list(caps),
                intra_op_threads=threads or None,
                log=logging.getLogger("asrag.embedder"))
            _CACHE[key] = emb
        return emb


def get_active_embedder(settings: Settings) -> OnnxEmbedder:
    return get_embedder(settings.active_spec, settings=settings)


def reset_embedders() -> None:
    """测试/换模型用。"""
    with _LOCK:
        _CACHE.clear()
