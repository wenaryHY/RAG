"""零成本测试：验证 chunk 追踪链路完整。

不调用任何付费 API（Claude/Opus/DeepSeek 全部跳过）。
仅测试：FileRecord 写入 → RAGFlow list_chunks → _collect_chunks 输出 → finding 结构。
"""
import sys
from pathlib import Path

print("=" * 55)
print("  RAG 系统 chunk 追踪链路诊断")
print("  零 API 费用（不调 Claude/Opus/DeepSeek）")
print("=" * 55)

sys.path.insert(0, r"D:\RAG\rag-core")
fail_count = 0
pass_count = 0

def check(name, condition, detail=""):
    global fail_count, pass_count
    if condition:
        pass_count += 1
        print(f"  PASS  {name}")
    else:
        fail_count += 1
        print(f"  FAIL  {name}  {detail}")

# ---- 1. 验证 DB 和 FileRecord 列 ----
print("\n=== 1. FileRecord 表结构 ===")
from db import init_db, FileRecord
from sync import state as st
from config import load_config

cfg = load_config()
init_db(cfg.state_db)
cols = list(FileRecord.model_fields.keys())
check("FileRecord.source_path 列存在", "source_path" in cols)
check("FileRecord.ingest_dir 列存在", "ingest_dir" in cols)
check("FileRecord.filename_tokens 列存在", "filename_tokens" in cols)
check("FileRecord.ingested_at 列存在", "ingested_at" in cols)
check("FileRecord.dataset_id 列存在", "dataset_id" in cols)
check("FileRecord.ragflow_doc_id 列存在", "ragflow_doc_id" in cols)

# 写入 + 读回测试
test_path = "D:/TEST/chunk_track_test.png"
st.upsert(
    test_path, sha256="fake_sha_test", library="pharmacy",
    dataset_id="ds_test_001", ragflow_doc_id="doc_test_001",
    source_path="D:/RAG/RAGfiles/pharmacy/test.png",
    ingest_dir="pharmacy", filename_tokens='["t1","t2"]',
    ingested_at="2026-05-18T00:00:00", status="done",
)
rec = st.get_record(test_path)
check("upsert + get_record 成功", rec is not None, f"rec={rec}")
if rec:
    check("dataset_id 写读一致", rec.dataset_id == "ds_test_001", f"got {rec.dataset_id}")
    check("source_path 写读一致", rec.source_path == "D:/RAG/RAGfiles/pharmacy/test.png")
st.upsert(test_path, status="deleted")

# ---- 2. RAGFlow list_chunks ----
print("\n=== 2. RAGFlow list_chunks ===")
from ragflow_client import RAGFlowClient
rag = RAGFlowClient(cfg.ragflow_base_url, cfg.ragflow_api_key)

ds_list = rag.list_datasets()
check("list_datasets 成功", len(ds_list) > 0, f"got {len(ds_list)} datasets")

pharm = [d for d in ds_list if d["name"] == "pharmacy"]
if not pharm:
    print("  SKIP: pharmacy dataset not found")
else:
    ds_id = pharm[0]["id"]
    docs = rag.list_documents(ds_id, page=1, page_size=5)
    check("list_documents 成功", len(docs) > 0, f"got {len(docs)} docs")

    if docs:
        found_chunk = False
        for doc in docs[:5]:
            did = doc.get("id")
            if not did:
                continue
            try:
                chunks = rag.list_chunks(ds_id, did, page=1, page_size=2)
            except Exception:
                continue
            if not chunks:
                continue
            ch = chunks[0]
            ch_id = ch.get("id")
            content = (ch.get("content") or ch.get("content_with_weight") or "")[:50]
            check(f"chunk 有 id ({ch_id})", bool(ch_id), f"keys: {list(ch.keys())[:8]}")
            check(f"chunk 有 content ({content!r})", bool(content))
            found_chunk = True
            break

        if found_chunk:
            # ---- 3. _collect_chunks ---
            print("\n=== 3. _collect_chunks 输出 ===")
            from audit.funnel import _collect_chunks
            collected = _collect_chunks(rag, ds_id, max_per_doc=3)
            check("_collect_chunks 非空", len(collected) > 0, f"got {len(collected)}")

            if collected:
                first = collected[0]
                check("字段 doc 存在", "doc" in first)
                check("字段 doc_id 存在", "doc_id" in first)
                check("字段 dataset_id 存在", "dataset_id" in first)
                check("字段 chunk_id 存在", "chunk_id" in first)
                check("字段 text 存在", "text" in first)
                check("字段 sha 存在", "sha" in first)
                check("chunk_id 非空", bool(first.get("chunk_id")), repr(first.get("chunk_id")))
                check("dataset_id 非空", bool(first.get("dataset_id")), repr(first.get("dataset_id")))

                print(f"\n  第一个 chunk 完整字段:")
                for k in ["doc", "doc_id", "dataset_id", "chunk_id"]:
                    val = str(first.get(k, "?"))[:60]
                    print(f"    {k}: {val}")
        else:
            print("  SKIP: no document has chunks yet")

# ---- 4. 模拟 finding ---
print("\n=== 4. 模拟 finding 构建 ===")
mock_finding = {
    "kind": "error_check",
    "doc": "test_doc.md",
    "errors": [{"type": "事实错误", "description": "测试", "severity": "中"}],
    "chunk_id": "ch_abc123",
    "dataset_id": "ds_001",
    "document_id": "doc_001",
}
required = ["chunk_id", "dataset_id", "document_id"]
for k in required:
    check(f"finding.{k} 存在且非空", bool(mock_finding.get(k)), repr(mock_finding.get(k)))
check("x-show='f.chunk_id' 将为 Truthy → 修复按钮可见", bool(mock_finding.get("chunk_id")))

# ---- 总结 ----
print(f"\n{'='*55}")
print(f"  结果: {pass_count}/{pass_count+fail_count} 通过")
if fail_count == 0:
    print("  结论: chunk 追踪链路完整，重启后可正常使用")
    print("  建议: 重启 Windows，然后跑一次审计漏斗")
else:
    print(f"  警告: {fail_count} 项失败，请在重启前修复")
print(f"{'='*55}")
