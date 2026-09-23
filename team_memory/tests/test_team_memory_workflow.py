from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from team_memory import service as module
from team_memory.aggregation.sources import AccountUserCredential
from team_memory.compile_client import CompileClient
from team_memory.maintenance import workflow
from team_memory.routes import AggregationMixin
from team_memory.service import MemoryAggregationService, RunConflictError
from teamEvolver.config import TeamEvolverConfig


def make_service(tmp_path):
    return MemoryAggregationService(
        TeamEvolverConfig(
            sharing_viking_endpoint="https://ov.example",
            sharing_viking_account="acct",
            sharing_viking_user="team",
            aggregation_state_dir=str(tmp_path),
        )
    )


@pytest.fixture
def engine(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    events = []
    current = {"fingerprint": "initial", "copy_ok": True, "compile_ok": True}

    async def users(_self):
        return [AccountUserCredential("alice"), AccountUserCredential("team")]

    async def freeze(_service, run, client, stage):
        events.append(f"skill:{stage}")
        return f"viking://user/team/skills/{stage}", stage + "-revision"

    async def pipeline(**kwargs):
        events.append("staging")
        return ["viking://user/team/resources/snapshot"]

    async def merge(**kwargs):
        events.append("aggregate")
        return current.get("aggregate_ok", True)

    class Client:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

        async def mkdir(self, uri):
            pass

        async def copy_tree(self, **kwargs):
            events.append("copy")
            assert kwargs["source_uri"].startswith("viking://resources/")
            assert kwargs["target_uri"].startswith("viking://user/team/resources/")
            return {"ok": current["copy_ok"], "stderr": ""}

        async def run_batch(self, **kwargs):
            events.append("maintain")
            assert kwargs["skill_uri"].endswith("/maintenance")
            current["fingerprint"] = "maintained"
            return {"ok": current["compile_ok"], "stderr": ""}

    class Inspector:
        async def inspect(self, *args, **kwargs):
            return SimpleNamespace(files=["a.md"], fingerprint=current["fingerprint"])

    monkeypatch.setattr(module.AccountSourceBuilder, "list_account_user_credentials", users)
    monkeypatch.setattr(module, "CompileClient", Client)
    monkeypatch.setattr(workflow, "freeze_skill", freeze)
    monkeypatch.setattr(workflow, "_inspector", lambda *args: Inspector())
    monkeypatch.setattr(service, "_run_pipeline", pipeline)
    monkeypatch.setattr(service, "_merge_staged_roots", merge)
    return service, events, current


def test_two_compiles_share_one_target_and_private_snapshot(engine):
    service, events, _ = engine
    run = service.new_run("acct")
    service.run(run, api_key="secret")
    assert run.status == "completed"
    assert events == ["skill:aggregation", "skill:maintenance", "staging", "aggregate", "copy", "maintain"]
    assert run.snapshot_uri.startswith(run.work_root + "/maintenance/")
    assert not service._active_targets
    assert "secret" not in json.dumps(service.list_runs())


@pytest.mark.parametrize("failure,expected", [("copy_ok", ["copy"]), ("aggregate_ok", [])])
def test_failure_prevents_later_writes(engine, failure, expected):
    service, events, current = engine
    current[failure] = False
    run = service.new_run("acct")
    service.run(run, api_key="secret")
    assert run.status == "failed"
    assert "maintain" not in events
    assert (["copy"] if "copy" in events else []) == expected


def test_maintenance_only_skips_aggregation_and_reuses_unchanged_result(engine):
    service, events, _ = engine
    run = service.new_run("acct", pipeline="maintain")
    service.run(run, api_key="secret")
    assert run.status == "completed"
    assert "staging" not in events and "aggregate" not in events
    second = service.new_run("acct", pipeline="maintain")
    service.run(second, api_key="secret")
    assert second.status == "completed"
    assert events.count("maintain") == 1
    third = service.new_run("acct", pipeline="maintain")
    service.run(third, api_key="secret", full=True)
    assert events.count("maintain") == 2


def test_failed_maintenance_keeps_snapshot_and_can_retry(engine):
    service, events, current = engine
    current["compile_ok"] = False
    first = service.new_run("acct", pipeline="maintain")
    service.run(first, api_key="secret")
    assert first.status == "failed" and first.snapshot_uri
    current["compile_ok"] = True
    second = service.new_run("acct", pipeline="maintain")
    service.run(second, api_key="secret")
    assert second.status == "completed"
    assert first.snapshot_uri != second.snapshot_uri
    assert events.count("maintain") == 2


def test_maintenance_skill_change_invalidates_only_maintenance(engine, monkeypatch):
    service, events, _ = engine
    first = service.new_run("acct", pipeline="maintain")
    service.run(first, api_key="secret")

    async def freeze(*args):
        return "viking://user/team/skills/maintenance", "new-revision"

    monkeypatch.setattr(workflow, "freeze_skill", freeze)
    second = service.new_run("acct", pipeline="maintain")
    service.run(second, api_key="secret")
    assert second.status == "completed" and events.count("maintain") == 2


def test_uncertain_tasks_keep_target_locked_after_restart(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    first = service.new_run("acct")
    first.merge_user_id = "team"
    first.status = "running"
    service._acquire_target(first)
    service._track_task(first, "cmp_1", "pending")
    restored = make_service(tmp_path)
    second = restored.new_run("acct")
    assert restored.target_is_active(second)
    with pytest.raises(RunConflictError):
        restored._acquire_target(second)

    async def status(_self, task_id):
        return {"status": "completed"}

    monkeypatch.setattr(CompileClient, "task_status", status)
    asyncio.run(restored.reconcile(second, "secret"))
    assert not restored.target_is_active(second)


def test_new_compile_contract_and_copy_endpoint(monkeypatch):
    calls = []
    task_events = []

    def respond(request):
        calls.append(request)
        if request.url.path == "/api/v1/compile":
            body = json.loads(request.content)
            assert set(body) == {"from", "to", "skill", "instruction"}
            return httpx.Response(202, json={"status": "ok", "result": {"task_id": "cmp_1"}})
        if request.url.path == "/api/v1/tasks/cmp_1":
            return httpx.Response(
                200, json={"status": "ok", "result": {"status": "completed", "result": {"page_count": 1}}}
            )
        assert request.url.path == "/api/v1/fs/cp"
        assert json.loads(request.content)["recursive"] is True
        return httpx.Response(200, json={"status": "ok", "result": {"phase": "completed"}})

    client = CompileClient(
        endpoint="https://ov.example",
        account_id="acct",
        api_key="secret",
        task_callback=lambda *args: task_events.append(args),
    )
    monkeypatch.setattr(client, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(respond)))

    async def scenario():
        assert (
            await client.copy_tree(
                source_uri="viking://resources/team", target_uri="viking://user/team/resources/snapshot"
            )
        )["ok"]
        assert (
            await client.run_batch(
                source_uris=["viking://resources/team"],
                target_uri="viking://resources/team",
                skill_uri="viking://agent/skills/maintenance",
                skill_revision="old",
                reason="maintain",
            )
        )["ok"]

    asyncio.run(scenario())
    assert task_events == [("cmp_1", "pending"), ("cmp_1", "completed")]


def test_stage_skill_routes_keep_accounts_and_stages_separate(tmp_path, monkeypatch):
    import team_memory.routes as routes

    service = make_service(tmp_path)
    seen = []

    async def get_skill(**kwargs):
        seen.append(kwargs)
        return {"content": "body", "revision": "rev"}

    monkeypatch.setattr(service, "get_shared_skill", get_skill)
    monkeypatch.setattr(routes, "_request_user", lambda request: {"role": "admin"})

    class Owner(AggregationMixin):
        config = replace(service.config, sharing_viking_team_api_key="secret")
        _aggregation_service_instance = service

        def _mark_request_activity(self):
            pass

    app = FastAPI()
    Owner()._register_aggregation_routes(app)
    with TestClient(app) as client:
        assert client.get("/api/aggregation/okf-skill?stage=maintenance&account_id=other").status_code == 200
        assert seen[-1]["stage"] == "maintenance" and seen[-1]["account_id"] == "other"
        assert client.get("/api/aggregation/okf-skill?stage=invalid").status_code == 400


def test_service_import_resolves_to_canonical_implementation():
    import team_memory.service as imported

    assert imported is module
    from team_replay.memory import MemoryTrueReplayRunner

    assert MemoryTrueReplayRunner


def test_freeze_skill_hashes_all_copied_files_stably(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    run = service.new_run("acct")
    run.merge_user_id = "team"
    payloads = {"SKILL.md": b"skill body", "references/policy.md": b"policy one"}

    class Client:
        async def mkdir(self, uri):
            pass

        async def copy_tree(self, **kwargs):
            assert kwargs["target_uri"].startswith("viking://user/team/skills/")
            return {"ok": True}

        async def download_bytes(self, uri):
            return next(value for path, value in payloads.items() if uri.endswith(path))

    class Inspector:
        async def inspect(self, *args, **kwargs):
            return SimpleNamespace(
                fingerprint="inventory", files=[SimpleNamespace(relative_path=path) for path in payloads]
            )

    async def ensure(*args):
        return {}

    monkeypatch.setattr(service, "_ensure_shared_skill", ensure)
    monkeypatch.setattr(workflow, "_inspector", lambda *args: Inspector())
    _, first = asyncio.run(workflow.freeze_skill(service, run, Client(), "maintenance"))
    next_run = service.new_run("acct")
    next_run.merge_user_id = "team"
    _, same = asyncio.run(workflow.freeze_skill(service, next_run, Client(), "maintenance"))
    assert first == same
    payloads["references/policy.md"] = b"policy two"
    _, changed = asyncio.run(workflow.freeze_skill(service, next_run, Client(), "maintenance"))
    assert changed != first


def test_lost_compile_submission_retains_unknown_reservation(monkeypatch):
    events = []

    def timeout(request):
        raise httpx.ReadTimeout("response lost", request=request)

    client = CompileClient(
        endpoint="https://ov.example", account_id="acct", task_callback=lambda *args: events.append(args)
    )
    monkeypatch.setattr(client, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(timeout)))
    result = asyncio.run(
        client.run_batch(
            source_uris=["viking://resources/team"],
            target_uri="viking://resources/team",
            skill_uri="viking://agent/skills/maintain",
        )
    )
    assert not result["ok"]
    assert events == [("submission_unknown", "unknown")]


def test_overlapping_target_is_rejected(tmp_path):
    service = make_service(tmp_path)
    first = service.new_run("acct", target_uri="viking://resources/team")
    second = service.new_run("acct", target_uri="viking://resources/team/part")
    service._acquire_target(first)
    with pytest.raises(RunConflictError):
        service._acquire_target(second)
