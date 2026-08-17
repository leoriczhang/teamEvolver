from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import yaml

SKILLMINER_DIR = Path(__file__).resolve().parents[1] / "teamEvolver" / "skillminer"
sys.path.insert(0, str(SKILLMINER_DIR))

import run_benchmark as rb  # noqa: E402
import run_pipeline as rp  # noqa: E402
import benchmark_format as bf  # noqa: E402
from teamEvolver.mining_settings import settings_payload, update_settings


def _progressive_benchmark(skill_name="demo", query="测试情境"):
    question = {
        "id": "BM-01",
        "name": "测试场景",
        "input": query,
        "gold": {"must_hit": [f"可核验要求 {index}" for index in range(1, 13)]},
        "trajectory_requirements": ["先确认关键事实", "完成后核验结果"],
    }
    return bf.build_document(skill_name, [question], created_at="2026-08-13T00:00:00+00:00")


def _point_pipeline_at(monkeypatch, root: Path) -> None:
    monkeypatch.setattr(rp, "PROJECT_ROOT", root)
    monkeypatch.setattr(rp, "RUN_HISTORY_DIR", root / "run_history")


def test_progressive_benchmark_matches_skillgene_schema():
    payload = _progressive_benchmark()
    assert set(payload) == {
        "schema_version", "dataset_format", "skill_name", "generation_id",
        "candidate_revision", "source_session_ids", "datasets", "created_at",
    }
    dataset = payload["datasets"][0]
    assert set(dataset) == {
        "dataset_id", "dataset_format", "skill_name", "split", "name", "query",
        "requirements", "trajectory_requirements", "checklist", "source_session_ids",
        "evidence_window", "synthesis_mode", "requirement_count",
        "minimum_requirement_target", "progressive_disclosure", "created_at",
    }
    assert payload["schema_version"] == 1
    assert dataset["minimum_requirement_target"] == 12
    assert dataset["progressive_disclosure"] == {
        "enabled": True,
        "initial_visibility": "query_only",
        "batch_size": 4,
        "stop_when": "all_checklist_items_satisfied",
    }
    assert bf.validate_document(payload, expected_skill_name="demo") == []


