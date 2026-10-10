"""Team Memory: aggregate with compile, copy a snapshot, maintain with compile.

One account-shared Resource directory is authoritative. Private snapshots and
per-run Skill copies are inputs, never a second published Memory directory.
The historical Job/ReAct maintenance engine is not part of this package.
"""

from __future__ import annotations

from team_memory.service import MemoryAggregationService
from team_memory.aggregation.state import AggregationState

__all__ = ["MemoryAggregationService", "AggregationState"]
