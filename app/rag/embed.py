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

from app.core import runtime
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
    """本地 embedding 模型（CLS 池化 + 归一化）。

    没有用 sentence-transformers：它会把 scikit-learn / scipy 一起拉进来，
    体积翻倍，而这里需要的只是"分词 → 前向 → 取 CLS → L2 归一化"，
    十来行就够。运行环境因此只需要 torch(CPU) + transformers。
    """
    import torch
    from transformers import AutoModel, AutoTokenizer

    path = str(settings.emb_local_model)
    log.info("加载本地 embedding 模型：%s", path)
    tok = AutoTokenizer.from_pretrained(path)
    model = AutoModel.from_pretrained(path)
    model.eval()
    return tok, model


def _local_embed(texts: list[str], batch: int = 16, max_len: int = 512) -> np.ndarray:
    import torch

    tok, model = _local_model()
    out: list[np.ndarray] = []
    with torch.no_grad():
        for i in range(0, len(texts), batch):
            b = tok([t[:4000] for t in texts[i : i + batch]], padding=True, truncation=True,
                    max_length=max_len, return_tensors="pt")
            h = model(**b).last_hidden_state[:, 0]      # bge / bge-m3 都用 CLS 池化
            h = torch.nn.functional.normalize(h, dim=-1)
            out.append(h.float().numpy())
    return np.vstack(out).astype(np.float32)


def embed_texts(texts: list[str]) -> np.ndarray:
    """返回 (n, dim) 的 float32 归一化矩阵。"""
    if not texts:
        return np.zeros((0, settings.emb_dim), dtype=np.float32)

    # 离线模式强制走本地：这是"完全断网"的关键一步，不能因为 .env 里还留着
    # EMB_PROVIDER=api 就偷偷发一次网络请求。
    provider = "local" if runtime.is_offline() else settings.emb_provider.lower()
    try:
        if provider == "api" and settings.emb_api_key:
            mat = _api_embed(texts)
        elif provider == "local":
            mat = _local_embed(texts)
        else:
            mat = np.vstack([_hash_embed(t, settings.emb_dim) for t in texts])
    except Exception as exc:  # noqa: BLE001 任何异常都降级，绝不让入库失败
        log.warning("embedding 失败(%s)，降级为 hash 向量：%s", provider, exc)
        mat = np.vstack([_hash_embed(t, settings.emb_dim) for t in texts])

    # 统一归一化，后面一律用内积当余弦
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (mat / norms).astype(np.float32)


# 查询向量缓存：同一个问题（含"重新生成"、连续追问同一句）不必再打一次远程接口。
# 一条 1024 维 float32 只占 4KB，512 条约 2MB，换掉的是每次 150–300ms 的网络往返。
# 只缓存**查询**，不缓存入库文本——入库是批量且一次性的，缓存只会白占内存。
_QUERY_VEC_CACHE: dict[tuple[str, str], np.ndarray] = {}
_QUERY_VEC_MAX = 512


def clear_query_cache() -> None:
    """切换运行模式后必须调用：同一句话在云端/离线两种模式下算出的向量不同，
    留着旧缓存会让切换后的第一次检索拿到错误向量，而且不报错。"""
    _QUERY_VEC_CACHE.clear()


def embed_query(text: str) -> np.ndarray:
    # 缓存键带上运行模式：云端和离线是两套语义空间，同一个问题算出来的向量
    # 不一样，混用会让切换后的第一次检索拿到错误向量（且看起来毫无异常）。
    key = ("offline" if runtime.is_offline() else settings.emb_provider.lower(),
           (text or "").strip())
    hit = _QUERY_VEC_CACHE.get(key)
    if hit is not None:
        return hit
    vec = embed_texts([text])[0]
    if len(_QUERY_VEC_CACHE) >= _QUERY_VEC_MAX:
        _QUERY_VEC_CACHE.pop(next(iter(_QUERY_VEC_CACHE)), None)
    _QUERY_VEC_CACHE[key] = vec
    return vec


def to_bytes(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def from_bytes(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)
