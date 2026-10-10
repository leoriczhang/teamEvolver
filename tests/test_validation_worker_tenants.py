from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace

import httpx
import pytest

from team_skills.candidates.store import ValidationStore
from team_skills.candidates.worker import ValidationWorker
from team_skills.library.bundle import bundle_tree_sha256
from teamEvolver.config import TeamEvolverConfig
from teamEvolver.launcher import Launcher
from teamEvolver.replay_adapter import TeamEvolverReplayHost
from teamEvolver.tenants.registry import (
    TenantContext,
    current_tenant_id,
    reset_current_tenant,
    set_current_tenant,
)


def _config(tmp_path) -> TeamEvolverConfig:
    return TeamEvolverConfig(
        sharing_session_backend="local",
        sharing_local_root=str(tmp_path / "sessions"),
        sharing_skill_backend="local",
        sharing_skill_local_root=str(tmp_path / "skills"),
        sharing_skill_mirror_enabled=False,
        validation_enabled=True,
    )


@pytest.mark.asyncio
async def test_launcher_runs_one_validation_worker_per_enabled_tenant(
    monkeypatch,
    tmp_path,
) -> None:
    config = _config(tmp_path)
    tenants = [
        TenantContext(tenant_id="tenant-a"),
        TenantContext(tenant_id="tenant-b"),
        TenantContext(
            tenant_id="tenant-disabled",
            config_overrides={"validation_enabled": False},
        ),
        TenantContext(tenant_id="tenant-inactive", status="disabled"),
    ]

    class FakeRegistry:
        def __init__(self, _config):
            pass

        def list_tenants(self):
            return tenants

    created = []

    class FakeWorker:
        def __init__(
            self,
            worker_config,
            *,
            idle_provider,
            tenant_id,
            tenant_context,
        ):
            self.config = worker_config
            self.tenant_id = tenant_id
            self.tenant_context = tenant_context
            self.idle_provider = idle_provider
            self.init_context = current_tenant_id()
            self.run_context = ""
            self.stopped = False
            self._stopped = asyncio.Event()
            created.append(self)

        async def run(self):
            self.run_context = current_tenant_id()
            await self._stopped.wait()

        def stop(self):
            self.stopped = True
            self._stopped.set()

    monkeypatch.setattr(
        "teamEvolver.tenants.registry.TenantRegistry",
        FakeRegistry,
    )
    monkeypatch.setattr(
        "team_skills.candidates.ValidationWorker",
        FakeWorker,
    )

    launcher = Launcher(SimpleNamespace())
    supervisor = asyncio.create_task(
        launcher._run_validation_workers(config, object())
    )
    for _ in range(100):
        if len(created) == 2 and all(
            worker.run_context for worker in created
        ):
            break
        await asyncio.sleep(0)

    assert {worker.tenant_id for worker in created} == {
        "tenant-a",
        "tenant-b",
    }
    assert all(worker.init_context == worker.tenant_id for worker in created)
    assert all(worker.run_context == worker.tenant_id for worker in created)

    supervisor.cancel()
    await asyncio.gather(supervisor, return_exceptions=True)

    assert all(worker.stopped for worker in created)
    assert launcher._validation_workers == {}
    assert launcher._validation_tasks == {}


@pytest.mark.asyncio
async def test_validation_worker_binds_context_and_isolates_store(
    tmp_path,
    monkeypatch,
) -> None:
    config = _config(tmp_path)
    worker = ValidationWorker(
        config,
        tenant_id="tenant-a",
        tenant_context=TenantContext(tenant_id="tenant-a"),
    )
    worker._store.save_job({"job_id": "tenant-a-job"})

    assert ValidationStore.from_config(
        config,
        tenant_id="tenant-a",
    ).load_job("tenant-a-job")
    assert (
        ValidationStore.from_config(
            config,
            tenant_id="tenant-b",
        ).load_job("tenant-a-job")
        is None
    )

    async def current_context(*, force=False):
        del force
        return {"tenant_id": current_tenant_id()}

    monkeypatch.setattr(worker, "_run_once", current_context)
    assert await worker.run_once() == {"tenant_id": "tenant-a"}
    assert current_tenant_id() == "default"


