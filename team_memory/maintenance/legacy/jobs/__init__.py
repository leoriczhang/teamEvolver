"""Maintenance job definitions."""

from team_memory.maintenance.legacy.jobs.base import Job, JobResult, JobStatus
from team_memory.maintenance.legacy.jobs.team_overview import TeamOverviewJob
from team_memory.maintenance.legacy.jobs.dedup import DeduplicationJob
from team_memory.maintenance.legacy.jobs.cleanup import CleanupJob
from team_memory.maintenance.legacy.jobs.onboarding import OnboardingCheckJob
from team_memory.maintenance.legacy.jobs.consolidate import ConsolidateJob

ALL_JOBS = [
    TeamOverviewJob,
    DeduplicationJob,
    CleanupJob,
    OnboardingCheckJob,
    ConsolidateJob,
]

__all__ = [
    "Job", "JobResult", "JobStatus", "ALL_JOBS",
    "TeamOverviewJob", "DeduplicationJob", "CleanupJob",
    "OnboardingCheckJob", "ConsolidateJob",
]
