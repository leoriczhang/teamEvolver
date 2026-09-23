"""Working memory for ReAct agent — tracks observations, thoughts, actions within a cycle."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class StepType(str, Enum):
    OBSERVE = "observe"
    THINK = "think"
    ACT = "act"
    REFLECT = "reflect"


@dataclass
class Step:
    """A single step in the ReAct loop."""
    type: StepType
    content: str
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)
    tool_name: Optional[str] = None
    tool_args: Optional[Dict[str, Any]] = None
    tool_result: Optional[str] = None
    success: Optional[bool] = None


class WorkingMemory:
    """Maintains the agent's working memory during a single job execution.
    
    Tracks the full trace of Observe→Think→Act→Reflect steps,
    provides summary for context window management, and enables
    backtracking on errors.
    """

    def __init__(self, max_steps: int = 100):
        self._steps: List[Step] = []
        self._max_steps = max_steps
        self._observations: List[str] = []
        self._actions_taken: List[str] = []
        self._errors: List[str] = []
        self._conclusions: List[str] = []

    def add_step(self, step: Step) -> None:
        """Record a step."""
        self._steps.append(step)
        if step.type == StepType.OBSERVE:
            self._observations.append(step.content[:200])
        elif step.type == StepType.ACT:
            desc = f"{step.tool_name}({step.tool_args})" if step.tool_name else step.content
            self._actions_taken.append(desc[:150])
            if step.success is False:
                self._errors.append(f"{step.tool_name}: {step.tool_result or step.content}"[:200])
        elif step.type == StepType.REFLECT:
            self._conclusions.append(step.content[:200])

        # Trim if exceeded
        if len(self._steps) > self._max_steps:
            self._steps = self._steps[-self._max_steps:]

    def observe(self, content: str, **metadata) -> None:
        self.add_step(Step(type=StepType.OBSERVE, content=content, metadata=metadata))

    def think(self, content: str, **metadata) -> None:
        self.add_step(Step(type=StepType.THINK, content=content, metadata=metadata))

    def act(self, tool_name: str, tool_args: Dict, result: str, success: bool) -> None:
        self.add_step(Step(
            type=StepType.ACT,
            content=f"Called {tool_name}",
            tool_name=tool_name,
            tool_args=tool_args,
            tool_result=result,
            success=success,
        ))

    def reflect(self, content: str, **metadata) -> None:
        self.add_step(Step(type=StepType.REFLECT, content=content, metadata=metadata))

    @property
    def step_count(self) -> int:
        return len(self._steps)

    @property
    def error_count(self) -> int:
        return len(self._errors)

    @property
    def action_count(self) -> int:
        return len(self._actions_taken)

    def get_summary(self) -> str:
        """Generate a concise summary of the working memory for context injection."""
        parts = []
        if self._observations:
            parts.append(f"Observations ({len(self._observations)}):")
            for obs in self._observations[-5:]:
                parts.append(f"  - {obs}")
        if self._actions_taken:
            parts.append(f"Actions taken ({len(self._actions_taken)}):")
            for act in self._actions_taken[-8:]:
                parts.append(f"  - {act}")
        if self._errors:
            parts.append(f"Errors ({len(self._errors)}):")
            for err in self._errors[-3:]:
                parts.append(f"  ! {err}")
        if self._conclusions:
            parts.append(f"Conclusions ({len(self._conclusions)}):")
            for conc in self._conclusions[-3:]:
                parts.append(f"  → {conc}")
        return "\n".join(parts) if parts else "(empty working memory)"

    def get_trace(self) -> List[Dict[str, Any]]:
        """Get the full execution trace for logging/debugging."""
        return [
            {
                "type": s.type.value,
                "content": s.content,
                "timestamp": s.timestamp,
                "tool_name": s.tool_name,
                "tool_args": s.tool_args,
                "success": s.success,
            }
            for s in self._steps
        ]

    def reset(self) -> None:
        """Clear working memory for a new job."""
        self._steps.clear()
        self._observations.clear()
        self._actions_taken.clear()
        self._errors.clear()
        self._conclusions.clear()
