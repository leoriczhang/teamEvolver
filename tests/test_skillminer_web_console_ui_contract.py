from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest
import yaml

SKILLMINER_ROOT = Path(__file__).resolve().parents[1] / "teamEvolver" / "skillminer"
if str(SKILLMINER_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILLMINER_ROOT))

from web_console import server  # noqa: E402


def test_mining_model_settings_can_be_saved_without_returning_secret(tmp_path, monkeypatch):
    config_path = tmp_path / ".hermes_home" / "config.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "model:\n  default: old-model\n  provider: custom\n  base_url: https://old.example/v1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(server, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(server.rp, "resolve_ark_key", lambda: "")

    saved = server.save_mining_model_settings({
        "model": "new-model",
        "base_url": "https://model.example/v1/",
        "max_tokens": 4096,
        "temperature": 0.3,
        "api_key": "test-secret-key",
    })

    assert saved == {
        "provider": "openai-compatible",
        "id": "new-model",
        "model": "new-model",
        "base_url": "https://model.example/v1",
        "max_tokens": 4096,
        "temperature": 0.3,
        "api_key_present": True,
        "configured": True,
    }
    assert "api_key" not in saved
    persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert persisted["model"]["default"] == "new-model"
    assert persisted["model"]["provider"] == "custom"
    assert persisted["model"]["api_mode"] == "chat_completions"
    assert persisted["model"]["api_key"] == "test-secret-key"
    assert config_path.stat().st_mode & 0o777 == 0o600


def test_saving_mining_model_settings_drops_legacy_evolution_hooks(tmp_path, monkeypatch):
    config_path = tmp_path / ".hermes_home" / "config.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        """
model:
  default: old-model
  base_url: https://old.example/v1
hooks:
  on_session_end:
    - command: python push_session.py
skills:
  external_dirs:
    - /Users/example/.hermes/team_skills/skillgene
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(server, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(server.rp, "resolve_ark_key", lambda: "")

    server.save_mining_model_settings({
        "model": "new-model",
        "base_url": "https://model.example/v1",
        "max_tokens": 4096,
        "temperature": 0.2,
    })

    persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert "hooks" not in persisted
    assert persisted["hooks_auto_accept"] is False
    assert "skills" not in persisted


def test_mining_model_test_uses_current_form_values(tmp_path, monkeypatch):
    config_path = tmp_path / ".hermes_home" / "config.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "model:\n  default: saved-model\n  provider: custom\n  base_url: https://saved.example/v1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(server, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(server.rp, "resolve_ark_key", lambda: "")
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return json.dumps({"choices": [{"message": {"content": "OK"}}]}).encode()

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["authorization"] = request.get_header("Authorization")
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(server.urllib.request, "urlopen", fake_urlopen)
    result = server.test_mining_model_settings({
        "model": "edited-model",
        "base_url": "https://edited.example/v1",
        "api_key": "edited-secret-key",
        "max_tokens": 8192,
        "temperature": 0.1,
    })

    assert result["ok"] is True
    assert result["model"] == "edited-model"
    assert result["response"] == "OK"
    assert captured["url"] == "https://edited.example/v1/chat/completions"
    assert captured["authorization"] == "Bearer edited-secret-key"
    assert captured["payload"]["model"] == "edited-model"
    assert captured["timeout"] == 60


def test_config_schema_reports_real_source_and_artifact_readiness(tmp_path, monkeypatch):
    input_dir = tmp_path / "data" / "input"
    input_dir.mkdir(parents=True)
    (input_dir / ".gitkeep").write_text("", encoding="utf-8")
    (input_dir / "guide.md").write_text("usable knowledge", encoding="utf-8")

    skill_dir = tmp_path / "compiled_skill" / "support-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Skill", encoding="utf-8")
    (skill_dir / "EVALUATION.md").write_text("# Evaluation", encoding="utf-8")
    (skill_dir / "benchmark.jsonl").write_text('{"id":"q1"}\n{"id":"q2"}\n', encoding="utf-8")

    monkeypatch.setattr(server, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(server.li, "PROJECT_ROOT", tmp_path)

    schema = server.build_config_schema()

    assert schema["default_input_dir"] == "data/input"
    assert schema["input_sources"] == [{
        "path": "data/input",
        "document_count": 1,
        "total_bytes": len("usable knowledge"),
        "ready": True,
    }]
    assert schema["compiled_skill_details"] == [{
        "name": "support-skill",
        "has_skill": True,
        "has_evaluation": True,
        "has_benchmark": True,
        "question_count": 2,
    }]


def test_mining_history_merges_rounds_with_next_run_preexisting_snapshot(tmp_path, monkeypatch):
    round_skill = (
        tmp_path / "reflection_rounds" / "20260805_150618_777997" /
        "round_1" / "compiled_skill" / "support-skill"
    )
    round_skill.mkdir(parents=True)
    (round_skill / "SKILL.md").write_text("# Skill", encoding="utf-8")
    (round_skill / "EVALUATION.md").write_text("# Evaluation", encoding="utf-8")

    final_skill = (
        tmp_path / "run_history" / "20260809_223901_916214" / "preexisting" /
        "compiled_skill" / "support-skill"
    )
    final_skill.mkdir(parents=True)
    (final_skill / "SKILL.md").write_text("# Final Skill", encoding="utf-8")
    (final_skill / "EVALUATION.md").write_text("# Final Evaluation", encoding="utf-8")
    (final_skill / "benchmark.jsonl").write_text('{"id":"q1"}\n{"id":"q2"}\n', encoding="utf-8")
    report = (
        tmp_path / "run_history" / "20260809_223901_916214" / "preexisting" /
        "semantic_reports" / "report.md"
    )
    report.parent.mkdir(parents=True)
    report.write_text("# Report", encoding="utf-8")

    monkeypatch.setattr(server, "PROJECT_ROOT", tmp_path)
    runs = server.list_mining_runs()

    assert len(runs) == 1
    assert runs[0]["run_id"] == "20260805_150618_777997"
    assert runs[0]["started_at"] == "2026-08-05T15:06:18"
    assert runs[0]["rounds"][0]["round"] == 1
    assert runs[0]["skills"] == [{
        "name": "support-skill",
        "has_skill": True,
        "has_evaluation": True,
        "has_benchmark": True,
        "question_count": 2,
    }]
    assert any(item["kind"] == "semantic" for item in runs[0]["final_artifacts"])


def test_history_artifact_reader_is_path_safe(tmp_path, monkeypatch):
    artifact = tmp_path / "reflection_rounds" / "run-1" / "round_1" / "compiled_skill" / "x" / "SKILL.md"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("# Safe", encoding="utf-8")
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    monkeypatch.setattr(server, "PROJECT_ROOT", tmp_path)

    result = server.read_history_artifact(
        "reflection_rounds/run-1/round_1/compiled_skill/x/SKILL.md"
    )
    assert result["content"] == "# Safe"

    try:
        server.read_history_artifact("secret.txt")
    except ValueError:
        pass
    else:
        raise AssertionError("artifact reader allowed a path outside history roots")


def test_empty_input_is_rejected_before_worker_start(tmp_path, monkeypatch):
    input_dir = tmp_path / "data" / "empty"
    input_dir.mkdir(parents=True)
    (input_dir / ".gitkeep").write_text("", encoding="utf-8")
    monkeypatch.setattr(server, "PROJECT_ROOT", tmp_path)

    manager = server.RunManager()
    ok, message = manager.start({"input_dir": "data/empty"})

    assert ok is False
    assert "没有可用于挖掘的文档" in message
    assert manager.thread is None
    assert manager.state == "idle"


def test_compiled_skill_selection_is_exact_and_path_safe(tmp_path, monkeypatch):
    skill_dir = tmp_path / "compiled_skill" / "chosen-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Skill", encoding="utf-8")
    monkeypatch.setattr(server, "PROJECT_ROOT", tmp_path)

    assert server._find_compiled_skill("chosen-skill") == skill_dir.resolve()
    assert server._find_compiled_skill("../chosen-skill") is None
    assert server._find_compiled_skill("missing") is None


