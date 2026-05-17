"""鲁棒 JSON 提取 + Claude 调用包装。"""
from __future__ import annotations

import json
import re
from typing import Optional

import httpx

from config import ProviderKey


_FENCE = re.compile(r"```(?:json)?\s*\n?", re.IGNORECASE)


def parse_json(text: str, *, debug_path: Optional[str] = None) -> dict:
    text = text.strip()
    text = _FENCE.sub("", text)
    text = re.sub(r"```\s*$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        if debug_path:
            try:
                from pathlib import Path
                Path(debug_path).write_text(text[:2000], encoding="utf-8")
            except Exception:
                pass
        raise


async def call_claude(
    xstx_key: ProviderKey,
    messages: list[dict],
    *,
    model: str = "claude-opus-4-7",
    max_tokens: int = 2048,
    temperature: float = 0.1,
    timeout: float = 120.0,
) -> dict:
    headers = {
        "Authorization": f"Bearer {xstx_key.key}",
        "Content-Type": "application/json",
        "X-API-Key": xstx_key.key,
    }
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    url = f"{xstx_key.base_url.rstrip('/')}/chat/completions"
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(url, headers=headers, json=payload)
    if r.status_code != 200:
        raise RuntimeError(f"Claude API {r.status_code}: {r.text[:300]}")
    return r.json()
