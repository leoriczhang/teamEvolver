"""Maintenance job definitions."""

from .base import Job, JobResult, JobStatus
from .team_overview import TeamOverviewJob
from .dedup import DeduplicationJob
from .cleanup import CleanupJob
from .onboarding import OnboardingCheckJob
from .consolidate import ConsolidateJob

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
