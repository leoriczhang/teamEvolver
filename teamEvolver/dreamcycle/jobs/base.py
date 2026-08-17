"""Base job interface and common job infrastructure."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from ..react.engine import ReActEngine
from ..react.planner import TaskPlan

logger = logging.getLogger(__name__)


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class JobResult:
    """Result of a job execution."""
    job_name: str
    status: JobStatus
    started_at: datetime
    finished_at: Optional[datetime] = None
    duration_seconds: float = 0.0
    tasks_completed: int = 0
    tasks_failed: int = 0
    tasks_skipped: int = 0
    turns_used: int = 0
    actions_taken: int = 0
    writes_applied: int = 0
    memory_changes: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_name": self.job_name,
            "status": self.status.value,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_seconds": self.duration_seconds,
            "tasks_completed": self.tasks_completed,
            "tasks_failed": self.tasks_failed,
            "tasks_skipped": self.tasks_skipped,
            "turns_used": self.turns_used,
            "actions_taken": self.actions_taken,
            "writes_applied": self.writes_applied,
            "memory_changes": self.memory_changes,
            "errors": self.errors,
            "summary": self.summary,
        }


class Job(ABC):
    """Abstract base class for maintenance jobs.
    
    Each job:
    1. Creates a TaskPlan (via the planner)
    2. Hands it to the ReAct engine for execution
    3. Reports results
    """

    def __init__(self, team_name: str = ""):
        self.team_name = str(team_name or "").strip()

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique job identifier."""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable description of what this job does."""
        ...

    @property
    def priority(self) -> int:
        """Execution priority (lower = runs first). Default: 50."""
        return 50

    @abstractmethod
    def create_plan(self) -> TaskPlan:
        """Create the task plan for this job."""
        ...

    @abstractmethod
    def get_system_prompt(self) -> str:
        """Get the system prompt specific to this job."""
        ...

    def execute(self, engine: ReActEngine) -> JobResult:
        """Execute this job using the ReAct engine."""
        started_at = datetime.now(timezone.utc)
        logger.info("[Job:%s] Starting: %s", self.name, self.description)

        try:
            plan = self.create_plan()
            completed_plan = engine.execute_plan(plan)

            finished_at = datetime.now(timezone.utc)
            duration = (finished_at - started_at).total_seconds()

            result = JobResult(
                job_name=self.name,
                status=JobStatus.COMPLETED if not completed_plan.failed_tasks else JobStatus.FAILED,
                started_at=started_at,
                finished_at=finished_at,
                duration_seconds=duration,
                tasks_completed=len(completed_plan.completed_tasks),
                tasks_failed=len(completed_plan.failed_tasks),
                tasks_skipped=len([t for t in completed_plan.tasks if t.status.value == "skipped"]),
                turns_used=engine.turn_count,
                actions_taken=engine.working_memory.action_count,
                writes_applied=engine.successful_writes,
                errors=[t.error for t in completed_plan.failed_tasks if t.error],
                summary=self._generate_summary(completed_plan),
            )

            logger.info(
                "[Job:%s] Finished in %.1fs — %d/%d tasks completed, %d writes applied, %d turns",
                self.name, duration, result.tasks_completed,
                len(completed_plan.tasks), result.writes_applied, result.turns_used,
            )
            return result

        except Exception as e:
            finished_at = datetime.now(timezone.utc)
            logger.error("[Job:%s] Failed with exception: %s", self.name, e, exc_info=True)
            return JobResult(
                job_name=self.name,
                status=JobStatus.FAILED,
                started_at=started_at,
                finished_at=finished_at,
                duration_seconds=(finished_at - started_at).total_seconds(),
                errors=[str(e)],
                summary=f"Job failed with exception: {e}",
            )

    def _generate_summary(self, plan: TaskPlan) -> str:
        """Generate a human-readable summary of the plan execution."""
        lines = [f"Job: {self.name}"]
        for task in plan.tasks:
            icon = {"completed": "✅", "failed": "❌", "skipped": "⏭️"}.get(task.status.value, "⬜")
            lines.append(f"  {icon} {task.description[:60]}")
            if task.result:
                lines.append(f"      → {task.result[:100]}")
        return "\n".join(lines)
