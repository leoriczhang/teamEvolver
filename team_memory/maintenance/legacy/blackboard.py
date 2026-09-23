"""Cross-job shared blackboard for a single maintenance round.

Each maintenance round runs several jobs (team_overview -> dedup -> cleanup ->
onboarding -> consolidate), each on a fresh ReAct engine. Without shared state
they re-discover the same members/projects and may re-touch documents an
earlier job already archived or merged.

The blackboard is an in-process scratchpad shared by every job in one round:

- **facts**: durable observations (members, projects, authoritative doc URIs)
  a later job can consult instead of re-searching.
- **processed**: URIs already archived/merged this round, so later jobs skip
  them. Mutating tools record here automatically; the LLM can also read it.

It is intentionally ephemeral — not persisted — so it never becomes another
memory that needs maintaining.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class Blackboard:
    """Shared, round-scoped scratchpad. One instance per round, all jobs share it."""

    facts: Dict[str, List[str]] = field(default_factory=dict)
    processed: Dict[str, str] = field(default_factory=dict)  # uri -> action:reason

    def add_fact(self, topic: str, value: str) -> None:
        topic = (topic or "").strip() or "general"
        value = (value or "").strip()
        if not value:
            return
        bucket = self.facts.setdefault(topic, [])
        if value not in bucket:
            bucket.append(value)

    def mark_processed(self, uri: str, action: str, reason: str = "") -> None:
        uri = (uri or "").strip()
        if uri:
            self.processed[uri] = f"{action}: {reason}".strip(": ").strip()

    def is_processed(self, uri: str) -> bool:
        return (uri or "").strip() in self.processed

    def snapshot(self) -> Dict[str, Any]:
        return {"facts": self.facts, "processed": self.processed}
