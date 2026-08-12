from __future__ import annotations

import base64
import time
from pathlib import Path

from fastapi.testclient import TestClient

from teamEvolver.config import TeamEvolverConfig
from teamEvolver.proxy import ProxyServer
from teamEvolver.session_store import SessionStore
from teamEvolver.skill_lab import (
    SkillLabStore,
    evolution_datasets,
    parse_dataset_markdown,
    prepare_experiment_job,
    render_dataset_markdown,
)
from teamEvolver.skills.manager import SkillManager
from teamEvolver.true_replay import annotate_cases, build_sandbox
from teamEvolver.validation.store import ValidationStore


def _skill_md(name: str, body: str = "# Procedure\n\nDo the work.") -> str:
    return (
        "---\n"
        f"name: {name}\n"
        "description: Experiment skill\n"
        "category: general\n"
        "---\n\n"
        f"{body}\n"
    )


def _config(tmp_path: Path) -> TeamEvolverConfig:
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "demo-skill"
    skill_dir.mkdir(parents=True)
    skill_dir.joinpath("SKILL.md").write_text(
        _skill_md("demo-skill"),
        encoding="utf-8",
    )
    return TeamEvolverConfig(
        skills_dir=str(skills_dir),
        users_registry_path=str(tmp_path / "users.json"),
        sharing_enabled=True,
        sharing_backend="local",
        sharing_session_backend="local",
        sharing_local_root=str(tmp_path / "store"),
        sharing_user_alias="tester",
    )


def _authed_client(server: ProxyServer) -> TestClient:
    client = TestClient(server.app)
    response = client.post(
        "/api/auth/bootstrap",
        json={
            "username": "admin",
            "display_name": "Admin",
            "password": "password123",
        },
    )
    assert response.status_code == 200
    return client


def test_dataset_markdown_round_trip() -> None:
    source = (
        "### query\n\n分析材料并输出报告。\n\n"
        "### 要求\n\n1. 保存文件\n2. 不编造\n\n"
        "### 轨迹要求\n\n1. 先读取材料\n"
    )

    parsed = parse_dataset_markdown(source)

    assert parsed["query"] == "分析材料并输出报告。"
    assert "2. 不编造" in parsed["requirements"]
    assert parse_dataset_markdown(render_dataset_markdown(parsed)) == parsed


