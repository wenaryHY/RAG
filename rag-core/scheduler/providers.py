"""统一的 LLM 调用客户端。

所有 provider 走 OpenAI-compatible /chat/completions，差异点：
- XSTX 需要额外 X-API-Key 头（沿用原 Scheduler 行为）
- base_url 已经在 keys.ini 里规范成含 /v1 的形式
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import httpx

from config import ProviderKey


@dataclass
class ModelTarget:
    """分类后选定的目标 model。"""

    provider: str        # deepseek / openrouter / xstx / siliconflow / lmstudio
    model: str
    cost_label: str      # 用于日志/费用估算字符串


class ProviderError(RuntimeError):
    def __init__(self, provider: str, status: int, body: str):
        super().__init__(f"{provider} error {status}: {body[:300]}")
        self.provider = provider
        self.status = status
        self.body = body


async def chat_completion(
    target: ModelTarget,
    provider_key: ProviderKey,
    messages: list[dict],
    *,
    temperature: float = 0.3,
    max_tokens: int = 2048,
    timeout: float = 60.0,
) -> dict:
    """OpenAI 兼容协议下的 /chat/completions 调用。"""
    headers = {
        "Authorization": f"Bearer {provider_key.key}",
        "Content-Type": "application/json",
    }
    if target.provider == "xstx":
        headers["X-API-Key"] = provider_key.key

    payload = {
        "model": target.model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    url = f"{provider_key.base_url.rstrip('/')}/chat/completions"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, headers=headers, json=payload)
    if resp.status_code != 200:
        raise ProviderError(target.provider, resp.status_code, resp.text)
    return resp.json()


def text_of(response: dict) -> str:
    """从 OpenAI 兼容响应中抽取文本，处理 None / reasoning 字段分离。"""
    try:
        msg = response["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return ""
    # 1) 标准 content 字段
    content = msg.get("content")
    if content:
        return content
    # 2) 部分 reasoning 模型把答案放 reasoning_content
    reasoning = msg.get("reasoning_content") or msg.get("reasoning")
    if reasoning:
        return reasoning
    # 3) tool/function-call 模式
    tcs = msg.get("tool_calls") or []
    if tcs:
        return f"[tool_calls: {tcs}]"
    return ""
