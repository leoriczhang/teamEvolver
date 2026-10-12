"""Regressions for the compiler contract that OV receives, without model or OV calls."""

import hashlib
import json
from copy import deepcopy

import pytest

from team_miner import benchmark_format
from team_miner.compile_strategy import (
    COMPILE_CONTRACT,
    COMPILE_PLAN,
    compiler_execution_guidance,
    multi_compiler_execution_guidance,
)
from team_miner.output_language import OUTPUT_LANGUAGE_GUIDANCE
from team_miner.ov_compile_mining import LEGACY_SKILL_BODY, SKILL_BODY, OvSkillMiningRepository
from team_miner.ov_round_orchestration import _compiler_package
from team_miner.settings import compiler_prompt
from team_miner.tests.dataset_fixtures import build_document
from team_skills.library.frontmatter import parse_skill_md_text


def embedded_contract(body):
    """Read the JSON actually embedded in the Skill, rather than a separate fixture."""
    return json.loads(body.split("```json\n", 1)[1].split("\n```", 1)[0])


def test_collection_reduce_cannot_request_ready_drafts_or_complete_files():
    contract = embedded_contract(SKILL_BODY)
    reduce = contract["reduce"]

    assert reduce["output"] == "records" and reduce["execution"] == "direct"
    assert contract["synthesize"]["output"] == "files"
    # Native Record supports these shortcuts even when business fields do not.
    # Cover the exact fields seen in the truncated real Reduce attempts.
    instructions = reduce["instructions"]
    for field in ("ready_content", "ready_content_ref", "ready_path"):
        assert field in instructions
        assert field not in reduce["fields"]
    assert "Omit ALL ready_* fields" in instructions
    assert "final content anywhere in a record, including fact_digest" in instructions
    assert "8000 output tokens for the ENTIRE Reduce emit" in instructions


def test_collection_planning_fields_are_scoped_instead_of_repeated_in_every_record():
    reduce = embedded_contract(SKILL_BODY)["reduce"]
    instructions, fields = reduce["instructions"], reduce["fields"]

    assert "Only the plan assignment has capability_plan" in instructions
    assert "exactly once" in instructions
    assert "Only cases_json and cases_markdown have case_set" in instructions
    assert "All other assignments MUST omit case_set and case_blueprint" in instructions
    assert "ONLY case_id and a short scenario of at most 48 characters" in instructions
    assert "Do not include conditions, source_refs, query, checks or requirements in case_blueprint" in instructions
    assert "omit everywhere else" in fields["case_blueprint"]
    assert "Only name, task and boundary" in fields["capability"]
    assert "160 characters" in fields["fact_digest"]
    assert "240 characters" in fields["capability"]


def test_compact_dispatch_preserves_candidate_boundaries_and_original_evidence():
    contract = embedded_contract(SKILL_BODY)
    instructions = contract["reduce"]["instructions"]

    assert "Choose 0-8 Skill Candidates" in instructions
    assert "four shared analysis work records" in instructions
    assert "plus four work records per selected Skill" in instructions
    assert "Empty skills requires a nonempty deferred list" in instructions
    assert "all and only extraction IDs supporting its facts in top-level inputs" in instructions
    assert "Never omit supporting inputs" in instructions
    assert "retain all chosen candidates, required assignments, source_refs and supporting input IDs" in instructions
    assert "Recover exact scenario conditions, exceptions, source URIs and full checks" in contract["synthesize"][
        "instructions"
    ]
    assert contract["routing"] == {"mode": "all"}
    assert COMPILE_PLAN in SKILL_BODY


def test_collection_guidance_does_not_mutate_saved_single_candidate_contract():
    before = json.dumps(COMPILE_CONTRACT, sort_keys=True)
    legacy = compiler_execution_guidance()

    multi_compiler_execution_guidance()
    multi_compiler_execution_guidance()

    assert json.dumps(COMPILE_CONTRACT, sort_keys=True) == before
    assert compiler_execution_guidance() == legacy
    assert "exactly seven compact work records" in embedded_contract(legacy)["reduce"]["instructions"]
    assert "capability_plan" not in embedded_contract(legacy)["reduce"]["fields"]


def test_new_job_freezes_the_same_bounded_contract_in_its_immutable_compiler_package(tmp_path):
    repository = OvSkillMiningRepository(tmp_path / "state", tmp_path / "miner")
    job = repository.create_job(
        name="Bounded capability planning",
        account="default",
        source_uri="viking://resources/team/source",
        source_snapshot_uri="viking://resources/test/source",
        output_uri="viking://resources/test/output",
        document_count=2,
        max_rounds=2,
        compiler_body=SKILL_BODY,
    )
    frozen = (repository.jobs_root / job["job_id"] / "compiler-skill.md").read_text(encoding="utf-8")
    name, published, source_hash = _compiler_package(frozen)

    assert job["candidate_contract_version"] == 2
    assert frozen == SKILL_BODY
    assert source_hash == hashlib.sha256(SKILL_BODY.encode("utf-8")).hexdigest()
    assert name.endswith(source_hash[:32])
    assert embedded_contract(published) == embedded_contract(SKILL_BODY)


