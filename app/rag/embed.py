"""向量化：三档实现，按配置自动选择，保证"任何环境都能跑"。

- api  : 任一 OpenAI 兼容的 /embeddings（默认接 bge-m3，中文论文效果明显好于英文小模型）
- local: sentence-transformers 本地小模型（2060 6G 可跑，呼应本地部署加分）
- hash : 零依赖哈希向量兜底（可跑通链路，召回质量一般，仅作降级）
"""
from __future__ import annotations

import hashlib
import re
from functools import lru_cache

import httpx
import numpy as np

from app.core.config import settings
from app.core.logger import get_logger

log = get_logger(__name__)

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_\-]{1,}|[\u4e00-\u9fa5]")


def _tokens(text: str) -> list[str]:
    out = []
    for t in _WORD.findall(text.lower()):
        out.append(t)
        if len(t) > 4 and t.isascii():  # 英文加 4-gram，缓解哈希碰撞
            out.extend(t[i : i + 4] for i in range(0, len(t) - 3, 2))
    return out


def _hash_embed(text: str, dim: int) -> np.ndarray:
    vec = np.zeros(dim, dtype=np.float32)
    for tok in _tokens(text):
        h = int(hashlib.md5(tok.encode("utf-8")).hexdigest()[:8], 16)
        idx = h % dim
        sign = 1.0 if (h >> 31) & 1 else -1.0
        vec[idx] += sign
    norm = np.linalg.norm(vec)
    return vec / norm if norm > 0 else vec


def _api_embed(texts: list[str]) -> np.ndarray:
    url = f"{settings.emb_base_url.rstrip('/')}/embeddings"
    headers = {"Authorization": f"Bearer {settings.emb_api_key}"}
    out: list[list[float]] = []
    with httpx.Client(timeout=60) as client:
        for i in range(0, len(texts), 16):  # 分批，避免单次请求过大
            batch = [t[:6000] for t in texts[i : i + 16]]
            resp = client.post(url, headers=headers, json={"model": settings.emb_model, "input": batch})
            resp.raise_for_status()
            data = resp.json()["data"]
            out.extend([d["embedding"] for d in data])
    return np.asarray(out, dtype=np.float32)


@lru_cache(maxsize=1)
def _local_model():
    from sentence_transformers import SentenceTransformer

    log.info("加载本地 embedding 模型：%s", settings.emb_local_model)
    return SentenceTransformer(settings.emb_local_model)


def embed_texts(texts: list[str]) -> np.ndarray:
    """返回 (n, dim) 的 float32 归一化矩阵。"""
    if not texts:
        return np.zeros((0, settings.emb_dim), dtype=np.float32)

    provider = settings.emb_provider.lower()
    try:
        if provider == "api" and settings.emb_api_key:
            mat = _api_embed(texts)
        elif provider == "local":
            mat = _local_model().encode(texts, normalize_embeddings=True).astype(np.float32)
        else:
            mat = np.vstack([_hash_embed(t, settings.emb_dim) for t in texts])
    except Exception as exc:  # noqa: BLE001 任何异常都降级，绝不让入库失败
        log.warning("embedding 失败(%s)，降级为 hash 向量：%s", provider, exc)
        mat = np.vstack([_hash_embed(t, settings.emb_dim) for t in texts])

    # 统一归一化，后面一律用内积当余弦
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (mat / norms).astype(np.float32)


def embed_query(text: str) -> np.ndarray:
    return embed_texts([text])[0]


def to_bytes(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def from_bytes(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)
