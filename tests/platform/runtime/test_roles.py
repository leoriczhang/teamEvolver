"""test roles."""

from __future__ import annotations

import pytest

from teamEvolver.runtime import (
    DEFAULT_MAX_ATTEMPTS,
    RETRY_BACKOFF_SECONDS,
    backoff_for_attempt,
    build_services,
)
from tests.support.runtime import _FakeTx


def test_api_role_refuses_task_handlers(tx: _FakeTx):
    with pytest.raises(ValueError, match="must not register task handlers"):
        build_services(role="api", tx=tx, handlers={"session.pull": lambda *a: None})


def test_api_role_registry_is_empty(tx: _FakeTx):
    services = build_services(role="api", tx=tx)
    assert services.runs_api is True and services.runs_worker is False
    assert services.registry.kinds() == frozenset()


def test_worker_role_runs_worker_not_api(tx: _FakeTx):
    services = build_services(role="worker", tx=tx, handlers={})
    assert services.runs_worker is True and services.runs_api is False


def test_unknown_task_kind_is_rejected_at_registration(tx: _FakeTx):
    with pytest.raises(ValueError, match="unknown task kind"):
        build_services(role="worker", tx=tx, handlers={"not.a.kind": lambda *a: None})


def test_retry_backoff_schedule(tx: _FakeTx):
    assert backoff_for_attempt(1) == RETRY_BACKOFF_SECONDS[0]
    assert backoff_for_attempt(2) == RETRY_BACKOFF_SECONDS[1]
    assert backoff_for_attempt(3) == RETRY_BACKOFF_SECONDS[2]
    assert backoff_for_attempt(99) == RETRY_BACKOFF_SECONDS[-1]  # clamps
    assert backoff_for_attempt(1, jitter=0.5) == RETRY_BACKOFF_SECONDS[0] + 0.5


def test_default_max_attempts_is_three():
    assert DEFAULT_MAX_ATTEMPTS == 3