@pytest.mark.parametrize("body", [LEGACY_SKILL_BODY, SKILL_BODY], ids=["single", "collection"])
def test_output_language_guidance_reaches_every_embedded_compile_stage(body):
    contract = embedded_contract(body)

    assert "## Output language\n\n" + OUTPUT_LANGUAGE_GUIDANCE in body
    for stage in ("extract", "reduce", "synthesize"):
        assert contract[stage]["instructions"].count(OUTPUT_LANGUAGE_GUIDANCE) == 1
    assert contract["extract"]["output"] == "records"
    assert contract["reduce"]["output"] == "records"
    assert contract["synthesize"]["output"] == "files"


def test_language_defaults_do_not_rewrite_saved_or_frozen_custom_compiler(tmp_path):
    custom = (
        "---\nname: saved-compiler\ndescription: Existing custom compiler.\n---\n\n"
        "Produce one Skill Candidate. Keep this saved presentation instruction in English.\n"
    )
    settings = {"mining": {"prompts": {"ov_compile": custom}}}
    original_settings = deepcopy(settings)
    source_hash = hashlib.sha256(custom.encode("utf-8")).hexdigest()
    repository = OvSkillMiningRepository(tmp_path / "state", tmp_path / "miner")
    job = repository.create_job(
        name="Saved compiler language",
        account="default",
        source_uri="viking://resources/team/source",
        source_snapshot_uri="viking://resources/test/source",
        output_uri="viking://resources/test/output",
        document_count=1,
        max_rounds=2,
        candidate_contract_version=1,
        compiler_body=compiler_prompt(settings),
    )

    assert settings == original_settings and compiler_prompt(settings) == custom
    settings["mining"]["prompts"]["ov_compile"] = SKILL_BODY
    frozen = (repository.jobs_root / job["job_id"] / "compiler-skill.md").read_text(encoding="utf-8")
    name, published, frozen_hash = _compiler_package(frozen)

    assert frozen == custom and frozen_hash == source_hash
    assert name.endswith(source_hash[:32])
    assert published.split("---", 2)[2].strip() == custom.split("---", 2)[2].strip()
    assert OUTPUT_LANGUAGE_GUIDANCE not in frozen


def test_chinese_natural_language_preserves_the_existing_skill_and_dataset_schema(tmp_path):
    skill = "return-check"
    raw_skill = "---\nname: return-check\ndescription: 核验退货申请并说明证据缺口。\n---\n\n# 退货核验\n"
    assert parse_skill_md_text(raw_skill) == {
        "name": skill,
        "description": "核验退货申请并说明证据缺口。",
    }
    document = build_document(skill, [{
        "id": "case-01",
        "name": "有效退货申请",
        "query": "请核验 TEST-RETURN 的退货申请。",
        "requirements": [f"核对第 {index} 项来源支持的退货条件。" for index in range(1, 13)],
        "trajectory_requirements": ["核对提供的 Evidence，发现缺口时向用户说明。"],
        "source_session_ids": ["viking://resources/team/source/returns.md"],
        "materials": [{
            "path": "order-note.md", "name": "原始订单说明", "content": "商品未拆封，原文编号 TEST-RETURN。",
        }],
    }])
    document["name"] = "退货核验 Test Dataset"
    document["description"] = "中文案例和 Checklist，协议标识保持原样。"
    path = tmp_path / "benchmark.json"

    benchmark_format.write_document(path, document)
    normalized, errors = benchmark_format.read_document(path)

    assert errors == [] and normalized is not None
    assert normalized["name"] == document["name"]
    assert normalized["description"] == document["description"]
    case = normalized["cases"][0]
    assert case["query"] == document["cases"][0]["query"]
    assert case["checks"] == document["cases"][0]["checks"]
    assert case["materials"] == document["cases"][0]["materials"]
    assert case["materials"][0]["content"] == "商品未拆封，原文编号 TEST-RETURN。"
    assert normalized["schema_version"] == "team-replay.dataset"
    assert case["case_id"] == "case-01" and case["skill_ids"] == [skill]
    assert case["checks"][0]["kind"] == "output" and case["metadata"]["split"] == "test"
    assert case["provenance"]["session_ids"] == ["viking://resources/team/source/returns.md"]


def test_round_archive_records_the_frozen_output_language_policy(tmp_path):
    repository = OvSkillMiningRepository(tmp_path / "state", tmp_path / "miner")
    job = repository.create_job(
        name="Language audit",
        account="default",
        source_uri="viking://resources/team/source",
        source_snapshot_uri="viking://resources/test/source",
        output_uri="viking://resources/test/output",
        document_count=1,
        max_rounds=2,
        compiler_body=SKILL_BODY,
    )
    policy = {"version": 1, "basis": "frozen_original_source", "language": "zh-CN"}
    job = repository.update_job(job["job_id"], output_language_policy=policy)

    archived = repository._archive_round(job)
    destination = repository.project_root / archived["path"]
    repository.update_job(job["job_id"], output_language_policy={**policy, "language": "en"})

    assert archived["output_language_policy"] == policy
    assert json.loads((destination / "input.json").read_text())["output_language_policy"] == policy
    assert json.loads((destination / "meta.json").read_text())["output_language_policy"] == policy
