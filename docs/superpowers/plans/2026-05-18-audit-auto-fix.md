# Audit Auto-Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 审计发现冲突/错误后，支持用户在 Panel 一键修正 RAGFlow 中的 chunk 文本。

**Architecture:** 在现有三级漏斗审计流程的 Stage 3 阶段，增强 Claude Opus 输出附带修正建议文本。新增 `POST /audit/fix` 端点接收修正指令，调用 RAGFlow `PATCH /chunks/{id}` API 写入修正内容。所有修正记录写入 `audit_fixes` SQLite 表。

**Tech Stack:** Python FastAPI, SQLModel, httpx, Alpine.js

**RAGFlow API discovered:** `PATCH /api/v1/datasets/{dataset_id}/documents/{document_id}/chunks/{chunk_id}` — updates a single chunk's content.

---

### Task 1: RAGFlow Client — `update_chunk()` method

**Files:**
- Modify: `D:\RAG\rag-core\ragflow_client.py`

- [ ] **Step 1: Add `update_chunk()` method**

In `ragflow_client.py`, after the existing `delete_documents()` method, add:

```python
def update_chunk(self, dataset_id: str, document_id: str, chunk_id: str, content: str) -> dict:
    """PATCH 单个 chunk 的 content 字段。"""
    data = self._request(
        "PATCH",
        f"/api/v1/datasets/{dataset_id}/documents/{document_id}/chunks/{chunk_id}",
        json={"content": content, "important_keywords": []},
    )
    return data.get("data", data) if isinstance(data, dict) else data
```

- [ ] **Step 2: Commit**

```bash
git add rag-core/ragflow_client.py
git commit -m "feat(audit-fix): add update_chunk() to RAGFlow client"
```

---

### Task 2: DB — `AuditFix` table

**Files:**
- Modify: `D:\RAG\rag-core\db.py`

- [ ] **Step 1: Add `AuditFix` model**

In `db.py`, after the `AuditPairCache` class (line 72), add:

```python
class AuditFix(SQLModel, table=True):
    __tablename__ = "audit_fixes"

    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: int
    finding_idx: int
    chunk_id: str
    dataset_id: str
    document_id: str
    doc_name: str = ""
    original_text: Optional[str] = None
    fixed_text: str
    suggestion: Optional[str] = None
    applied_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    status: str = "applied"   # applied | rejected
```

- [ ] **Step 2: Commit**

```bash
git add rag-core/db.py
git commit -m "feat(audit-fix): add AuditFix table to state.sqlite"
```

---

### Task 3: Audit Fix Logic — `fix.py`

**Files:**
- Create: `D:\RAG\rag-core\audit\fix.py`
- Modify: `D:\RAG\rag-core\audit\prompts.py`

- [ ] **Step 1: Add fix generation prompt to `prompts.py`**

At end of `D:\RAG\rag-core\audit\prompts.py`, add:

```python
FIX_PROMPT = """你是知识库修正专家。以下文档片段被标记为存在冲突/错误，请生成修正后的文本。

原文来源：<<SOURCE>>
原文内容：
<<CONTENT>>

冲突/错误描述：<<ISSUE>>
建议处理方式：<<RECOMMENDATION>>

请只返回修正后的文本，不要添加任何解释、不要 markdown、不要 JSON。直接输出修正后的完整文本。
如果原文无需修改，原样返回原文。"""
```

- [ ] **Step 2: Create `fix.py`**

Create `D:\RAG\rag-core\audit\fix.py` with application logic that:
1. Loads finding from report JSON
2. Calls Claude Opus to generate fix if no suggestion exists
3. Reads original chunk text for audit trail
4. Calls `rag.update_chunk()` to PATCH
5. Writes `AuditFix` record

- [ ] **Step 3: Commit**

```bash
git add rag-core/audit/fix.py rag-core/audit/prompts.py
git commit -m "feat(audit-fix): add fix generation and apply logic"
```

---

### Task 4: Audit Funnel — Store chunk tracking in findings

**Files:**
- Modify: `D:\RAG\rag-core\audit\funnel.py`
- Modify: `D:\RAG\rag-core\audit\prompts.py`

- [ ] **Step 1: Add `suggestion` field to CONFLICT_PROMPT**

Update `CONFLICT_PROMPT` JSON schema to include:

```json
"suggestion": "若存在冲突，给出文档A的修正版本（保持原风格，仅修正冲突部分）"
```

- [ ] **Step 2: Attach chunk metadata to findings in `_stage3_opus()`**

In `funnel.py` `_stage3_opus()`, after finding is returned, attach:
```python
finding["chunk_id"] = a.get("chunk_id")
finding["dataset_id"] = ds_id
finding["document_id"] = a.get("doc_id")
```

- [ ] **Step 3: Ensure serialized in `_persist_run()`**

In `_persist_run()`, findings are already serialized via `json.dumps`, so new fields auto-serialize.

- [ ] **Step 4: Commit**

```bash
git add rag-core/audit/funnel.py rag-core/audit/prompts.py
git commit -m "feat(audit-fix): store chunk tracking metadata in findings"
```

---

### Task 5: Audit Router — `POST /audit/fix` + `GET /audit/fixes`

**Files:**
- Modify: `D:\RAG\rag-core\audit\router.py`

- [ ] **Step 1: Add `FixRequest` model and endpoints**

Add import for `fix` module, then add `FixRequest` model and two endpoints:
- `POST /audit/fix` — applies a single finding fix
- `GET /audit/fixes` — lists fix history

- [ ] **Step 2: Commit**

```bash
git add rag-core/audit/router.py
git commit -m "feat(audit-fix): add POST /audit/fix and GET /audit/fixes endpoints"
```

---

### Task 6: Panel UI — Fix button in audit tab

**Files:**
- Modify: `D:\RAG\rag-core\panel\static\index.html`

- [ ] **Step 1: Replace report detail view with structured finding list + fix button**

Replace the `<pre>` JSON dump with a finding-by-finding card view including "修复" button.

- [ ] **Step 2: Add JS methods for fix flow**

Add `applyFix()` method (calls `/audit/fix` POST) and `fixLoading`/`fixResult` state.

- [ ] **Step 3: Add actionMsg toast display**

- [ ] **Step 4: Commit**

```bash
git add rag-core/panel/static/index.html
git commit -m "feat(audit-fix): add per-finding fix button in audit panel"
```

---

### Task 7: Verification

**Files:**
- Test: `D:\RAG\rag-core\tests\`

- [ ] **Step 1: Syntax check all modified files**
- [ ] **Step 2: Run existing tests (expected 28 passed)**
- [ ] **Step 3: Verify FastAPI loads with new routes**
- [ ] **Step 4: Commit**