def test_hermes_discovery_uses_project_venv_and_ignores_global_path(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    global_bin = tmp_path / "global-bin"
    global_bin.mkdir()
    global_hermes = global_bin / "hermes"
    global_hermes.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    global_hermes.chmod(0o755)

    monkeypatch.setattr(rp, "REPOSITORY_ROOT", repository)
    monkeypatch.setattr(rp.sys, "prefix", rp.sys.base_prefix)
    monkeypatch.delenv("TEAMEVOLVER_HERMES_BIN", raising=False)
    monkeypatch.setenv("PATH", str(global_bin))

    assert rp.find_hermes_bin() is None

    project_hermes = repository / ".venv" / "bin" / "hermes"
    project_hermes.parent.mkdir(parents=True)
    project_hermes.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    project_hermes.chmod(0o755)
    assert rp.find_hermes_bin() == str(project_hermes)


def test_hermes_home_initializes_from_project_template_not_user_home(tmp_path, monkeypatch):
    base_home = tmp_path / "project" / ".hermes_home"
    task_home = tmp_path / "task" / ".hermes_home"
    template = tmp_path / "config.yaml.example"
    template.write_text("model:\n  default: project-model\n", encoding="utf-8")

    monkeypatch.setattr(rp, "PROJECT_HERMES_HOME", base_home)
    monkeypatch.setattr(rp, "HERMES_HOME", task_home)
    monkeypatch.setattr(rp, "HERMES_CONFIG_TEMPLATE", template)

    assert rp.ensure_hermes_home() is True
    initialized = yaml.safe_load((task_home / "config.yaml").read_text(encoding="utf-8"))
    assert initialized["model"]["default"] == "project-model"
    assert initialized["hooks_auto_accept"] is False

    base_home.mkdir(parents=True)
    (base_home / "config.yaml").write_text("model:\n  default: configured-model\n", encoding="utf-8")
    second_task = tmp_path / "second-task" / ".hermes_home"
    monkeypatch.setattr(rp, "HERMES_HOME", second_task)
    assert rp.ensure_hermes_home() is True
    assert "configured-model" in (second_task / "config.yaml").read_text(encoding="utf-8")


def test_hermes_home_removes_inherited_evolution_hooks_and_external_skills(tmp_path, monkeypatch):
    base_home = tmp_path / "project" / ".hermes_home"
    task_home = tmp_path / "task" / ".hermes_home"
    base_home.mkdir(parents=True)
    (base_home / "config.yaml").write_text(
        """
model:
  default: configured-model
skills:
  external_dirs:
    - /Users/example/.hermes/team_skills/skillgene
hooks_auto_accept: true
hooks:
  pre_llm_call:
    - command: python sync_skills.py
  on_session_end:
    - command: python push_session.py
""".lstrip(),
        encoding="utf-8",
    )

    monkeypatch.setattr(rp, "PROJECT_HERMES_HOME", base_home)
    monkeypatch.setattr(rp, "HERMES_HOME", task_home)

    assert rp.ensure_hermes_home() is True
    sanitized = yaml.safe_load((task_home / "config.yaml").read_text(encoding="utf-8"))
    assert sanitized["model"]["default"] == "configured-model"
    assert "hooks" not in sanitized
    assert sanitized["hooks_auto_accept"] is False
    assert "skills" not in sanitized


def test_hermes_environment_prefers_project_config_over_global_overrides(tmp_path, monkeypatch):
    monkeypatch.setattr(rp, "HERMES_HOME", tmp_path / ".hermes_home")
    monkeypatch.setattr(rp, "resolve_ark_key", lambda: "")
    monkeypatch.setenv("HERMES_HOME", "/global/hermes")
    monkeypatch.setenv("HERMES_INFERENCE_MODEL", "global-model")
    monkeypatch.setenv("HERMES_IGNORE_USER_CONFIG", "1")
    monkeypatch.setenv("HERMES_ACCEPT_HOOKS", "1")
    monkeypatch.setenv("TEAMEVOLVER_URL", "https://evolve.example")
    monkeypatch.setenv("TEAMEVOLVER_USER", "embedded-hermes")
    monkeypatch.setenv("EVOLVE_INGEST_API_KEY", "feed-secret")

    env, has_key = rp.build_hermes_env()

    assert env["HERMES_HOME"] == str(tmp_path / ".hermes_home")
    assert "HERMES_INFERENCE_MODEL" not in env
    assert "HERMES_IGNORE_USER_CONFIG" not in env
    assert "HERMES_ACCEPT_HOOKS" not in env
    assert "TEAMEVOLVER_URL" not in env
    assert "TEAMEVOLVER_USER" not in env
    assert "EVOLVE_INGEST_API_KEY" not in env
    assert env["TEAMEVOLVER_DISABLE_SESSION_FEED"] == "1"
    assert env["PYTHONNOUSERSITE"] == "1"
    assert has_key is False


def test_prepare_run_workspace_moves_old_outputs_recoverably(tmp_path, monkeypatch):
    _point_pipeline_at(monkeypatch, tmp_path)
    for name in rp.GENERATED_STAGE_DIRS:
        stage = tmp_path / name
        stage.mkdir()
        (stage / "old.txt").write_text(name, encoding="utf-8")

    session_dir = rp.prepare_run_workspace("session-a")

    assert session_dir == tmp_path / "run_history" / "session-a"
    for name in rp.GENERATED_STAGE_DIRS:
        assert list((tmp_path / name).iterdir()) == []
        archived = session_dir / "preexisting" / name / "old.txt"
        assert archived.read_text(encoding="utf-8") == name


def test_prepare_round_workspace_removes_all_stale_stage_outputs(tmp_path, monkeypatch):
    _point_pipeline_at(monkeypatch, tmp_path)
    for name in rp.GENERATED_STAGE_DIRS:
        nested = tmp_path / name / "stale"
        nested.mkdir(parents=True)
        (nested / "artifact.md").write_text("stale", encoding="utf-8")

    rp.prepare_round_workspace(2)

    for name in rp.GENERATED_STAGE_DIRS:
        assert (tmp_path / name).is_dir()
        assert list((tmp_path / name).iterdir()) == []


def test_archive_round_is_namespaced_by_session(tmp_path, monkeypatch):
    _point_pipeline_at(monkeypatch, tmp_path)
    compiled = tmp_path / "compiled_skill" / "demo"
    compiled.mkdir(parents=True)
    (compiled / "SKILL.md").write_text("first", encoding="utf-8")

    first = rp.archive_round(1, "session-a")
    (compiled / "SKILL.md").write_text("second", encoding="utf-8")
    second = rp.archive_round(1, "session-b")

    assert (first / "compiled_skill" / "demo" / "SKILL.md").read_text() == "first"
    assert (second / "compiled_skill" / "demo" / "SKILL.md").read_text() == "second"


def test_find_compiled_skill_requires_exactly_one_current_output(tmp_path, monkeypatch):
    _point_pipeline_at(monkeypatch, tmp_path)
    first = tmp_path / "compiled_skill" / "one" / "SKILL.md"
    first.parent.mkdir(parents=True)
    first.write_text("# One", encoding="utf-8")
    assert rp.find_compiled_skill_md() == first

    second = tmp_path / "compiled_skill" / "two" / "SKILL.md"
    second.parent.mkdir(parents=True)
    second.write_text("# Two", encoding="utf-8")
    assert rp.find_compiled_skill_md() is None


def test_compiled_artifact_contract_requires_pair_and_consistent_confidence(tmp_path, monkeypatch):
    _point_pipeline_at(monkeypatch, tmp_path)
    out = tmp_path / "compiled_skill" / "demo"
    out.mkdir(parents=True)
    skill_md = out / "SKILL.md"
    skill_md.write_text("# Demo\n\n置信档：候选级\n", encoding="utf-8")

    assert any("EVALUATION.md" in error for error in rp.validate_compiled_artifacts())

    (out / "EVALUATION.md").write_text("# Evaluation\n", encoding="utf-8")
    assert rp.validate_compiled_artifacts() == []

    skill_md.write_text("# Demo\n\n置信档：生产级\n\n高严重度缺口仍存在\n", encoding="utf-8")
    assert any("高严重度" in error for error in rp.validate_compiled_artifacts())

    skill_md.write_text("# Demo\n\n置信档：生产级\n\n无高严重度缺口\n", encoding="utf-8")
    assert rp.validate_compiled_artifacts() == []


def test_final_artifact_contract_requires_valid_benchmark_pair(tmp_path, monkeypatch):
    _point_pipeline_at(monkeypatch, tmp_path)
    out = tmp_path / "compiled_skill" / "demo"
    out.mkdir(parents=True)
    (out / "SKILL.md").write_text("# Demo\n\n置信档：候选级\n", encoding="utf-8")
    (out / "EVALUATION.md").write_text("# Evaluation\n", encoding="utf-8")

    missing = rp.validate_final_artifacts()
    assert any("BENCHMARK.md" in error for error in missing)
    assert any("benchmark.json" in error for error in missing)

    (out / "BENCHMARK.md").write_text("# Benchmark\n", encoding="utf-8")
    bf.write_document(out / "benchmark.json", _progressive_benchmark())
    assert rp.validate_final_artifacts() == []


def test_final_benchmark_builder_targets_isolated_workspace(tmp_path, monkeypatch):
    _point_pipeline_at(monkeypatch, tmp_path)
    out = tmp_path / "compiled_skill" / "demo"
    out.mkdir(parents=True)
    (out / "SKILL.md").write_text("# Demo\n\n置信档：候选级\n", encoding="utf-8")
    (out / "EVALUATION.md").write_text("# Evaluation\n", encoding="utf-8")

    def fake_build(skill_dir, skill_name, hermes_env):
        assert skill_dir == out
        assert skill_name == "demo"
        assert hermes_env == {"TOKEN": "configured"}
        bf.write_document(out / "benchmark.json", _progressive_benchmark())
        (out / "BENCHMARK.md").write_text("# Benchmark\n", encoding="utf-8")
        return [{"id": "BM-01", "input": "测试情境"}]

    monkeypatch.setattr(rb, "build_phase", fake_build)

    assert rp.build_final_benchmark({"TOKEN": "configured"}) is True
    assert rb.PROJECT_ROOT == tmp_path
    assert rb.rst.PROJECT_ROOT == tmp_path


def test_pipeline_finalizer_builds_and_archives_benchmark_once(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        rp,
        "build_final_benchmark",
        lambda env: calls.append(("build", env)) or True,
    )
    monkeypatch.setattr(
        rp,
        "archive_round",
        lambda round_idx, session_tag: calls.append(("archive", round_idx, session_tag))
        or (tmp_path / "round_1"),
    )

    assert rp.finalize_pipeline_artifacts({"TOKEN": "configured"}, 1, "session-a") is True
    assert calls == [
        ("build", {"TOKEN": "configured"}),
        ("archive", 1, "session-a"),
    ]


def test_security_policy_is_injected_into_every_pipeline_prompt(tmp_path, monkeypatch):
    _point_pipeline_at(monkeypatch, tmp_path)
    prompt_file = tmp_path / "demo_prompt.py"
    prompt_file.write_text('PROMPT = "read {INPUT_DIR} write {OUTPUT_DIR}"\n', encoding="utf-8")
    info = {
        "module": "demo_prompt",
        "prompt_var": "PROMPT",
        "input_dir": "input",
        "output_dir": "output",
    }

    rendered = rp.get_prompt_with_paths(info)

    assert str(tmp_path / "input") in rendered
    assert str(tmp_path / "output") in rendered
    assert "{INPUT_DIR}" not in rendered
    assert "不可信输入安全边界" in rendered


def test_pipeline_prompt_override_is_loaded_from_whitebox_config(tmp_path, monkeypatch):
    _point_pipeline_at(monkeypatch, tmp_path)
    prompt_file = tmp_path / "sample_package_constructor_agent_prompt.py"
    prompt_file.write_text(
        'SAMPLE_PACKAGE_CONSTRUCTOR_AGENT_PROMPT = "DEFAULT {INPUT_DIR} {OUTPUT_DIR}"\n',
        encoding="utf-8",
    )
    config = tmp_path / "config.yaml"
    config.write_text(
        "mining:\n"
        "  prompts:\n"
        "    sample_package: 'CUSTOM {INPUT_DIR} {OUTPUT_DIR}'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEAMEVOLVER_CONFIG_FILE", str(config))

    rendered = rp.get_prompt_with_paths(
        {
            "module": "sample_package_constructor_agent_prompt",
            "prompt_var": "SAMPLE_PACKAGE_CONSTRUCTOR_AGENT_PROMPT",
            "input_dir": "input",
            "output_dir": "output",
        }
    )

    assert "CUSTOM" in rendered
    assert "DEFAULT" not in rendered


def test_mining_whitebox_settings_roundtrip():
    data = {
        "llm": {
            "provider": "custom",
            "model_id": "global-model",
            "api_base": "https://example.invalid/v1",
            "api_key": "secret",
        }
    }
    before = settings_payload(data)
    assert before["model"]["model"] == "global-model"
    assert len(before["prompts"]) == 10

    updated = update_settings(
        data,
        {
            "model": {
                "model": "mining-model",
                "base_url": "https://mining.invalid/v1",
                "max_tokens": 4096,
            },
            "pipeline": {
                "max_rounds": 7,
                "max_retries": 4,
                "strict_step1": False,
            },
            "prompts": [
                {"id": "semantic_discovery", "prompt": "CUSTOM SEMANTIC"}
            ],
        },
    )

    assert updated["model"]["model"] == "mining-model"
    assert updated["pipeline"]["max_rounds"] == 7
    assert updated["pipeline"]["strict_step1"] is False
    semantic = next(
        item for item in updated["prompts"]
        if item["id"] == "semantic_discovery"
    )
    assert semantic["effective_prompt"] == "CUSTOM SEMANTIC"
    assert semantic["overridden"] is True


def test_benchmark_prompt_override_preserves_dynamic_input(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text(
        "mining:\n"
        "  prompts:\n"
        "    benchmark_usage: 'CUSTOM TASK: {{question_input}}'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEAMEVOLVER_CONFIG_FILE", str(config))

    rendered = rb.usage_prompt_for({"input": "dynamic case"})

    assert rendered == "CUSTOM TASK: dynamic case"


def test_model_probe_requires_success_exit_and_expected_marker(tmp_path, monkeypatch):
    _point_pipeline_at(monkeypatch, tmp_path)
    monkeypatch.setattr(rp, "_HERMES_BIN", "/fake/hermes")

    monkeypatch.setattr(
        rp.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="HERMES_OK\n", stderr=""),
    )
    assert rp.test_model_connection({}) is True

    monkeypatch.setattr(
        rp.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="looks fine\n", stderr=""),
    )
    assert rp.test_model_connection({}) is False

    monkeypatch.setattr(
        rp.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="HERMES_OK\n", stderr="failed"),
    )
    assert rp.test_model_connection({}) is False


