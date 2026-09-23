"""Hub-backed Skill workbench: path safety, optimistic concurrency, experiment baselines."""

import hashlib
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from team_replay.lab.service import SkillLabError, prepare_experiment_job
from team_skills.library.bundle import candidate_skill_bundle
from team_skills.library.hub import SkillHub
from teamEvolver.proxy.skill_workspace import (
    list_workspace,
    read_workspace_file,
    register_skill_workspace_routes,
    write_workspace_file,
)
from teamEvolver.storage.memory import InMemoryObjectStore


def _seed_skill(hub: SkillHub, root: Path, name: str, skill_md: str, files: dict[str, bytes] | None = None) -> None:
    """Push a skill into the hub the way the library does (bundle on disk → hub)."""
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(skill_md)
    for rel_path, data in (files or {}).items():
        target = folder / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    hub.push_skills(str(root), include_names=[name])


@pytest.fixture
def hub(tmp_path):
    store = InMemoryObjectStore("memory://workbench-tests")
    hub = SkillHub.from_bucket(store, tenant_id="default")
    _seed_skill(
        hub,
        tmp_path,
        "demo",
        "---\nname: demo\ndescription: Example\n---\nOriginal\n",
        {"scripts/run.py": b"print('original')\n"},
    )
    return hub


def _loader(*bundles: tuple[str, str]):
    """Bundle loader for experiments: ``(name, skill_md)`` pairs."""
    table = {name: {"SKILL.md": md.encode("utf-8")} for name, md in bundles}

    def load(name: str):
        if name not in table:
            raise FileNotFoundError(name)
        return table[name]

    return load


def test_save_detects_conflict_and_bumps_version(hub):
    snapshot = read_workspace_file(hub, "demo", "scripts/run.py")
    saved = write_workspace_file(hub, "demo", "scripts/run.py", "print('中文')\n", snapshot["sha256"])
    assert saved["content"] == "print('中文')\n"
    assert saved["record"]["version"] == 2
    with pytest.raises(HTTPException) as exc:
        write_workspace_file(hub, "demo", "scripts/run.py", "stale", snapshot["sha256"])
    assert exc.value.status_code == 409
    assert hub.read_skill_file("demo", "scripts/run.py") == "print('中文')\n".encode()


def test_create_nested_file_without_overwriting_existing(hub):
    created = write_workspace_file(hub, "demo", "references/new.md", "New", None)
    assert created["sha256"] == hashlib.sha256(b"New").hexdigest()
    with pytest.raises(HTTPException):
        write_workspace_file(hub, "demo", "references/new.md", "Bad", None)
    with pytest.raises(ValueError):
        write_workspace_file(hub, "demo", "SKILL.md", "---\nname: other\ndescription: X\n---\nBody", None)


@pytest.mark.parametrize(
    "path", ["../outside", "/tmp/outside", "scripts/../../outside", "C:/outside", r"..\outside", ".git/config"],
)
def test_workspace_rejects_escaping_paths(hub, path):
    with pytest.raises((ValueError, HTTPException)):
        read_workspace_file(hub, "demo", path)


def test_workspace_rejects_unknown_skill_and_binary_files(hub):
    with pytest.raises(FileNotFoundError):
        read_workspace_file(hub, "missing-skill", "SKILL.md")
    hub.write_skill_file("demo", "asset.bin", b"abc\x00xyz", expected_sha256=None)
    result = read_workspace_file(hub, "demo", "asset.bin")
    assert not result["editable"]
    assert "content" not in result


def test_index_lists_hub_versions_once(hub, monkeypatch):
    import teamEvolver.proxy.skill_workspace as workspace

    monkeypatch.setattr(workspace, "team_hub", lambda owner: hub)
    index = list_workspace(_Owner(hub))
    assert index["source"]["backend"] == "memory"
    assert index["skills"][0]["name"] == "demo"
    assert index["skills"][0]["version"] == hub.list_remote()[0]["version"]
    assert index["skills"][0]["files"] == ["SKILL.md", "scripts/run.py"]


