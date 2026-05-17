"""Audit prompts.

用 str.replace() 占位符 <<...>> 而非 .format()，避免 prompt 内嵌 JSON 花括号
被当成字段名解析（原 RAG-Audit 已经踩过坑）。
"""

CONFLICT_PROMPT = """你是知识库审计专家。分析以下两段文档内容是否存在冲突、矛盾或不一致。

文档A来源：<<SOURCE_A>>
文档A内容：
<<CONTENT_A>>

文档B来源：<<SOURCE_B>>
文档B内容：
<<CONTENT_B>>

请返回JSON：
{
  "has_conflict": true/false,
  "conflict_type": "事实矛盾|版本不一致|观点分歧|无",
  "severity": "高|中|低|无",
  "description": "冲突描述",
  "recommendation": "建议处理方式",
  "suggestion": "若存在冲突，给出修正后的文本（保持原风格，仅修正冲突部分）"
}
只返回JSON，不要 markdown 代码块。"""


ERROR_CHECK_PROMPT = """你是知识库审计专家。检查以下文档内容是否存在错误。

文档来源：<<SOURCE>>
文档内容：
<<CONTENT>>

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
只返回JSON，不要 markdown 代码块。"""


def render_conflict(source_a: str, content_a: str, source_b: str, content_b: str) -> str:
    return (
        CONFLICT_PROMPT
        .replace("<<SOURCE_A>>", source_a)
        .replace("<<CONTENT_A>>", content_a[:3000])
        .replace("<<SOURCE_B>>", source_b)
        .replace("<<CONTENT_B>>", content_b[:3000])
    )


def render_error_check(source: str, content: str) -> str:
    return (
        ERROR_CHECK_PROMPT
        .replace("<<SOURCE>>", source)
        .replace("<<CONTENT>>", content[:3000])
    )


FIX_PROMPT = """你是知识库修正专家。以下文档片段被标记为存在冲突/错误，请生成修正后的文本。

原文来源：<<SOURCE>>
原文内容：
<<CONTENT>>

冲突/错误描述：<<ISSUE>>
建议处理方式：<<RECOMMENDATION>>

请只返回修正后的文本，不要添加任何解释、不要 markdown、不要 JSON。直接输出修正后的完整文本。
如果原文无需修改，原样返回原文。"""
