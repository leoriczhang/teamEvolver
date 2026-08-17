from __future__ import annotations

import base64
import io
import json
import tarfile
from pathlib import Path

import pytest

from teamEvolver.dataset_synthesizer import synthesize_evolution_datasets
from teamEvolver.evolve.kernel.settings import EvolveServerConfig
from teamEvolver.evolve.runtime.orchestrator import EvolveServer
from teamEvolver.session_materials import collect_session_materials


def _snapshot(tmp_path: Path, session_id: str) -> Path:
    root = tmp_path / "session_snapshots" / "tenant_demo" / session_id
    root.mkdir(parents=True)
    archive = root / "snapshot.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for name, data in (
            ("uploads/input.csv", b"name,value\nA,1\n"),
            ("artifacts/output.html", b"<html></html>"),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return archive


def _session(tmp_path: Path, session_id: str = "session-source") -> dict:
    return {
        "session_id": session_id,
        "runtime": {"type": "agentshub"},
        "runtime_context": {
            "tenant_id": "tenant_demo",
            "sandbox_snapshot_path": str(_snapshot(tmp_path, session_id)),
        },
        "turns": [
            {
                "turn_num": 1,
                "prompt_text": "读取 uploads/input.csv 并完成分析。",
                "response_text": "已完成。",
            }
        ],
    }


def test_collect_session_materials_reads_only_snapshot_uploads(
    tmp_path: Path,
) -> None:
    materials = collect_session_materials([_session(tmp_path)])

    assert [item["path"] for item in materials] == ["uploads/input.csv"]
    assert base64.b64decode(materials[0]["content_b64"]) == (
        b"name,value\nA,1\n"
    )


@pytest.mark.anyio
async def test_synthesized_dataset_carries_real_source_upload(
    tmp_path: Path,
) -> None:
    class FakeLLM:
        async def chat(self, *_args, **_kwargs):
            return json.dumps(
                {
                    "test_datasets": [
                        {
                            "name": "CSV analysis",
                            "query": "读取 uploads/input.csv 并输出分析报告。",
                            "requirements": [
                                f"要求 {index}" for index in range(1, 13)
                            ],
                            "trajectory_requirements": [
                                "读取 uploads/input.csv",
                            ],
                            "source_session_ids": ["session-source"],
                        }
                    ]
                },
                ensure_ascii=False,
            )

    datasets = await synthesize_evolution_datasets(
        FakeLLM(),
        skill_name="demo-skill",
        sessions=[_session(tmp_path)],
        candidate_skill={"description": "Demo", "content": "Read input."},
        case_count=1,
    )

    assert len(datasets) == 1
    assert datasets[0]["materials"][0]["path"] == "uploads/input.csv"
    assert datasets[0]["materials"][0]["source_session_id"] == "session-source"


def test_evolve_archive_preserves_runtime_snapshot_and_materials(
    tmp_path: Path,
) -> None:
    server = EvolveServer(
        EvolveServerConfig(llm_api_key="test"),
        mock=True,
        mock_root=str(tmp_path),
    )
    session = _session(tmp_path)
    session["user_alias"] = "tester"
    session["source"] = "agentshub"
    session["source_materials"] = [
        {
            "path": "uploads/input.csv",
            "content_b64": base64.b64encode(b"a,b\n1,2\n").decode("ascii"),
        }
    ]

    normalized = server._normalize_ingest_session(session)
    server._archive_sessions([normalized])
    archived = json.loads(
        server._bucket.get_object(
            server._archive_key("session-source")
        ).read()
    )

    assert archived["runtime"]["type"] == "agentshub"
    assert archived["runtime_context"]["sandbox_snapshot_path"].endswith(
        "snapshot.tar.gz"
    )
    assert archived["source_materials"][0]["path"] == "uploads/input.csv"
