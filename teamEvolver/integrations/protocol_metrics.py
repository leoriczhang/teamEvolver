"""Small process-local counters, exported for release-window monitoring."""

from __future__ import annotations

import json
import logging
import threading
from collections import Counter

_lock = threading.Lock()
_counts: Counter = Counter()
_logger = logging.getLogger(__name__)
_labels = {
    "agent_identity_requests_total": {"mode"},
    "agent_identity_rejected_total": {"reason"},
    "skill_pull_requests_total": {"tenant_id", "status"},
    "replay_adapter_resolve_total": {"tenant_id", "result"},
}


def increment(name: str, **labels: str) -> None:
    if name not in _labels or set(labels) != _labels[name]:
        raise ValueError("unknown protocol metric")
    key = (name, tuple(sorted((label, str(value)) for label, value in labels.items())))
    with _lock:
        _counts[key] += 1
    _logger.info("agent_protocol_metric %s", json.dumps({"name": name, **labels}, ensure_ascii=False))


def render() -> str:
    with _lock:
        counts = sorted(_counts.items())
    lines = [f"# TYPE {name} counter" for name in sorted(_labels)]
    for (name, labels), count in counts:
        formatted = ",".join(f"{key}={json.dumps(value)}" for key, value in labels)
        lines.append(f"{name}{{{formatted}}} {count}")
    return "\n".join(lines) + "\n"
