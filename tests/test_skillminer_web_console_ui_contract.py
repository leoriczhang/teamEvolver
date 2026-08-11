from __future__ import annotations

import base64
import sys
from pathlib import Path

SKILLMINER_ROOT = Path(__file__).resolve().parents[1] / "teamEvolver" / "skillminer"
if str(SKILLMINER_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILLMINER_ROOT))

from web_console import server  # noqa: E402


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