def test_replay_host_reads_current_tenant_storage(tmp_path) -> None:
    config = _config(tmp_path)
    ValidationStore.from_config(
        config,
        tenant_id="tenant-a",
    ).save_job({"job_id": "tenant-a-job"})

    host = TeamEvolverReplayHost()
    host._config = lambda: config
    token = set_current_tenant(TenantContext(tenant_id="tenant-a"))
    try:
        assert host.load_candidate_job("tenant-a-job") is not None
    finally:
        reset_current_tenant(token)

    token = set_current_tenant(TenantContext(tenant_id="tenant-b"))
    try:
        assert host.load_candidate_job("tenant-a-job") is None
    finally:
        reset_current_tenant(token)


@pytest.mark.asyncio
async def test_validation_trigger_and_publish_confirmation_use_tenant(
    tmp_path,
    monkeypatch,
) -> None:
    config = _config(tmp_path)
    worker = ValidationWorker(
        config,
        tenant_id="tenant-a",
        tenant_context=TenantContext(tenant_id="tenant-a"),
    )
    requests = []

    class FakeResponse:
        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, *, headers):
            requests.append((url, headers))
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setenv("TEAMEVOLVER_ROOT_API_KEY", "root-key")

    assert await worker._trigger_evolve_finalize() is True
    assert requests[0][1] == {
        "X-Tenant-Id": "tenant-a",
        "Authorization": "Bearer root-key",
    }

    markdown = b"# Tenant Skill\n"
    expected_sha = hashlib.sha256(markdown).hexdigest()
    expected_tree = bundle_tree_sha256({"SKILL.md": markdown})

    class FakeStore:
        @staticmethod
        def load_decision(_job_id):
            return {"status": "published"}

    class FakeHub:
        @staticmethod
        def list_skills():
            return [
                {
                    "name": "tenant-skill",
                    "version": 1,
                    "sha256": expected_sha,
                    "tree_sha256": expected_tree,
                }
            ]

        @staticmethod
        def read_version_bundle(_name, _version):
            return {"SKILL.md": markdown}

    hub_tenants = []

    def build_hub(_cls, _config, *, tenant_id="default"):
        hub_tenants.append(tenant_id)
        return FakeHub()

    from team_skills.library.hub import SkillHub

    monkeypatch.setattr(
        SkillHub,
        "team_from_config",
        classmethod(build_hub),
    )
    worker._store = FakeStore()

    result = await worker._wait_for_published_commit(
        {
            "job_id": "tenant-job",
            "candidate_skill": {"name": "tenant-skill"},
        },
        timeout_seconds=0.1,
    )

    assert result == {
        "name": "tenant-skill",
        "version": 1,
        "sha256": expected_sha,
        "tree_sha256": expected_tree,
    }
    assert hub_tenants == ["tenant-a"]


@pytest.mark.asyncio
async def test_validation_publish_delivery_uses_tenant(
    tmp_path,
    monkeypatch,
) -> None:
    config = _config(tmp_path)
    worker = ValidationWorker(
        config,
        tenant_id="tenant-a",
        tenant_context=TenantContext(tenant_id="tenant-a"),
    )
    service_tenants = []
    saved_decisions = []

    class FakeService:
        @staticmethod
        def record_committed(**_kwargs):
            return {"event_id": "event-1"}

    class FakeStore:
        @staticmethod
        def load_decision(_job_id):
            return {"status": "published"}

        @staticmethod
        def save_decision(job_id, decision):
            saved_decisions.append((job_id, decision))

    def build_service(_cls, _config, *, tenant_id="default"):
        service_tenants.append(tenant_id)
        return FakeService()

    from team_skills.library.mutations import SkillMutationService

    monkeypatch.setattr(
        SkillMutationService,
        "from_config",
        classmethod(build_service),
    )
    monkeypatch.setattr(
        worker,
        "_source_tenant_ids",
        lambda _job: ["tenant-a"],
    )
    worker._store = FakeStore()

    result = await worker._record_skill_delivery(
        {"job_id": "tenant-job"},
        {"name": "tenant-skill", "version": 1},
    )

    assert result == {"status": "published", "delivery_mode": "pull"}
    assert service_tenants == ["tenant-a"]
    assert saved_decisions[0][0] == "tenant-job"