def test_upload_knowledge_writes_utf8_and_preserves_existing_file(tmp_path, monkeypatch):
    input_dir = tmp_path / "data" / "input"
    input_dir.mkdir(parents=True)
    (input_dir / "guide.md").write_text("existing", encoding="utf-8")
    monkeypatch.setattr(server, "PROJECT_ROOT", tmp_path)

    uploaded = "新的领域知识".encode("gb18030")
    result = server.save_uploaded_knowledge({
        "source_path": "data/input",
        "files": [{
            "name": "guide.md",
            "content_b64": base64.b64encode(uploaded).decode("ascii"),
        }],
    })

    assert result["ok"] is True
    assert result["written"] == [{
        "name": "guide-2.md",
        "path": "data/input/guide-2.md",
        "size_bytes": len(uploaded),
        "renamed": True,
        "source_encoding": "gb18030",
    }]
    assert (input_dir / "guide.md").read_text(encoding="utf-8") == "existing"
    assert (input_dir / "guide-2.md").read_text(encoding="utf-8") == "新的领域知识"
    assert result["source"]["document_count"] == 2


def test_upload_knowledge_rejects_unsafe_or_binary_inputs(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "PROJECT_ROOT", tmp_path)
    payload = base64.b64encode(b"safe text").decode("ascii")

    for source_path, filename in [
        ("../outside", "guide.md"),
        ("data/input", "../guide.md"),
        ("data/input", "guide.pdf"),
    ]:
        try:
            server.save_uploaded_knowledge({
                "source_path": source_path,
                "files": [{"name": filename, "content_b64": payload}],
            })
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe upload was accepted: {source_path} / {filename}")


