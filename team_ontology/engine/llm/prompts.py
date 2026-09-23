"""各阶段 prompt 模板与版本号（prompt 版本钉住 → 确定性回归）。

Prompt 版本号是提示词内容的身份标识：改动模板必须同步改版本号，
否则录制/重放缓存会静默混用不同行为。
"""

from __future__ import annotations

PROMPT_TERMS_V1 = "terms-v1"
PROMPT_TAXONOMY_V1 = "taxonomy-v1"
PROMPT_RELATIONS_V2 = "relations-v2"
PROMPT_CLOSED_VALUES_V2 = "closed-values-v2"
PROMPT_IRON_LAWS_V3 = "iron-laws-v3"
PROMPT_WORKFLOWS_V4 = "workflows-v4"


def array_field(response: dict, key: str) -> list:
    """容错读取响应中的数组字段：优先取 key；缺失时取首个数组值字段。"""
    value = response.get(key)
    if isinstance(value, list):
        return value
    for candidate in response.values():
        if isinstance(candidate, list):
            return candidate
    return []


SYSTEM_COMMON = """你是领域本体工程师。你的任务是从领域语料中提取受控概念、关系、约束与工作流，
产出 HugAgentOS Domain Pack v1.0 的组成部分。

铁律：
1. 只输出调用方要求的 JSON，不要输出任何解释、前言或代码块标记。
2. 概念/术语必须真实出现在给定语料中，禁止编造语料中不存在的词面。
3. 证据只能引用给定 chunk 的 id，禁止引用不存在的 chunk id，禁止虚构原文。
4. 概念 id 只能从给定概念清单中选择（或按规则生成），禁止引用清单外的 id。
5. 定义必须概括语料中该术语的实际含义，不确定时写"语料中未给出完整定义"。
6. 保留原文用词：中文术语用中文词面，英文术语保持英文词面。
"""


def render_chunks(chunks: list[dict]) -> str:
    lines: list[str] = []
    for chunk in chunks:
        heading = f" 标题: {chunk['heading']}" if chunk.get("heading") else ""
        lines.append(f"[chunk {chunk['chunk_id']}]（文档: {chunk['doc']}{heading}）\n{chunk['text']}")
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# 概念提取
# ---------------------------------------------------------------------------


def terms_system() -> str:
    return (
        SYSTEM_COMMON
        + "\n"
        + "当前任务：把候选术语整合为受控概念。规则：\n"
        + "1. 同义或近义的候选术语合并为一个概念：最常用词面作为 name，其余作为 aliases。\n"
        + '2. 过于宽泛的通用词（如"公司""流程"）且与领域无关时可放入 dropped。\n'
        + "3. id 必须是英文 CamelCase（如 ProcurementRequest），全局唯一。\n"
        + "4. definition 用一句话定义；risk 按领域影响取 low/medium/high。\n"
        + "5. source_terms 列出该概念吸收的候选术语（必须来自给定清单）。\n"
        + "6. 每个概念至少吸收一个候选术语，否则放入 dropped。\n"
    )


def terms_user(batch_terms: list[str], chunks: list[dict]) -> str:
    return (
        "候选术语清单：\n" + "\n".join(f"- {t}" for t in batch_terms) + "\n\n相关语料片段：\n" + render_chunks(chunks)
    )


TERMS_SCHEMA = {
    "type": "object",
    "properties": {
        "concepts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_.:-]{0,127}$"},
                    "name": {"type": "string", "minLength": 1, "maxLength": 255},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                    "definition": {"type": "string", "minLength": 1, "maxLength": 4000},
                    "risk": {"type": "string", "enum": ["low", "medium", "high"]},
                    "source_terms": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                },
                "required": ["id", "name", "aliases", "definition", "risk", "source_terms"],
            },
        },
        "dropped": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["concepts", "dropped"],
}


# ---------------------------------------------------------------------------
# 层级归纳
# ---------------------------------------------------------------------------


def taxonomy_system() -> str:
    return (
        SYSTEM_COMMON
        + "\n"
        + "当前任务：为每个概念提议 parent_id（单继承层级）。规则：\n"
        + "1. parent_id 只能取给定概念清单中的 id，或为 null（顶层概念）。\n"
        + '2. 只能建立"是一种"（is-a）关系：子概念是父概念的一种或子类。\n'
        + '3. 依据语料中概念间的上下位表述（如"X 包括 Y""Y 属于 X""Y 是一种 X"）。\n'
        + "4. 没有语料依据时 parent_id 取 null，禁止猜测。\n"
        + "5. 每个概念只输出一次。\n"
    )