def test_store_persists_dataset_materials_and_runs(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = SkillLabStore.from_config(config)
    content = b"code,value\nA,1\n"
    dataset = store.save_dataset(
        {
            "skill_name": "demo-skill",
            "name": "CSV case",
            "query": "读取 materials/input.csv。",
            "requirements": "1. 输出结果",
            "trajectory_requirements": "1. 使用文件工具",
        },
        files=[
            {
                "path": "materials/input.csv",
                "content_b64": base64.b64encode(content).decode("ascii"),
            }
        ],
    )

    loaded = store.load_dataset(dataset["dataset_id"])
    materials = store.material_payloads(loaded or {})
    assert loaded is not None
    assert loaded["materials"][0]["path"] == "materials/input.csv"
    assert base64.b64decode(materials[0]["content_b64"]) == content

    run = store.create_run(
        {
            "run_id": store.make_run_id(),
            "skill_name": "demo-skill",
            "dataset_id": dataset["dataset_id"],
            "status": "running",
        }
    )
    store.finish_run(
        run["run_id"],
        result={"status": "evaluated", "verdict": "accept", "efficiency": {}},
        status="completed",
    )

    detail = store.load_run(run["run_id"])
    assert detail is not None
    assert detail["status"] == "completed"
    assert detail["result"]["verdict"] == "accept"
    assert store.list_runs(skill_name="demo-skill")[0]["run_id"] == run["run_id"]


def test_synthesized_evolution_tests_are_exposed_as_read_only_datasets(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    validation = ValidationStore.from_config(config)
    validation.save_job(
        {
            "job_id": "job-demo",
            "candidate_skill_name": "demo-skill",
            "candidate_skill": {
                "name": "demo-skill",
                "description": "Experiment skill",
                "content": "Do it better.",
            },
            "test_datasets": [
                {
                    "dataset_id": "synth-historical-1",
                    "name": "历史分析测试",
                    "query": "分析历史会话中的材料。",
                    "requirements": ["输出结论", "标注来源"],
                    "trajectory_requirements": [
                        "read materials",
                        "write output",
                    ],
                    "source_session_ids": ["session-1"],
                    "evidence_window": "historical",
                }
            ],
        }
    )

    datasets = evolution_datasets(config, skill_name="demo-skill")

    assert len(datasets) == 1
    assert datasets[0]["read_only"] is True
    assert datasets[0]["source"]["job_id"] == "job-demo"
    assert datasets[0]["source"]["session_id"] == "session-1"
    assert "read materials" in datasets[0]["trajectory_requirements"]
    assert "输出结论" in datasets[0]["requirements"]


def test_legacy_bare_replay_cases_are_hidden_and_formal_datasets_are_deduped(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    validation = ValidationStore.from_config(config)
    formal = {
        "dataset_id": "synth-shared",
        "name": "正式测试",
        "query": "执行同类任务",
        "requirements": ["输出报告"],
        "trajectory_requirements": ["读取材料"],
        "source_session_ids": ["session-1"],
        "evidence_window": "recent",
    }
    validation.save_job(
        {
            "job_id": "job-old",
            "created_at": "2026-08-01T00:00:00+00:00",
            "candidate_skill_name": "demo-skill",
            "candidate_skill": {"name": "demo-skill"},
            "test_datasets": [formal],
        }
    )
    validation.save_job(
        {
            "job_id": "job-new",
            "created_at": "2026-08-02T00:00:00+00:00",
            "candidate_skill_name": "demo-skill",
            "candidate_skill": {"name": "demo-skill"},
            "test_datasets": [formal],
        }
    )
    validation.save_job(
        {
            "job_id": "job-legacy",
            "created_at": "2026-08-03T00:00:00+00:00",
            "candidate_skill_name": "demo-skill",
            "candidate_skill": {"name": "demo-skill"},
            "replay_cases": [
                {
                    "session_id": "session-legacy",
                    "instruction": "这只是旧 replay turn",
                }
            ],
        }
    )

    datasets = evolution_datasets(config, skill_name="demo-skill")

    assert len(datasets) == 1
    assert datasets[0]["dataset_id"] == "synth-shared"
    assert datasets[0]["source"]["job_id"] == "job-new"


def test_uploaded_materials_make_relative_case_runnable_and_mount_in_both_sandboxes(
    tmp_path: Path,
) -> None:
    content = base64.b64encode(b"hello\n").decode("ascii")
    materials = [{"path": "q1_materials/input.txt", "content_b64": content}]
    cases = annotate_cases(
        {
            "replay_cases": [
                {
                    "instruction": "读取 q1_materials/ 下的 input.txt 并处理。",
                    "materials": materials,
                }
            ]
        },
        [],
    )
    harness = {
        "base_url": "http://model",
        "api_key": "key",
        "model": "model",
        "api_mode": "chat",
        "max_tokens": 1024,
    }

    assert cases[0]["runnable"] is True
    assert cases[0]["referenced_paths"][0]["resolved"].startswith("uploaded://")
    for branch in ("baseline", "candidate"):
        sandbox = build_sandbox(
            tmp_path,
            branch,
            harness,
            {"name": "demo-skill", "description": "Demo", "content": "Do it."},
            materials=materials,
        )
        mounted = Path(sandbox["workspace"]) / "q1_materials" / "input.txt"
        assert mounted.read_text("utf-8") == "hello\n"


def test_prepare_experiment_job_preserves_bundle_and_full_trace_flag(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    skill_dir = Path(config.skills_dir) / "demo-skill"
    skill_dir.joinpath("references").mkdir()
    skill_dir.joinpath("references/guide.md").write_text("guide\n", encoding="utf-8")
    draft = _skill_md("demo-skill", "# Procedure\n\nDo the work in fewer steps.")

    job = prepare_experiment_job(
        skills_dir=config.skills_dir,
        skill_name="demo-skill",
        candidate_skill_md=draft,
        dataset={
            "dataset_id": "ds-1",
            "query": "执行任务。",
            "requirements": "1. 输出结果",
            "trajectory_requirements": "1. 读取材料",
            "source": {"kind": "manual"},
        },
        materials=[],
        run_id="run-1",
    )

    assert job["include_full_trace"] is True
    assert job["current_skill"]["bundle"]["format"] == "bundle_v1"
    assert job["candidate_skill"]["bundle"]["format"] == "bundle_v1"
    paths = {
        item["path"]
        for item in job["candidate_skill"]["bundle"]["files"]
    }
    assert "references/guide.md" in paths
    replay_case = job["replay_cases"][0]
    assert replay_case["instruction"] == "执行任务。"
    assert "输出结果" not in replay_case["instruction"]
    assert replay_case["checklist"] == [
        {"id": "R01", "text": "输出结果", "kind": "output"},
        {"id": "T01", "text": "读取材料", "kind": "trajectory"},
    ]
    assert replay_case["progressive_disclosure"]["initial_visibility"] == (
        "query_only"
    )


def test_skill_lab_routes_list_and_create_dataset(tmp_path: Path) -> None:
    config = _config(tmp_path)
    server = ProxyServer(config, skill_manager=SkillManager(config.skills_dir))
    client = _authed_client(server)

    created = client.post(
        "/api/skill-lab/datasets",
        json={
            "skill_name": "demo-skill",
            "name": "Manual",
            "query": "执行任务。",
            "requirements": "1. 完成",
            "files": [],
        },
    )
    listed = client.get(
        "/api/skill-lab/datasets",
        params={"skill_name": "demo-skill"},
    )

    assert created.status_code == 200
    assert listed.status_code == 200
    assert listed.json()["manual_count"] == 1
    assert listed.json()["datasets"][0]["query"] == "执行任务。"


def test_skill_lab_route_imports_dataset_markdown_with_materials(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    server = ProxyServer(config, skill_manager=SkillManager(config.skills_dir))
    client = _authed_client(server)
    markdown = (
        "### query\n\n分析输入材料。\n\n"
        "### 要求\n\n1. 输出报告\n2. 标注来源\n\n"
        "### 轨迹要求\n\n1. 读取 q1_materials/input.csv\n"
    )

    response = client.post(
        "/api/skill-lab/datasets",
        json={
            "skill_name": "demo-skill",
            "name": "Imported",
            "dataset_markdown": markdown,
            "files": [
                {
                    "path": "q1_materials/input.csv",
                    "content_b64": base64.b64encode(b"name,value\nA,1\n").decode(),
                }
            ],
        },
    )

    assert response.status_code == 200
    dataset = response.json()
    assert dataset["query"] == "分析输入材料。"
    assert "2. 标注来源" in dataset["requirements"]
    assert dataset["materials"][0]["path"] == "q1_materials/input.csv"


def test_skill_lab_run_route_persists_background_true_replay(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = _config(tmp_path)
    server = ProxyServer(config, skill_manager=SkillManager(config.skills_dir))

    def fake_evaluate_job(run_id, **kwargs):  # noqa: ANN001
        job = kwargs["job"]
        assert job["include_full_trace"] is True
        assert job["replay_cases"][0]["instruction"] == "执行任务。"
        assert "requirements" in job["replay_cases"][0]
        return {
            "status": "evaluated",
            "job_id": run_id,
            "verdict": "accept",
            "accepted": True,
            "efficiency": {
                "dimensions": {
                    "interaction_turns": {
                        "baseline": 2,
                        "candidate": 1,
                        "delta": 1,
                        "winner": "candidate",
                    }
                }
            },
            "cases": [
                {
                    "baseline": {
                        "ok": True,
                        "messages": [{"role": "assistant", "content": "before"}],
                    },
                    "candidate": {
                        "ok": True,
                        "messages": [{"role": "assistant", "content": "after"}],
                    },
                }
            ],
        }

    monkeypatch.setattr(
        "teamEvolver.true_replay.evaluate_job",
        fake_evaluate_job,
    )
    with TestClient(server.app) as client:
        response = client.post(
            "/api/auth/bootstrap",
            json={
                "username": "admin",
                "display_name": "Admin",
                "password": "password123",
            },
        )
        assert response.status_code == 200
        dataset = client.post(
            "/api/skill-lab/datasets",
            json={
                "skill_name": "demo-skill",
                "name": "Run case",
                "query": "执行任务。",
                "files": [],
            },
        ).json()
        started = client.post(
            "/api/skill-lab/runs",
            json={
                "skill_name": "demo-skill",
                "dataset_id": dataset["dataset_id"],
                "candidate_skill_md": _skill_md(
                    "demo-skill",
                    "# Procedure\n\nUse fewer steps.",
                ),
            },
        )
        assert started.status_code == 200
        run_id = started.json()["run_id"]

        detail = {}
        for _ in range(20):
            detail_response = client.get(f"/api/skill-lab/runs/{run_id}")
            assert detail_response.status_code == 200
            detail = detail_response.json()
            if detail["status"] != "running":
                break
            time.sleep(0.02)

    assert detail["status"] == "completed"
    assert detail["result"]["verdict"] == "accept"
    assert detail["result"]["cases"][0]["candidate"]["messages"][0]["content"] == "after"


def test_skill_lab_can_synthesize_editable_dataset_from_history(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = _config(tmp_path)
    SessionStore.from_config(config).save_queued(
        {
            "session_id": "session-history",
            "used_skills": ["demo-skill"],
            "turns": [
                {
                    "turn_num": 1,
                    "prompt_text": "基于材料完成历史任务",
                    "response_text": "已完成",
                    "used_skills": ["demo-skill"],
                }
            ],
        }
    )

    async def fake_synthesize(_llm, **kwargs):  # noqa: ANN001
        assert kwargs["sessions"][0]["session_id"] == "session-history"
        return [
            {
                "name": "历史合成测试",
                "query": "执行新的同类任务",
                "requirements": ["输出结果", "标注来源"],
                "trajectory_requirements": ["读取材料"],
                "source_session_ids": ["session-history"],
                "evidence_window": "recent",
                "synthesis_mode": "model",
                "progressive_disclosure": {
                    "enabled": True,
                    "batch_size": 2,
                },
            }
        ]

    monkeypatch.setattr(
        "teamEvolver.proxy.skill_lab.synthesize_evolution_datasets",
        fake_synthesize,
    )
    server = ProxyServer(config, skill_manager=SkillManager(config.skills_dir))
    client = _authed_client(server)

    response = client.post(
        "/api/skill-lab/datasets/synthesize",
        json={"skill_name": "demo-skill"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["source_session_count"] == 1
    assert payload["count"] == 1
    dataset = payload["datasets"][0]
    assert dataset["source"]["kind"] == "synthesized"
    assert dataset["source"]["source_session_ids"] == ["session-history"]
    assert dataset["read_only"] is False
