"""Regressions for the compiler contract that OV receives, without model or OV calls."""

import ast
import hashlib
import json
from copy import deepcopy

import pytest

from team_miner.skill_miner import benchmark_format
from team_miner.skill_miner.compile_strategy import (
    COMPILE_CONTRACT,
    COMPILE_PLAN,
    DEFAULT_MINING_POLICY,
    MINING_POLICY_SCHEMA,
    MULTI_COMPILE_PLAN,
    STAGE_RULES,
    TE_STAGED_COMPILE,
    compiler_business_policy,
    compiler_execution_guidance,
    mining_policy_block,
    multi_compile_contract,
    multi_compiler_execution_guidance,
    render_single_stage_compiler,
    stage_compiler_body,
)
from team_miner.skill_miner.output_language import OUTPUT_LANGUAGE_GUIDANCE
from team_miner.skill_miner.ov_compile_mining import LEGACY_SKILL_BODY, SKILL_BODY, OvSkillMiningRepository
from team_miner.skill_miner.ov_round_orchestration import _compiler_package
from team_miner.skill_miner.settings import compiler_prompt
from team_skills.library.frontmatter import parse_skill_md_text
from tests.miner.skill_miner.fixtures import MemoryMetadataBackend, build_document


def embedded_contract(body):
    """Read the JSON actually embedded in a child Skill or frozen legacy Skill."""
    return json.loads(body.split("```json\n", 1)[1].split("\n```", 1)[0])


def test_collection_defaults_use_te_staging_without_openviking_runtime_extensions():
    assert TE_STAGED_COMPILE in SKILL_BODY
    assert TE_STAGED_COMPILE not in LEGACY_SKILL_BODY
    assert "zero when evidence is insufficient" in SKILL_BODY
    for unsupported in ("p.expand", "input_limits", "record_limits", "ov-compile-plan-v1", "evidence_ref"):
        assert unsupported not in SKILL_BODY
    assert "twelve valid independent case outputs" in SKILL_BODY
    assert "Prior candidates and generated indexes never become original Evidence" in SKILL_BODY


@pytest.mark.parametrize("stage", sorted(STAGE_RULES))
def test_every_child_uses_existing_map_finalize_with_a_single_file_assignment(stage):
    body = stage_compiler_body(SKILL_BODY, stage)
    program = body.split("```python\n", 1)[1].split("\n```", 1)[0]
    contract = embedded_contract(body)
    nodes = ast.parse(program).body

    assert program == MULTI_COMPILE_PLAN
    assert [node.value.func.attr for node in nodes] == ["map", "finalize"]
    assert [node.value.args[0].id for node in nodes] == ["sources", "files"]
    assert nodes[0].value.keywords[0].value.attr == "synthesize"
    assert contract == multi_compile_contract() | {"synthesize": contract["synthesize"]}
    assert set(contract) == {"extract", "synthesize", "output_format"}
    assert contract["synthesize"]["output"] == "files"
    assert contract["synthesize"]["input_unit"] == "file"
    assert "ONLY packet.output_path" in body
    assert STAGE_RULES[stage] in body
    assert OUTPUT_LANGUAGE_GUIDANCE in contract["synthesize"]["instructions"]
    for unsupported in ("p.expand", "input_limits", "record_limits", "ov-compile-plan-v1", "evidence_ref"):
        assert unsupported not in body
    assert COMPILE_PLAN in LEGACY_SKILL_BODY


def test_case_child_outputs_only_its_canonical_case_or_an_explicit_deferred_reason():
    body = stage_compiler_body(SKILL_BODY, "case")
    instructions = embedded_contract(body)["synthesize"]["instructions"]

    assert "only the current complete canonical case JSON" in instructions
    assert "never a dataset or sibling cases" in instructions
    assert "12-24 distinct source-grounded output checks" in instructions
    assert "1-8 trajectory checks" in instructions
    assert "Do not pad generic conditions or fabricate facts" in instructions
    assert "skill-miner.deferred.v1" in instructions
    assert "THIS raw Evidence group" in instructions
    assert "logical chunk ID in the packet and a Viking citation are not runtime source_range IDs" in instructions


def test_all_case_writers_test_method_execution_and_preserve_source_uncertainty():
    from team_miner.skill_miner.compile_strategy import render_case_batch_compiler
    from team_miner.skill_miner.ov_case_group_orchestration import render_case_group_compiler

    for body in (
        stage_compiler_body(SKILL_BODY, "case"),
        render_case_batch_compiler(SKILL_BODY),
        render_case_group_compiler(SKILL_BODY),
    ):
        instructions = embedded_contract(body)["synthesize"]["instructions"]
        assert "carry out the candidate's procedure or decision on the supplied scenario" in instructions
        assert "not recall of source rows, persona labels, scenario IDs or unrelated facts" in instructions
        assert "Example names and amounts are scenario inputs, not universal rules" in instructions
        assert "a contradictory example must prompt clarification or an explicit limitation" in instructions
        assert "12-24 distinct source-grounded output checks" in instructions


