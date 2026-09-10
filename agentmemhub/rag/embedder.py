"""Embedder：模型无关的向量化接口。

契约（模型切换一等公民）：
- 一切行为由 ModelSpec 注册表驱动：onnx 文件、pooling、归一化、截断窗口、query 前缀；
- 首次推理实测维度并与 spec.dim 断言，注册表漂移立刻报错（拒绝 MemOS 式静默截断）；
- 输出恒为 (N, dim) float32、L2 归一化（spec.normalize 时）。
"""
from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from typing import Sequence

import numpy as np

from .config import ModelSpec


class Embedder(ABC):
    """向量化后端抽象。onnxruntime 只是第一个实现。"""

    spec: ModelSpec

    @abstractmethod
    def encode_passages(self, texts: Sequence[str]) -> np.ndarray:
        """会话/文档侧嵌入。返回 (len(texts), dim)。"""

    @abstractmethod
    def encode_query(self, text: str) -> np.ndarray:
        """查询侧嵌入（含模型自带指令前缀时由实现处理）。返回 (dim,)。"""


def _cls_pool(hidden: np.ndarray) -> np.ndarray:
    return hidden[:, 0, :]


def _mean_pool(hidden: np.ndarray, mask: np.ndarray) -> np.ndarray:
    m = mask.astype(np.float32)[:, :, None]
    summed = (hidden * m).sum(axis=1)
    counts = np.clip(m.sum(axis=1), 1e-9, None)
    return summed / counts


def _l2_normalize(v: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(v, axis=-1, keepdims=True)
    return v / np.clip(norms, 1e-12, None)


class OnnxEmbedder(Embedder):
    """onnxruntime + HF tokenizers 加载本地 ONNX 模型（默认 q8 量化版）。"""

    def __init__(
        self,
        spec: ModelSpec,
        *,
        batch_size: int = 32,
        intra_op_threads: int | None = None,
        bucket_caps: list[int] | None = None,
        bucketing: bool = True,
        log: logging.Logger | None = None,
    ):
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.spec = spec
        self.batch_size = max(1, int(batch_size))
        self.bucketing = bool(bucketing)
        self.bucket_caps = list(bucket_caps or [64, 128, 256, 384])
        self.log = log or logging.getLogger("asrag.embedder")

        t0 = time.perf_counter()
        tok = Tokenizer.from_file(str(spec.tokenizer_file))
        tok.enable_truncation(max_length=spec.max_tokens)
        pad_id = tok.token_to_id("[PAD]")
        if pad_id is None:  # 无 [PAD] 的模型退化用 0 号位
            pad_id = 0
        tok.enable_padding(pad_id=pad_id, pad_token="[PAD]")
        self.tokenizer = tok

        so = ort.SessionOptions()
        so.log_severity_level = 3
        if intra_op_threads:
            so.intra_op_num_threads = intra_op_threads
        self.session = ort.InferenceSession(
            str(spec.onnx_file), sess_options=so, providers=["CPUExecutionProvider"]
        )
        self._input_names = {i.name for i in self.session.get_inputs()}
        # 读 HF config 校验架构（轻量防呆，不做维度推断——以实测为准）
        cfg = json.loads((spec.path / "config.json").read_text(encoding="utf-8"))
        self._model_file_dim = int(cfg.get("hidden_size", 0) or 0)

        self.log.info(
            "embedder loaded model=%s dim=%s pooling=%s quantized=%s maxTokens=%s onnx=%s cost_ms=%d",
            spec.id, spec.dim, spec.pooling, spec.quantized, spec.max_tokens,
            spec.onnx_file.name, int((time.perf_counter() - t0) * 1000),
        )
        self._dim_checked = False

    def _check_dim(self, actual: int) -> None:
        if self._dim_checked:
            return
        if actual != self.spec.dim:
            raise ValueError(
                f"模型 {self.spec.id} 注册表声明 dim={self.spec.dim}，"
                f"实测输出 dim={actual}（config.hidden_size={self._model_file_dim}）。"
                f"模型切换后须同步更新 models.json，禁止静默截断。"
            )
        self._dim_checked = True
        self.log.info("embedder dim verified model=%s dim=%d", self.spec.id, actual)

    def _encode_batch(self, texts: list[str]) -> np.ndarray:
        encs = self.tokenizer.encode_batch(texts)
        ids = np.asarray([e.ids for e in encs], dtype=np.int64)
        mask = np.asarray([e.attention_mask for e in encs], dtype=np.int64)
        feed: dict[str, np.ndarray] = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self._input_names:
            feed["token_type_ids"] = np.asarray(
                [e.type_ids for e in encs], dtype=np.int64
            )
        out = self.session.run(None, feed)
        hidden = out[0]  # BertModel: last_hidden_state (B, T, H)
        vec = (
            _cls_pool(hidden)
            if self.spec.pooling == "cls"
            else _mean_pool(hidden, mask)
        )
        vec = vec.astype(np.float32)
        if self.spec.normalize:
            vec = _l2_normalize(vec)
        return vec

    def encode_passages(self, texts: Sequence[str]) -> np.ndarray:
        """批量嵌入（长度分桶，输出顺序与输入严格一致）。

        为什么分桶：一个 batch 内所有文本会被 padding 到该批最长的那条，
        而 Transformer 注意力开销随序列长度近似平方增长。真实会话文本长度
        从几十到 4096 字符悬殊，混批会让短文本陪跑长文本（实测全局 5.26 条/秒，
        而按长度分桶后短文本可达 60+ 条/秒）。
        分桶后同批长度相近，padding 浪费最小 —— 纯工程优化，不改变任何
        单条文本的编码结果（位级可复现契约仍然成立）。
        """
        texts = list(texts)
        n = len(texts)
        if not n:
            return np.zeros((0, self.spec.dim), dtype=np.float32)
        out = np.empty((n, self.spec.dim), dtype=np.float32)

        if not self.bucketing:
            for i in range(0, n, self.batch_size):
                idxs = list(range(i, min(i + self.batch_size, n)))
                vec = self._encode_batch([texts[j] for j in idxs])
                self._check_dim(vec.shape[1])
                out[idxs] = vec
            return out

        # 按字符长度分桶（粗分档：同档内长度接近，padding 浪费可控）
        buckets: dict[int, list[int]] = {}
        for idx, t in enumerate(texts):
            buckets.setdefault(self._bucket_of(len(t)), []).append(idx)

        for bucket in sorted(buckets):
            idxs = buckets[bucket]
            # 桶内按长度排序：使相邻同批长度更接近（进一步减少 padding）
            idxs.sort(key=lambda i: len(texts[i]))
            for i in range(0, len(idxs), self.batch_size):
                batch_idx = idxs[i : i + self.batch_size]
                vec = self._encode_batch([texts[j] for j in batch_idx])
                self._check_dim(vec.shape[1])
                if vec.shape[1] != self.spec.dim:  # 首块后仍需守门
                    raise ValueError(
                        f"模型 {self.spec.id} 输出维度漂移："
                        f"{vec.shape[1]} != {self.spec.dim}"
                    )
                out[batch_idx] = vec
        return out

    def _bucket_of(self, char_len: int) -> int:
        """字符长度 → 分桶号（边界来自配置 bucket_caps）。"""
        for i, cap in enumerate(self.bucket_caps):
            if char_len <= cap:
                return i
        return len(self.bucket_caps)   # 超出末档：多会被截断，归一起

    def encode_query(self, text: str) -> np.ndarray:
        if self.spec.query_prefix:
            text = f"{self.spec.query_prefix}{text}"
        return self.encode_passages([text])[0]