def test_candidate_contains_unsaved_scripts_without_changing_baseline(hub):
    job = prepare_experiment_job(
        load_bundle=hub.read_skill_bundle,
        skill_name="demo",
        candidate_skill_md="---\nname: demo\ndescription: Example\n---\nCandidate\n",
        candidate_files={"scripts/run.py": "print('candidate')", "references/new.md": "New reference"},
        dataset={"dataset_id": "test", "query": "Run demo", "requirements": "1. Complete"},
        materials=[], run_id="run-test",
    )
    baseline = candidate_skill_bundle(job["current_skill"])
    candidate = candidate_skill_bundle(job["candidate_skill"])
    assert baseline["scripts/run.py"] == b"print('original')\n"
    assert candidate["scripts/run.py"] == b"print('candidate')"
    assert candidate["references/new.md"] == b"New reference"
    # The library itself is untouched by an experiment.
    assert hub.read_skill_file("demo", "scripts/run.py") == b"print('original')\n"


def test_experiment_loads_peer_skills_for_both_branches(hub):
    job = prepare_experiment_job(
        load_bundle=_loader(
            ("demo", "---\nname: demo\ndescription: Example\n---\nCandidate\n"),
            ("helper", "---\nname: helper\ndescription: Helper\n---\nShared helper\n"),
        ),
        skill_name="demo",
        candidate_skill_md="---\nname: demo\ndescription: Example\n---\nCandidate\n",
        dataset={
            "dataset_id": "shared",
            "skill_ids": ["helper", "demo"],
            "cases": [
                {
                    "case_id": "task-both",
                    "skill_ids": ["helper", "demo"],
                    "query": "Run both skills",
                    "requirements": "Complete",
                },
                {
                    "case_id": "task-helper",
                    "skill_ids": ["helper"],
                    "query": "Run helper only",
                    "requirements": "Complete helper task",
                },
            ],
        },
        materials=[],
        run_id="run-shared",
    )

    assert job["skill_ids"] == ["demo", "helper"]
    assert job["changed_skill_ids"] == ["demo"]
    assert [item["name"] for item in job["current_skill"]["skills"]] == ["demo", "helper"]
    assert [item["name"] for item in job["candidate_skill"]["skills"]] == ["demo", "helper"]
    assert len(job["replay_cases"]) == 1
    assert job["replay_cases"][0]["case_id"] == "task-both"
    assert job["replay_cases"][0]["skill_ids"] == ["demo", "helper"]


def test_experiment_rejects_unknown_baseline_skill(hub):
    with pytest.raises(SkillLabError):
        prepare_experiment_job(
            load_bundle=hub.read_skill_bundle,
            skill_name="missing",
            candidate_skill_md="---\nname: missing\ndescription: X\n---\nBody",
            dataset={"query": "Run"}, materials=[], run_id="bad",
        )


@pytest.mark.parametrize("files", [{"../bad": "bad"}, {"/bad": "bad"}, {"SKILL.md": "bad"}, {"x": 1}, ["bad"]])
def test_candidate_rejects_invalid_file_overrides(hub, files):
    with pytest.raises(SkillLabError):
        prepare_experiment_job(
            load_bundle=hub.read_skill_bundle,
            skill_name="demo",
            candidate_skill_md="---\nname: demo\ndescription: Example\n---\nCandidate\n",
            candidate_files=files, dataset={"query": "Run"}, materials=[], run_id="bad",
        )


class _Owner:
    """Minimal ProxyServer stand-in: the workbench routes need the hub only."""

    def __init__(self, hub, config=None):
        self._hub = hub
        self.config = config

    def _eff_config(self):
        return None


def test_routes_read_from_the_hub_and_protect_writes(hub, monkeypatch):
    import teamEvolver.proxy.skill_workspace as workspace

    monkeypatch.setattr(workspace, "team_hub", lambda owner: hub)
    monkeypatch.setattr(workspace, "_eff_config", lambda owner: None)
    app = FastAPI()
    register_skill_workspace_routes(_Owner(hub), app)
    client = TestClient(app)

    index = client.get("/api/replay-lab/workspace").json()
    assert [skill["name"] for skill in index["skills"]] == ["demo"]
    assert index["skills"][0]["files"] == ["SKILL.md", "scripts/run.py"]

    snapshot = client.get("/api/replay-lab/workspace/demo/file?path=SKILL.md").json()
    assert snapshot["editable"]
    assert client.get("/api/replay-lab/workspace/demo/file?path=../etc/passwd").status_code == 400

    response = client.put(
        "/api/replay-lab/workspace/demo/file",
        json={"path": "SKILL.md", "content": "x", "expected_sha256": snapshot["sha256"]},
    )
    assert response.status_code in {401, 403}