def test_fragment_absence_is_provisional_and_planning_rechecks_it_without_hiding_real_gaps():
    index = embedded_contract(stage_compiler_body(SKILL_BODY, "index"))["synthesize"]["instructions"]
    plan = embedded_contract(stage_compiler_body(SKILL_BODY, "plan"))["synthesize"]["instructions"]

    assert "Absence from this fragment does not establish absence from the complete document" in index
    assert "provisional gap requiring verification after merging" in index
    assert "Recheck provisional fragment gaps against all assigned indexes" in plan
    assert "Resolve a gap when another assigned fragment supplies the missing support" in plan
    assert "preserve genuinely unresolved gaps explicitly" in plan
    assert "Missing historical metadata does not automatically block an executable method" in plan
    assert "Defer when Evidence essential to applicability, conditions or execution is truly insufficient" in plan


def test_selection_consumes_only_assigned_methods_and_never_disposes_readonly_notes():
    instructions = embedded_contract(stage_compiler_body(SKILL_BODY, "plan"))["synthesize"]["instructions"]
    assert "All disposition rows apply ONLY to derived_controls.items" in instructions
    assert "related_facts and provisional_gaps are readonly notes, not additional items" in instructions
    assert "Only assigned method item_id values can appear in derived_from" in instructions
    assert "never create knowledge, deferred, resolved or skills rows for them" in instructions


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
    repository = OvSkillMiningRepository(tmp_path / "state", tmp_path / "miner", backend=MemoryMetadataBackend())
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
    assert TE_STAGED_COMPILE in published
    assert published.split("---", 2)[2].strip() == frozen.split("---", 2)[2].strip()


@pytest.mark.parametrize(
    "body", [LEGACY_SKILL_BODY, stage_compiler_body(SKILL_BODY, "case")], ids=["single", "collection-case"]
)
def test_output_language_guidance_reaches_every_embedded_compile_stage(body):
    contract = embedded_contract(body)

    if "reduce" in contract:
        assert "## Output language\n\n" + OUTPUT_LANGUAGE_GUIDANCE in body
    stages = ("extract", "reduce", "synthesize") if "reduce" in contract else ("synthesize",)
    for stage in stages:
        assert contract[stage]["instructions"].count(OUTPUT_LANGUAGE_GUIDANCE) == 1
    assert contract["extract"]["output"] == "records"
    assert contract["synthesize"]["output"] == "files"


@pytest.mark.parametrize("stage", sorted(STAGE_RULES))
def test_current_stage_policy_is_present_once_without_the_parent_collection_contract(stage):
    body = stage_compiler_body(SKILL_BODY, stage)
    instructions = embedded_contract(body)["synthesize"]["instructions"]

    assert instructions.count(DEFAULT_MINING_POLICY) == 1
    assert instructions.count(STAGE_RULES[stage]) == 1
    assert instructions.count(OUTPUT_LANGUAGE_GUIDANCE) == 1
    assert body.count(DEFAULT_MINING_POLICY) == 1
    assert "compiled_skill/" not in body
    assert "analysis/capability-plan.json" not in body
    assert "analysis/review.json" not in body
    assert "Write shared analysis" not in body
    assert "complete four-file bundle" not in body
    for other in set(STAGE_RULES) - {stage}:
        assert STAGE_RULES[other] not in instructions
    if stage != "case":
        assert "12-24 distinct source-grounded output checks" not in instructions
        assert "skill-miner.deferred.v1" not in instructions


def test_auxiliary_renderer_uses_exact_owned_frozen_default_without_mutating_it():
    # The pre-policy presentation is the exact owned version already frozen by
    # existing rounds. New auxiliaries may use it; existing child bytes stay cached.
    old_owned = SKILL_BODY.split("\n## TE mining business policy\n", 1)[0]
    old_hash = hashlib.sha256(old_owned.encode()).hexdigest()
    rules = "Return only assigned identity IDs and their complete disjoint groups."
    for stage in ("identity_group", "evidence_review"):
        body = render_single_stage_compiler(old_owned, stage, rules)
        instructions = embedded_contract(body)["synthesize"]["instructions"]
        assert instructions.count(rules) == 1
        assert instructions.count(DEFAULT_MINING_POLICY) == 1
        assert "compiled_skill/" not in body
        assert all(value not in instructions for value in STAGE_RULES.values())
        assert "skill-miner.deferred.v1" not in instructions
        assert "p.reduce" not in body
    assert hashlib.sha256(old_owned.encode()).hexdigest() == old_hash


