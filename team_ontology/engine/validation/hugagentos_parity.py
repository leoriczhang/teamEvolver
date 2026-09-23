"""与 HugAgentOS ``src/backend/core/ontology/validator.py`` 的 ``DomainPackValidator``
行为一致的 parity 校验器。

校验内容（逐项对齐上游）：
1. pydantic 模型全量校验（类型/id 正则/唯一性/parent 环/引用完整性/extra 字段禁止），
   错误定位到 ``concepts.3`` 之类的字段路径；
2. 每条约束的 ``schema`` 必须是合法的 Draft 2020-12 JSON Schema；
3. 工具引用（约束 target.tool、工作流 required/forbidden_tools、asset_trigger 工具 id）
   必须存在于工具目录，除非 ``config.allow_unresolved_tools`` 为 true（降级为警告）；
4. ``tool_parameter`` 约束的 parameter 必须存在于目标工具输入 schema 的 properties；
5. 工作流 required_tools 与 forbidden_tools 不得有交集。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator
from pydantic import ValidationError as PydanticValidationError

from ..schemas import OntologyPackDocument


@dataclass
class ValidationIssue:
    severity: str  # "error" | "warning" | "info"
    path: str
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {"severity": self.severity, "path": self.path, "message": self.message}


@dataclass
class ValidationReport:
    valid: bool
    errors: list[ValidationIssue] = field(default_factory=list)
    warnings: list[ValidationIssue] = field(default_factory=list)
    infos: list[ValidationIssue] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": [i.as_dict() for i in self.errors],
            "warnings": [i.as_dict() for i in self.warnings],
            "infos": [i.as_dict() for i in self.infos],
        }


def tool_input_schema(tool_schema: dict[str, Any]) -> dict[str, Any]:
    """提取工具输入 schema，兼容上游 ``_tool_input_schema`` 的两种形态。"""
    function = tool_schema.get("function") if isinstance(tool_schema, dict) else None
    if isinstance(function, dict):
        return function.get("parameters") or function.get("inputSchema") or {}
    return tool_schema.get("inputSchema") or tool_schema.get("parameters") or {}


class DomainPackValidator:
    """Validate structure, JSON Schema, and tool references before activation."""

    def validate(
        self,
        payload: dict[str, Any],
        *,
        tool_schemas: dict[str, dict[str, Any]] | None = None,
        known_tools: Iterable[str] = (),
    ) -> tuple[OntologyPackDocument | None, ValidationReport]:
        errors: list[ValidationIssue] = []
        warnings: list[ValidationIssue] = []
        try:
            document = OntologyPackDocument.model_validate(payload)
        except PydanticValidationError as exc:
            for item in exc.errors(include_url=False):
                path = ".".join(str(part) for part in item["loc"])
                # 模型级错误（如 parent 环）的 loc 为空，回退到 pack 便于定位
                errors.append(ValidationIssue("error", path or "pack", item["msg"]))
            return None, ValidationReport(False, errors, warnings)

        schemas = tool_schemas or {}
        known = set(known_tools) | set(schemas)
        for index, rule in enumerate(document.constraints):
            path = f"constraints.{index}"
            if rule.schema_:
                try:
                    Draft202012Validator.check_schema(rule.schema_)
                except Exception as exc:  # SchemaError is version-specific
                    errors.append(ValidationIssue("error", f"{path}.schema", str(exc)))
            tool_name = rule.target.tool
            if not tool_name:
                continue
            if tool_name not in known:
                issue = ValidationIssue(
                    "warning" if document.config.allow_unresolved_tools else "error",
                    f"{path}.target.tool",
                    f"unknown tool reference: {tool_name}",
                )
                (warnings if document.config.allow_unresolved_tools else errors).append(issue)
                continue
            if rule.target.kind == "tool_parameter" and tool_name in schemas:
                properties = tool_input_schema(schemas[tool_name]).get("properties", {})
                if rule.target.parameter not in properties:
                    errors.append(
                        ValidationIssue(
                            "error",
                            f"{path}.target.parameter",
                            f"unknown parameter {rule.target.parameter!r} for tool {tool_name}",
                        )
                    )

        for index, workflow in enumerate(document.workflows):
            for tool_name in set(workflow.required_tools + workflow.forbidden_tools):
                if tool_name not in known:
                    issue = ValidationIssue(
                        "warning" if document.config.allow_unresolved_tools else "error",
                        f"workflows.{index}",
                        f"unknown tool reference: {tool_name}",
                    )
                    (warnings if document.config.allow_unresolved_tools else errors).append(issue)
            overlap = set(workflow.required_tools) & set(workflow.forbidden_tools)
            if overlap:
                errors.append(
                    ValidationIssue(
                        "error",
                        f"workflows.{index}",
                        f"tools cannot be both required and forbidden: {sorted(overlap)}",
                    )
                )
            for trigger_index, trigger in enumerate(workflow.asset_triggers):
                if trigger.kind != "tool":
                    continue
                for tool_name in trigger.ids:
                    if tool_name in known:
                        continue
                    issue = ValidationIssue(
                        "warning" if document.config.allow_unresolved_tools else "error",
                        f"workflows.{index}.asset_triggers.{trigger_index}.ids",
                        f"unknown tool reference: {tool_name}",
                    )
                    (warnings if document.config.allow_unresolved_tools else errors).append(issue)

        return document, ValidationReport(not errors, errors, warnings)
