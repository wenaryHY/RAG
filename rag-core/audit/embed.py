"""SiliconFlow embedding client (BAAI/bge-m3, FREE tier).

用于 Stage1 跨文档 chunk 配对的低成本相似度筛选。
批量 32 一次调用,失败自动 retry 一次。
"""
from __future__ import annotations

import math
from typing import Iterable

import httpx

from config import ProviderKey


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


async def embed_batch(
    sf_key: ProviderKey,
    texts: list[str],
    *,
    model: str = "BAAI/bge-m3",
    batch: int = 32,
    timeout: float = 60.0,
) -> list[list[float]]:
    """逐批调用 SiliconFlow /embeddings,返回与 texts 对齐的向量列表。"""
    url = f"{sf_key.base_url.rstrip('/')}/embeddings"
    headers = {
        "Authorization": f"Bearer {sf_key.key}",
        "Content-Type": "application/json",
    }
    out: list[list[float]] = []
    async with httpx.AsyncClient(timeout=timeout) as client:
        for i in range(0, len(texts), batch):
            chunk = texts[i : i + batch]
            payload = {"model": model, "input": chunk}
            r = await client.post(url, headers=headers, json=payload)
            if r.status_code != 200:
                # retry once
                r = await client.post(url, headers=headers, json=payload)
                if r.status_code != 200:
                    raise RuntimeError(
                        f"siliconflow embed {r.status_code}: {r.text[:300]}"
                    )
            data = r.json()
            for item in data.get("data", []):
                out.append(item.get("embedding", []))
    return out


def cosine_pairs(
    vectors_a: list[list[float]],
    vectors_b: list[list[float]],
    *,
    threshold: float = 0.82,
) -> list[tuple[int, int, float]]:
    """两组向量两两计算 cosine, 返回 ≥threshold 的 (i, j, score) 列表。"""
    out: list[tuple[int, int, float]] = []
    for i, va in enumerate(vectors_a):
        for j, vb in enumerate(vectors_b):
            s = _cosine(va, vb)
            if s >= threshold:
                out.append((i, j, s))
    return out
