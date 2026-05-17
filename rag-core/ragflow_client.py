"""RAGFlow REST 客户端。

基于 v0.25.4 抓取的 /openapi.json 校对的真实路径。
关键差异（与档案 3.2 节文档不同）：
  - 触发解析：POST /api/v1/datasets/{dataset_id}/documents/parse
    （档案写的 POST /chunks 是错的）
  - 检索：POST /api/v1/retrieval

认证：Authorization: Bearer <api_key>
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential


class RAGFlowError(RuntimeError):
    def __init__(self, status: int, body: Any):
        super().__init__(f"RAGFlow API error {status}: {body}")
        self.status = status
        self.body = body


class RAGFlowClient:
    def __init__(self, base_url: str, api_key: str, timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    # ------------------------------------------------------------------
    # primitives
    # ------------------------------------------------------------------
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
        reraise=True,
    )
    def _request(self, method: str, path: str, **kwargs) -> Any:
        r = self._client.request(method, path, **kwargs)
        if r.status_code >= 400:
            try:
                body = r.json()
            except Exception:
                body = r.text
            raise RAGFlowError(r.status_code, body)
        if r.headers.get("content-type", "").startswith("application/json"):
            return r.json()
        return r.text

    # ------------------------------------------------------------------
    # datasets
    # ------------------------------------------------------------------
    def list_datasets(self) -> list[dict]:
        data = self._request("GET", "/api/v1/datasets")
        return data.get("data", []) if isinstance(data, dict) else []

    def create_dataset(
        self,
        name: str,
        *,
        description: str = "",
        language: str = "Chinese",
        chunk_method: str = "naive",
        chunk_token_num: int = 256,
        embedding_model: str = "BAAI/bge-m3@SILICONFLOW",
    ) -> dict:
        # 注意：v0.25.4 OpenAPI 规范与实际行为不一致：
        #   - 顶层不接受 language，会报 "Extra inputs are not permitted"
        #   - parser_config.language 同样被拒
        #   - 实测 RAGFlow 会基于文档内容自动识别语言
        # language 参数保留接口兼容，但当前不传给后端。
        body = {
            "name": name,
            "description": description,
            "chunk_method": chunk_method,
            "embedding_model": embedding_model,
            "parser_config": {
                "chunk_token_num": chunk_token_num,
                "delimiter": "\n\n",
            },
        }
        data = self._request("POST", "/api/v1/datasets", json=body)
        return data.get("data", data) if isinstance(data, dict) else data

    def get_dataset(self, dataset_id: str) -> dict:
        data = self._request("GET", f"/api/v1/datasets/{dataset_id}")
        return data.get("data", data) if isinstance(data, dict) else data

    def delete_datasets(self, ids: list[str]) -> Any:
        # API: DELETE /api/v1/datasets accepts {"ids": [...]}
        return self._request("DELETE", "/api/v1/datasets", json={"ids": ids})

    # ------------------------------------------------------------------
    # documents
    # ------------------------------------------------------------------
    def list_documents(self, dataset_id: str, *, page: int = 1, page_size: int = 200) -> list[dict]:
        params = {"page": page, "page_size": page_size}
        data = self._request("GET", f"/api/v1/datasets/{dataset_id}/documents", params=params)
        if isinstance(data, dict):
            payload = data.get("data", data)
            if isinstance(payload, dict):
                return payload.get("docs", []) or payload.get("documents", []) or []
            if isinstance(payload, list):
                return payload
        return []

    def upload_document(self, dataset_id: str, file_path: Path, metadata: Optional[dict] = None) -> dict:
        with file_path.open("rb") as f:
            files = {"file": (file_path.name, f, "application/octet-stream")}
            data_kwargs: dict = {"files": files}
            if metadata:
                data_kwargs["data"] = {"meta_fields": json.dumps(metadata, ensure_ascii=False)}
            data = self._request(
                "POST",
                f"/api/v1/datasets/{dataset_id}/documents",
                **data_kwargs,
            )
        return data.get("data", data) if isinstance(data, dict) else data

    def parse_documents(self, dataset_id: str, document_ids: list[str]) -> Any:
        # 档案写的 POST /chunks 是错的，正确是 /documents/parse
        return self._request(
            "POST",
            f"/api/v1/datasets/{dataset_id}/documents/parse",
            json={"document_ids": document_ids},
        )

    def get_document(self, dataset_id: str, document_id: str) -> dict:
        data = self._request(
            "GET",
            f"/api/v1/datasets/{dataset_id}/documents/{document_id}",
        )
        return data.get("data", data) if isinstance(data, dict) else data

    # ------------------------------------------------------------------
    # chunks
    # ------------------------------------------------------------------
    def list_chunks(
        self,
        dataset_id: str,
        document_id: str,
        *,
        page: int = 1,
        page_size: int = 200,
    ) -> list[dict]:
        params = {"page": page, "page_size": page_size}
        data = self._request(
            "GET",
            f"/api/v1/datasets/{dataset_id}/documents/{document_id}/chunks",
            params=params,
        )
        if isinstance(data, dict):
            payload = data.get("data", data)
            if isinstance(payload, dict):
                return payload.get("chunks", []) or []
            if isinstance(payload, list):
                return payload
        return []

    # ------------------------------------------------------------------
    # retrieval
    # ------------------------------------------------------------------
    def retrieve(
        self,
        question: str,
        dataset_ids: list[str],
        *,
        top_k: int = 8,
        similarity_threshold: float = 0.2,
        keyword: bool = True,
    ) -> dict:
        body = {
            "question": question,
            "dataset_ids": dataset_ids,
            "top_k": top_k,
            "similarity_threshold": similarity_threshold,
            "keyword": keyword,
        }
        data = self._request("POST", "/api/v1/retrieval", json=body)
        return data.get("data", data) if isinstance(data, dict) else data

    # ------------------------------------------------------------------
    # health
    # ------------------------------------------------------------------
    def ping(self) -> bool:
        try:
            self.list_datasets()
            return True
        except Exception:
            return False

    def close(self):
        self._client.close()
