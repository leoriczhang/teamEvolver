"""LLM-facing tool over the round-scoped shared blackboard."""

from __future__ import annotations

import json
from typing import Any, Dict

from ..blackboard import Blackboard
from .base import Tool, ToolResult


class BlackboardTool(Tool):
    """Read/write the cross-job shared scratchpad for this maintenance round.

    Lets a later job reuse what an earlier one already established (team
    members, active projects, authoritative doc URIs) and see which documents
    were already archived/merged this round, instead of rediscovering them.
    """

    def __init__(self, blackboard: Blackboard):
        self._bb = blackboard

    @property
    def name(self) -> str:
        return "shared_notes"

    @property
    def description(self) -> str:
        return (
            "Shared scratchpad across maintenance jobs in this round. "
            "action=recall reads accumulated facts and already-processed URIs; "
            "action=record_fact stores a durable observation (members/projects/"
            "authoritative doc URIs) under a topic; action=mark_processed records "
            "that a URI was handled (archived/merged) so later jobs skip it. "
            "Consult recall before searching from scratch."
        )

    @property
    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["recall", "record_fact", "mark_processed"],
                },
                "topic": {"type": "string", "description": "Fact topic (record_fact), e.g. members, projects, authoritative_docs."},
                "value": {"type": "string", "description": "Fact value (record_fact)."},
                "uri": {"type": "string", "description": "Processed URI (mark_processed)."},
                "note": {"type": "string", "description": "Optional action/reason (mark_processed)."},
            },
            "required": ["action"],
        }

    def execute(self, **kwargs) -> ToolResult:
        action = kwargs.get("action", "recall")
        if action == "record_fact":
            topic = kwargs.get("topic", "general")
            value = kwargs.get("value", "")
            if not value:
                return ToolResult(success=False, output="value is required for record_fact")
            self._bb.add_fact(topic, value)
            return ToolResult(success=True, output=f"OK: noted under '{topic}'")
        if action == "mark_processed":
            uri = kwargs.get("uri", "")
            if not uri:
                return ToolResult(success=False, output="uri is required for mark_processed")
            self._bb.mark_processed(uri, "manual", kwargs.get("note", ""))
            return ToolResult(success=True, output=f"OK: marked processed {uri}")
        # recall
        return ToolResult(
            success=True,
            output=json.dumps(self._bb.snapshot(), ensure_ascii=False, indent=2),
        )