def test_explicit_custom_policy_preserves_rule_strings_and_frozen_source_exactly(tmp_path):
    common = "  自定义业务：金额单位为元；冲突时请求复核。\n第二行保留空格。  "
    stage_rule = "\n  本阶段只允许核验已给材料；禁止省略业务约束。\n"
    body = (
        "---\nname: custom-stage-compiler\ndescription: Explicit custom mining policy.\n---\n\n"
        + TE_STAGED_COMPILE + "\nskill-miner.plan.v1\n"
        + mining_policy_block(common, {"evidence_review": stage_rule, "case": "Only source-supported cases."})
    )
    frozen_hash = hashlib.sha256(body.encode()).hexdigest()
    package = _compiler_package(body)
    child = render_single_stage_compiler(body, "evidence_review", "Return the evidence-review contract only.")
    instructions = embedded_contract(child)["synthesize"]["instructions"]
    assert common in instructions and stage_rule in instructions
    assert "Only source-supported cases." not in instructions
    assert compiler_business_policy(body)["common"] == common
    assert package[2] == frozen_hash
    assert body == body[:] and hashlib.sha256(body.encode()).hexdigest() == frozen_hash


def test_custom_policy_change_in_owned_presentation_is_preserved_without_copying_layout():
    original = compiler_business_policy(SKILL_BODY)
    replacement = "Keep this additional custom rule exactly: audit every conflicting amount."
    body = SKILL_BODY.replace(json.dumps(original["common"]), json.dumps(replacement))
    instructions = embedded_contract(stage_compiler_body(body, "index"))["synthesize"]["instructions"]
    assert replacement in instructions and DEFAULT_MINING_POLICY not in instructions
    assert "compiled_skill/" not in instructions


@pytest.mark.parametrize(
    "body",
    [
        "---\nname: custom\ndescription: Custom.\n---\n" + TE_STAGED_COMPILE + "\nKeep my freeform rule.",
        TE_STAGED_COMPILE + mining_policy_block() + "\nDo not lose this additional business rule.\n",
        TE_STAGED_COMPILE + mining_policy_block() + mining_policy_block(),
        TE_STAGED_COMPILE + "\n```te-mining-policy.v2\n{}\n```\n",
        TE_STAGED_COMPILE + "\n```te-mining-policy.v1\n"
        '{"schema_version":"te-mining-policy.v1","common":"a","common":"b","stages":{}}\n```\n',
    ],
    ids=["freeform", "unclassified-exterior", "multiple-blocks", "unknown-version", "duplicate-keys"],
)
def test_custom_policy_cannot_be_silently_stripped_or_ambiguously_parsed(body):
    original_hash = hashlib.sha256(body.encode()).hexdigest()
    with pytest.raises(ValueError):
        stage_compiler_body(body, "index")
    assert hashlib.sha256(body.encode()).hexdigest() == original_hash


def test_selection_v2_reuses_only_the_known_authoritative_candidate_identity():
    instructions = embedded_contract(stage_compiler_body(SKILL_BODY, "plan"))["synthesize"]["instructions"]
    assert "derived_controls.selection_schema" in instructions
    assert "when it is absent use skill-miner.selection.v1" in instructions
    assert "exactly derived_from, reuse_candidate_id and reason" in instructions
    assert "exact ID from derived_controls.known_candidates" in instructions
    assert "do not add name, task, boundary or other metadata to a reuse row" in instructions
    assert "A v1 response cannot contain reuse rows" in instructions
    assert "known_candidates are identity context, never Evidence" in instructions
    assert MINING_POLICY_SCHEMA in SKILL_BODY


def test_language_defaults_do_not_rewrite_saved_or_frozen_custom_compiler(tmp_path):
    custom = (
        "---\nname: saved-compiler\ndescription: Existing custom compiler.\n---\n\n"
        "Produce one Skill Candidate. Keep this saved presentation instruction in English.\n"
    )
    settings = {"mining": {"prompts": {"ov_compile": custom}}}
    original_settings = deepcopy(settings)
    source_hash = hashlib.sha256(custom.encode("utf-8")).hexdigest()
    repository = OvSkillMiningRepository(tmp_path / "state", tmp_path / "miner", backend=MemoryMetadataBackend())
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
    document = build_document(
        skill,
        [
            {
                "id": "case-01",
                "name": "有效退货申请",
                "query": "请核验 TEST-RETURN 的退货申请。",
                "requirements": [f"核对第 {index} 项来源支持的退货条件。" for index in range(1, 13)],
                "trajectory_requirements": ["核对提供的 Evidence，发现缺口时向用户说明。"],
                "source_session_ids": ["viking://resources/team/source/returns.md"],
                "materials": [
                    {
                        "path": "order-note.md",
                        "name": "原始订单说明",
                        "content": "商品未拆封，原文编号 TEST-RETURN。",
                    }
                ],
            }
        ],
    )
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
    repository = OvSkillMiningRepository(tmp_path / "state", tmp_path / "miner", backend=MemoryMetadataBackend())
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