def taxonomy_user(concepts: list[dict], chunks: list[dict]) -> str:
    concept_lines = "\n".join(f"- {c['id']}: {c['name']}（{c['definition'][:80]}）" for c in concepts)
    return f"概念清单：\n{concept_lines}\n\n相关语料片段：\n{render_chunks(chunks)}"


TAXONOMY_SCHEMA = {
    "type": "object",
    "properties": {
        "edges": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "child": {"type": "string"},
                    "parent": {"type": ["string", "null"]},
                },
                "required": ["child", "parent"],
            },
        }
    },
    "required": ["edges"],
}


# ---------------------------------------------------------------------------
# 关系提取
# ---------------------------------------------------------------------------


def relations_system() -> str:
    return (
        SYSTEM_COMMON
        + "\n"
        + "当前任务：从语料提取概念间关系。规则：\n"
        + "1. subject/object 必须是给定概念清单中的 id。\n"
        + '2. predicate 用简洁的中文动/名词短语（如"包含""由…评估"），必须源自语料表述。\n'
        + '3. 语料明确写"X 不得/禁止/不能 Y"时 forbidden=true，否则 false。\n'
        + '4. min_cardinality/max_cardinality 仅在语料有明确数量依据时填写（如"至少一个"），否则为 null。\n'
        + "5. 只输出有语料依据的关系；同类关系只保留最概括的一条。\n"
    )


def relations_user(concepts: list[dict], chunks: list[dict]) -> str:
    concept_lines = "\n".join(f"- {c['id']}: {c['name']}" for c in concepts)
    return f"概念清单：\n{concept_lines}\n\n语料片段：\n{render_chunks(chunks)}"


# 传输 schema 保持宽松：id/description/基数可缺省（后处理确定性兜底），
# 小模型可用性优先；最终产物仍由 parity 校验器严格把关。
RELATIONS_SCHEMA = {
    "type": "object",
    "properties": {
        "relations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_.:-]{0,127}$"},
                    "subject": {"type": "string"},
                    "predicate": {"type": "string", "minLength": 1, "maxLength": 128},
                    "object": {"type": "string"},
                    "description": {"type": "string", "maxLength": 2000},
                    "min_cardinality": {"type": ["integer", "string", "null"]},
                    "max_cardinality": {"type": ["integer", "string", "null"]},
                    "forbidden": {"type": "boolean"},
                },
                "required": ["subject", "predicate", "object"],
            },
        }
    },
    "required": ["relations"],
}


# ---------------------------------------------------------------------------
# 受控取值（closed_values）
# ---------------------------------------------------------------------------


def closed_values_system() -> str:
    return (
        SYSTEM_COMMON
        + "\n"
        + "当前任务：为概念提取受控取值（closed_values）。规则：\n"
        + '1. 只提取语料中明确列出的枚举（如"风险等级分为：低、中、高"）。\n'
        + "2. 没有明确枚举的概念不输出条目。\n"
        + '3. 取值为规范词面，去除序号（如"1. 低"→"低"）。\n'
    )


def closed_values_user(concepts: list[dict], chunks: list[dict]) -> str:
    concept_lines = "\n".join(f"- {c['id']}: {c['name']}" for c in concepts)
    return f"概念清单：\n{concept_lines}\n\n语料片段：\n{render_chunks(chunks)}"


CLOSED_VALUES_SCHEMA = {
    "type": "object",
    "properties": {
        "entries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "concept_id": {"type": "string"},
                    "values": {"type": "array", "items": {"type": "string"}, "minItems": 2},
                },
                "required": ["concept_id", "values"],
            },
        }
    },
    "required": [],
}


# ---------------------------------------------------------------------------
# 铁律（输出约束）
# ---------------------------------------------------------------------------


