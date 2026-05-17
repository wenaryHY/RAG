"""Audit 子包 - rag-core/audit。

阶段 1.C：基础迁移
- /audit/health
- /audit/run        手动触发批量审计（O(n²) 全量比对，用于小库或调试）
- /audit/single     单文档审计
- /audit/reports    报告列表

阶段 4：三级漏斗 embedding→Flash→Opus + 周日 02:00 自动调度。
"""
