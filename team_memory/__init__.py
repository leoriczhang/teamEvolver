"""Team Memory: aggregate with compile, copy a snapshot, maintain with compile.

One account-shared Resource directory is authoritative. Private snapshots and
per-run Skill copies are inputs, never a second published Memory directory.
Legacy ReAct code is retained only for old imports and historical Replay.
"""

from __future__ import annotations

from team_memory.service import MemoryAggregationService
from team_memory.aggregation.state import AggregationState

__all__ = ["MemoryAggregationService", "AggregationState"]