def test_knowledge_sources_can_be_created_merged_and_deleted(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "PROJECT_ROOT", tmp_path)

    created = server.create_knowledge_source({"name": "abc"})
    assert created["source"]["path"] == "data/abc"
    assert created["source"]["document_count"] == 0

    source_a = tmp_path / "data" / "source-a"
    source_b = tmp_path / "data" / "source-b"
    source_a.mkdir(parents=True)
    source_b.mkdir(parents=True)
    (source_a / "guide.md").write_text("A", encoding="utf-8")
    (source_b / "guide.md").write_text("B", encoding="utf-8")
    (source_b / "nested").mkdir()
    (source_b / "nested" / "faq.txt").write_text("FAQ", encoding="utf-8")

    merged = server.merge_knowledge_sources({
        "source_paths": ["data/source-a", "data/source-b"],
        "target_name": "combined",
    })

    assert merged["source"]["path"] == "data/combined"
    assert merged["source"]["document_count"] == 3
    assert (tmp_path / "data" / "combined" / "guide.md").read_text(encoding="utf-8") == "A"
    assert (tmp_path / "data" / "combined" / "guide-2.md").read_text(encoding="utf-8") == "B"
    assert (tmp_path / "data" / "combined" / "nested" / "faq.txt").is_file()
    assert (source_a / "guide.md").is_file()
    assert (source_b / "guide.md").is_file()

    deleted = server.delete_knowledge_source("abc")
    assert deleted["deleted"]["path"] == "data/abc"
    assert not (tmp_path / "data" / "abc").exists()


def test_job_artifact_reader_does_not_expose_snapshot_or_metadata(tmp_path, monkeypatch):
    artifact = tmp_path / "mining_jobs" / "job-1" / "workspace" / "compiled_skill" / "demo" / "SKILL.md"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("# Skill", encoding="utf-8")
    metadata = tmp_path / "mining_jobs" / "job-1" / "job.json"
    metadata.write_text('{"secret": "not-an-artifact"}', encoding="utf-8")
    snapshot = tmp_path / "mining_jobs" / "job-1" / "workspace" / "data" / "input" / "private.md"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text("private source", encoding="utf-8")
    monkeypatch.setattr(server, "PROJECT_ROOT", tmp_path)

    assert server.read_history_artifact(
        "mining_jobs/job-1/workspace/compiled_skill/demo/SKILL.md"
    )["content"] == "# Skill"
    for forbidden in (
        "mining_jobs/job-1/job.json",
        "mining_jobs/job-1/workspace/data/input/private.md",
    ):
        try:
            server.read_history_artifact(forbidden)
        except ValueError:
            pass
        else:
            raise AssertionError(f"non-artifact job file was exposed: {forbidden}")


def test_completed_job_artifact_can_be_human_edited(tmp_path, monkeypatch):
    job_dir = tmp_path / "mining_jobs" / "job-1"
    artifact = job_dir / "workspace" / "compiled_skill" / "demo" / "SKILL.md"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("# Before", encoding="utf-8")
    (job_dir / "job.json").write_text(
        json.dumps({"job_id": "job-1", "status": "succeeded"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(server, "PROJECT_ROOT", tmp_path)

    result = server.save_history_artifact(
        "mining_jobs/job-1/workspace/compiled_skill/demo/SKILL.md",
        "# After\n\nHuman refined content.\n",
    )

    assert result["edited"] is True
    assert result["content"].startswith("# After")
    assert artifact.read_text(encoding="utf-8") == result["content"]


def test_active_job_artifact_cannot_be_edited(tmp_path, monkeypatch):
    job_dir = tmp_path / "mining_jobs" / "job-1"
    artifact = job_dir / "workspace" / "compiled_skill" / "demo" / "SKILL.md"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("# Original", encoding="utf-8")
    (job_dir / "job.json").write_text(
        json.dumps({"job_id": "job-1", "status": "running"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(server, "PROJECT_ROOT", tmp_path)

    with pytest.raises(ValueError, match="已完成"):
        server.save_history_artifact(
            "mining_jobs/job-1/workspace/compiled_skill/demo/SKILL.md",
            "# Unauthorized edit",
        )
    assert artifact.read_text(encoding="utf-8") == "# Original"


def test_gap_questions_are_concrete_form_items(tmp_path):
    skill = tmp_path / "SKILL.md"
    skill.write_text(
        "## 维度一：时效判定\n"
        "- **高严重度缺口**：升级处理的时效指标缺失——请问超过多少小时必须升级？（来源：SOP 第 3 节）\n"
        "- **中严重度缺口**：特殊订单的判定标准未明确（来源：规则表）\n",
        encoding="utf-8",
    )

    questions = server.extract_gap_questions_from_skill(skill)

    assert len(questions) == 2
    assert questions[0]["question"] == "请问超过多少小时必须升级？"
    assert questions[0]["answer_type"] == "short_text"
    assert "单位" in questions[0]["field_label"]
    assert questions[0]["context"].startswith("升级处理的时效指标缺失")
    assert questions[0]["source"] == "SOP 第 3 节"
    assert questions[1]["question"].startswith("请明确“特殊订单的判定标准”")
    assert questions[1]["answer_type"] == "long_text"
