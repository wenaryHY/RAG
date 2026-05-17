import json, os, re
from pathlib import Path
from datetime import datetime
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# ---------------------------------------------------------------------------
KEYS_FILE = Path("D:/private/密钥.txt")
REPORT_DIR = Path("D:/RAG-Audit/reports")
REPORT_DIR.mkdir(parents=True, exist_ok=True)

def parse_json(text: str) -> dict:
    """Robust JSON extraction from LLM response."""
    text = text.strip()
    # Remove markdown code blocks
    text = re.sub(r'```(?:json)?\s*\n?', '', text)
    text = re.sub(r'```\s*$', '', text)
    # Find JSON object boundaries
    start = text.find('{')
    end = text.rfind('}')
    if start >= 0 and end > start:
        text = text[start:end+1]
    else:
        # Try wrapping — Claude sometimes returns JSON without braces
        if ':' in text:
            text = '{' + text + '}'
    # Log raw text if parse fails
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        Path("D:/RAG-Audit/debug_last_response.txt").write_text(text[:2000], encoding="utf-8")
        raise

# ---------------------------------------------------------------------------
def load_keys():
    raw = KEYS_FILE.read_text(encoding="utf-8")
    for line in raw.strip().split("\n"):
        line = line.strip()
        if line.startswith('{"_type":"newapi'):
            cfg = json.loads(line)
            return cfg["key"], cfg["url"]
    return None, None

API_KEY, API_URL = load_keys()
CLAUDE_MODEL = "claude-opus-4-7"

# ---------------------------------------------------------------------------
CONFLICT_PROMPT = """你是知识库审计专家。分析以下两段文档内容是否存在冲突、矛盾或不一致。

文档A来源：{source_a}
文档A内容：
{content_a}

文档B来源：{source_b}
文档B内容：
{content_b}

请返回JSON：
{
  "has_conflict": true/false,
  "conflict_type": "事实矛盾|版本不一致|观点分歧|无",
  "severity": "高|中|低|无",
  "description": "冲突描述",
  "recommendation": "建议处理方式"
}
只返回JSON。"""

ERROR_CHECK_PROMPT = """你是知识库审计专家。检查以下文档内容是否存在错误。

文档来源：{source}
文档内容：
{content}

检查项：
1. 事实性错误（与已知事实/常识相悖）
2. 内部逻辑矛盾（前后不一致）
3. 数据/数字错误
4. 与当前现实脱节的内容

请返回JSON：
{
  "has_error": true/false,
  "errors": [
    {"type": "事实错误|逻辑矛盾|数据错误|过时内容", "description": "...", "location": "原文位置/引用", "severity": "高|中|低"}
  ],
  "overall_assessment": "整体评价"
}
只返回JSON。"""

# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.reports = []
    yield

app = FastAPI(title="RAG Audit Layer", version="1.0", lifespan=lifespan)

class AuditRequest(BaseModel):
    documents: list[dict]  # [{"name": "...", "content": "...", "source": "..."}, ...]
    check_types: list[str] = ["conflict", "error"]  # "conflict", "error", "all"

class AuditResponse(BaseModel):
    report_id: str
    timestamp: str
    conflicts: list[dict]
    errors: list[dict]
    summary: str

async def call_claude(messages: list, max_tokens: int = 2048) -> dict:
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
        "X-API-Key": API_KEY,
        "anthropic-version": "2023-06-01",
    }
    payload = {
        "model": CLAUDE_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.1,
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            f"{API_URL}/v1/chat/completions",
            headers=headers,
            json=payload,
        )
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Claude error: {resp.text[:300]}")
        return resp.json()

@app.post("/audit", response_model=AuditResponse)
async def audit(req: AuditRequest):
    conflicts = []
    errors = []
    docs = req.documents

    # Conflict detection — pairwise comparison
    if "conflict" in req.check_types:
        for i in range(len(docs)):
            for j in range(i + 1, len(docs)):
                try:
                    prompt = CONFLICT_PROMPT.replace("{source_a}", docs[i].get("name", f"doc_{i}")) \
                        .replace("{content_a}", docs[i].get("content", "")[:3000]) \
                        .replace("{source_b}", docs[j].get("name", f"doc_{j}")) \
                        .replace("{content_b}", docs[j].get("content", "")[:3000])
                    result = await call_claude([{"role": "user", "content": prompt}])
                    text = result["choices"][0]["message"]["content"]
                    r = parse_json(text)
                    if r.get("has_conflict"):
                        conflicts.append(r)
                except Exception as e:
                    conflicts.append({"has_conflict": True, "conflict_type": "检测失败", "description": str(e)})

    # Error detection
    if "error" in req.check_types:
        for doc in docs:
            try:
                prompt = ERROR_CHECK_PROMPT.replace("{source}", doc.get("name", "unknown")) \
                    .replace("{content}", doc.get("content", "")[:3000])
                result = await call_claude([{"role": "user", "content": prompt}])
                text = result["choices"][0]["message"]["content"]
                r = parse_json(text)
                if r.get("has_error"):
                    errors.append({"source": doc.get("name", "unknown"), "errors": r.get("errors", [])})
            except Exception as e:
                errors.append({"source": doc.get("name", "unknown"), "errors": [{"type": "检测失败", "description": str(e)}]})

    # Summary
    high_conflicts = sum(1 for c in conflicts if c.get("severity") == "高")
    high_errors = sum(1 for e in errors for err in e.get("errors", []) if err.get("severity") == "高")
    summary = f"审计完成：{len(conflicts)}处冲突（{high_conflicts}处高危），{len(errors)}个文档有误（{high_errors}处高危）"

    report_id = datetime.now().strftime("audit-%Y%m%d-%H%M%S")
    report = {
        "report_id": report_id,
        "timestamp": datetime.now().isoformat(),
        "conflicts": conflicts,
        "errors": errors,
        "summary": summary,
    }
    # Save report
    (REPORT_DIR / f"{report_id}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    return AuditResponse(**report)

@app.post("/audit/single")
async def audit_single(doc: dict, check_type: str = "error"):
    """Quick single-document audit."""
    req = AuditRequest(documents=[doc], check_types=[check_type])
    return await audit(req)

@app.get("/health")
async def health():
    reports = list(REPORT_DIR.glob("*.json"))
    return {
        "status": "ok",
        "claude_model": CLAUDE_MODEL,
        "reports_count": len(reports),
        "latest_report": reports[-1].name if reports else None,
    }

@app.get("/reports")
async def list_reports(limit: int = 10):
    reports = sorted(REPORT_DIR.glob("*.json"), reverse=True)[:limit]
    return {"reports": [r.name for r in reports]}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8860)
