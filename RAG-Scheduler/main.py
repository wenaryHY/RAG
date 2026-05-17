"""
RAG Scheduler — 智能模型路由层
DeepSeek Flash 判官 → 免费API做简单任务 → 付费API做复杂任务 → Claude Opus审计
"""

import os
import json
import time
import hashlib
from pathlib import Path
from datetime import datetime
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
KEYS_FILE = Path("D:/private/密钥.txt")
LOG_DIR = Path("D:/RAG-Scheduler/logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Key parser
# ---------------------------------------------------------------------------
def load_keys() -> dict:
    """Parse the keys file into a usable dict."""
    raw = KEYS_FILE.read_text(encoding="utf-8")
    keys = {}
    current_label = None
    for line in raw.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("sk-") or line.startswith("{"):
            keys[current_label] = line
        else:
            current_label = line.rstrip(":")
            if "deepseek" in current_label.lower():
                current_label = "deepseek"
            elif "openrouter" in current_label.lower():
                current_label = "openrouter"
            elif "xstx" in current_label.lower() or "星途" in current_label:
                current_label = "xstx"
            elif "硅基" in current_label:
                current_label = "siliconflow"
    return keys


def get_xstx_config(raw: str) -> dict:
    """Parse XSTX JSON config."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"key": raw, "url": "https://api.xstx.info"}


# ---------------------------------------------------------------------------
# Model registry — what each model is good at
# ---------------------------------------------------------------------------
MODELS = {
    "judge": {  # 判官：分析问题复杂度
        "name": "deepseek-v4-flash",
        "base_url": "https://api.deepseek.com/v1",
        "key_label": "deepseek",
    },
    "simple_free": {  # 简单任务免费
        "name": "deepseek/deepseek-v4-flash:free",
        "base_url": "https://openrouter.ai/api/v1",
        "key_label": "openrouter",
    },
    "simple_paid": {  # 简单但需高准确（药学）
        "name": "deepseek-v4-flash",
        "base_url": "https://api.deepseek.com/v1",
        "key_label": "deepseek",
    },
    "complex": {  # 复杂任务
        "name": "deepseek-v4-pro",
        "base_url": "https://api.deepseek.com/v1",
        "key_label": "deepseek",
    },
    "audit": {  # 审计
        "name": "claude-opus-4-7",
        "base_url": "https://api.xstx.info/v1",
        "key_label": "xstx",
    },
}

# ---------------------------------------------------------------------------
# Complexity classification prompt
# ---------------------------------------------------------------------------
CLASSIFY_PROMPT = """分析以下用户问题的复杂度，只返回一个JSON：
{"complexity": "simple|medium|complex", "domain": "dev|pharmacy|general", "reason": "一句话理由"}

规则：
- simple: 简单事实查询、定义解释、单一知识点
- medium: 需要推理、对比、多步分析
- complex: 需要深度推理、跨领域知识、多个子问题

用户问题：{query}

只返回JSON，不要其他内容。"""


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.keys = load_keys()
    app.state.request_log = []
    yield

app = FastAPI(title="RAG Scheduler", version="1.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def get_api_key(label: str, keys: dict) -> str:
    if label == "xstx":
        cfg = get_xstx_config(keys.get("xstx", "{}"))
        return cfg["key"]
    return keys.get(label, "").strip()


async def call_llm(model_info: dict, messages: list, keys: dict, max_tokens: int = 2048) -> dict:
    """Call any LLM API with OpenAI-compatible format."""
    key_label = model_info["key_label"]
    api_key = get_api_key(key_label, keys)
    base_url = model_info["base_url"]
    model_name = model_info["name"]

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model_name,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }
    # XSTX needs extra header
    if key_label == "xstx":
        headers["X-API-Key"] = api_key

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json=payload,
        )
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Model error: {resp.status_code} {resp.text[:300]}")
        return resp.json()


async def classify_query(query: str, keys: dict) -> dict:
    """Use judge model to classify query complexity and domain."""
    try:
        result = await call_llm(
            MODELS["judge"],
            [{"role": "user", "content": CLASSIFY_PROMPT.format(query=query)}],
            keys,
            max_tokens=200,
        )
        content = result["choices"][0]["message"]["content"].strip()
        # Extract JSON from possible markdown wrapper
        if "```" in content:
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        return json.loads(content.strip())
    except Exception:
        return {"complexity": "medium", "domain": "general", "reason": "分类失败，默认中等复杂度"}


def select_model(classification: dict, pharmacy_mode: bool = False) -> dict:
    """Select the appropriate model based on classification."""
    complexity = classification.get("complexity", "medium")
    domain = classification.get("domain", "general")

    if domain == "pharmacy" or pharmacy_mode:
        # 药学内容用付费模型，确保准确性
        if complexity == "simple":
            return MODELS["simple_paid"]
        return MODELS["complex"]
    elif complexity == "simple":
        return MODELS["simple_free"]
    elif complexity == "complex":
        return MODELS["complex"]
    else:
        return MODELS["simple_paid"]


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------
class QueryRequest(BaseModel):
    query: str
    pharmacy_mode: bool = False
    system_prompt: str | None = None


class QueryResponse(BaseModel):
    answer: str
    model_used: str
    complexity: str
    domain: str
    cost_estimate: str
    timestamp: str


@app.post("/query", response_model=QueryResponse)
async def query(req: QueryRequest):
    """Main query endpoint with intelligent routing."""
    start = time.time()
    keys = app.state.keys

    # Step 1: classify
    classification = await classify_query(req.query, keys)

    # Step 2: select model
    model_info = select_model(classification, req.pharmacy_mode)

    # Step 3: call selected model
    messages = []
    if req.system_prompt:
        messages.append({"role": "system", "content": req.system_prompt})
    messages.append({"role": "user", "content": req.query})

    result = await call_llm(model_info, messages, keys)

    elapsed = time.time() - start
    answer = result["choices"][0]["message"]["content"]

    # Log
    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "query": req.query[:200],
        "complexity": classification.get("complexity"),
        "domain": classification.get("domain"),
        "model": model_info["name"],
        "elapsed": round(elapsed, 2),
        "pharmacy_mode": req.pharmacy_mode,
    }
    app.state.request_log.append(log_entry)

    # Cost estimate
    cost_map = {
        "deepseek-v4-flash": "~0.001元",
        "deepseek/deepseek-v4-flash:free": "免费",
        "deepseek-v4-pro": "~0.01元",
        "claude-opus-4-7": "按次计费",
    }

    return QueryResponse(
        answer=answer,
        model_used=model_info["name"],
        complexity=classification.get("complexity", "unknown"),
        domain=classification.get("domain", "unknown"),
        cost_estimate=cost_map.get(model_info["name"], "未知"),
        timestamp=datetime.now().isoformat(),
    )


@app.get("/health")
async def health():
    keys = app.state.keys
    return {
        "status": "ok",
        "models_configured": len([k for k in keys if k]),
        "total_requests": len(app.state.request_log),
    }


@app.get("/logs")
async def get_logs(limit: int = 20):
    return {"logs": app.state.request_log[-limit:]}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8850)
