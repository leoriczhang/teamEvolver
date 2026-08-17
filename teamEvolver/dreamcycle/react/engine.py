"""ReAct Engine — the core reasoning loop.

Implements: Observe → Think → Act → Reflect
with automatic error recovery and turn budgeting.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional

import httpx
from openai import OpenAI
from ..config import DreamCycleConfig
from ..tools.base import ToolRegistry, ToolResult
from .memory import WorkingMemory
from .planner import Task, TaskPlan, TaskStatus

logger = logging.getLogger(__name__)


class ReActEngine:
    """ReAct reasoning engine that drives tool-using agents.
    
    The engine maintains a conversation with the LLM, presenting it with:
    - A system prompt describing the current job
    - The task plan and current progress
    - Working memory summary
    - Tool results
    
    On each turn it:
    1. Sends context to LLM
    2. If LLM returns tool_calls → execute tools (ACT)
    3. If LLM returns text → parse as THINK/REFLECT
    4. Check termination conditions
    """

    def __init__(
        self,
        config: DreamCycleConfig,
        tools: ToolRegistry,
        system_prompt: str,
    ):
        self._config = config
        self._tools = tools
        self._system_prompt = system_prompt
        self._memory = WorkingMemory()
        self._messages: List[Dict[str, Any]] = []
        self._client = OpenAI(
            api_key=config.llm.api_key,
            base_url=config.llm.base_url.rstrip("/"),
            timeout=httpx.Timeout(120.0, connect=30.0),
        )
        self._turn_count = 0
        self._consecutive_errors = 0
        self._max_turns = config.scheduler.max_turns_per_job
        self._max_errors = config.scheduler.max_consecutive_errors
        # Count of mutating tool calls that actually succeeded this plan. Task
        # completion is anchored to these events, not to LLM self-report.
        self._successful_writes = 0
        self._nudged_for_empty_completion = False

    # Tools that mutate the maintained memory. A plan claiming completion with
    # zero successful writes did no real maintenance and must not be trusted.
    _WRITE_TOOLS = frozenset(
        {"viking_remember", "viking_forget", "viking_merge", "memory_sanitize"}
    )

    def execute_plan(self, plan: TaskPlan, on_step: Optional[Callable] = None) -> TaskPlan:
        """Execute a full task plan using the ReAct loop.
        
        Args:
            plan: The task plan to execute
            on_step: Optional callback for each step (for logging)
            
        Returns:
            The updated TaskPlan with results
        """
        logger.info("[ReAct] Starting plan: %s (%d tasks)", plan.job_name, len(plan.tasks))
        self._memory.reset()
        self._messages = [{"role": "system", "content": self._system_prompt}]
        self._turn_count = 0
        self._consecutive_errors = 0
        self._successful_writes = 0
        self._nudged_for_empty_completion = False

        # Initial user message with the plan
        plan_desc = self._format_plan(plan)
        self._messages.append({
            "role": "user",
            "content": (
                f"Execute the following maintenance plan:\n\n{plan_desc}\n\n"
                "Work through each task in order. Use tools to gather information and make changes. "
                "After completing each task, state which task you've finished and what was done. "
                "When all tasks are complete, write a final summary starting with 'PLAN COMPLETE:'."
            ),
        })

        while self._turn_count < self._max_turns:
            if self._consecutive_errors >= self._max_errors:
                logger.warning("[ReAct] Too many consecutive errors (%d), aborting", self._consecutive_errors)
                break

            if plan.is_complete:
                logger.info("[ReAct] All tasks complete")
                break

            # Call LLM
            response = self._call_llm()
            if response is None:
                self._consecutive_errors += 1
                time.sleep(2)
                continue

            choice = response.get("choices", [{}])[0]
            message = choice.get("message", {})
            finish_reason = choice.get("finish_reason", "")

            self._messages.append(message)
            self._turn_count += 1

            # Handle tool calls (ACT phase)
            tool_calls = message.get("tool_calls")
            if tool_calls:
                any_tool_success = False
                for tc in tool_calls:
                    func = tc.get("function", {})
                    name = func.get("name", "")
                    try:
                        args = json.loads(func.get("arguments", "{}"))
                    except json.JSONDecodeError:
                        args = {}

                    logger.debug("[ReAct] ACT: %s(%s)", name, json.dumps(args, ensure_ascii=False)[:80])
                    result = self._tools.execute(name, **args)

                    if result.success:
                        any_tool_success = True
                        if name in self._WRITE_TOOLS:
                            self._successful_writes += 1

                    # Record in working memory
                    self._memory.act(name, args, result.output[:500], result.success)
                    if on_step:
                        on_step("act", name, args, result)

                    # Send tool result back
                    self._messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": result.truncated(3000),
                    })

                # Anchor the error circuit-breaker to real tool outcomes: a turn
                # whose every tool call failed (e.g. all writes DENIED) counts as
                # an error round instead of silently resetting progress.
                if any_tool_success:
                    self._consecutive_errors = 0
                else:
                    self._consecutive_errors += 1

            # Handle text response (THINK/REFLECT phase)
            elif message.get("content"):
                content = message["content"]
                self._consecutive_errors = 0

                # Check for plan completion signal
                if "PLAN COMPLETE:" in content:
                    # Anchor completion to real work: if the model declares the
                    # plan done without any successful mutating tool call, treat
                    # the claim as unsubstantiated and push back once before
                    # accepting it (some rounds legitimately find nothing to fix).
                    if self._successful_writes == 0 and not self._nudged_for_empty_completion:
                        self._nudged_for_empty_completion = True
                        self._messages.append({
                            "role": "user",
                            "content": (
                                "You signalled PLAN COMPLETE but no memory was actually "
                                "updated, merged, or archived this round. Either perform the "
                                "concrete write/merge/archive the tasks call for (via "
                                "viking_remember with target_uri, viking_forget, or "
                                "memory_sanitize), or, if the memory is genuinely already "
                                "clean, restate 'PLAN COMPLETE:' and explicitly say that no "
                                "changes were needed and why."
                            ),
                        })
                        continue
                    self._memory.reflect(content)
                    self._mark_remaining_complete(plan, content)
                    if on_step:
                        on_step("reflect", "plan_complete", {}, None)
                    break

                self._memory.think(content)
                if on_step:
                    on_step("think", content[:100], {}, None)

            # Check if LLM is done
            if finish_reason == "stop" and not tool_calls:
                content = message.get("content", "")
                if "PLAN COMPLETE:" in content or plan.is_complete:
                    break
                # LLM stopped but plan isn't done — nudge it
                self._messages.append({
                    "role": "user",
                    "content": (
                        f"Progress: {plan.progress:.0%} complete. "
                        f"Remaining tasks: {[t.description for t in plan.pending_tasks][:3]}. "
                        "Continue working on the next task."
                    ),
                })

        # Mark any still-pending tasks as skipped
        for task in plan.pending_tasks:
            task.status = TaskStatus.SKIPPED
            task.result = "Skipped: turn budget exhausted"

        logger.info(
            "[ReAct] Plan %s finished: %d completed, %d failed, %d skipped (%d turns)",
            plan.job_name,
            len(plan.completed_tasks),
            len(plan.failed_tasks),
            len([t for t in plan.tasks if t.status == TaskStatus.SKIPPED]),
            self._turn_count,
        )
        return plan

    def _call_llm(self) -> Optional[Dict]:
        """Call the LLM with retry logic."""
        try:
            resp = self._client.chat.completions.create(
                model=self._config.llm.model,
                messages=self._messages,
                tools=self._tools.all_schemas(),
                temperature=self._config.llm.temperature,
                max_tokens=self._config.llm.max_tokens,
            )
            return resp.model_dump()
        except Exception as e:
            logger.error("[ReAct] LLM call failed: %s", e)
            return None

    def _format_plan(self, plan: TaskPlan) -> str:
        """Format the plan for the LLM."""
        lines = [f"## Plan: {plan.job_name}\n"]
        for i, task in enumerate(plan.tasks, 1):
            status_icon = {"pending": "⬜", "in_progress": "🔄", "completed": "✅", "failed": "❌", "skipped": "⏭️"}
            icon = status_icon.get(task.status.value, "⬜")
            lines.append(f"{icon} {i}. [{task.priority.value}] {task.description}")
        return "\n".join(lines)

    def _mark_remaining_complete(self, plan: TaskPlan, summary: str) -> None:
        """Mark remaining pending tasks based on the final summary."""
        for task in plan.pending_tasks:
            task.status = TaskStatus.COMPLETED
            task.result = "Completed in batch (per final summary)"
    @property
    def working_memory(self) -> WorkingMemory:
        return self._memory

    @property
    def turn_count(self) -> int:
        return self._turn_count

    @property
    def successful_writes(self) -> int:
        """Mutating tool calls (write/archive/sanitize) that succeeded this plan."""
        return self._successful_writes
