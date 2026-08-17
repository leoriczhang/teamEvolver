"""Task planner — decomposes high-level maintenance goals into actionable steps."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class TaskPriority(str, Enum):
    CRITICAL = "critical"    # Must complete (e.g., fix broken shared memory)
    HIGH = "high"            # Should complete (e.g., dedup, cleanup)
    MEDIUM = "medium"        # Nice to have (e.g., onboarding check)
    LOW = "low"              # Opportunistic (e.g., consolidation)


class TaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class Task:
    """A single actionable task within a job."""
    id: str
    description: str
    priority: TaskPriority = TaskPriority.MEDIUM
    status: TaskStatus = TaskStatus.PENDING
    result: Optional[str] = None
    error: Optional[str] = None
    attempts: int = 0
    max_attempts: int = 2


@dataclass
class TaskPlan:
    """A plan consisting of ordered tasks for a maintenance job."""
    job_name: str
    tasks: List[Task] = field(default_factory=list)
    
    @property
    def pending_tasks(self) -> List[Task]:
        return [t for t in self.tasks if t.status == TaskStatus.PENDING]

    @property
    def completed_tasks(self) -> List[Task]:
        return [t for t in self.tasks if t.status == TaskStatus.COMPLETED]

    @property
    def failed_tasks(self) -> List[Task]:
        return [t for t in self.tasks if t.status == TaskStatus.FAILED]

    @property
    def progress(self) -> float:
        if not self.tasks:
            return 1.0
        done = len([t for t in self.tasks if t.status in (TaskStatus.COMPLETED, TaskStatus.SKIPPED)])
        return done / len(self.tasks)

    @property
    def is_complete(self) -> bool:
        return all(t.status in (TaskStatus.COMPLETED, TaskStatus.SKIPPED, TaskStatus.FAILED) for t in self.tasks)

    def next_task(self) -> Optional[Task]:
        """Get the next pending task (highest priority first)."""
        pending = self.pending_tasks
        if not pending:
            return None
        priority_order = [TaskPriority.CRITICAL, TaskPriority.HIGH, TaskPriority.MEDIUM, TaskPriority.LOW]
        for p in priority_order:
            for t in pending:
                if t.priority == p:
                    return t
        return pending[0]


class TaskPlanner:
    """Generates task plans for maintenance jobs.
    
    Each job type has a predefined set of tasks. The planner can also
    dynamically add tasks based on observations during execution.
    """

    @staticmethod
    def plan_team_overview() -> TaskPlan:
        return TaskPlan(
            job_name="team_overview",
            tasks=[
                Task(id="to-1", description="List all team members from OpenViking user directory", priority=TaskPriority.HIGH),
                Task(id="to-2", description="Find the single authoritative team overview/onboarding document", priority=TaskPriority.HIGH),
                Task(id="to-3", description="Check whether member/project facts changed since the authoritative document", priority=TaskPriority.MEDIUM),
                Task(id="to-4", description="Update the authoritative document in place; do not create a parallel version", priority=TaskPriority.HIGH),
                Task(id="to-5", description="Archive redundant team overview/project summary variants after merging", priority=TaskPriority.MEDIUM),
                Task(id="to-6", description="Record members, projects, and the authoritative doc URI to shared_notes for later jobs", priority=TaskPriority.MEDIUM),
            ],
        )

    @staticmethod
    def plan_deduplication() -> TaskPlan:
        return TaskPlan(
            job_name="deduplication",
            tasks=[
                Task(id="dd-0", description="Recall shared_notes to skip URIs already processed this round", priority=TaskPriority.MEDIUM),
                Task(id="dd-1", description="Browse your own user memory and group files by topic", priority=TaskPriority.HIGH),
                Task(id="dd-2", description="Find redundant variants: guides, overviews, search-friendly entries, reports, diagnostics", priority=TaskPriority.HIGH),
                Task(id="dd-3", description="Read all candidate duplicates in one viking_read_many call and choose the authoritative survivor", priority=TaskPriority.HIGH),
                Task(id="dd-4", description="Consolidate with a single viking_merge (write survivor + archive sources) instead of manual remember+forget", priority=TaskPriority.HIGH),
            ],
        )

    @staticmethod
    def plan_cleanup() -> TaskPlan:
        return TaskPlan(
            job_name="cleanup",
            tasks=[
                Task(id="cl-1", description="Search for stale, temporary, or maintenance-generated memories", priority=TaskPriority.HIGH),
                Task(id="cl-2", description="Check whether each candidate is covered by an authoritative document", priority=TaskPriority.MEDIUM),
                Task(id="cl-3", description="Archive completed diagnostics/reports and superseded status updates", priority=TaskPriority.MEDIUM),
                Task(id="cl-4", description="Remove maintenance-project-as-team wording from surviving user-facing docs", priority=TaskPriority.HIGH),
            ],
        )

    @staticmethod
    def plan_onboarding() -> TaskPlan:
        return TaskPlan(
            job_name="onboarding_check",
            tasks=[
                Task(id="ob-1", description="Search and browse for the authoritative onboarding/team overview entry", priority=TaskPriority.HIGH),
                Task(id="ob-2", description="Verify the entry covers team purpose and member responsibilities", priority=TaskPriority.HIGH),
                Task(id="ob-3", description="Verify the entry covers current projects and useful tools", priority=TaskPriority.HIGH),
                Task(id="ob-4", description="Report missing coverage instead of creating duplicate search-friendly docs", priority=TaskPriority.MEDIUM),
                Task(id="ob-5", description="Save a local report for search/index issues", priority=TaskPriority.MEDIUM),
            ],
        )

    @staticmethod
    def plan_consolidation() -> TaskPlan:
        return TaskPlan(
            job_name="consolidate",
            tasks=[
                Task(id="cs-0", description="Recall shared_notes for members/projects already established this round", priority=TaskPriority.MEDIUM),
                Task(id="cs-1", description="List peers and scan each peer's personal memory for candidate patterns/SOPs", priority=TaskPriority.MEDIUM),
                Task(id="cs-2", description="Read candidate patterns across peers with viking_read_many; keep only ones recurring across 2+ different peers", priority=TaskPriority.HIGH),
                Task(id="cs-3", description="Check team memory for an existing doc on the same topic to update instead of duplicating", priority=TaskPriority.HIGH),
                Task(id="cs-4", description="Distill at most one de-identified common pattern into team memory, or skip if none truly recur", priority=TaskPriority.LOW),
            ],
        )
