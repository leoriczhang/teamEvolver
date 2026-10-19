"""composition and source boundaries."""

import ast
from types import SimpleNamespace

from teamEvolver.app import build_role_services
from teamEvolver.config import TeamEvolverConfig
from teamEvolver.runtime.tasks import TASK_KINDS
from tests.support.paths import REPO_ROOT

ROOT = REPO_ROOT


def test_role_composition_registers_complete_worker_and_empty_api():
    tx = SimpleNamespace(pool=SimpleNamespace(schema="fixture"))
    worker = build_role_services(role="worker", tx=tx, config=TeamEvolverConfig())
    api = build_role_services(role="api", tx=tx, config=TeamEvolverConfig())
    assert worker.registry.kinds() == frozenset(TASK_KINDS)
    assert api.registry.kinds() == frozenset()


def test_worker_runs_every_dispatcher_and_resolves_secrets(monkeypatch):
    """Every tenant dispatcher runs on the Worker; one failure skips none."""
    import asyncio

    import session_ingestion.schedule as schedule
    import team_memory.schedule as memory_schedule
    import team_skills.evolution.schedule as evolution_schedule
    import team_skills.experiences.supervisor as experiences

    calls = []

    async def failing(services, tenant):
        calls.append(("datasource", tenant))
        raise RuntimeError("upstream down")

    def record(name):
        async def dispatch(services, *, tenant_id, owner_id=""):
            calls.append((name, tenant_id))

        return dispatch

    monkeypatch.setattr(schedule, "enqueue_due", failing)
    monkeypatch.setattr(evolution_schedule, "evolution_tick", record("evolution"))
    monkeypatch.setattr(memory_schedule, "memory_tick", record("memory"))
    monkeypatch.setattr(experiences, "experience_tick", record("experience"))
    monkeypatch.setenv("TEAMEVOLVER_SECRET_MASTER_KEY", "test-master-key")
    tx = SimpleNamespace(pool=SimpleNamespace(schema="fixture"))
    worker = build_role_services(role="worker", tx=tx, config=TeamEvolverConfig())
    asyncio.run(worker.extras["scheduled_tasks"]("tenant-a"))
    assert calls == [
        ("datasource", "tenant-a"),
        ("evolution", "tenant-a"),
        ("memory", "tenant-a"),
        ("experience", "tenant-a"),
    ]
    assert callable(worker.extras["experience_pattern_compiler"])
    assert worker.extras["secret_repo"] is not None
    monkeypatch.delenv("TEAMEVOLVER_SECRET_MASTER_KEY")
    monkeypatch.delenv("TEAMEVOLVER_ROOT_API_KEY", raising=False)
    assert "secret_repo" not in build_role_services(role="api", tx=tx, config=TeamEvolverConfig()).extras


def test_worker_applies_console_tracing_config_to_langfuse_runtime(monkeypatch):
    """Console-saved tracing settings reach the Worker's Langfuse runtime."""
    import asyncio
    from contextlib import asynccontextmanager

    import teamEvolver.observability as observability
    from teamEvolver.app import _tracing_refresh
    from teamEvolver.observability.langfuse import LangfuseTracingSettings

    for name in ("LANGFUSE_TRACING_ENABLED", "LANGFUSE_HOST", "LANGFUSE_BASE_URL", "LANGFUSE_SAMPLE_RATE"):
        monkeypatch.delenv(name, raising=False)
    applied = []
    monkeypatch.setattr(observability, "configure_langfuse", lambda cfg: applied.append(cfg))

    @asynccontextmanager
    async def conn():
        yield None

    class Repo:
        async def read_global(self, _conn):
            return SimpleNamespace(
                config={"tracing": {"enabled": True, "host": "https://lf.example", "sample_rate": 0.25}}
            )

    config = TeamEvolverConfig(langfuse_tracing_public_key="pk", langfuse_tracing_secret_key="sk")
    services = SimpleNamespace(
        extras={"config": config, "config_repo": Repo()},
        tx=SimpleNamespace(pool=SimpleNamespace(admin_conn=conn)),
    )
    refresh = _tracing_refresh(services)
    asyncio.run(refresh())
    asyncio.run(refresh())
    assert len(applied) == 1
    settings = LangfuseTracingSettings.from_config(applied[0])
    assert (settings.enabled, settings.host, settings.sample_rate) == (True, "https://lf.example", 0.25)
    assert (settings.public_key, settings.secret_key) == ("pk", "sk")


def test_new_miner_paths_have_no_other_business_implementation_imports():
    paths = [
        ROOT / "team_miner" / name
        for name in ("config.py", "schemas.py", "openviking.py", "state.py", "handlers.py", "links.py")
    ]
    paths.extend((ROOT / "team_miner/api").rglob("*.py"))
    for path in paths:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            imports = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else ([node.module] if isinstance(node, ast.ImportFrom) and node.level == 0 else [])
            )
            assert not any(
                (name or "").split(".")[0] in {"team_memory", "team_skills"} for name in imports
            ), f"{path}:{node.lineno}"


def test_production_worker_rejects_missing_domain_handlers(monkeypatch):
    import pytest

    import team_miner.handlers as miner

    monkeypatch.setattr(miner, "register_handlers", lambda: {})
    tx = SimpleNamespace(pool=SimpleNamespace(schema="fixture"))
    with pytest.raises(ValueError, match="missing handler"):
        build_role_services(role="worker", tx=tx, config=TeamEvolverConfig())


def test_duplicate_routes_report_the_owners():
    import pytest
    from fastapi import APIRouter

    from teamEvolver.app import _reject_duplicate_routes

    first, second = APIRouter(), APIRouter()
    first.add_api_route("/collision", lambda: {}, methods=["GET"])
    second.add_api_route("/collision", lambda: {}, methods=["GET"])
    owners = {}
    _reject_duplicate_routes(first, "first.api", owners)
    with pytest.raises(RuntimeError, match="first.api and second.api"):
        _reject_duplicate_routes(second, "second.api", owners)
