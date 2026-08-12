from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

SKILLMINER_DIR = Path(__file__).resolve().parents[1] / "teamEvolver" / "skillminer"
sys.path.insert(0, str(SKILLMINER_DIR))

import run_benchmark as rb  # noqa: E402
import run_pipeline as rp  # noqa: E402


def _point_pipeline_at(monkeypatch, root: Path) -> None:
    monkeypatch.setattr(rp, "PROJECT_ROOT", root)
    monkeypatch.setattr(rp, "RUN_HISTORY_DIR", root / "run_history")


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
    assert (task_home / "config.yaml").read_text(encoding="utf-8") == template.read_text(encoding="utf-8")

    base_home.mkdir(parents=True)
    (base_home / "config.yaml").write_text("model:\n  default: configured-model\n", encoding="utf-8")
    second_task = tmp_path / "second-task" / ".hermes_home"
    monkeypatch.setattr(rp, "HERMES_HOME", second_task)
    assert rp.ensure_hermes_home() is True
    assert "configured-model" in (second_task / "config.yaml").read_text(encoding="utf-8")


def test_hermes_environment_prefers_project_config_over_global_overrides(tmp_path, monkeypatch):
    monkeypatch.setattr(rp, "HERMES_HOME", tmp_path / ".hermes_home")
    monkeypatch.setattr(rp, "resolve_ark_key", lambda: "")
    monkeypatch.setenv("HERMES_HOME", "/global/hermes")
    monkeypatch.setenv("HERMES_INFERENCE_MODEL", "global-model")
    monkeypatch.setenv("HERMES_IGNORE_USER_CONFIG", "1")

    env, has_key = rp.build_hermes_env()

    assert env["HERMES_HOME"] == str(tmp_path / ".hermes_home")
    assert "HERMES_INFERENCE_MODEL" not in env
    assert "HERMES_IGNORE_USER_CONFIG" not in env
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
    assert any("benchmark.jsonl" in error for error in missing)

    (out / "BENCHMARK.md").write_text("# Benchmark\n", encoding="utf-8")
    (out / "benchmark.jsonl").write_text(
        '{"id":"BM-01","input":"测试情境"}\n',
        encoding="utf-8",
    )
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
        (out / "benchmark.jsonl").write_text(
            '{"id":"BM-01","input":"测试情境"}\n', encoding="utf-8"
        )
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
            '"gold":{"must_hit":["正确处理"]}}]',
            encoding="utf-8",
        )
        return True, "done"

    monkeypatch.setattr(rb.rst, "run_hermes", fake_run_hermes)

    questions = rb.build_phase(skill_dir, "demo", {})

    assert len(questions) == 1
    assert captured["args"][:2] == ["-t", "file"]
    assert captured["args"][2] == "-z"
    assert "唯一允许的工具动作" in captured["args"][3]
    assert captured["timeout"] == 1500
