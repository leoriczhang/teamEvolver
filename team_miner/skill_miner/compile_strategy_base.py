"""Finite, provenance-preserving default plan for the OV Skill Candidate compiler."""

import hashlib
import json
import re
from importlib import import_module

from .output_language import OUTPUT_LANGUAGE_GUIDANCE

COMPILE_PLAN = (
    "records = p.map(sources, task=contract.extract)\n"
    "groups = p.shuffle(records, by=contract.routing)\n"
    "work = p.reduce(groups, task=contract.reduce)\n"
    "files = p.map(work, task=contract.synthesize)\n"
    "p.finalize(files, into=target)"
)

TE_STAGED_COMPILE = "te-staged-compile.v1"
MINING_POLICY_SCHEMA = "te-mining-policy.v1"
# File Maps stay independently bounded; sharing admission avoids repeated Native startup.
MAX_FILE_MAP_PACKETS = 8
DEFAULT_MINING_POLICY = (
    "Create reviewable Skill Candidates only; never publish or install them. Use original Evidence "
    "to support stable executable procedures and decision rules, preserving numerical conditions, "
    "units, applicability, exceptions and contradictions. Cite exact original source URIs for material "
    "claims; never invent business facts, rules, missing files or resources. Human supplements are "
    "traceable assertions: retain their sources and flag conflicts with original Evidence. Previous "
    "Skill Candidates and review comments are revision context, never trusted Evidence. Verify any "
    "reused content against assigned originals and supplements. An executable method performs a "
    "reusable decision, calculation or procedure with clear inputs and outputs; looking up a name, "
    "code, version or description alone is factual information. Keep factual information without "
    "fabricating a method or an Evidence gap. Preserve distinct supported methods and merge only "
    "overlapping complete workflows; a bounded assignment's capacity is not a reason to discard a "
    "method. Keep each Candidate and its evaluation within the same independently executable task "
    "boundary. Missing support for one operation restricts that operation, not all methods from its "
    "source. Absence from an assigned fragment does not prove absence from the whole source file. "
    "A required runtime input is not an Evidence gap merely because its value varies by execution. "
    "State genuine uncertainty and contradictions without inventing resolutions. Preserve authoritative "
    "candidate identity when supported scope remains stable; names and previous metadata are not facts."
)
_OWNED_COMPILER_POLICIES: dict[str, dict] = {}
_OWNED_POLICY_FRAMES: set[str] = set()
_POLICY_BLOCK = re.compile(r"(?m)^```te-mining-policy\.v1[ \t]*\n(.*?)\n```[ \t]*(?=\n|$)", re.DOTALL)


def _body_sha(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def mining_policy_block(common: str = DEFAULT_MINING_POLICY, stages: dict[str, str] | None = None) -> str:
    """Encode explicit policy without modifying the caller's rule text."""
    policy = {"schema_version": MINING_POLICY_SCHEMA, "common": common, "stages": stages or {}}
    _validate_mining_policy(policy)
    return "\n## TE mining business policy\n\n```te-mining-policy.v1\n" + json.dumps(
        policy, ensure_ascii=False, indent=2
    ) + "\n```\n"


def _validate_mining_policy(policy: object) -> dict:
    if not isinstance(policy, dict) or set(policy) != {"schema_version", "common", "stages"}:
        raise ValueError("Mining policy must contain exactly schema_version, common and stages")
    if policy["schema_version"] != MINING_POLICY_SCHEMA:
        raise ValueError("Unsupported mining business policy schema")
    if not isinstance(policy["common"], str) or not policy["common"].strip():
        raise ValueError("Mining policy common rules must be nonempty text")
    stages = policy["stages"]
    if not isinstance(stages, dict) or any(
        not isinstance(key, str)
        or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key)
        or not isinstance(value, str)
        or not value.strip()
        for key, value in stages.items()
    ):
        raise ValueError("Mining policy stages must map safe stage names to nonempty rule text")
    # A JSON roundtrip isolates registration from mutable caller mappings, without
    # normalizing or truncating the policy strings themselves.
    return json.loads(json.dumps(policy, ensure_ascii=False))


