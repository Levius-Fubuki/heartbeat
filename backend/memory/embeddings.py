"""fastembed 本地中文嵌入单例。

首次调用懒加载模型(首次下载 BAAI/bge-small-zh-v1.5 ONNX ~130MB 到 ~/.cache/fastembed)。
double-checked locking 保证多线程下只加载一次。所有向量 L2 归一化,
故 l3.recall 里 `mat @ q` 点积即余弦相似度,无需再除范数。

本模块硬依赖 numpy + fastembed —— 调用方(l3.py)负责在 EMBED_ENABLED 且 numpy 可用时才 import,
失败降级到纯 recent_text(见 l3._build_index 的 try/except)。
"""
from __future__ import annotations

import logging
import threading

import numpy as np

from backend.config import EMBED_MODEL

log = logging.getLogger("heartbeat.memory.embed")

_lock = threading.Lock()
_model = None  # fastembed.TextEmbedding 句柄


def get_model():
    """懒加载 fastembed 模型(线程安全)。首次会下载 ONNX 权重。"""
    global _model
    if _model is None:
        with _lock:
            if _model is None:  # double-checked locking
                from fastembed import TextEmbedding
                log.info("loading embed model %s (first time; downloading ~130MB if missing)...", EMBED_MODEL)
                _model = TextEmbedding(model_name=EMBED_MODEL)
                log.info("embed model ready: %s", EMBED_MODEL)
    return _model


def _l2_normalize(mat: np.ndarray) -> np.ndarray:
    """逐行 L2 归一化(零向量保持零,避免除 0)。归一化后点积 = 余弦。"""
    if mat.shape[0] == 0:
        return mat
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (mat / norms).astype(np.float32)


def embed_texts(texts: list) -> np.ndarray:
    """批量嵌入。返回 shape=(len(texts), dim) 的 float32 归一化矩阵。空 list → shape=(0,0)。"""
    if not texts:
        return np.zeros((0, 0), dtype=np.float32)
    m = get_model()
    # fastembed 的 embed 返回 generator of ndarray(每条一个向量)
    mat = np.asarray(list(m.embed(texts)), dtype=np.float32)
    return _l2_normalize(mat)


def embed_query(text: str) -> np.ndarray:
    """单条 query → shape=(dim,) 的归一化向量。"""
    return embed_texts([text])[0]
