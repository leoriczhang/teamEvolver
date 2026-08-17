from __future__ import annotations

from teamEvolver.dreamcycle.config import DreamCycleConfig
from teamEvolver.dreamcycle.react.engine import ReActEngine
from teamEvolver.dreamcycle.react.planner import (
    Task,
    TaskPlan,
    TaskPriority,
    TaskStatus,
)
from teamEvolver.dreamcycle.tools.base import Tool, ToolRegistry, ToolResult


class _StubWriteTool(Tool):
    def __init__(self, outcomes: list[bool]) -> None:
        self._outcomes = outcomes
        self.calls = 0

    @property
    def name(self) -> str:
        return "viking_remember"

    @property
    def description(self) -> str:
        return "stub write tool"

    @property
    def parameters_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    def execute(self, **kwargs) -> ToolResult:
        success = self._outcomes[min(self.calls, len(self._outcomes) - 1)]
        self.calls += 1
        return ToolResult(
            success=success,
            output="OK" if success else "DENIED: duplicate",
        )


def _config(tmp_path, monkeypatch) -> DreamCycleConfig:
    monkeypatch.setenv("DREAMCYCLE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DREAMCYCLE_LLM_API_KEY", "test-key")
    return DreamCycleConfig()


def _plan() -> TaskPlan:
    return TaskPlan(
        job_name="deduplication",
        tasks=[
            Task(
                id="dd-1",
                description="merge duplicates",
                priority=TaskPriority.HIGH,
            )
        ],
    )


def _tool_call_message():
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "c1",
                "function": {
                    "name": "viking_remember",
                    "arguments": "{}",
                },
            }
        ],
    }


def _text_message(text: str):
    return {"role": "assistant", "content": text}


def test_plan_complete_without_writes_is_pushed_back(
    tmp_path,
    monkeypatch,
) -> None:
    registry = ToolRegistry()
    registry.register(_StubWriteTool([True]))
    engine = ReActEngine(_config(tmp_path, monkeypatch), registry, "system")
    scripted = [
        {
            "choices": [
                {
                    "message": _text_message("PLAN COMPLETE: nothing to do"),
                    "finish_reason": "stop",
                }
            ]
        },
        {
            "choices": [
                {
                    "message": _tool_call_message(),
                    "finish_reason": "tool_calls",
                }
            ]
        },
        {
            "choices": [
                {
                    "message": _text_message("PLAN COMPLETE: merged one doc"),
                    "finish_reason": "stop",
                }
            ]
        },
    ]
    calls = {"count": 0}

    def fake_call_llm():
        index = calls["count"]
        calls["count"] += 1
        return scripted[min(index, len(scripted) - 1)]

    monkeypatch.setattr(engine, "_call_llm", fake_call_llm)
    plan = engine.execute_plan(_plan())

    assert engine.successful_writes == 1
    assert calls["count"] >= 3
    assert plan.is_complete


def test_all_tools_failing_trips_error_breaker(
    tmp_path,
    monkeypatch,
) -> None:
    registry = ToolRegistry()
    registry.register(_StubWriteTool([False]))
    engine = ReActEngine(_config(tmp_path, monkeypatch), registry, "system")

    monkeypatch.setattr(
        engine,
        "_call_llm",
        lambda: {
            "choices": [
                {
                    "message": _tool_call_message(),
                    "finish_reason": "tool_calls",
                }
            ]
        },
    )

    plan = engine.execute_plan(_plan())

    assert engine.successful_writes == 0
    assert engine.turn_count <= engine._max_errors + 1
    assert all(
        task.status != TaskStatus.COMPLETED
        for task in plan.tasks
    )
