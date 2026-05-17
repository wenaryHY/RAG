"""Verify list_chunks returns chunks with id field."""
import httpx, json
from configparser import ConfigParser
cp = ConfigParser()
cp.read("D:/private/keys.ini", encoding="utf-8")
k = cp["ragflow"]["key"]
h = {"Authorization": f"Bearer {k}"}
c = httpx.Client(base_url="http://127.0.0.1:9380", timeout=10, headers=h)

ds = c.get("/api/v1/datasets").json()["data"]
pharm = [d for d in ds if d["name"] == "pharmacy"][0]
did = pharm["id"]

docs = c.get(f"/api/v1/datasets/{did}/documents").json()
dl = docs.get("data", docs).get("docs", [])

# Get the .txt doc
txt_doc = [d for d in dl if d.get("name", "").endswith(".txt")][0]
doc_id = txt_doc["id"]

# Call the actual list_chunks method
import sys
sys.path.insert(0, "D:/RAG/rag-core")
from ragflow_client import RAGFlowClient
from config import load_config
cfg = load_config()
rag = RAGFlowClient(cfg.ragflow_base_url, cfg.ragflow_api_key)

chunks = rag.list_chunks(did, doc_id, page=1, page_size=3)
print(f"list_chunks returned {len(chunks)} chunks")
if chunks:
    ch = chunks[0]
    print(f"First chunk keys: {list(ch.keys())[:8]}")
    print(f"id: {repr(ch.get('id'))}")
    print(f"content: {str(ch.get('content', ''))[:60]}")
else:
    print("NO CHUNKS RETURNED - list_chunks parsing bug!")
    # Debug raw API response
    raw = c.get(f"/api/v1/datasets/{did}/documents/{doc_id}/chunks", params={"page": 1, "page_size": 3})
    print(f"Raw response structure: {json.dumps(list(raw.json().keys() if isinstance(raw.json(), dict) else [type(raw.json()).__name__]))[:200]}")
    raw_data = raw.json()
    if isinstance(raw_data, dict):
        data_field = raw_data.get("data", {})
        if isinstance(data_field, dict):
            print(f"data.chunks count: {len(data_field.get('chunks', []))}")
        elif isinstance(data_field, list):
            print(f"data is list, length: {len(data_field)}")
