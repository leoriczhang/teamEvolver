from __future__ import annotations

from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _source(relative_path: str) -> str:
    return (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")


def test_mining_ui_has_no_separate_model_configuration_entry():
    app_source = _source("web-ui/src/App.tsx")
    mining_source = _source("team_miner/frontend/MiningView.tsx")
    assert '"mine-model"' not in app_source
    assert 'page === "model"' not in mining_source
    assert '"/api/mining/model"' not in mining_source


def test_mining_task_identity_artifacts_and_errors_are_selectable_for_copying():
    mining_source = _source("team_miner/frontend/MiningView.tsx")
    toaster_source = _source("web-ui/src/components/ui/sonner.tsx")
    assert "select-text" in mining_source
    assert "job.job_id}</span>" in mining_source
    assert "job.error || job.stop_reason" in mining_source
    assert "title: 'select-text cursor-text'" in toaster_source
    assert "description: 'select-text cursor-text whitespace-pre-wrap break-words'" in toaster_source


def test_mining_ui_surfaces_artifact_quality_and_confirms_cautious_submission():
    mining_source = _source("team_miner/frontend/MiningView.tsx")
    assert "artifact_quality?: MiningArtifactQuality | null" in mining_source
    assert "产物质量" in mining_source
    assert "谨慎提交到进化" in mining_source
    assert "window.confirm" in mining_source
    assert "/api/mined-jobs/" in mining_source


def test_mining_task_deletion_contract_is_available_in_api_and_ui():
    mining_source = _source("team_miner/frontend/MiningView.tsx")
    bridge_source = _source("team_miner/bridge.py")
    assert 'method: "DELETE"' in mining_source
    assert "删除挖掘任务" in mining_source
    assert "全部 Skill、语义报告和 Test Dataset 产物" in mining_source
    assert '@app.delete("/api/mining/jobs/{job_id}")' in bridge_source


def test_new_mining_task_preserves_round_selection_and_checkpoint_choice():
    mining_source = _source("team_miner/frontend/MiningView.tsx")
    assert "const [humanCheckpoints, setHumanCheckpoints] = useState(true);" in mining_source
    assert "human_checkpoints: humanCheckpoints" in mining_source
    assert "挖掘轮数上限" in mining_source
    assert "/supplements" in mining_source
    assert 'action: "continue" | "finish"' in mining_source


def test_mining_sources_use_the_ov_workspace_and_current_api():
    mining_source = _source("team_miner/frontend/MiningView.tsx")
    workspace = _source("team_miner/frontend/SkillSourceWorkspace.tsx")
    assert "<SkillSourceWorkspace" in mining_source
    assert "/api/mining/ov-source-upload" in workspace
    assert "/api/mining/ov-source-content" in workspace
    assert "/api/mining/ov-source-tree" in workspace


def test_mining_overview_exposes_totals_and_parallel_job_pipelines():
    mining_source = _source("team_miner/frontend/MiningView.tsx")
    overview = mining_source.split("{/* ---- 总览 ---- */}", 1)[1].split("{/* ---- 知识源 ---- */}", 1)[0]
    assert 'label="知识源"' in overview
    assert 'label="文档总数"' in overview
    assert 'title="任务流水线"' in overview
    assert "pipelineJobs.map" in overview
    assert "job.phase?.[step.key]" in overview
    assert '<Panel title="近期任务">' in overview
    assert "mining.jobs.slice(0, recentTaskCount)" in overview
