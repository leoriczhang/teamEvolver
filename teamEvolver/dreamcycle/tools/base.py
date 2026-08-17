"""Base tool interface and registry."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ToolResult:
    """Result of a tool execution."""
    success: bool
    output: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    def truncated(self, max_len: int = 4000) -> str:
        """Return output truncated to max length."""
        if len(self.output) <= max_len:
            return self.output
        return self.output[:max_len] + f"\n... (truncated, {len(self.output)} total chars)"


class Tool(ABC):
    """Abstract base class for DreamCycle tools."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Tool name used in LLM function calling."""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable description for the LLM."""
        ...

    @property
    @abstractmethod
    def parameters_schema(self) -> Dict[str, Any]:
        """JSON Schema for the tool's parameters."""
        ...

    @abstractmethod
    def execute(self, **kwargs) -> ToolResult:
        """Execute the tool with given parameters."""
        ...

    def to_openai_schema(self) -> Dict[str, Any]:
        """Convert to OpenAI function calling format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters_schema,
            },
        }


class ToolRegistry:
    """Registry of available tools."""

    def __init__(self):
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Register a tool."""
        self._tools[tool.name] = tool
        logger.debug("Registered tool: %s", tool.name)

    def get(self, name: str) -> Optional[Tool]:
        """Get a tool by name."""
        return self._tools.get(name)

    def execute(self, name: str, **kwargs) -> ToolResult:
        """Execute a tool by name."""
        tool = self._tools.get(name)
        if not tool:
            return ToolResult(success=False, output="", error=f"Unknown tool: {name}")
        try:
            return tool.execute(**kwargs)
        except Exception as e:
            logger.error("Tool %s failed: %s", name, e, exc_info=True)
            return ToolResult(success=False, output="", error=f"Tool execution error: {e}")

    def all_schemas(self) -> List[Dict[str, Any]]:
        """Get OpenAI schemas for all registered tools."""
        return [t.to_openai_schema() for t in self._tools.values()]

    @property
    def tool_names(self) -> List[str]:
        return list(self._tools.keys())