def _write_package_notes(packages: Path, names: list[str]) -> None:
    notes = packages / "package_notes"
    global_notes = packages / "global_notes"
    notes.mkdir(parents=True)
    global_notes.mkdir(parents=True)
    for name in names:
        (notes / f"{name}.md").write_text("体量判断：小簇单包透传", encoding="utf-8")
    for filename in rp.vsp.GLOBAL_NOTE_FILES:
        (global_notes / filename).write_text("已记录", encoding="utf-8")


def test_small_cluster_single_package_passthrough_is_allowed(tmp_path):
    input_dir = tmp_path / "data" / "input"
    packages = tmp_path / "sample_packages"
    package = packages / "样本包001"
    input_dir.mkdir(parents=True)
    package.mkdir(parents=True)
    content = "仓配客诉处理规则与边界案例。" * 2500
    (input_dir / "guide.md").write_text(content, encoding="utf-8")
    (package / "guide.md").write_text(content, encoding="utf-8")
    _write_package_notes(packages, ["样本包001"])

    report = rp.vsp.validate(input_dir, packages)

    assert report["metrics"]["small_cluster_passthrough"] is True
    assert report["hard"] == []


def test_oversized_single_package_full_copy_is_still_rejected(tmp_path):
    input_dir = tmp_path / "data" / "input"
    packages = tmp_path / "sample_packages"
    package = packages / "样本包001"
    input_dir.mkdir(parents=True)
    package.mkdir(parents=True)
    # Keep this fixture above the current 200,000-character downstream limit.
    content = "超出下游容量的领域规则。" * 20000
    (input_dir / "guide.md").write_text(content, encoding="utf-8")
    (package / "guide.md").write_text(content, encoding="utf-8")
    _write_package_notes(packages, ["样本包001"])

    report = rp.vsp.validate(input_dir, packages)

    assert report["metrics"]["small_cluster_passthrough"] is False
    assert any("接近整份复制" in error for error in report["hard"])
    assert any("超出单包容量" in error for error in report["hard"])


