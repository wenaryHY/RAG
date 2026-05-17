"""分类器：判断问题复杂度 + 领域。

修复原 Scheduler 的隐藏 bug：原 prompt 用 .format(query=...) 但内嵌 JSON 例
{"complexity": ...} 会让 .format 尝试把 "complexity" 当字段名失败，被外层
try/except 吞掉，分类永远 fallback 到 medium。

这里用 str.replace 占位符 <<QUERY>>，与 audit 层的做法一致。
"""
from __future__ import annotations

import json
import re
from typing import Optional

from config import ProviderKey

from .providers import ModelTarget, ProviderError, chat_completion, text_of


CLASSIFY_PROMPT = """分析以下用户问题的复杂度和领域，仅返回单行 JSON，不要 markdown 代码块。

期望格式：{"complexity": "simple|medium|complex", "domain": "dev|pharmacy|general", "reason": "一句话理由"}

规则：
- simple: 简单事实查询、定义解释、单一知识点
- medium: 需要推理、对比、多步分析
- complex: 需要深度推理、跨领域知识、多个子问题
- domain.dev: 编程、技术
- domain.pharmacy: 药学、医学相关
- domain.general: 其他

用户问题：<<QUERY>>"""


_JSON_FENCE = re.compile(r"```(?:json)?\s*\n?", re.IGNORECASE)


def _extract_json(text: str) -> dict:
    text = text.strip()
    text = _JSON_FENCE.sub("", text)
    text = re.sub(r"```\s*$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    return json.loads(text)


async def classify(
    query: str,
    judge_target: ModelTarget,
    judge_key: ProviderKey,
) -> dict:
    """返回 {complexity, domain, reason}。任何错误回落到 medium/general。"""
    prompt = CLASSIFY_PROMPT.replace("<<QUERY>>", query)
    try:
        resp = await chat_completion(
            judge_target,
            judge_key,
            [{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=200,
            timeout=30.0,
        )
        return _extract_json(text_of(resp))
    except (ProviderError, json.JSONDecodeError, KeyError, ValueError):
        return {"complexity": "medium", "domain": "general", "reason": "fallback"}


def select_target(classification: dict, *, pharmacy_mode: bool = False) -> ModelTarget:
    """与原 Scheduler 等价的路由表。"""
    complexity = classification.get("complexity", "medium")
    domain = classification.get("domain", "general")

    if domain == "pharmacy" or pharmacy_mode:
        if complexity == "simple":
            return ModelTarget("deepseek", "deepseek-v4-flash", "~¥0.001")
        return ModelTarget("deepseek", "deepseek-v4-pro", "~¥0.01")

    if complexity == "simple":
        return ModelTarget(
            "openrouter", "deepseek/deepseek-v4-flash:free", "免费"
        )
    if complexity == "complex":
        return ModelTarget("deepseek", "deepseek-v4-pro", "~¥0.01")
    return ModelTarget("deepseek", "deepseek-v4-flash", "~¥0.001")


JUDGE_TARGET = ModelTarget("deepseek", "deepseek-v4-flash", "~¥0.001")
