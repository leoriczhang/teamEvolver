"""Optional, request-content-free accounting for TE-owned provider attempts.

The context variable carries a shared, locked collector through async child tasks,
``to_thread`` and the LLM dispatcher's copied context. Nested scopes attribute an
attempt to the innermost collector only; callers must not sum overlapping scopes.
Tokens are observed provider totals, never estimates of missing usage. Concurrent
requests already in flight can exceed the budget before another attempt is denied.
"""

from __future__ import annotations

import contextvars
import math
import threading
import uuid
from contextlib import contextmanager
from typing import Any, Iterator, Mapping

_MAX_ATTEMPT_RECORDS = 1024
_TOKEN_FIELDS = ("input_tokens", "output_tokens", "total_tokens")
_CURRENT_COLLECTOR: contextvars.ContextVar[LLMUsageCollector | None] = contextvars.ContextVar(
    "te_llm_usage_collector", default=None
)


class LLMUsageBudgetExceeded(RuntimeError):
    """The observed token budget disallows another provider attempt."""


def _token_usage(value: Mapping[str, Any]) -> dict[str, int]:
    result: dict[str, int] = {}
    for field in _TOKEN_FIELDS:
        amount = value.get(field)
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            continue
        if isinstance(amount, float) and (not math.isfinite(amount) or not amount.is_integer()):
            continue
        if amount >= 0:
            result[field] = int(amount)
    return result


class _ProviderAttempt:
    def __init__(self, collector: LLMUsageCollector, record: dict[str, Any]) -> None:
        self._collector = collector
        self._record = record
        self._usage: dict[str, int] = {}

    def record_usage(self, usage: Mapping[str, Any]) -> None:
        """Accept only token counters, retaining no arbitrary provider payload."""
        sanitized = _token_usage(usage)
        if sanitized:
            with self._collector._lock:
                self._usage.update(sanitized)
                self._record["usage"] = dict(self._usage)


class _PendingDispatch:
    def __init__(self, collector: LLMUsageCollector) -> None:
        self._collector = collector
        self._finished = False
        with collector._lock:
            collector._pending_dispatches += 1

    def finish(self) -> None:
        with self._collector._lock:
            if not self._finished:
                self._finished = True
                self._collector._pending_dispatches -= 1


class LLMUsageCollector:
    """A bounded ledger whose totals remain accurate when entries are omitted."""

    def __init__(self, label: str, budget_tokens: int | None = None) -> None:
        if budget_tokens is not None and (
            isinstance(budget_tokens, bool) or not isinstance(budget_tokens, int) or budget_tokens < 0
        ):
            raise ValueError("budget_tokens must be a nonnegative integer or None")
        self.label = str(label)[:200]
        self.budget_tokens = budget_tokens
        self._lock = threading.Lock()
        self._records: list[dict[str, Any]] = []
        self._attempt_count = 0
        self._in_flight = 0
        self._pending_dispatches = 0
        self._known_tokens = 0
        self._unknown_completed = 0

    def _start(self, model: str) -> _ProviderAttempt:
        with self._lock:
            if self.budget_tokens is not None and self._known_tokens >= self.budget_tokens:
                raise LLMUsageBudgetExceeded("TE-owned LLM token budget reached")
            self._attempt_count += 1
            self._in_flight += 1
            record = {
                "attempt_id": uuid.uuid4().hex,
                "model": str(model)[:200],
                "status": "running",
                "usage": {},
                "usage_known": False,
            }
            if len(self._records) < _MAX_ATTEMPT_RECORDS:
                self._records.append(record)
            return _ProviderAttempt(self, record)

    def _finish(self, attempt: _ProviderAttempt, *, succeeded: bool) -> None:
        with self._lock:
            usage = attempt._usage
            usage_known = succeeded and "total_tokens" in usage
            # A failed stream may still expose a known lower bound. Its remaining
            # cost is unknown, so the attempt also contributes to unknown_attempts.
            self._known_tokens += usage.get("total_tokens", 0)
            self._unknown_completed += int(not usage_known)
            self._in_flight -= 1
            attempt._record.update(
                status="succeeded" if succeeded else "failed",
                usage_known=usage_known,
            )

    def snapshot(self) -> dict[str, Any]:
        """Return a stable JSON value without messages, credentials or errors.

        ``complete`` means every started attempt has finished with a provider
        total and no dispatcher call remains queued or active. Keep the collector
        when cancelling: a shielded call can finish later and another snapshot
        will include its usage.
        """
        with self._lock:
            unknown = self._unknown_completed + self._in_flight
            omitted = self._attempt_count - len(self._records)
            return {
                "schema_version": 1,
                "channel": "te_owned_llm",
                "label": self.label,
                "budget_tokens": self.budget_tokens,
                "known_total_tokens": self._known_tokens,
                "unknown_attempts": unknown,
                "attempt_count": self._attempt_count,
                "in_flight_attempts": self._in_flight,
                "pending_dispatches": self._pending_dispatches,
                "complete": unknown == 0 and self._pending_dispatches == 0,
                "budget_reached": self.budget_tokens is not None and self._known_tokens >= self.budget_tokens,
                "truncated": omitted > 0,
                "omitted_attempts": omitted,
                "attempts": [
                    {**record, "usage": dict(record["usage"])} for record in self._records
                ],
            }


@contextmanager
def collect_llm_usage(label: str, budget_tokens: int | None = None) -> Iterator[LLMUsageCollector]:
    """Collect actual AsyncLLMClient attempts in this task and its copied contexts."""
    collector = LLMUsageCollector(label, budget_tokens)
    token = _CURRENT_COLLECTOR.set(collector)
    try:
        yield collector
    finally:
        _CURRENT_COLLECTOR.reset(token)


def begin_llm_dispatch() -> _PendingDispatch | None:
    """Track a queued call until its real future ends, even after cancellation."""
    collector = _CURRENT_COLLECTOR.get()
    return _PendingDispatch(collector) if collector is not None else None


@contextmanager
def provider_attempt(model: str) -> Iterator[_ProviderAttempt | None]:
    """Wrap the actual synchronous provider call, including stream consumption."""
    collector = _CURRENT_COLLECTOR.get()
    if collector is None:
        yield None
        return
    attempt = collector._start(model)
    succeeded = False
    try:
        yield attempt
        succeeded = True
    finally:
        collector._finish(attempt, succeeded=succeeded)