def test_benchmark_builder_limits_hermes_to_file_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(rb, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("SKILLMINER_LIFT_AUTO_DRAFT", "0")
    skill_dir = tmp_path / "compiled_skill" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Demo Skill", encoding="utf-8")
    (skill_dir / "EVALUATION.md").write_text("# Demo Evaluation", encoding="utf-8")
    captured = {}

    def fake_run_hermes(args, _env, timeout):
        captured["args"] = args
        captured["timeout"] = timeout
        (skill_dir / "benchmark_bank.json").write_text(
            '[{"id":"BM-01","target_dimensions":["维度一"],'
            '"difficulty":"easy","input":"测试情境",'
            '"gold":{"must_hit":['
            + ",".join(f'"可核验要求 {index}"' for index in range(1, 13))
            + ']},"trajectory_requirements":["先确认事实","完成后核验"]}]',
            encoding="utf-8",
        )
        return True, "done"

    monkeypatch.setattr(rb.rst, "run_hermes", fake_run_hermes)

    questions = rb.build_phase(skill_dir, "demo", {})

    assert len(questions) == 1
    assert captured["args"][:2] == ["-t", "file"]
    assert captured["args"][2] == "-z"
    assert "唯一允许的工具动作" in captured["args"][3]
    assert captured["timeout"] == rp.HERMES_ONESHOT_TIMEOUT
    payload = json.loads((skill_dir / "benchmark.json").read_text(encoding="utf-8"))
    assert payload["dataset_format"] == "teamEvolver-progressive-test-v1"
    assert payload["datasets"][0]["requirement_count"] == 12
    assert payload["datasets"][0]["checklist"][0]["id"] == "R01"
    assert not (skill_dir / "benchmark.jsonl").exists()
    assert not (skill_dir / "benchmark_bank.json").exists()