def _unique_policy_keys(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Mining business policy cannot contain duplicate JSON keys")
        result[key] = value
    return result


def register_owned_compiler_policy(body: str, common: str = DEFAULT_MINING_POLICY) -> None:
    """Register only locally constructed default templates, never user freeform text."""
    policy = _validate_mining_policy({"schema_version": MINING_POLICY_SCHEMA, "common": common, "stages": {}})
    _OWNED_COMPILER_POLICIES[_body_sha(body)] = policy
    matches = list(_POLICY_BLOCK.finditer(body))
    if len(matches) == 1:
        match = matches[0]
        _OWNED_POLICY_FRAMES.add(_body_sha(body[: match.start()] + body[match.end() :]))


def _minimal_policy_exterior(exterior: str) -> bool:
    """Classify a minimal explicit-policy source; never strip arbitrary prose."""
    if exterior.startswith("---\n"):
        end = exterior.find("\n---\n", 4)
        if end < 0:
            return False
        # The frontmatter is compiler identity, not a second policy store. Extra
        # custom metadata must migrate explicitly rather than disappear silently.
        import yaml

        try:
            header = yaml.safe_load(exterior[4:end])
        except yaml.YAMLError:
            return False
        if not isinstance(header, dict) or set(header) - {"name", "description"}:
            return False
        exterior = exterior[end + len("\n---\n") :]
    allowed = {TE_STAGED_COMPILE, "skill-miner.plan.v1", "## TE mining business policy"}
    return all(not line.strip() or line.strip() in allowed for line in exterior.splitlines())


def compiler_business_policy(base_body: str) -> dict:
    """Resolve a frozen explicit policy, or an exact known owned default body."""
    if not isinstance(base_body, str) or not base_body.strip():
        raise ValueError("The frozen base compiler is required")
    if not _OWNED_COMPILER_POLICIES:
        # The owned constructor registers exact historical bodies and presentation
        # frames. Loading it here also makes the auxiliary renderer standalone;
        # caller import order never determines whether an owned frozen body works.
        import_module(".ov_compile_mining", package=__package__)
    matches = list(_POLICY_BLOCK.finditer(base_body))
    markers = re.findall(r"(?m)^```te-mining-policy(?:\.[^\s`]*)?[^\n]*$", base_body)
    if not matches and not markers:
        owned = _OWNED_COMPILER_POLICIES.get(_body_sha(base_body))
        if owned is not None:
            return json.loads(json.dumps(owned, ensure_ascii=False))
        raise ValueError(
            "Custom staged compiler needs one explicit te-mining-policy.v1 block; "
            "move all business rules into common/stages or keep its existing non-staged compiler"
        )
    if len(matches) != 1 or len(markers) != 1:
        raise ValueError("Exactly one valid te-mining-policy.v1 fenced block is required")
    match = matches[0]
    exterior = base_body[: match.start()] + base_body[match.end() :]
    if _body_sha(exterior) not in _OWNED_POLICY_FRAMES and not _minimal_policy_exterior(exterior):
        raise ValueError(
            "Custom staged compiler contains unclassified rules outside te-mining-policy.v1; "
            "move every custom business rule into common/stages before migration"
        )
    try:
        policy = json.loads(match.group(1), object_pairs_hook=_unique_policy_keys)
    except (ValueError, TypeError) as exc:
        raise ValueError("Mining business policy must be valid JSON") from exc
    return _validate_mining_policy(policy)


MULTI_COMPILE_PLAN = "files = p.map(sources, task=contract.synthesize)\np.finalize(files, into=target)"
MULTI_EVIDENCE_GUIDANCE = (
    "Each Compile reads one TE-owned bounded packet file. Only packet.evidence content is original "
    "Evidence: preserve its source_uri, offsets and hashes. derived_controls and revision_context are "
    "planning context, never facts or final provenance. Read every assigned packet range before emit; "
    "read_evidence accepts only actual runtime-provided source_range IDs. A logical chunk ID in the "
    "packet and a Viking citation are not runtime source_range IDs. This compiler has no attachments "
    "or scripts: never guess Skill resources or invoke them. Use the existing FileResponse with one "
    "file at packet.output_path. FileResponse.files[].inputs contains only the exact native runtime "
    "range IDs supplied by OV. These IDs are separate from JSON file content: content.input_ids, when "
    "required by that stage, contains only TE logical IDs. Source-based section IDs come from "
    "packet.input_ids; plan/merge control IDs come from derived_controls.input_ids. Never copy native "
    "range IDs into content.input_ids or source_refs. The index stage has its own TE-owned input_ids "
    "bookkeeping rule. Do not emit intermediate work records or multiple final files. Do not shorten "
    "original source URIs or fabricate missing facts. Planning and merging use traceable provisional "
    "summaries to organize work; their lack of raw text in packet.evidence is not proof that original "
    "Evidence is missing. Final Skill sections and cases must verify business facts against their "
    "assigned original text. A proposed task or boundary defines scope, not a verified product code, "
    "numerical condition or business rule. "
)

COMPILE_CONTRACT = {
    "extract": {
        "instructions": (
            "Read the complete assigned file, preserving coverage of every supplied range. "
            "Return one concise source-grounded fact record per assigned inputs[].id, with only "
            "that exact ID in top-level inputs. Keep boundary context and cross-range conditions "
            "explicit; when a fact genuinely depends on neighbouring ranges, include those IDs too. "
            "Do not bind every fact record to every range of the file. Preserve actionable rules, numbers, "
            "units, conditions, exceptions and exact source URIs in payload.facts. "
            "Omit optional evidence_spans entirely; the runtime retains the full original evidence. "
            "Never emit a standalone evidence_spans object. Each record is one complete JSON object "
            "with top-level inputs, routing_text, scope and payload.fields.facts. For example, with "
            "an actual assigned ID substituted for source-id: "
            '{"inputs":["source-id"],"routing_text":"source facts","scope":{},'
            '"payload":{"fields":{"facts":"Concise facts and exact source URI."}}}. '
            "Do not close the record before payload ends; verify the entire records array is valid JSON. "
            "Human supplements preserve their author, timestamp and references; label unverified assertions "
            "and conflicts explicitly. Prior round Skill Candidates and review comments are revision "
            "context, never trusted Evidence or business facts. Do not create ready drafts or final files "
            "in this step. " + OUTPUT_LANGUAGE_GUIDANCE
        ),
        "output": "records",
        "execution": "direct",
        "input_unit": "file",
        "fields": {"facts": "Source-grounded rules with applicability, exceptions and exact source URIs."},
    },
    "routing": {"mode": "all"},
    "reduce": {
        "instructions": (
            "Return exactly seven compact work records, one per concrete required_path. "
            "Plan only file assignments and brief fact digests, never write file content, full case questions, "
            "rubrics or requirements here. Each payload contains only file_path, file_role, "
            "skill_name, case_set='source-grounded-12-v1' and a brief trusted fact_digest. "
            "Use the same chosen skill_name as the required_paths. "
            "Each record.inputs must name all and only supplied extraction records needed by that file. "
            "Select by the actual facts required, never by arbitrary input count. Comprehensive evidence "
            "and Skill files may need every range; evaluation should cite only facts its checks need. "
            "Case files must retain every range supporting their chosen scenarios, conditions and exceptions. "
            "Do not copy the complete input list into every work record by default, and never omit "
            "a needed source merely to reduce prompt size. "
            "The runtime propagates their original source ranges, so facts remain in shared "
            "source storage and writers can read assigned original evidence. "
            "Omit optional evidence_spans; never fabricate range IDs or lines. "
            "fact_digest carries only concise facts already supplied by selected extraction records, "
            "including exact conditions and source range IDs; do not create full questions or rubrics. "
            "Do not copy full source text, source URI lists, package outlines or case blueprints "
            "into payload: writers receive those through the runtime and compiler instructions. "
            "Target under 1800 output tokens for all seven records; this is a planning target, "
            "not permission to omit any file or supporting input. " + OUTPUT_LANGUAGE_GUIDANCE
        ),
        "output": "records",
        "execution": "direct",
        "fields": {
            "file_path": "One concrete path from the seven required_paths.",
            "file_role": "One of evidence, gaps, review, skill, evaluation, cases_json, cases_markdown.",
            "skill_name": "The same selected package name in every work record.",
            "case_set": "The literal source-grounded-12-v1, defined in compiler instructions.",
            "fact_digest": (
                "Brief trusted selected facts with conditions and assigned source IDs; no raw text or full rubric."
            ),
        },
    },
    "synthesize": {
        "instructions": (
            "Write exactly the one complete file assigned by inputs[].payload.fields.file_path. "
            "Use the source-grounded-12-v1 case blueprint in the compiler instructions. "
            "Start from the trusted fact_digest and supplied original_evidence. Read its runtime-provided "
            "source_ranges and exact original evidence when necessary, preserving source conditions. "
            "Do not try to read sibling work records or resolve case_set as a file or tool resource. "
            "Read only the assigned source ranges needed to verify this file. "
            "Never reread a range already supplied complete in original_evidence or earlier tool results. "
            "Evaluation describes the evaluation method; do not expand it into a second full source digest. "
            "For analysis/review.json, emit the skill-miner.review.v1 contract with concrete questions "
            "and exact source_refs; an empty questions array is allowed only when no unresolved "
            "Evidence gaps remain. "
            "Other required_paths belong to other assignments. "
            "Return files with exactly one entry at that assigned path, not a partial page. "
            "A successful emit ends this assignment immediately: it does not append files or "
            "promise further work. Put complete content and supporting supplied record IDs in "
            "that single emit; every file.inputs must reference exact assigned input IDs. "
            "Keep output concise enough for the configured output limit. Cite exact original "
            "source URIs; do not replace them with shortened filenames. "
            "For benchmark.json, use the shared team-replay.dataset document contract. Each case MUST "
            "have checks with distinct nonempty text strings and output or trajectory kinds. "
            "Aim for 14-16 independent source-grounded requirements per case, leaving a margin "
            "above the mandatory minimum of 12. Before emit, count each case separately after "
            "deduplicating its output check texts; a single case with 11 checks fails the entire bundle. "
            "Use a richer source-supported scenario when needed to cover enough real checks. "
            "Never pad with generic checks or invent facts to meet the count. "
            "BENCHMARK.md must use the same case IDs and source-grounded checks. " + OUTPUT_LANGUAGE_GUIDANCE
        ),
        "output": "files",
        "execution": "direct",
    },
    "output_format": "files",
}


CASE_BLUEPRINT = (
    "source-grounded-12-v1 is shared compiler guidance, not a stored file or evidence ID. "
    "Use the same twelve ordered case IDs case-01..case-12 across the JSON, Markdown and evaluation. "
    "Theme order: product identity/version; intended use and product boundaries; service regions/routes; "
    "delivery timing; pricing; billable weight/rounding; volumetric rules; weight/size/acceptance limits; "
    "surcharges or documented alternatives; pickup workflow; transfer/security/exception handling; "
    "delivery/signature/aftercare. Spread scenarios across the supplied products. "
    "Themes are question topics, not business facts; if a topic is absent, use an explicitly "
    "source-supported boundary comparison instead of inventing rules. "
    "Keep the package structure fixed: SKILL.md gives source-grounded operating guidance; "
    "EVALUATION.md explains how to evaluate these cases; benchmark.json and BENCHMARK.md "
    "cover the same ordered cases. Each final case still needs at least twelve concrete, distinct "
    "source-supported output checks and exact provenance.session_ids; generate full questions and "
    "requirements only in final file writing. Use exact numeric conditions from original evidence, "
    "not generic repeated criteria. The shared themes do not replace semantic consistency review."
)


def compiler_execution_guidance() -> str:
    """Describe existing OV operators; do not override runtime validation or execution."""
    return (
        "\n## Evidence and tool contract\n\n"
        "The complete selected SKILL.md is already supplied. This compiler has no attachments or scripts. "
        "Do not call read_skill_resource or run_skill_script. Do not read the Skill name, README.md, "
        "source paths, templates or guessed files as Skill attachments.\n"
        "Read supplied inputs and original_evidence first. Only when necessary evidence is absent, "
        "call read_evidence with an assigned source_range ID from inputs[].id or "
        "inputs[].payload.source_ranges; omit line bounds to read the complete shard. "
        "A Viking URI is a citation, not an evidence ID or a Skill-relative path.\n"
        "Each intermediate record MUST contain top-level inputs: a nonempty array of exact supplied "
        "inputs[].id values. Do not put inputs inside payload. Omit optional evidence_spans entirely; "
        "never guess line numbers. Each final file also needs supporting input IDs in file.inputs. "
        "Use the runtime emit schema; do not wrap records/files in an invented schema.\n"
        "\n## Shared final-file case blueprint\n\n" + CASE_BLUEPRINT + "\n\n## Finite execution plan\n\n"
        "This task creates one candidate from one frozen snapshot, not many separately routed assets. "
        "Use exactly the five-step plan below. Use routing.mode=all for shared evidence planning; "
        "then Map the seven file work records independently. "
        "do not use semantic routing, related IDs, against=target, extra synthesis rounds or ready drafts. "
        "Choose one skill name at planning time and list all seven concrete required_paths from the output "
        "contract under that directory. Replace <skill-name>; do not emit placeholder paths. "
        "Preserve the full original output contract and all source-grounding requirements.\n\n"
        "```python\n" + COMPILE_PLAN + "\n```\n\n"
        "Use these transform settings in the planner's contract (add the seven required_paths):\n\n"
        "```json\n" + json.dumps(COMPILE_CONTRACT, ensure_ascii=False, indent=2) + "\n```\n"
    )


STAGE_RULES = {
    "index": (
        "Return skill-miner.index.v1: schema_version, "
        "methods [{task,boundary,source_refs}], facts [{text,source_refs}], deferred [{reason,source_refs}]. "
        "You may omit input_ids: TE sets it to exact packet.input_ids only after validating that all "
        "assigned references are classified in methods, facts or deferred. If input_ids is present, "
        "use only exact TE packet.input_ids, never native OV runtime range IDs. "
        "Use exactly these fields, at most 16 methods, 32 concise facts and 16 deferred entries. Each "
        "task, boundary, fact text and deferred reason must be nonempty and at most 1000 characters; "
        "the complete JSON object remains at most 12000 characters. At least one of methods, facts or "
        "deferred must be nonempty. Facts-only input is valid: preserve its facts and complete reference "
        "coverage without inventing a method or a missing-Evidence reason. The absence of an executable "
        "method is not itself an Evidence gap. The current packet may contain only part of a source document. "
        "Absence from this fragment does not establish absence from the complete document or source "
        "collection. State this fragment's coverage and any provisional gap requiring verification "
        "after merging; never claim that the entire source lacks a fact merely because it is outside "
        "this packet. References select exact allowed_source_refs from derived_controls "
        "when supplied; those internal aliases are selection handles, never Evidence citations. Otherwise "
        "use exact original source_uri values. Preserve all inputs, numerical conditions and exceptions."
    ),
    "merge_index": (
        "Merge only the assigned provisional planning items, never select or discard final candidates. "
        "Return skill-miner.merge-index.v1 with exactly schema_version, input_ids, methods, facts, "
        "deferred and resolved. input_ids uses all assigned control unit IDs exactly once. Each methods "
        "row has task, boundary and derived_from; each facts row has text and derived_from; each deferred "
        "row has reason and derived_from. derived_from lists exact assigned item_id values, not source "
        "URIs or native OV IDs. Every item must be consumed exactly once. Method proposals stay methods; "
        "merge only overlapping workflows, never remove a method due to this window's fragment gaps or "
        "historical metadata. Facts stay facts and unresolved provisional gaps stay deferred. All row "
        "text is nonempty and at most 1000 characters, and the whole JSON is at most 12000 characters. "
        "A resolved row has derived_from (gap IDs), supported_by (method/fact item IDs) and reason. "
        "Resolve only when supplied items actually fill the same original file and workflow gap; "
        "unrelated documents cannot fill it. TE derives source_refs from the item ledger; omit them. "
        "Retain distinct supported methods rather than force an eight-method intermediate limit. "
        "Indexes remain controls, not new Evidence or executable Skill text."
    ),
    "plan": (
        "Select candidates only for this bounded assigned method window. Return the schema specified "
        "by derived_controls.selection_schema; when it is absent use skill-miner.selection.v1. "
        "Use exactly schema_version, input_ids, skills, knowledge, deferred and resolved. input_ids "
        "covers every current control unit exactly once. Choose 0-8 independently executable methods "
        "within this window, never one per document. "
        "All disposition rows apply ONLY to derived_controls.items, the assigned method catalog. "
        "related_facts and provisional_gaps are readonly notes, not additional items: never create "
        "knowledge, deferred, resolved or skills rows for them. Only assigned method item_id values "
        "can appear in derived_from; knowledge describes why an assigned method is factual lookup, "
        "and deferred describes a genuine execution gap for that assigned method. "
        "This is a per-call limit; other pending methods "
        "are processed by TE in further windows, not declared insufficient. Merge overlapping methods "
        "and keep a workflow together. Each skills entry has name, task, trigger, inputs, outputs, "
        "boundary, rationale and derived_from. For skill-miner.selection.v2 only, an existing candidate "
        "may instead use exactly derived_from, reuse_candidate_id and reason. reuse_candidate_id must "
        "be an exact ID from derived_controls.known_candidates, and reason is nonempty text of at most "
        "1000 characters. TE restores that candidate's authoritative fields and extends its verified "
        "source scope: do not add name, task, boundary or other metadata to a reuse row. Reuse only when "
        "the assigned method belongs to the same independently executable workflow and boundary. "
        "known_candidates are identity context, never Evidence or business facts. A v1 response cannot "
        "contain reuse rows. Use stable lowercase kebab "
        "names <=64 characters, preserving previous names when scope is stable. Each deferred entry has "
        "reason and derived_from; each knowledge entry has reason and derived_from. Each unselected "
        "method needs its own reason row; never use one generic missing-file explanation to discard "
        "multiple methods. derived_from selects exact assigned item IDs; every item must be consumed "
        "exactly once. Facts can remain knowledge; do not force reference tables or historical metadata "
        "into Skills. A knowledge outcome must describe why it is factual lookup, not an executable "
        "workflow. TE derives source_refs from the ledger; omit them. "
        "task, trigger, boundary and rationale must "
        "be nonempty and at most 400 characters each. inputs and outputs each contain 1-16 nonempty "
        "strings of at most 240 characters each. deferred has at most 32 entries, with nonempty reason "
        "of at most 400 characters; knowledge reasons have the same limit. The complete JSON object is "
        "at most 12000 characters. Zero skills must still explicitly dispose of every item. "
        "Recheck provisional fragment gaps against all assigned indexes. Resolve a gap when another "
        "assigned fragment supplies the missing support; preserve genuinely unresolved gaps explicitly. "
        "resolved rows have gap derived_from, method/fact supported_by and a nonempty reason of at most "
        "1000 characters; support must address the same original file and workflow, never unrelated "
        "source material. A provisional summary supports planning even when raw Evidence is not "
        "copied into this packet; final writers will verify the originals. "
        "Missing historical metadata does not automatically block an executable method already fully "
        "supported by its operational Evidence. Defer when Evidence essential to applicability, "
        "conditions or execution is truly insufficient; never invent a resolution or a missing fact. "
        "Do not include file assignments, complete Skill text, case questions or Checklist."
    ),
    "skill_section": (
        "Write only a source-backed section for the assigned candidate and raw Evidence group. Return "
        "skill-miner.section.v1 with schema_version, input_ids (every assigned raw chunk ID), title and "
        "content, with no extra fields. title is nonempty and at most 200 characters; content is nonempty "
        "Markdown without frontmatter and at most 10000 characters. The complete JSON object is at most "
        "12000 characters. Preserve candidate task/boundary and exact "
        "conditions, exceptions and source citations. Never claim this section covers unassigned groups. "
        "If this assigned fragment contains no additional rules within the candidate boundary, return "
        "skill-miner.section-skip.v1 with exactly schema_version, input_ids (every assigned chunk ID) and "
        "a nonempty reason of at most 2000 characters. This records inspected scope, not an Evidence gap "
        "for the whole method. Do not summarise unrelated background into the Skill or request missing "
        "material solely because relevant operations are in other assigned fragments. TE checks complete "
        "coverage including skipped ranges, renders only applicable sections and requires actual method steps."
    ),
    "evaluation": (
        "Write concise Markdown describing evaluation method, scope, positive/negative coverage, risks "
        "and acceptance. Assigned raw Evidence is representative, not an exhaustive new policy. Do not "
        "invent missing requirements or repeat a complete Test Dataset or full Evidence digest. "
        "Use the candidate task in the artifact language for the display title and headings; never use "
        "the internal skill_name slug as a display title, even with a translated report suffix. "
        "Return nonempty Markdown of at most 12000 characters."
    ),
    "case": (
        "Return only the current complete canonical case JSON, never a dataset or sibling cases. Use "
        "assigned case_id and skill_name from controls; keys are exactly case_id, query, skill_ids, checks, "
        "materials, provenance, replay and metadata. materials MUST be a JSON array; use an empty array "
        "when no separate material is needed. For each included source-derived material use an object "
        "with a short name and its exact content. Do not emit materials as a named object. "
        "Provide all scenario facts/materials explicitly in "
        "query/materials. Use derived_controls.coverage_dimension as a coverage hint and "
        "derived_controls.previous_queries to avoid repeating an already generated scenario. These "
        "controls are not facts or Evidence. Generate a substantively different source-supported query, "
        "not just a changed identifier or superficial rewording. If the requested dimension is absent "
        "from the assigned Evidence, choose another distinct supported scenario or explicitly defer; "
        "never invent business conditions to satisfy the dimension or case count. "
        "The assigned derived_controls.candidate task, boundary and trigger define the EXCLUSIVE Case goal. "
        "First filter the assigned raw Evidence to actions within that candidate. Raw fragments can include "
        "adjacent workflows and background; sharing a document, actor or topic does not place those actions "
        "inside this candidate. Keep the query's primary trigger and requested end result within the assigned "
        "method. Never substitute an earlier/later workflow or broaden the goal to satisfy the coverage hint, "
        "Case count or Checklist minimum. If this assignment cannot support an in-boundary Case, explicitly "
        "defer it so TE can inspect another source group. "
        "Each assignment's method_input_ids identify the original method Evidence; context_input_ids "
        "identify attached constraints, headers or neighboring text. These routing labels are not new "
        "Evidence. Select the executable scenario from method_input_ids and only actions matching the "
        "candidate's trigger and outcome. Use context_input_ids to qualify that scenario, never as a "
        "replacement task. Even method chunks can contain adjacent rows: do not adopt their unrelated "
        "goals. Before returning each Case, compare its requested action and every Checklist item with "
        "the candidate task; remove neighboring-workflow requirements and retain applicable constraints. "
        "Ask the executor to carry out the candidate's procedure or decision on the supplied scenario. "
        "Evaluate observable actions, results, prerequisites and boundaries, not recall of source rows, "
        "persona labels, scenario IDs or unrelated facts. Example names and amounts are scenario inputs, "
        "not universal rules. Keep qualifications and distinguish a source's claim from a verified result; "
        "a contradictory example must prompt clarification or an explicit limitation, not a check "
        "requiring the executor to repeat an inconsistent calculation as correct. "
        "Each check has unique id, kind output/trajectory and concrete text; include "
        "12-24 distinct source-grounded output checks and 1-8 trajectory checks. "
        "query is nonempty and at most 12000 characters; each check id is nonempty and at "
        "most 160 characters, each check text is nonempty and at most 4000 characters. Check fields are "
        "only id, kind, text and optional polarity=must_not. The complete case JSON object, including "
        "all checks and materials, remains at most 12000 characters. Do not pad generic "
        "conditions or fabricate facts to meet these minima. provenance.session_ids cites only exact "
        "original source_uri values from THIS raw Evidence group. Keep replay.progressive_disclosure "
        "equal to enabled:true, initial_visibility:query_only, batch_size:4, "
        "stop_when:all_checklist_items_satisfied; metadata.split=test and minimum_requirement_target=12. "
        "If sufficient independent conditions or a valid scenario are unsupported, return the explicit "
        "deferred object instead. Any repair_error attached to this exact assignment identifies the previous "
        "format failure: correct that shape while preserving the scenario and its exact source scope. "
        "TE validates each case independently and assembles the dataset."
    ),
    "evidence": (
        "Write concise Markdown mapping supported claims to exact original source URIs; expose coverage limits."
    ),
    "gaps": (
        "Write Markdown exposing source conflicts, missing Evidence and unresolved questions; "
        "never invent a resolution."
    ),
    "review": (
        "Return skill-miner.review.v1 with schema_version and questions. Each question contains unique "
        "safe qid, question, reason, nonempty exact original source_refs and required:boolean. Questions "
        "and reasons <=4000 characters; at most 50 questions. Empty questions is valid when no gaps remain."
    ),
    "benchmark_view": (
        "Return a concise Markdown case index using assigned case IDs. TE renders the complete "
        "BENCHMARK.md from the validated assembled Test Dataset; do not generate another full dataset."
    ),
}


def multi_compile_contract() -> dict:
    """Only pre-existing OV transforms: one packet file produces one bounded output file."""
    return {
        "extract": {"instructions": "Unused by this two-step stage plan.", "output": "records"},
        "synthesize": {
            "instructions": MULTI_EVIDENCE_GUIDANCE
            + " Perform only the packet.stage assignment. "
            + OUTPUT_LANGUAGE_GUIDANCE,
            "output": "files",
            "execution": "direct",
            "input_unit": "file",
        },
        "output_format": "files",
    }


def multi_compiler_execution_guidance() -> str:
    """Declare TE orchestration without requiring any OpenViking runtime extensions."""
    return (
        "\n## TE bounded Compile orchestration\n\n" + TE_STAGED_COMPILE + "\n\n"
        "TE controls lossless source batching, hierarchical planning and deterministic file/case dispatch. "
        "This Skill declares business requirements; no single Compile produces the entire collection. "
        "Each child uses only the existing two-step Map/Finalize plan and produces one bounded file. "
        "Candidate scopes are selected dynamically from all indexed Evidence; maximum eight per "
        "selection window, including "
        "zero when evidence is insufficient. Every candidate retains all of its original chunk coverage. "
        "TE assembles canonical datasets after twelve valid independent case outputs and renders their "
        "Markdown view. Insufficient source-backed conditions defer a candidate instead of inventing "
        "Checklist items. Prior candidates and generated indexes never become original Evidence.\n\n"
        + MULTI_EVIDENCE_GUIDANCE
        + "\n"
    )


def render_single_stage_compiler(base_body: str, stage: str, stage_instructions: str) -> str:
    """Render explicit policy and one stage through the existing public OV contract."""
    if not isinstance(stage, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", stage):
        raise ValueError("TE Compile stage must be a safe stage name")
    if not isinstance(stage_instructions, str) or not stage_instructions.strip():
        raise ValueError("Current stage instructions are required")
    policy = compiler_business_policy(base_body)
    contract = multi_compile_contract()
    contract["synthesize"]["instructions"] += (
        "\n\nAuthoritative mining business policy ("
        + MINING_POLICY_SCHEMA
        + "):\n"
        + policy["common"]
        + ("\n\nCustom policy for this stage:\n" + policy["stages"][stage] if stage in policy["stages"] else "")
        + "\n\nCurrent stage: " + stage + ".\n"
        + stage_instructions
        + "\n\nOutput JSON or Markdown must stay within 12000 characters. A schema or length violation "
        "is a formatting error to correct, not evidence that source materials are insufficient. "
        "The sole output is packet.output_path. Do not read this output as a Skill resource."
    )
    return (
        "---\nname: skill-miner-te-"
        + stage.replace("_", "-")
        + "\ndescription: Produce one bounded TE Skill mining stage output.\n---\n\n"
        "# Single-file TE assignment\n\n"
        "Read one packet file and emit ONLY packet.output_path. The synthesize instructions below "
        "are the complete business policy and current-stage contract. This compiler has no auxiliary "
        "resources or scripts. Use only this existing OV plan.\n\n"
        "```python\n" + MULTI_COMPILE_PLAN + "\n```\n\n"
        "```json\n" + json.dumps(contract, ensure_ascii=False, indent=2) + "\n```\n"
    )


def stage_compiler_body(base_body: str, stage: str) -> str:
    """Build a fresh child; callers keep previously frozen child packages unchanged."""
    stage = {"extract": "index", "section": "skill_section"}.get(stage, stage)
    if stage not in STAGE_RULES:
        raise ValueError(f"Unknown TE Compile stage: {stage}")
    rules = STAGE_RULES[stage]
    if stage == "case":
        rules += (
            " The explicit deferred object has exactly schema_version='skill-miner.deferred.v1', "
            "reason (nonempty text of at most 2000 characters) and source_refs (a nonempty list of "
            "unique exact assigned original URIs). Use it only for unsupported scenarios, never padding."
        )
    return render_single_stage_compiler(base_body, stage, rules)


def render_stage_batch_compiler(
    base_body: str, stage: str, max_packets: int = MAX_FILE_MAP_PACKETS, *, integrated_body: bool = False,
    body_patch: bool = False,
    body_insert: bool = False,
) -> str:
    """Batch independent artifacts with the same public file Map and Finalize plan."""
    if not isinstance(stage, str) or stage not in {"skill_section", "evaluation"}:
        raise ValueError("Artifact batches support Skill sections and evaluation only")
    if type(max_packets) is not int or not 1 <= max_packets <= MAX_FILE_MAP_PACKETS:
        raise ValueError("A TE artifact Compile batch supports one to eight packet files")
    policy = compiler_business_policy(base_body)
    rules = STAGE_RULES[stage]
    if integrated_body and stage == "skill_section":
        from .skill_body import RULE

        rules = RULE
    if body_insert and stage == "skill_section":
        from .skill_body_insert import RULE

        rules = RULE + "\nOnly for packets WITHOUT body_insert, apply this full-body contract:\n" + rules
    if body_patch and stage == "skill_section":
        from .ov_body_patch_orchestration import RULE

        rules = RULE
    contract = multi_compile_contract()
    instructions = contract["synthesize"]["instructions"].replace(
        "Each Compile reads one TE-owned bounded packet file.",
        f"One Compile reads one to {max_packets} independent TE-owned packet files. "
        "With input_unit=file, each Map assignment reads only its own packet, including all of "
        "that packet's native ranges. Other packets belong to independent assignments.",
        1,
    )
    contract["synthesize"]["instructions"] = (
        instructions
        + "\n\nAuthoritative mining business policy (" + MINING_POLICY_SCHEMA + "):\n"
        + policy["common"]
        + ("\n\nCustom policy for this stage:\n" + policy["stages"][stage] if stage in policy["stages"] else "")
        + "\n\nCurrent per-file stage: " + stage + ".\n" + rules
        + "\n\nEach Map assignment emits exactly one FileResponse.files entry at its own "
        "packet.output_path. Preserve that path exactly. Use only this packet's candidate identity, "
        "original Evidence and native range IDs. Never read, merge, cite or emit a sibling packet's "
        "content or output. Prior candidate context is not Evidence. Finalize collects independent "
        "files without synthesizing a combined candidate. JSON or Markdown stays within 12000 "
        "characters per output file. TE validates each file independently and retains accepted "
        "outputs while repairing only missing or invalid files; formatting errors do not imply "
        "missing business Evidence."
    )
    return (
        "---\nname: skill-miner-te-" + stage.replace("_", "-")
        + "-batch\ndescription: Produce independent bounded Skill mining artifact files.\n---\n\n"
        "# Independent TE artifact assignments\n\n"
        f"Read at most {max_packets} independent packet files. Each file Map reads only its own "
        "packet and emits ONLY packet.output_path. The embedded synthesize instructions contain "
        "the complete business policy and stage contract. This compiler has no auxiliary resources "
        "or scripts. Use only the existing OV plan.\n\n"
        "```python\n" + MULTI_COMPILE_PLAN + "\n```\n\n"
        "```json\n" + json.dumps(contract, ensure_ascii=False, indent=2) + "\n```\n"
    )


def render_case_batch_compiler(base_body: str, max_packets: int = MAX_FILE_MAP_PACKETS, *,
                               source_bound_context: bool = False) -> str:
    """Keep each original public OV file-Map assignment independent of its siblings."""
    if type(max_packets) is not int or not 1 <= max_packets <= MAX_FILE_MAP_PACKETS:
        raise ValueError("A TE case Compile batch supports one to eight packet files")
    policy = compiler_business_policy(base_body)
    case_rules = STAGE_RULES["case"]
    if source_bound_context:
        from .case_context import source_bound_case_rules

        case_rules = source_bound_case_rules(case_rules)
    contract = multi_compile_contract()
    original_intro = "Each Compile reads one TE-owned bounded packet file."
    batch_intro = (
        f"One Compile has one to {max_packets} distinct TE-owned bounded packet file URIs. "
        "With input_unit=file, each Map assignment reads all native ranges of only its own packet "
        "file; never read or consume another packet or generate its case. Each packet.stage is case."
    )
    # Change only this owned renderer's transport wording. The shared guidance,
    # parent compiler, existing per-stage renderer and frozen jobs stay unchanged.
    contract["synthesize"]["instructions"] = contract["synthesize"]["instructions"].replace(
        original_intro, batch_intro, 1
    )
    custom = "".join(
        "\n\nCustom policy for " + key + ":\n" + policy["stages"][key]
        for key in ("case", "case_batch") if key in policy["stages"]
    )
    contract["synthesize"]["instructions"] += (
        "\n\nAuthoritative mining business policy (" + MINING_POLICY_SCHEMA + "):\n"
        + policy["common"] + custom
        + "\n\nCurrent per-file stage: case.\n" + case_rules
        + " The explicit deferred object has exactly schema_version='skill-miner.deferred.v1', "
        "reason (nonempty text of at most 2000 characters) and source_refs (a nonempty list of "
        "unique exact assigned original URIs). Use it only for unsupported scenarios, never padding."
        + "\n\nEach Map job emits exactly one FileResponse.files entry at its own packet.output_path. "
        "Its inputs are unique supporting native OV range IDs supplied to THAT file job, never "
        "TE chunk IDs, case IDs or another packet's ranges. Every packet has a different output_path: "
        "preserve it exactly; do not emit a shared literal filename or rename it. Finalize collects "
        "these independent files; never generate a complete Test Dataset or merge sibling cases. "
        "Output content remains at most 12000 characters for THIS case. Schema or length errors "
        "require correction, not a fabricated Evidence gap. A native completed task may have partial "
        "outputs; TE validates each expected case independently before dataset assembly. "
        "The shared request lists batch destinations, not additional files for this Map assignment."
    )
    return (
        "---\nname: skill-miner-te-case-batch\n"
        "description: Produce independently bounded TE case files through file Map and Finalize.\n---\n\n"
        "# Independent TE case assignments\n\n"
        f"This Compile reads at most {max_packets} distinct packet files. Each file Map task reads "
        "its own packet and emits only that packet's output_path. Other cases are separate assignments. "
        "The synthesize instructions contain the complete business policy and case contract. "
        "This compiler has no auxiliary resources or scripts. Use only this existing OV plan.\n\n"
        "```python\n" + MULTI_COMPILE_PLAN + "\n```\n\n"
        "```json\n" + json.dumps(contract, ensure_ascii=False, indent=2) + "\n```\n"
    )


def render_refinement_plan_compiler(base_body: str) -> str:
    """Render a future R2 selection protocol without changing any frozen compiler."""
    policy = compiler_business_policy(base_body)
    refinement_policy = (
        "Custom policy for selection_refinement:\n" + policy["stages"]["selection_refinement"] + "\n\n"
        if "selection_refinement" in policy["stages"] else ""
    )
    rules = (
        "This auxiliary performs packet.stage=plan only when derived_controls.selection_schema is "
        "skill-miner.selection.v3. Return exactly schema_version, input_ids, skills, knowledge, "
        "deferred and resolved; schema_version must equal skill-miner.selection.v3. input_ids must "
        "equal derived_controls.input_ids, the assigned TE container IDs, not item IDs or native OV "
        "range IDs. Use ONLY item_id values from derived_controls.items in derived_from. These are "
        "the assigned method proposals: every item must be disposed of exactly once, without "
        "duplicates or omissions. related_facts and provisional_gaps are readonly context; never "
        "create disposition rows for them or copy their identifiers into derived_from. "
        "Select at most eight independently executable workflows in this assignment, including "
        "zero when warranted. Further method windows are handled by TE; capacity is not an Evidence "
        "gap. Merge only overlapping complete workflows, not merely similar names, a shared product, "
        "source file, numerical value or individual substep. Keep factual lookup as knowledge and "
        "genuine execution insufficiency as deferred, with an individual reason for each such method. "
        "Do not treat a required runtime input or the absence of raw text from this planning packet "
        "as an Evidence gap. These traceable proposals organize work; final writers verify original "
        "Evidence. Do not invent a product code, numerical rule, constraint or external dependency. "
        "Each skills row must use exactly ONE of three forms. A new candidate has exactly "
        "derived_from, name, task, trigger, inputs, outputs, boundary and rationale. A reuse row has "
        "exactly derived_from, reuse_candidate_id and reason; its ID must be an exact assigned "
        "derived_controls.known_candidates ID. TE retains its authoritative business fields, so use "
        "reuse only when those fields remain appropriate for the current supported workflow. "
        "A refinement row has exactly derived_from, revise_candidate_id, base_metadata_sha256, "
        "task, trigger, inputs, outputs, boundary, rationale and reason. The revise_candidate_id "
        "must be an exact assigned known candidate ID whose refinement_allowed is true; "
        "every derived_from item must also be in that same candidate's exact refinement_item_ids. "
        "base_metadata_sha256 must copy that same known candidate's exact base_metadata_sha256 "
        "value, never compute, guess or copy a different candidate's hash. Do not mix these forms. "
        "A refinement is a complete replacement of the six business fields, supported by the "
        "CURRENT assigned method proposals and their original-source lineage. Correct inaccurate "
        "conditions, missing inputs or misleading descriptions only within the SAME complete "
        "independently executable workflow. Never use refinement to change its purpose, broaden it "
        "into an unrelated workflow or import unsupported rules. Explain the source-supported "
        "correction in reason. If this is a different workflow, propose an independent new candidate; "
        "if source support is insufficient, explicitly defer that assigned method. "
        "Identity and business content are different: TE preserves the known candidate's name, "
        "identity and existing case IDs during refinement. Never include name, candidate IDs, "
        "case IDs or a Test Dataset in a refinement row beyond revise_candidate_id. Previous "
        "business metadata, previous Skill text and review feedback are NON-Evidence revision "
        "context. Their being authoritative identity metadata does not make their rules true. "
        "Do not retain a known error merely to preserve identity, and do not use old metadata as "
        "proof of a correction. Every new/reuse/refinement row must consume the exact current "
        "assigned method IDs; TE derives its source_refs and records the base hash and source audit. "
        "For a new candidate, name is a lowercase kebab slug of at most 64 characters. For new or "
        "refinement rows, task, trigger, boundary and rationale are nonempty strings of at most "
        "400 characters each; inputs and outputs each contain 1-16 nonempty strings of at most "
        "240 characters. Each reuse/refinement reason is nonempty and at most 1000 characters. "
        "Each knowledge or deferred row has exactly reason and derived_from, disposes of one "
        "assigned method, and has a nonempty reason of at most 400 characters. deferred has at "
        "most 32 rows. This methods-only assignment cannot dispose of readonly gaps: resolved must "
        "be empty. Omit source_refs, full Skill content, case queries, Checklist and file assignments. "
        "The complete JSON remains at most 12000 characters. Missing identity authorization, an "
        "incorrect base hash, mixed row forms or schema/length violations require correction, "
        "never silent fallback to v2 or an invented Evidence gap."
    )
    # The existing renderer supplies common policy and the plan stage's explicit
    # custom policy exactly once. Refinement policy remains complete, without copying
    # the old v2 transport contract into this future-only protocol.
    return render_single_stage_compiler(base_body, "plan", refinement_policy + rules)


EXTRACT_ROUTING_RULES_V2 = (
    "Return skill-miner.index.v2 with exactly schema_version, methods, facts, deferred, context_refs, "
    "context_uncertain and optional input_ids. methods rows contain task, boundary, source_refs; facts "
    "rows contain text, source_refs; deferred rows contain reason, source_refs. At most 16 methods, "
    "32 facts and 16 deferred rows; every task/boundary/text/reason is nonempty and at most 1000 "
    "characters. At least one of methods/facts/deferred must be nonempty, including facts-only input. "
    "Every row uses exactly its declared keys: facts use text, while deferred use reason, never text. "
    "No aliases or extra row fields are accepted. For a genuine source gap, an example deferred row "
    "is {\"reason\":\"The source refers to a missing attachment.\",\"source_refs\":[\"<an exact allowed handle>\"]}. "
    "This example illustrates the shape only: copy neither its reason nor its placeholder into output. "
    "If derived_controls.repair_error is present, correct that exact validation problem while retaining "
    "all supported original coverage. Never return an unchanged invalid object. "
    "The schema_version is an inline JSON protocol string, not a file or Skill resource; never search "
    "for or read a resource named skill-miner.index.v2. All required schema fields are declared here. "
    "The entire canonical JSON stays within 12000 characters. You may omit input_ids because TE sets "
    "this container bookkeeping only after exact assigned source coverage has been validated. Never "
    "copy native OV range IDs into content IDs or references. Every source_refs and context_refs value "
    "must be an exact derived_controls.allowed_source_refs handle for THIS packet; reference_map binds "
    "each handle to the exact current source chunk. These handles organize source selection; they are "
    "not invented facts or final provenance. Classify all assigned source coverage without inventing "
    "methods or gaps. context_refs is a unique list, possibly empty, locating actual shared applicability "
    "conditions, exceptions, overriding rules or heading context needed across supported methods. "
    "Keep those rules in source-grounded facts too, with their exact references. Do not mark every "
    "persona, example, product fact or paragraph as shared merely because it is in the same document. "
    "context_uncertain must be a boolean. Set it true when the applicability of actual constraints "
    "cannot be safely localized from the supplied source; TE will conservatively revisit the source. "
    "Do not set it true merely because this assignment is bounded, other documents are not copied, "
    "or some runtime input has no concrete value yet. Preserve visible continuations, cross-section "
    "references, numerical conditions, units and exceptions; state unresolved cross-fragment linkage "
    "as provisional rather than invent its missing content. Absence from this packet is not proof of "
    "absence from the complete source. Bind each method to the precise supporting assigned ranges, "
    "not the entire packet by default. Cross-fragment constraints are indexed once and later writers "
    "verify the corresponding originals; the index itself never becomes original Evidence."
)


def _efficient_workflow_guidance() -> str:
    # Import lazily: only new protocol renderers depend on this planning version.
    from .workflow_planning import WORKFLOW_PLANNING_RULE

    return WORKFLOW_PLANNING_RULE


def render_extract_batch_compiler(base_body: str, max_packets: int = MAX_FILE_MAP_PACKETS) -> str:
    """Fresh routing indexes through unchanged public file Map/Finalize, never whole-corpus Reduce."""
    if type(max_packets) is not int or not 1 <= max_packets <= MAX_FILE_MAP_PACKETS:
        raise ValueError("An extract batch supports one to eight independent source packets")
    policy = compiler_business_policy(base_body)
    contract = multi_compile_contract()
    contract["synthesize"]["instructions"] = contract["synthesize"]["instructions"].replace(
        "Each Compile reads one TE-owned bounded packet file.",
        f"One Compile reads at most {max_packets} independent TE-owned source packet files. "
        "Each input_unit=file Map assignment reads only its own packet and all its native ranges.",
        1,
    )
    custom = "".join(
        "\n\nCustom policy for " + key + ":\n" + policy["stages"][key]
        for key in ("index", "extract", "extract_batch") if key in policy["stages"]
    )
    contract["synthesize"]["instructions"] += (
        "\n\nAuthoritative mining business policy (" + MINING_POLICY_SCHEMA + "):\n"
        + policy["common"] + custom
        + "\n\nCurrent per-file stage: extract.\n" + EXTRACT_ROUTING_RULES_V2
        + "\n\nEach Map produces one complete index at THAT packet.output_path. FileResponse.files[].inputs "
        "uses supporting native IDs from that same file Map only; content references use that packet's "
        "exact TE reference_map. Never combine sibling packets, source scopes, indexes or output paths. "
        "Finalize collects independent index files. Partial native output is not full batch success: "
        "TE validates and retains each successful index, and repairs only rejected assignments."
    )
    return (
        "---\nname: skill-miner-te-extract-batch\n"
        "description: Produce independent source indexes with exact constraint routing.\n---\n\n"
        "# Independent source index assignments\n\n"
        f"Read at most {max_packets} separate bounded packet files. Only packet.evidence is original "
        "Evidence. Each file Map emits only its own packet.output_path. The synthesize instructions "
        "contain complete business policy and the routing index contract. No scripts, auxiliary "
        "resources or whole-collection output planning are required.\n\n"
        "```python\n" + MULTI_COMPILE_PLAN + "\n```\n\n"
        "```json\n" + json.dumps(contract, ensure_ascii=False, indent=2) + "\n```\n"
    )


def render_efficient_stage_compiler(base_body: str, stage: str, *, refinement: bool = False) -> str:
    """Add fresh workflow rules to an owned stage rendering without changing legacy renderers."""
    if stage in {"extract", "index"}:
        return render_extract_batch_compiler(base_body)
    if stage not in {"merge_index", "plan", "skill_section", "evaluation", "case"}:
        raise ValueError("Unsupported efficient mining stage")
    if refinement and stage != "plan":
        raise ValueError("Business metadata refinement belongs to the plan stage")
    rendered = render_refinement_plan_compiler(base_body) if refinement else stage_compiler_body(base_body, stage)
    marker = "```json\n"
    start = rendered.rfind(marker) + len(marker)
    end = rendered.index("\n```", start)
    contract = json.loads(rendered[start:end])
    if stage in {"merge_index", "plan"}:
        contract["synthesize"]["instructions"] += (
            "\n\nFresh workflow and source-scope policy:\n" + _efficient_workflow_guidance()
        )
    return rendered[:start] + json.dumps(contract, ensure_ascii=False, indent=2) + rendered[end:]


def render_merge_batch_compiler(base_body: str, max_packets: int = MAX_FILE_MAP_PACKETS) -> str:
    """Plan independently bounded method windows without changing selection's serial authority."""
    if type(max_packets) is not int or not 1 <= max_packets <= MAX_FILE_MAP_PACKETS:
        raise ValueError("A merge batch supports one to eight independent method packets")
    policy = compiler_business_policy(base_body)
    contract = multi_compile_contract()
    contract["synthesize"]["instructions"] = contract["synthesize"]["instructions"].replace(
        "Each Compile reads one TE-owned bounded packet file.",
        f"One Compile reads at most {max_packets} independent TE-owned method packet files. "
        "Each input_unit=file Map assignment reads only its own packet and its native ranges.",
        1,
    )
    custom = "".join("\n\nCustom policy for " + key + ":\n" + policy["stages"][key]
                     for key in ("merge_index", "merge_batch") if key in policy["stages"])
    contract["synthesize"]["instructions"] += (
        "\n\nAuthoritative mining business policy (" + MINING_POLICY_SCHEMA + "):\n"
        + policy["common"] + custom
        + "\n\nCurrent per-file stage: merge_index.\n" + STAGE_RULES["merge_index"]
        + "\n\n" + _efficient_workflow_guidance()
        + "\n\nThe schema label skill-miner.merge-index.v1 is an inline JSON string, not a resource or file. "
        "Its entire definition is already in these instructions. Do not look up or read a schema path. "
        "Every assigned packet has at most eight method items; consume only its exact derived_controls.items "
        "item_id values, each exactly once in derived_from. These are traceable planning proposals, not "
        "original Evidence. Preserve distinct supported methods; capacity is not an Evidence gap. "
        "The result has exactly schema_version, input_ids, methods, facts, deferred and resolved. "
        "For this method-only window facts, deferred and resolved are empty arrays. "
        "Each methods row has exactly derived_from, task and "
        "boundary; do not invent source_refs, IDs, business facts or a final Skill Candidate. "
        "input_ids must equal this packet's derived_controls.input_ids; TE also accepts the complete assigned "
        "item ID set as equivalent container bookkeeping. Native runtime IDs must never appear in content IDs. "
        "Example for one assigned method m1/container u1: "
        '{"schema_version":"skill-miner.merge-index.v1","input_ids":["u1"],'
        '"methods":[{"derived_from":["m1"],"task":"Preserve the assigned workflow",'
        '"boundary":"Preserve its source-supported boundary"}],"facts":[],"deferred":[],"resolved":[]}. '
        "These example identifiers are placeholders; copy the actual assigned identifiers. "
        "Each Map produces exactly one FileResponse.files entry at THAT packet.output_path. "
        "FileResponse.inputs uses only supporting native IDs supplied to that same file Map. Never combine "
        "sibling windows, items, source scopes or destinations. Finalize collects independent merge files; "
        "selection is a later serial TE stage. The complete content for EACH output is at most 12000 characters. "
        "Partial native output is not batch success: TE retains valid sibling results and repairs only "
        "missing or rejected assignments. Schema errors require format repair, not fabricated Evidence gaps."
    )
    return (
        "---\nname: skill-miner-te-merge-batch\n"
        "description: Merge independent bounded method windows through file Map and Finalize.\n---\n\n"
        "# Independent method merge assignments\n\n"
        f"Read at most {max_packets} separate packet files. Each file Map emits only its own packet.output_path. "
        "All schemas are inline; no schema resources or auxiliary files are required. "
        "The synthesize instructions contain complete business policy and the exact merge contract.\n\n"
        "```python\n" + MULTI_COMPILE_PLAN + "\n```\n\n"
        "```json\n" + json.dumps(contract, ensure_ascii=False, indent=2) + "\n```\n"
    )
