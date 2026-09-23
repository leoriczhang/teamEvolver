"""Human candidate feedback persists independently from replay and publication."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from team_skills.candidates.feedback import CandidateFeedbackStore
from team_skills.candidates.store import ValidationStore
from teamEvolver.config import TeamEvolverConfig
from teamEvolver.proxy import ProxyServer
from teamEvolver.storage import LocalObjectStore

ACTOR = {"actor_id": "reviewer", "actor_name": "审阅人", "candidate_revision": 1}


def test_feedback_persists_toggles_and_history_across_instances(tmp_path):
    def store():
        return CandidateFeedbackStore(LocalObjectStore(tmp_path))

    assert store().load("job")["reviewed"] is False
    first = store().update("job", {"reviewed": True}, **ACTOR)
    assert first["adopted"] is False
    assert first["rejected"] is False
    assert store().update("job", {"reviewed": True}, **ACTOR) == first
    store().update("job", {"adopted": True}, **ACTOR)
    store().update("job", {"rejected": True}, **ACTOR)
    result = store().update("job", {"reviewed": False}, **{**ACTOR, "actor_id": "other", "candidate_revision": 2})
    assert store().load("job") == result
    assert result["reviewed"] is False and result["adopted"] is True and result["rejected"] is True
    assert [(e["field"], e["value"]) for e in result["history"]] == [
        ("reviewed", True), ("adopted", True), ("rejected", True), ("reviewed", False),
    ]
    assert result["history"][-1]["actor_id"] == "other"
    assert result["history"][-1]["candidate_revision"] == 2
    assert all(e["at"] for e in result["history"])


def test_concurrent_updates_preserve_both_marks(tmp_path):
    def update(field):
        return CandidateFeedbackStore(LocalObjectStore(tmp_path)).update("job", {field: True}, **ACTOR)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(update, ["reviewed", "adopted"]))
    result = CandidateFeedbackStore(LocalObjectStore(tmp_path)).load("job")
    assert result["reviewed"] and result["adopted"]
    assert len(result["history"]) == 2


def test_tenant_isolation(tmp_path):
    config = TeamEvolverConfig(sharing_local_root=str(tmp_path))
    one = CandidateFeedbackStore.from_config(config, tenant_id="tenant-a")
    two = CandidateFeedbackStore.from_config(config, tenant_id="tenant-b")
    one.update("same-job", {"reviewed": True}, **ACTOR)
    assert two.load("same-job") == {
        "reviewed": False,
        "adopted": False,
        "rejected": False,
        "version": 0,
        "history": [],
    }


def test_feedback_loads_records_written_before_rejected_marker(tmp_path):
    bucket = LocalObjectStore(tmp_path)
    store = CandidateFeedbackStore(bucket)
    bucket.put_object(
        store._key("job"),
        json.dumps({
            "job_id": "job",
            "reviewed": True,
            "adopted": False,
            "version": 1,
            "history": [],
        }).encode(),
    )

    assert store.load("job")["rejected"] is False


def test_conditional_writes_use_the_hash_of_the_loaded_record(tmp_path):
    class ConditionalBucket(LocalObjectStore):
        native_batch_write = True

        def batch_write(self, objects, *, preconditions):
            for key, payload in objects.items():
                try:
                    prior = self.get_object(key).read()
                except FileNotFoundError:
                    assert preconditions[key] == {"kind": "create_if_absent"}
                else:
                    assert preconditions[key] == {
                        "kind": "replace_if_hash", "base_hash": "sha256:" + hashlib.sha256(prior).hexdigest(),
                    }
                self.put_object(key, payload)

    store = CandidateFeedbackStore(ConditionalBucket(tmp_path))
    store.update("job", {"reviewed": True}, **ACTOR)
    store.update("job", {"adopted": True}, **ACTOR)
    assert len(store.load("job")["history"]) == 2


def test_unavailable_or_corrupt_storage_is_not_overwritten(tmp_path, monkeypatch):
    bucket = LocalObjectStore(tmp_path)
    store = CandidateFeedbackStore(bucket)
    store.update("job", {"reviewed": True}, **ACTOR)

    def fail(*args):
        raise OSError("disk full")

    with monkeypatch.context() as m:
        m.setattr(bucket, "put_object", fail)
        with pytest.raises(OSError, match="disk full"):
            store.update("job", {"adopted": True}, **ACTOR)
    assert store.load("job")["adopted"] is False
    key = next(bucket.iter_objects()).key
    bucket.put_object(key, b"invalid json")
    with pytest.raises(ValueError):
        store.update("job", {"adopted": True}, **ACTOR)
    assert bucket.get_object(key).read() == b"invalid json"


@pytest.fixture
def candidate_api(tmp_path, monkeypatch):
    config = TeamEvolverConfig(
        users_registry_path=str(tmp_path / "users.json"),
        skills_dir=str(tmp_path / "skills"),
        sharing_enabled=False,
        sharing_skill_mirror_enabled=False,
        sharing_skill_backend="local",
        sharing_local_root=str(tmp_path / "storage"),
    )
    monkeypatch.setenv("TEAMEVOLVER_ROOT_API_KEY", "test-root-key")
    server = ProxyServer(config)
    client = TestClient(server.app, headers={"Authorization": "Bearer test-root-key"})
    store = ValidationStore.from_config(config)
    store.save_job({
        "job_id": "job", "candidate_revision": 1, "proposed_action": "create_skill",
        "candidate_skill": {
            "name": "example",
            "description": "Example candidate",
            "content": "candidate body",
        },
    })
    return client, store, config, server


def test_api_refresh_and_history_keep_feedback(candidate_api):
    client, store, config, _ = candidate_api
    base = "/api/skill-candidates"
    # Warm the cached candidate list before marking.
    assert client.get(base).json()["candidates"][0]["feedback"]["reviewed"] is False
    saved = client.patch(base + "/job/feedback", json={"reviewed": True})
    assert saved.status_code == 200, saved.text
    feedback = saved.json()["feedback"]
    assert feedback["history"][0]["actor_id"] == "service-root"
    assert client.get(base + "?compact=true").json()["candidates"][0]["feedback"] == feedback
    assert client.get(base + "/job/detail").json()["feedback"] == feedback
    # A fresh service/store reads the same persisted state.
    restarted = TestClient(ProxyServer(config).app, headers=client.headers)
    assert restarted.get(base + "/job/detail").json()["feedback"] == feedback
    store.reset_job_artifacts("job")
    assert client.get(base + "/job/detail").json()["feedback"] == feedback
    store.save_decision("job", {"status": "published", "accepted": True})
    assert client.get(base + "?scope=processed").json()["candidates"][0]["feedback"] == feedback
    assert client.patch(base + "/job/feedback", json={"reviewed": False}).status_code == 409


@pytest.mark.parametrize("body", [
    {},
    {"reviewed": "true"},
    {"reviewed": None},
    {"adopted": True},
    {"rejected": True},
    {"actor_id": "forged"},
])
def test_api_rejects_invalid_feedback(candidate_api, body):
    client, _, _, _ = candidate_api
    assert client.patch("/api/skill-candidates/job/feedback", json=body).status_code == 400


def test_api_checks_candidate_and_authenticated_user(candidate_api):
    import time

    from teamEvolver.proxy.routes import _SESSION_COOKIE

    client, _, config, server = candidate_api
    assert client.patch("/api/skill-candidates/missing/feedback", json={"reviewed": True}).status_code == 404
    anonymous = TestClient(server.app)
    assert anonymous.patch("/api/skill-candidates/job/feedback", json={"reviewed": True}).status_code == 401
    # Exercise the real console session identity, without trusting an actor in the body.
    from pathlib import Path

    users = [{"id": "alice", "username": "alice", "display_name": "Alice", "role": "admin"}]
    Path(config.users_registry_path).write_text(json.dumps({"users": users}))
    server._console_sessions["test-session"] = {"user_id": "alice", "expires_at": time.time() + 3600}
    anonymous.cookies.set(_SESSION_COOKIE, "test-session")
    response = anonymous.patch("/api/skill-candidates/job/feedback", json={"reviewed": True})
    assert response.status_code == 200, response.text
    event = response.json()["feedback"]["history"][-1]
    assert (event["actor_id"], event["actor_name"]) == ("alice", "Alice")
    users[0]["role"] = "user"
    Path(config.users_registry_path).write_text(json.dumps({"users": users}))
    assert anonymous.patch("/api/skill-candidates/job/feedback", json={"reviewed": False}).status_code == 403


def test_reject_keeps_candidate_and_marks_it_rejected(candidate_api):
    client, store, _, _ = candidate_api

    response = client.post("/api/skill-candidates/job/reject")

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "rejected"
    assert result["feedback"]["rejected"] is True
    assert result["feedback"]["adopted"] is False
    assert store.load_job("job") is not None
    history = client.get("/api/skill-candidates?scope=processed").json()["candidates"]
    assert history[0]["feedback"]["rejected"] is True
    assert client.delete("/api/skill-candidates/job").status_code == 200
    assert store.load_job("job") is not None


@pytest.mark.parametrize("mode", ["auto", "force"])
def test_publish_marks_candidate_adopted(candidate_api, mode):
    client, store, _, _ = candidate_api
    store.save_evaluation("job", {"accepted": True})

    response = client.post(
        "/api/skill-candidates/job/validate",
        json={"mode": mode},
    )

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "published"
    assert result["feedback"]["adopted"] is True
    assert result["feedback"]["rejected"] is False
