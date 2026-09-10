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


def get_embedder(spec: ModelSpec, *, batch_size: int = 32) -> OnnxEmbedder:
    key = (spec.id, spec.dim, spec.quantized, batch_size)
    with _LOCK:
        emb = _CACHE.get(key)
        if emb is None:
            emb = OnnxEmbedder(
                spec, batch_size=batch_size,
                log=logging.getLogger("asrag.embedder"))
            _CACHE[key] = emb
        return emb


def get_active_embedder(settings: Settings) -> OnnxEmbedder:
    return get_embedder(settings.active_spec)


def reset_embedders() -> None:
    """测试/换模型用。"""
    with _LOCK:
        _CACHE.clear()
