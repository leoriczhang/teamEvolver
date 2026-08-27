"""Cross-user memory aggregation into shared team knowledge.

This package replaces the DreamCycle P50 ``consolidate`` job (and, per the
current product decision, DreamCycle as a whole) with a two-phase pipeline:
enumerate every user under an OpenViking account, copy each user's selected
Memory into an immutable private snapshot, then use ``ov compile`` to merge
those snapshots into an account-shared tree under
``viking://resources/<shared_knowledge_prefix>/``.

Design notes and rationale live in
``docs/design/cross-user-memory-aggregation.md`` (OpenViking repo carries the
capability research). Key constraints honored here:

- Cross-user reads use a request-scoped OpenViking Root/Admin credential; the
  credential is not persisted in aggregation state.
- ``ov compile`` currently caps sources at 16 and outputs at 128, so the merge
  uses bounded tree-reduce instead of one giant task.
- The compile memory target cannot be a bare ``.../memories`` root, which is
  one more reason the output lands in the ``resources`` namespace.
"""

from __future__ import annotations

from .service import MemoryAggregationService
from .state import AggregationState

__all__ = ["MemoryAggregationService", "AggregationState"]
