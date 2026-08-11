"""Internal teamEvolver API for mining Benchmark datasets from trajectories.

This is the application-facing boundary.  teamEvolver components should call
these Python functions directly instead of making an HTTP request back into
their own process.  The HTTP routes in the embedded SkillMiner console are a
thin adapter for UI clients only.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Callable


_SKILLMINER_ROOT = Path(__file__).resolve().parent / "skillminer"
# SkillMiner started life as a collection of executable scripts whose sibling
# imports are intentionally still top-level.  Make that implementation
# importable behind this stable package API without requiring callers to know
# about or mutate sys.path themselves.
if str(_SKILLMINER_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILLMINER_ROOT))

from .skillminer import trajectory_benchmark as _implementation  # noqa: E402


DATASET_FORMAT = _implementation.DATASET_FORMAT
TrajectoryBenchmarkError = _implementation.TrajectoryBenchmarkError
TrajectoryBenchmarkStopped = _implementation.TrajectoryBenchmarkStopped


def mine_benchmark_from_trajectories(
    payload: dict[str, Any],
    *,
    project_root: Path | str | None = None,
    stop_requested: Callable[[], bool] | None = None,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Validate trajectories, mine Benchmark only, and persist its artifacts.

    ``project_root`` is the directory below which ``trajectory_benchmarks/``
    is created.  It defaults to SkillMiner's own data root.  This function is
    synchronous because model generation is blocking; async teamEvolver
    components should use :func:`amine_benchmark_from_trajectories`.
    """
    request = _implementation.normalize_request(payload)
    return _implementation.mine_trajectory_benchmark(
        request,
        project_root=project_root or _SKILLMINER_ROOT,
        stop_requested=stop_requested,
        log=log,
    )


async def amine_benchmark_from_trajectories(
    payload: dict[str, Any],
    *,
    project_root: Path | str | None = None,
    stop_requested: Callable[[], bool] | None = None,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Async, event-loop-safe variant for teamEvolver services and workers."""
    return await asyncio.to_thread(
        mine_benchmark_from_trajectories,
        payload,
        project_root=project_root,
        stop_requested=stop_requested,
        log=log,
    )


def list_trajectory_benchmark_runs(
    *, project_root: Path | str | None = None,
) -> list[dict[str, Any]]:
    """List persisted trajectory-Benchmark runs without using HTTP."""
    return _implementation.list_runs(project_root=project_root or _SKILLMINER_ROOT)


def get_trajectory_benchmark_run(
    run_id: str,
    *,
    project_root: Path | str | None = None,
) -> dict[str, Any] | None:
    """Load one persisted run and its normalized questions."""
    return _implementation.get_run(run_id, project_root=project_root or _SKILLMINER_ROOT)


__all__ = [
    "DATASET_FORMAT",
    "TrajectoryBenchmarkError",
    "TrajectoryBenchmarkStopped",
    "mine_benchmark_from_trajectories",
    "amine_benchmark_from_trajectories",
    "list_trajectory_benchmark_runs",
    "get_trajectory_benchmark_run",
]
