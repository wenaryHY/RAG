"""FileSync 子包：D:/RAG/RAGfiles/<library>/<file> ↔ RAGFlow 知识库。

设计要点：
- 文件系统是 inbox，RAGFlow 是 source of truth
- watchdog 监听 + 5 秒 debounce + 启动期 reconcile（兜底漏事件）
- 30 秒轮询 RAGFlow 列表，反向同步：远端新建库 → 本地建文件夹
- 去重：sha256 + state.sqlite.files
- 完成后桌面通知（winotify）

阶段 1.C：基础双向同步。AI 跨库关联、metadata 标注留 Phase 5。
"""