def iron_laws_system() -> str:
    return (
        SYSTEM_COMMON
        + "\n"
        + '当前任务：从语料提取"铁律"（对输出/流程的硬性要求），转为输出约束。规则：\n'
        + "1. 只提取语料明确写出的要求（必须/不得/至少/不超过/需引用/需说明等句式）。\n"
        + "2. output_tag 是短英文标签（如 procurement_risk_summary），同一工作流内唯一。\n"
        + "3. schema 用 JSON Schema 表达该要求（如 minLength、minItems、required、enum）。\n"
        + '4. message 说明"为什么失败"，suggestion 说明"下一步怎么做"。\n'
        + "5. risk 取 low/medium/high；mode 一律 log。\n"
        + "6. 普通描述性句子（非要求）不要提取。\n"
    )


def iron_laws_user(concepts: list[dict], chunks: list[dict]) -> str:
    concept_lines = "\n".join(f"- {c['id']}: {c['name']}" for c in concepts)
    return f"概念清单：\n{concept_lines}\n\n语料片段：\n{render_chunks(chunks)}"


IRON_LAWS_SCHEMA = {
    "type": "object",
    "properties": {
        "constraints": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_.:-]{0,127}$"},
                    "name": {"type": "string", "minLength": 1, "maxLength": 255},
                    "output_tag": {"type": "string", "maxLength": 128},
                    "schema": {"type": "object"},
                    "concept_id": {"type": ["string", "null"]},
                    "requires_citations": {"type": "boolean"},
                    "risk": {"type": "string", "enum": ["low", "medium", "high"]},
                    "message": {"type": "string", "minLength": 1, "maxLength": 2000},
                    "suggestion": {"type": "string", "maxLength": 2000},
                },
                "required": [],
            },
        }
    },
    "required": [],
}


# ---------------------------------------------------------------------------
# 工作流提案
# ---------------------------------------------------------------------------


def workflows_system() -> str:
    return (
        SYSTEM_COMMON
        + "\n"
        + "当前任务：把概念与约束聚类为领域工作流。规则：\n"
        + "1. 工作流对应一个可验收任务；triggers 用用户提问中可能出现的短语（优先取概念名/别名及语料任务表述）。\n"
        + "2. required_tools/forbidden_tools 只能从给定工具清单中选，没有依据就留空。\n"
        + "3. output_tags 与给定输出约束的 output_tag 对齐。\n"
        + "4. risk 取聚合风险：任一 high 则为 high，否则任一 medium 为 medium，否则 low。\n"
        + "5. review_level 按映射：high→committee、medium→checkpoint、low→none。\n"
        + "6. 每个工作流至少 1 个 trigger。\n"
    )


def workflows_user(
    concepts: list[dict],
    constraints: list[dict],
    tools: list[dict],
    chunks: list[dict],
) -> str:
    concept_lines = "\n".join(f"- {c['id']}: {c['name']}（别名: {'、'.join(c['aliases']) or '-'}）" for c in concepts)
    constraint_lines = "\n".join(
        f"- {c['id']}: {c['name']}（output_tag={c.get('output_tag') or '-'}，concept={c.get('concept_id') or '-'}）"
        for c in constraints
    )
    tool_lines = "\n".join(f"- {t['name']}: {t['description']}" for t in tools)
    return (
        f"概念清单：\n{concept_lines}\n\n"
        f"输出约束清单：\n{constraint_lines}\n\n"
        f"可用工具清单：\n{tool_lines or '（无）'}\n\n"
        f"语料片段：\n{render_chunks(chunks)}"
    )


WORKFLOWS_SCHEMA = {
    "type": "object",
    "properties": {
        "workflows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_.:-]{0,127}$"},
                    "name": {"type": "string", "minLength": 1, "maxLength": 255},
                    "triggers": {"type": "array", "items": {"type": ["string", "object"]}, "minItems": 1},
                    "required_tools": {"type": "array", "items": {"type": ["string", "object"]}},
                    "forbidden_tools": {"type": "array", "items": {"type": ["string", "object"]}},
                    "output_tags": {"type": "array", "items": {"type": ["string", "object"]}},
                    "review_level": {"type": "string", "enum": ["none", "checkpoint", "committee"]},
                    "risk": {"type": "string", "enum": ["low", "medium", "high"]},
                    "concept_ids": {"type": "array", "items": {"type": ["string", "object"]}},
                },
                "required": [],
            },
        }
    },
    "required": [],
}
